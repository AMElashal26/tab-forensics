"""tabkit -- audit, archive and de-bloat a Chrome tab session.

Why this exists
---------------
Reading open tabs from Chrome is easy (AppleScript does it in two lines).
Doing it *correctly* is not, because of two non-obvious facts:

1. Tab-suspender extensions (Workona, The Great Suspender, Marvelous Suspender)
   do not merely unload a tab -- they *navigate it* to their own extension page
   with the real URL percent-encoded into the query string. A naive export
   therefore records ``workona.com/suspended/#title=...&url=...`` instead of the
   destination the user actually cared about. Worse, two suspenders installed at
   once can nest: a Great Suspender URL wrapped inside a Workona URL.

   :func:`resolve_target` unwraps all of those layers.

2. A "suspended" tab is *not* free. Suspension trades a heavy renderer heap for a
   light one, but the renderer process itself survives. A session with 1,500
   suspended tabs still holds ~1,500 processes, and macOS runs out of process
   table headroom (reported to the user as "out of application memory") long
   before it runs out of bytes.

   :func:`chrome_process_stats` exists to make that distinction measurable.

Everything here is read-only by default; the only functions that mutate state are
:func:`close_tabs` and :func:`open_urls`, and both are explicit.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

TAB = "\t"
LF = "\n"

#: Hosts / extension ids known to wrap a real URL inside a query parameter.
SUSPENDER_MARKERS = (
    "workona.com/suspended",
    "noogafoofpebimajpfpamcfhoaifemoa",  # The Great Suspender
    "plpkmjcnhhnpkblimgenmdhghfgghdpp",  # Marvelous Suspender
    "suspended.html",
)

#: Query parameters suspenders use to carry the original URL.
CARRIER_PARAMS = ("uri", "url")

_URL_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://([^/?#]*)(.*)$")
_HOST = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/?#]*)")
_MD_ITEM = re.compile(r"^\s*(?:\d+\.\s+|-\s+)\[(.*)\]\((.*)\)\s*$")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Tab:
    """One tab, as read from Chrome or recovered from an archive."""

    window: int
    index: int
    title: str
    raw_url: str
    url: str = ""
    suspended: bool = False

    def __post_init__(self) -> None:
        if not self.url:
            self.url = resolve_target(self.raw_url)
        if not self.suspended:
            self.suspended = is_suspended(self.raw_url)

    @property
    def key(self) -> Optional[str]:
        """Deduplication key. ``None`` for tabs that are not worth archiving."""
        return normalize_url(self.url)

    @property
    def domain(self) -> str:
        return domain_of(self.url)

    def as_dict(self) -> dict:
        return {
            "window": self.window,
            "index": self.index,
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "suspended": self.suspended,
        }


@dataclass
class Report:
    """Aggregate statistics for a set of tabs."""

    tabs: list[Tab] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.tabs)

    @property
    def keyed(self) -> Counter:
        return Counter(t.key for t in self.tabs if t.key)

    @property
    def unique(self) -> int:
        return len(self.keyed)

    @property
    def duplicates(self) -> dict[str, int]:
        return {u: c for u, c in self.keyed.items() if c > 1}

    @property
    def redundant(self) -> int:
        """How many tabs are surplus copies (total minus one per unique URL)."""
        return sum(c - 1 for c in self.keyed.values() if c > 1)

    @property
    def duplicate_rate(self) -> float:
        return (self.redundant / self.total) if self.total else 0.0

    @property
    def domains(self) -> Counter:
        return Counter(t.domain for t in self.tabs)

    @property
    def suspended_count(self) -> int:
        return sum(1 for t in self.tabs if t.suspended)

    def windows(self) -> dict[int, list[Tab]]:
        out: dict[int, list[Tab]] = {}
        for t in self.tabs:
            out.setdefault(t.window, []).append(t)
        return {k: sorted(v, key=lambda x: x.index) for k, v in sorted(out.items())}

    def summary(self) -> dict:
        return {
            "total_tabs": self.total,
            "windows": len(self.windows()),
            "unique_urls": self.unique,
            "duplicate_urls": len(self.duplicates),
            "redundant_tabs": self.redundant,
            "duplicate_rate": round(self.duplicate_rate, 4),
            "suspended_tabs": self.suspended_count,
            "top_domains": self.domains.most_common(25),
        }


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #


def is_suspended(url: str) -> bool:
    """True if ``url`` looks like a tab-suspender placeholder."""
    low = (url or "").lower()
    return any(m in low for m in SUSPENDER_MARKERS)


def unwrap_once(url: str) -> Optional[str]:
    """Peel a single suspender layer, or return ``None`` if there is none."""
    if "#" not in url:
        return None
    fragment = url.split("#", 1)[1]
    params: dict[str, str] = {}
    for part in fragment.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k] = v
    for carrier in CARRIER_PARAMS:
        if carrier in params and params[carrier]:
            return urllib.parse.unquote(params[carrier])
    return None


def resolve_target(url: str, max_depth: int = 5) -> str:
    """Fully unwrap nested suspender URLs back to the real destination.

    Handles the case of two suspenders installed simultaneously, which produces
    a Great Suspender URL nested inside a Workona URL (and vice versa).
    """
    if not url:
        return url
    seen = {url}
    current = url
    for _ in range(max_depth):
        if not is_suspended(current):
            break
        nxt = unwrap_once(current)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        current = nxt
    return current


def normalize_url(url: str) -> Optional[str]:
    """Produce a stable deduplication key.

    Returns ``None`` for tabs that carry no information worth keeping:
    new-tab pages and bare suspender placeholders with no resolvable target.
    """
    if not url:
        return None
    u = url.strip()
    low = u.lower()

    if low.startswith("chrome://newtab") or low.startswith("chrome://new-tab-page"):
        return None
    if low.startswith("chrome-extension://") and is_suspended(low):
        return None
    if low.startswith("file://"):
        return u
    if low.startswith("chrome://") or low.startswith("chrome-extension://"):
        return low

    m = _URL_SCHEME.match(u)
    if m:
        scheme, host, rest = m.group(1).lower(), m.group(2).lower(), m.group(3)
        if rest.endswith("/"):
            rest = rest[:-1]
        elif "/?" in rest:
            # Collapse a trailing slash that sits immediately before a query
            # string, so /a/?b=1 and /a?b=1 dedupe to the same key.
            rest = rest.replace("/?", "?", 1)
        return f"{scheme}://{host}{rest}"
    return u


def domain_of(url: str) -> str:
    """Best-effort registrable-ish host, for grouping."""
    if not url:
        return "(empty)"
    low = url.lower()
    # Browser-internal schemes must be checked BEFORE the generic host regex:
    # for `chrome://extensions/` the regex would happily capture the literal
    # string "extensions" as if it were a hostname.
    if low.startswith("chrome://") or low.startswith("chrome-extension://"):
        return "chrome-internal"
    if low.startswith("file://"):
        return "local-file"
    m = _HOST.match(url)
    if m and m.group(1):
        return m.group(1).lower().replace("www.", "")
    return "other"


# --------------------------------------------------------------------------- #
# Chrome control (AppleScript bridge)
# --------------------------------------------------------------------------- #


def osascript(source: str, timeout: int = 120) -> Optional[str]:
    """Run an AppleScript snippet. Returns ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", source],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def _chrome(script_body: str) -> str:
    return f'tell application "Google Chrome"\n{script_body}\nend tell'


def chrome_window_count() -> int:
    out = osascript(_chrome("return count of windows"))
    if out and out.strip().isdigit():
        return int(out.strip())
    return 0


def _window_dump_script(window: int) -> str:
    """Two bulk property reads per window.

    Deliberately NOT a per-tab loop: fetching title/URL one tab at a time over
    AppleScript takes O(n) round trips and times out on a 200-tab window.
    """
    return _chrome(
        "\n".join(
            [
                'set out to ""',
                "set tb to tab",
                "set lf to linefeed",
                f"set tL to title of every tab of window {window}",
                f"set uL to URL of every tab of window {window}",
                "set tc to count of tL",
                "repeat with ti from 1 to tc",
                "set out to out & item ti of tL & tb & item ti of uL & lf",
                "end repeat",
                "return out",
            ]
        )
    )


def read_window(window: int) -> list[Tab]:
    """Read every tab in one Chrome window."""
    out = osascript(_window_dump_script(window))
    if out is None:
        return []
    tabs: list[Tab] = []
    for i, line in enumerate(out.split(LF), 1):
        if not line.strip():
            continue
        if TAB in line:
            title, url = line.split(TAB, 1)
        else:
            title, url = line, ""
        tabs.append(Tab(window=window, index=i, title=title.strip(), raw_url=url.strip()))
    return tabs


def read_session() -> Report:
    """Read every tab in every Chrome window.

    Incremental by design: each window is a separate short AppleScript call, so a
    single slow window cannot blow the timeout for the whole session.
    """
    report = Report()
    for w in range(1, chrome_window_count() + 1):
        report.tabs.extend(read_window(w))
    return report


def close_tabs(window: int, indices: Iterable[int]) -> None:
    """Close tabs by index in one window.

    Indices are closed in descending order so that closing one tab does not
    shift the index of another still to be closed.
    """
    ordered = sorted(set(indices), reverse=True)
    if not ordered:
        return
    lines = ['tell application "Google Chrome"']
    for i in ordered:
        lines += ["try", f"close tab {i} of window {window}", "end try"]
    lines.append("end tell")
    osascript(LF.join(lines), timeout=240)


def open_urls(urls: Iterable[str], batch: int = 1, delay: float = 0.35) -> int:
    """Open URLs in Chrome, ``batch`` per window, paced to avoid a memory spike.

    Returns the number of URLs opened.
    """
    import time

    urls = [u for u in urls if u]
    opened = 0
    for i in range(0, len(urls), max(1, batch)):
        chunk = urls[i : i + max(1, batch)]
        for u in chunk:
            osascript(_chrome(f'make new tab at end of tabs of window 1 with properties {{URL:"{u}"}}'))
            opened += 1
            time.sleep(delay)
    return opened


# --------------------------------------------------------------------------- #
# System instrumentation
# --------------------------------------------------------------------------- #


def chrome_process_stats() -> dict:
    """Split Chrome's helper processes into categories.

    This is the measurement that reveals the real bottleneck: a healthy Chrome
    has a handful of renderers; a pathological one has one per tab.
    """
    proc = subprocess.run(
        ["ps", "-axo", "rss=,command="], capture_output=True, text=True, errors="replace"
    )
    categories: Counter = Counter()
    count = 0
    for line in (proc.stdout or "").split(LF):
        if "Google Chrome" not in line:
            continue
        count += 1
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            rss_kb = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1]
        if "--type=gpu-process" in cmd:
            kind = "gpu"
        elif "--type=renderer" in cmd:
            kind = "renderer"
        elif "--type=utility" in cmd:
            m = re.search(r"--utility-sub-type=([A-Za-z0-9._\-]+)", cmd)
            kind = "utility:" + (m.group(1) if m else "generic")
        elif "--type=" in cmd:
            m = re.search(r"--type=([A-Za-z0-9._\-]+)", cmd)
            kind = m.group(1) if m else "other"
        else:
            kind = "browser-main"
        categories[kind] += rss_kb
    return {
        "process_count": count,
        "total_mb": round(sum(categories.values()) / 1024),
        "renderer_mb": round(categories.get("renderer", 0) / 1024),
        "by_category_mb": {k: round(v / 1024) for k, v in categories.most_common()},
    }


def swap_used_mb() -> float:
    """Currently used swap, in MB. ``-1.0`` if unavailable."""
    out = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True).stdout or ""
    m = re.search(r"used = ([0-9.]+)M", out)
    return float(m.group(1)) if m else -1.0


def memory_free_percent() -> int:
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout or ""
    m = re.search(r"free percentage:\s*(\d+)%", out)
    return int(m.group(1)) if m else -1


# --------------------------------------------------------------------------- #
# Archive parsing / writing
# --------------------------------------------------------------------------- #


def parse_markdown_archive(path: str) -> list[Tab]:
    """Recover tabs from a markdown archive written by :func:`render_markdown`.

    Tolerant by design: a corruption in one line must not lose the other 1,500.
    """
    tabs: list[Tab] = []
    window = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip(LF)
            if line.startswith("## Window "):
                head = line[len("## Window ") :].split(" ")[0].split("-")[0].strip()
                if head.isdigit():
                    window = int(head)
                continue
            m = _MD_ITEM.match(line)
            if not m:
                continue
            title, url = m.group(1), m.group(2)
            tabs.append(
                Tab(window=window, index=len(tabs) + 1, title=title.strip(), raw_url=url.strip())
            )
    return tabs


def write_text(path: str, text: str) -> int:
    """Durably write a file: flush + fsync before returning.

    Safety-net files must be on disk *before* any destructive step runs, so this
    does not trust the OS write cache.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    return len(text)


def escape_md(text: str) -> str:
    """Make a title safe to embed in a markdown link."""
    text = " ".join((text or "").split())
    while text.startswith("#"):
        text = text[1:].strip()
    return text.replace("[", "(").replace("]", ")") or "(untitled)"


def render_markdown(report: Report, title: str) -> str:
    """Render a human-readable archive grouped by window."""
    s = report.summary()
    out = [f"# {title}", ""]
    out += [
        f"{s['total_tabs']} tabs across {s['windows']} windows.",
        "",
        "| Metric | Value |",
        "|:---|:---|",
        f"| Total tabs | {s['total_tabs']} |",
        f"| Windows | {s['windows']} |",
        f"| Unique URLs | {s['unique_urls']} |",
        f"| Duplicate URLs | {s['duplicate_urls']} |",
        f"| Redundant tabs | {s['redundant_tabs']} |",
        f"| Suspended tabs | {s['suspended_tabs']} |",
        "",
        "## Domains",
        "",
        "| Domain | Tabs |",
        "|:---|:---|",
    ]
    for dom, count in s["top_domains"]:
        out.append(f"| {dom} | {count} |")
    out += ["", "---", ""]
    for window, tabs in report.windows().items():
        out.append(f"## Window {window} - {len(tabs)} tabs")
        out.append("")
        for i, t in enumerate(tabs, 1):
            out.append(f"{i}. [{escape_md(t.title)}]({t.url})")
        out.append("")
    return LF.join(out)


def unique_urls(report: Report) -> list[str]:
    """Ordered, de-duplicated list of real destinations."""
    seen: dict[str, None] = {}
    for t in report.tabs:
        if t.key and t.url:
            seen.setdefault(t.key, None)
    lookup = {t.key: t.url for t in report.tabs if t.key}
    return [lookup[k] for k in seen]
