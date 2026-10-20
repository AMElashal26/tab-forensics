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
import tabkit  # noqa: E402

GREAT_SUSPENDER = "noogafoofpebimajpfpamcfhoaifemoa"
MARVELOUS = "plpkmjcnhhnpkblimgenmdhghfgghdpp"


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
        url = "https://github.com/AMElashal26/safari-reading-list"
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
