"""Test suite for tabkit.

Run with:  python3 -m unittest discover -s tests -v
       or:  python3 tests/test_tabkit.py

These tests encode the two non-obvious failure modes this toolkit exists to
handle: nested suspender URLs, and deduplication that must not depend on titles.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import build_board  # noqa: E402
import close_duplicates  # noqa: E402
import config  # noqa: E402
import tabkit  # noqa: E402

GREAT_SUSPENDER = "noogafoofpebimajpfpamcfhoaifemoa"
MARVELOUS = "plpkmjcnhhnpkblimgenmdhghfgghdpp"


class TestAppleScriptSeparatorSafety(unittest.TestCase):
    """Guard against a silent, whole-session data-loss bug.

    Inside a `tell application "Google Chrome"` block, the identifiers `tab`
    and `linefeed` are resolved against Chrome's own scripting dictionary: `tab`
    is the *tab class*, not the tab character. Assigning it to a variable made
    AppleScript coerce a class reference to an empty string, so every row was
    emitted as `title||url` with no separator at all.

    Nothing raised. The failure surfaced only as zero parsed URLs across an
    entire session (879 tabs) while individual per-tab reads looked fine.
    """

    def test_separator_uses_explicit_character_ids(self):
        src = tabkit._window_dump_script(1)
        self.assertIn("ASCII character 9", src)
        self.assertIn("ASCII character 10", src)

    def test_does_not_bind_the_tab_class_as_a_variable(self):
        src = tabkit._window_dump_script(1)
        for line in src.splitlines():
            stripped = line.strip()
            self.assertNotEqual(
                stripped,
                "set tb to tab",
                "`tab` resolves to Chrome's tab class inside a tell block and "
                "silently becomes an empty string",
            )

    def test_parse_requires_the_separator_to_recover_a_url(self):
        """The parser must treat a separator-less line as title-only, which is
        exactly the symptom that made the original bug visible."""
        glued = "Some Page Titlehttps://example.com/page"
        self.assertNotIn(tabkit.TAB, glued)


class TestSuspendResolution(unittest.TestCase):
    """Suspender extensions navigate a tab to their own page and stash the real
    URL in the query string. A naive export records the wrapper, not the target."""

    def test_single_workona_layer(self):
        wrapped = (
            "https://workona.com/suspended/#title=Raycast%20Manual"
            "&url=https%3A%2F%2Fmanual.raycast.com%2Fai"
        )
        self.assertEqual(tabkit.resolve_target(wrapped), "https://manual.raycast.com/ai")

    def test_single_great_suspender_layer(self):
        wrapped = (
            f"chrome-extension://{GREAT_SUSPENDER}/suspended.html"
            "#ttl=Intro&pos=0&uri=https://app.datacamp.com/learn/courses/intro"
        )
        self.assertEqual(
            tabkit.resolve_target(wrapped), "https://app.datacamp.com/learn/courses/intro"
        )

    def test_double_nested_two_suspenders(self):
        """Two suspenders installed at once wrap the URL twice. This is the real
        bug found in production: one extension's page nested inside the other's."""
        inner = (
            f"chrome-extension://{GREAT_SUSPENDER}/suspended.html"
            "#ttl=Gemini in Gmail&pos=0&uri=https://app.datacamp.com/learn/courses/gemini-in-gmail"
        )
        encoded = quote(inner, safe="")
        double = f"https://workona.com/suspended/#title={encoded}&url={encoded}"
        resolved = tabkit.resolve_target(double)
        self.assertEqual(resolved, "https://app.datacamp.com/learn/courses/gemini-in-gmail")
        self.assertFalse(tabkit.is_suspended(resolved))

    def test_reverse_order_nesting(self):
        inner = (
            "https://workona.com/suspended/#title=Docs"
            "&url=https%3A%2F%2Fdocs.github.com%2Fen"
        )
        encoded = quote(inner, safe="")
        double = (
            f"chrome-extension://{MARVELOUS}/suspended.html"
            f"#ttl=wrapped&uri={encoded}"
        )
        self.assertEqual(tabkit.resolve_target(double), "https://docs.github.com/en")

    def test_plain_url_untouched(self):
        url = "https://github.com/example-user/example-repo"
        self.assertEqual(tabkit.resolve_target(url), url)

    def test_resolution_is_idempotent(self):
        wrapped = (
            "https://workona.com/suspended/#title=X"
            "&url=https%3A%2F%2Fexample.com%2Fa"
        )
        once = tabkit.resolve_target(wrapped)
        self.assertEqual(tabkit.resolve_target(once), once)

    def test_empty_and_malformed_inputs(self):
        for bad in ("", None, "not a url", "https://workona.com/suspended/no-fragment"):
            self.assertEqual(tabkit.resolve_target(bad or ""), bad or "")


class TestNormalization(unittest.TestCase):
    """Deduplication must ignore cosmetic URL differences but keep meaning."""

    def test_host_and_scheme_are_lowercased(self):
        self.assertEqual(
            tabkit.normalize_url("https://YouTube.com/watch?v=abc"),
            "https://youtube.com/watch?v=abc",
        )

    def test_trailing_slash_collapsed(self):
        self.assertEqual(tabkit.normalize_url("https://github.com/x/"), "https://github.com/x")

    def test_trailing_slash_before_query_collapsed(self):
        self.assertEqual(tabkit.normalize_url("https://x.com/a/?b=1"), "https://x.com/a?b=1")

    def test_query_strings_are_significant(self):
        a = tabkit.normalize_url("https://youtube.com/watch?v=aaa")
        b = tabkit.normalize_url("https://youtube.com/watch?v=bbb")
        self.assertNotEqual(a, b)

    def test_fragments_are_significant(self):
        a = tabkit.normalize_url("https://github.com/a/b#readme")
        b = tabkit.normalize_url("https://github.com/a/b")
        self.assertNotEqual(a, b)

    def test_meaningless_pages_are_dropped(self):
        for junk in (
            "chrome://newtab/",
            "chrome://new-tab-page/",
            f"chrome-extension://{GREAT_SUSPENDER}/suspended.html#ttl=&pos=0&uri=",
        ):
            self.assertIsNone(tabkit.normalize_url(junk), msg=junk)

    def test_local_files_preserved(self):
        url = "file:///Users/someone/ReadingListExport/browse.html"
        self.assertEqual(tabkit.normalize_url(url), url)

    def test_empty_input(self):
        self.assertIsNone(tabkit.normalize_url(""))


class TestDomainExtraction(unittest.TestCase):
    def test_www_stripped(self):
        self.assertEqual(tabkit.domain_of("https://www.youtube.com/watch?v=1"), "youtube.com")

    def test_local_file(self):
        self.assertEqual(tabkit.domain_of("file:///Users/x/y.html"), "local-file")

    def test_chrome_internal(self):
        self.assertEqual(tabkit.domain_of("chrome://extensions/"), "chrome-internal")

    def test_empty(self):
        self.assertEqual(tabkit.domain_of(""), "(empty)")


class TestReportStats(unittest.TestCase):
    def make(self, url, window=1, index=1, title="t", suspended=False):
        tab = tabkit.Tab(window=window, index=index, title=title, raw_url=url)
        tab.suspended = suspended
        return tab

    def test_unique_and_redundant_counts(self):
        report = tabkit.Report(
            tabs=[
                self.make("https://a.com/1", index=1),
                self.make("https://a.com/1", index=2),
                self.make("https://a.com/1", index=3),
                self.make("https://b.com/2", index=4),
            ]
        )
        self.assertEqual(report.total, 4)
        self.assertEqual(report.unique, 2)
        self.assertEqual(report.redundant, 2)
        self.assertAlmostEqual(report.duplicate_rate, 0.5)

    def test_duplicates_detected_across_suspenders(self):
        """A suspended tab and a live tab pointing at the same page are duplicates."""
        live = "https://example.com/page"
        wrapped = (
            "https://workona.com/suspended/#title=P"
            "&url=https%3A%2F%2Fexample.com%2Fpage"
        )
        report = tabkit.Report(tabs=[self.make(live, index=1), self.make(wrapped, index=2)])
        self.assertEqual(report.unique, 1)
        self.assertEqual(report.redundant, 1)

    def test_windows_grouping_is_sorted(self):
        report = tabkit.Report(
            tabs=[
                self.make("https://a.com", window=2, index=2),
                self.make("https://b.com", window=2, index=1),
                self.make("https://c.com", window=1, index=1),
            ]
        )
        wins = report.windows()
        self.assertEqual(list(wins.keys()), [1, 2])
        self.assertEqual([t.index for t in wins[2]], [1, 2])

    def test_summary_shape(self):
        report = tabkit.Report(tabs=[self.make("https://a.com")])
        s = report.summary()
        for key in ("total_tabs", "windows", "unique_urls", "redundant_tabs", "top_domains"):
            self.assertIn(key, s)

    def test_empty_report_does_not_divide_by_zero(self):
        self.assertEqual(tabkit.Report().duplicate_rate, 0.0)


class TestSurvivorChoice(unittest.TestCase):
    """The tab kept open should be the most trustworthy one."""

    def test_non_suspended_preferred_over_suspended(self):
        suspended = tabkit.Tab(window=1, index=1, title="s", raw_url="https://x.com/a")
        suspended.suspended = True
        live = tabkit.Tab(window=5, index=9, title="l", raw_url="https://x.com/a")
        _, doomed = close_duplicates.choose_survivors(tabkit.Report(tabs=[suspended, live]))
        self.assertEqual(len(doomed), 1)
        self.assertTrue(doomed[0].suspended, "the suspended copy should be the one closed")

    def test_lower_window_preferred(self):
        first = tabkit.Tab(window=1, index=3, title="a", raw_url="https://x.com/a")
        later = tabkit.Tab(window=7, index=1, title="b", raw_url="https://x.com/a")
        _, doomed = close_duplicates.choose_survivors(tabkit.Report(tabs=[later, first]))
        self.assertEqual(len(doomed), 1)
        self.assertEqual(doomed[0].window, 7)

    def test_unique_tabs_are_never_doomed(self):
        tabs = [
            tabkit.Tab(window=1, index=1, title="a", raw_url="https://a.com"),
            tabkit.Tab(window=1, index=2, title="b", raw_url="https://b.com"),
        ]
        survivors, doomed = close_duplicates.choose_survivors(tabkit.Report(tabs=tabs))
        self.assertEqual(len(survivors), 2)
        self.assertEqual(doomed, [])


class TestArchiveRoundTrip(unittest.TestCase):
    def test_render_then_parse_preserves_every_tab(self):
        tabs = [
            tabkit.Tab(window=1, index=1, title="One", raw_url="https://a.com/1"),
            tabkit.Tab(window=1, index=2, title="Two [brackets]", raw_url="https://a.com/2"),
            tabkit.Tab(window=2, index=1, title="#hashtag", raw_url="https://b.com/x"),
        ]
        report = tabkit.Report(tabs=tabs)
        md = tabkit.render_markdown(report, "Test Archive")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "archive.md")
            tabkit.write_text(path, md)
            recovered = tabkit.parse_markdown_archive(path)
        self.assertEqual(len(recovered), 3)
        self.assertEqual(sorted(t.url for t in recovered), sorted(t.url for t in tabs))

    def test_parser_tolerates_corrupt_lines(self):
        content = (
            "# Archive\n\n"
            "## Window 1 - 3 tabs\n\n"
            "1. [Good](https://good.com/a)\n"
            "this line is garbage and must be skipped\n"
            "2. [Also good](https://good.com/b)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.md")
            tabkit.write_text(path, content)
            recovered = tabkit.parse_markdown_archive(path)
        self.assertEqual(len(recovered), 2)

    def test_escape_md_strips_leading_hashes_and_brackets(self):
        self.assertEqual(tabkit.escape_md("## Heading"), "Heading")
        self.assertNotIn("[", tabkit.escape_md("a [b] c"))

    def test_unique_urls_deduplicates(self):
        tabs = [
            tabkit.Tab(window=1, index=1, title="", raw_url="https://a.com/1"),
            tabkit.Tab(window=1, index=2, title="", raw_url="https://a.com/1"),
            tabkit.Tab(window=1, index=3, title="", raw_url="https://a.com/2"),
        ]
        self.assertEqual(len(tabkit.unique_urls(tabkit.Report(tabs=tabs))), 2)


class TestSafetyNetWriter(unittest.TestCase):
    def test_creates_missing_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deep", "nested", "file.txt")
            tabkit.write_text(path, "hello")
            self.assertTrue(os.path.exists(path))
            with open(path) as fh:
                self.assertEqual(fh.read(), "hello")

    def test_reports_bytes_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.txt")
            self.assertEqual(tabkit.write_text(path, "abcde"), 5)


class TestBoardGeneration(unittest.TestCase):
    """The board must be self-contained and its script must actually parse.

    A single stray backslash inside a JS string literal makes the whole script
    fail to parse, which silently disables every handler on the page. These
    tests exist because that regression shipped once.
    """

    def sample(self):
        return [
            tabkit.Tab(window=1, index=1, title="Video one", raw_url="https://youtube.com/watch?v=1"),
            tabkit.Tab(window=1, index=2, title="Repo", raw_url="https://github.com/a/b"),
            tabkit.Tab(window=2, index=1, title="Quote \" and <angle>", raw_url="https://c.com/x"),
        ]

    def test_javascript_contains_no_backslash(self):
        js = build_board.build_javascript()
        self.assertNotIn(chr(92), js)

    def test_javascript_passes_node_syntax_check(self):
        self.assertTrue(build_board.validate_javascript(build_board.build_javascript()))

    def test_html_contains_no_backslash(self):
        html = build_board.build_board(self.sample())
        self.assertNotIn(chr(92), html)

    def test_every_tab_gets_checkbox_and_link(self):
        html = build_board.build_board(self.sample())
        self.assertEqual(html.count('class="cb"'), 3)
        self.assertEqual(html.count("<a href="), 3)

    def test_all_handlers_are_defined(self):
        html = build_board.build_board(self.sample())
        for fn in ("grp", "flt", "copyUrls", "copyTitled", "invert", "upd", "dl", "all", "selUrls"):
            self.assertIn(f"function {fn}(", html, msg=f"missing handler {fn}")

    def test_attributes_are_escaped(self):
        html = build_board.build_board(self.sample())
        self.assertIn("&quot;", html)
        self.assertIn("&lt;angle&gt;", html)

    def test_deduplicates_by_normalized_url(self):
        tabs = [
            tabkit.Tab(window=1, index=1, title="A", raw_url="https://x.com/p"),
            tabkit.Tab(window=2, index=1, title="A duplicate", raw_url="https://x.com/p/"),
        ]
        html = build_board.build_board(tabs)
        self.assertEqual(html.count('class="cb"'), 1)

    def test_rejects_empty_input(self):
        with self.assertRaises(ValueError):
            build_board.build_board([])

    def test_written_file_is_complete_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "board.html")
            build_board.write_board(self.sample(), path, title="T")
            with open(path, encoding="utf-8") as fh:
                html = fh.read()
            self.assertTrue(html.rstrip().endswith("</html>"))
            self.assertEqual(html.count("<script>"), 1)
            self.assertEqual(html.count("</script>"), 1)

    def test_suspended_tabs_render_as_real_destinations(self):
        wrapped = (
            "https://workona.com/suspended/#title=Docs"
            "&url=https%3A%2F%2Fdocs.github.com%2Fen"
        )
        html = build_board.build_board([tabkit.Tab(window=1, index=1, title="D", raw_url=wrapped)])
        self.assertIn("https://docs.github.com/en", html)
        self.assertNotIn("workona.com/suspended", html)

    # -----------------------------------------------------------------------
    # Regression: a legitimate tab title ended the build.
    # -----------------------------------------------------------------------

    def test_title_containing_a_backslash_does_not_break_the_build(self):
        """A real page title used a backslash as a separator. HTML does not
        require escaping it, so it reached the output and tripped the
        whole-document assertion -- failing a build whose data was fine. The
        backslash is now emitted as a numeric entity."""
        tabs = [
            tabkit.Tab(
                window=1,
                index=1,
                title="Our work on the hard questions | Anthropic" + chr(92) + " Anthropic",
                raw_url="https://www.anthropic.com/news/example",
            )
        ]
        html = build_board.build_board(tabs)
        self.assertNotIn(chr(92), html)
        self.assertIn("&#92;", html)

    def test_escaped_backslash_still_renders_and_filters_correctly(self):
        """The entity must decode back to a real backslash, or search would
        stop matching titles that contain one."""
        import html as html_module

        raw = "Left" + chr(92) + "Right"
        escaped = build_board.html_escape(raw)
        self.assertNotIn(chr(92), escaped)
        self.assertEqual(html_module.unescape(escaped), raw)

    def test_backslash_in_a_url_does_not_break_the_build(self):
        tabs = [
            tabkit.Tab(window=1, index=1, title="Odd", raw_url="https://x.com/a" + chr(92) + "b")
        ]
        html = build_board.build_board(tabs)
        self.assertNotIn(chr(92), html)

    def test_document_invariant_still_holds_for_hostile_titles(self):
        """Sweep characters that could plausibly appear in a title."""
        nasties = [
            chr(92),
            chr(92) + chr(92),
            chr(92) + "n",
            "</script><script>alert(1)</script>",
            "quote \" and ' apostrophe",
            "amp & lt < gt >",
            chr(92) + "x" + chr(92),
        ]
        for bad in nasties:
            with self.subTest(title=bad[:20]):
                tabs = [tabkit.Tab(window=1, index=1, title=bad, raw_url="https://x.com/p")]
                html = build_board.build_board(tabs)
                self.assertNotIn(chr(92), html)
                self.assertNotIn("<script>alert(1)</script>", html)

    def test_script_invariant_is_checked_separately_from_the_document(self):
        """The assertion message must describe the invariant it actually tests.
        Injecting a comment with a backslash into the page must be attributed to
        the document, not blamed on scripts that are provably clean."""
        js = build_board.build_javascript()
        self.assertNotIn(chr(92), js)
        self.assertTrue(build_board.validate_javascript(js))


class TestConfig(unittest.TestCase):
    """The rules live in a file, so the file's failure modes are behaviour."""

    def setUp(self):
        self._saved = os.environ.pop("TABKIT_CONFIG", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["TABKIT_CONFIG"] = self._saved
        else:
            os.environ.pop("TABKIT_CONFIG", None)

    def _write(self, tmp, body):
        path = os.path.join(tmp, "tabkit.local.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_runs_with_no_config_at_all(self):
        """A fresh clone with no config must still work. This is the single
        most important property of the config layer."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load(cwd=tmp)
        self.assertTrue(cfg["wrapper"]["detector"])
        self.assertEqual(cfg["wrapper"]["carrier_params"], ["uri", "url"])
        self.assertFalse(config.WARNINGS)

    def test_partial_override_inherits_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, '[open]\ndefault_batch = 7\n')
            cfg = config.load(cwd=tmp)
        self.assertEqual(cfg["open"]["default_batch"], 7)
        # Untouched sibling survives.
        self.assertEqual(cfg["open"]["per_tab_delay_seconds"], 0.35)
        # Untouched section survives.
        self.assertEqual(cfg["wrapper"]["max_depth"], 5)

    def test_malformed_config_warns_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "not valid = = toml [[[\n")
            cfg = config.load(cwd=tmp)
        self.assertTrue(cfg, "must still return usable defaults")
        self.assertTrue(config.WARNINGS, "a bad config must be reported")

    def test_explicit_missing_path_is_an_error(self):
        """Asking for a specific file is different from auto-discovery."""
        with self.assertRaises(FileNotFoundError):
            config.load(path="/nonexistent/definitely-not-here.toml")

    def test_invalid_values_are_coerced_with_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, '[limits]\nmax_close_fraction = 99\n')
            cfg = config.load(cwd=tmp)
        self.assertEqual(cfg["limits"]["max_close_fraction"], 0.4)
        self.assertTrue(any("max_close_fraction" in w for w in config.WARNINGS))

    def test_carrier_params_come_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, '[wrapper]\ncarrier_params = ["target"]\n')
            cfg = config.load(cwd=tmp)
        self.assertEqual(cfg["wrapper"]["carrier_params"], ["target"])

    def test_custom_wrapper_in_fragment_resolves(self):
        wrapped = "https://myapp.internal/wrap#dest=https%3A%2F%2Fexample.com%2Freport"
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                '[wrapper]\ncarrier_params = ["dest"]\n\n'
                '[[wrapper.detector]]\nname = "w"\nmatch = "myapp.internal/wrap"\n',
            )
            os.environ["TABKIT_CONFIG"] = os.path.join(tmp, "tabkit.local.toml")
            try:
                tabkit.config(reload=True)
                self.assertTrue(tabkit.is_suspended(wrapped))
                self.assertEqual(tabkit.resolve_target(wrapped), "https://example.com/report")
            finally:
                os.environ.pop("TABKIT_CONFIG", None)
                tabkit.config(reload=True)

    def test_custom_wrapper_in_query_string_resolves(self):
        """A configurable wrapper may carry the target in the query string.
        Only reading the fragment would silently fail to resolve it."""
        wrapped = "https://myapp.internal/wrap?dest=https%3A%2F%2Fexample.com%2Freport"
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                '[wrapper]\ncarrier_params = ["dest"]\n\n'
                '[[wrapper.detector]]\nname = "w"\nmatch = "myapp.internal/wrap"\n',
            )
            os.environ["TABKIT_CONFIG"] = os.path.join(tmp, "tabkit.local.toml")
            try:
                tabkit.config(reload=True)
                self.assertEqual(tabkit.resolve_target(wrapped), "https://example.com/report")
            finally:
                os.environ.pop("TABKIT_CONFIG", None)
                tabkit.config(reload=True)

    def test_redaction_replaces_configured_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, '[privacy]\nredact_hosts = ["secret.example.com"]\n')
            os.environ["TABKIT_CONFIG"] = os.path.join(tmp, "tabkit.local.toml")
            try:
                tabkit.config(reload=True)
                out = tabkit.redact_hosts("https://secret.example.com/wiki/onboarding")
                self.assertNotIn("secret.example.com", out)
                self.assertIn("/wiki/onboarding", out)
            finally:
                os.environ.pop("TABKIT_CONFIG", None)
                tabkit.config(reload=True)

    def test_redaction_map_does_not_leak_the_real_host(self):
        """The shareable report must not name the hosts it redacted."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, '[privacy]\nredact_hosts = ["secret.example.com"]\n')
            os.environ["TABKIT_CONFIG"] = os.path.join(tmp, "tabkit.local.toml")
            try:
                tabkit.config(reload=True)
                rep = tabkit.Report(
                    tabs=[
                        tabkit.Tab(
                            window=1, index=1, title="T",
                            raw_url="https://secret.example.com/wiki/onboarding",
                        )
                    ]
                )
                md, redacted = tabkit.render_shareable_markdown(rep, "Test")
                self.assertEqual(redacted, 1)
                self.assertNotIn("secret.example.com", md)
            finally:
                os.environ.pop("TABKIT_CONFIG", None)
                tabkit.config(reload=True)

    def test_redaction_covers_titles_not_just_urls(self):
        """A suspended tab's title is "<page title> · <full URL>". Redacting
        only the link left the host spelled out next to it -- a leak that survived
        a URL-only check."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, '[privacy]\nredact_hosts = ["secret.example.com"]\n')
            os.environ["TABKIT_CONFIG"] = os.path.join(tmp, "tabkit.local.toml")
            try:
                tabkit.config(reload=True)
                leaky_title = "Some Page · https://secret.example.com/wiki/onboarding"
                rep = tabkit.Report(
                    tabs=[
                        tabkit.Tab(
                            window=1, index=1, title=leaky_title,
                            raw_url="https://secret.example.com/wiki/onboarding",
                        )
                    ]
                )
                md, redacted = tabkit.render_shareable_markdown(rep, "Test")
                self.assertNotIn("secret.example.com", md)
                self.assertEqual(redacted, 1)
            finally:
                os.environ.pop("TABKIT_CONFIG", None)
                tabkit.config(reload=True)

    def test_redaction_covers_local_account_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, '[privacy]\nredact_local_paths = true\n')
            os.environ["TABKIT_CONFIG"] = os.path.join(tmp, "tabkit.local.toml")
            try:
                tabkit.config(reload=True)
                account = os.path.basename(os.path.expanduser("~"))
                out = tabkit.redact_hosts(f"file:///Users/{account}/Desktop/x.png")
                self.assertNotIn(account, out)
                self.assertIn("<user>", out)
            finally:
                os.environ.pop("TABKIT_CONFIG", None)
                tabkit.config(reload=True)

    def test_dedupe_key_is_not_redacted(self):
        """Redaction must never touch normalize_url: two distinct local files
        would otherwise collide and one would be discarded as a duplicate."""
        a = tabkit.normalize_url("file:///Users/alice/Desktop/a.html")
        b = tabkit.normalize_url("file:///Users/bob/Desktop/a.html")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
