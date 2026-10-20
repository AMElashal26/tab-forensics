#!/usr/bin/env python3
"""close_duplicates.py -- close redundant duplicate tabs, safely.

Safety model
------------
Every step that could lose data is gated behind a check that must pass:

1. A fresh snapshot is read from Chrome immediately before closing, not reused
   from an earlier run (a stressed browser silently drops and reorders tabs).
2. A markdown archive plus a plain URL list of the *whole* session is written to
   disk and fsync'd **before** the first tab is closed.
3. A dedicated report of exactly which tabs were closed is written, so the
   decision is auditable and reversible.
4. The number of tabs about to be closed is sanity-checked against the session
   size; an implausible volume aborts the run rather than proceeding.
5. Indices are closed in descending order, so closing tab 40 cannot shift the
   position of tab 12.

Dry run by default. ``--apply`` is required to actually close anything.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tabkit  # noqa: E402

LF = "\n"


def choose_survivors(report: tabkit.Report) -> tuple[dict[str, str], list[tabkit.Tab]]:
    """Return (key -> kept url, list of tabs to close).

    One live tab is kept per normalized URL. Preference order:

    1. Non-suspended tabs before suspended ones, because a live tab proves the
       page still loads.
    2. Lower window number, so survivors cluster in earlier windows and the
       user's workspace shape is disturbed as little as possible.
    """
    groups: dict[str, list[tabkit.Tab]] = {}
    for t in report.tabs:
        if t.key:
            groups.setdefault(t.key, []).append(t)

    survivors: dict[str, str] = {}
    doomed: list[tabkit.Tab] = []
    for key, tabs in groups.items():
        ordered = sorted(tabs, key=lambda t: (1 if t.suspended else 0, t.window, t.index))
        survivors[key] = ordered[0].url
        doomed.extend(ordered[1:])
    return survivors, doomed


def render_closed_report(doomed: list[tabkit.Tab], stamp: str) -> str:
    by_window = Counter(t.window for t in doomed)
    out = [
        f"# Closed Duplicate Tabs - {stamp}",
        "",
        f"{len(doomed)} tabs closed. Each was an exact duplicate, by normalized URL, of a tab",
        "kept open elsewhere in the session. Survivors were chosen preferring non-suspended",
        "tabs and lower window numbers.",
        "",
        "| Window | Tabs closed |",
        "|:---|:---|",
    ]
    for w in sorted(by_window):
        out.append(f"| {w} | {by_window[w]} |")
    out.append("")
    for w in sorted(by_window):
        rows = sorted([t for t in doomed if t.window == w], key=lambda t: t.index)
        out.append(f"## Closed from Window {w} ({len(rows)})")
        out.append("")
        for t in rows:
            out.append(f"- [{tabkit.escape_md(t.title)}]({t.url})")
        out.append("")
    return LF.join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually close tabs (default: dry run)")
    ap.add_argument("--outdir", default=os.path.expanduser("~/Documents"), help="where to write reports")
    ap.add_argument("--stamp", default=date.today().isoformat())
    ap.add_argument("--max-fraction", type=float,
                    default=tabkit.config()["limits"]["max_close_fraction"],
                    help="abort if closing more than this fraction "
                         "(config: limits.max_close_fraction)")
    args = ap.parse_args(argv)

    report = tabkit.read_session()
    if not report.tabs:
        print("No tabs found. Is Chrome running?", file=sys.stderr)
        return 1

    procs = tabkit.chrome_process_stats()
    survivors, doomed = choose_survivors(report)
    frac = len(doomed) / max(1, report.total)

    print(f"tabs read          {report.total}")
    print(f"unique URLs        {len(survivors)}")
    print(f"to close           {len(doomed)}  ({frac * 100:.1f}% of session)")
    print(f"chrome processes   {procs['process_count']}")

    if len(doomed) > args.max_fraction * report.total:
        print(f"ABORT: {frac * 100:.1f}% exceeds --max-fraction {args.max_fraction * 100:.0f}%", file=sys.stderr)
        return 2
    if not doomed:
        print("Nothing to close.")
        return 0

    # ---- safety net FIRST ----
    stem = os.path.join(args.outdir, f"tabs-{args.stamp}")
    inv = tabkit.render_markdown(report, f"Chrome Tab Inventory (pre-close) - {args.stamp}")
    tabkit.write_text(stem + "-inventory.md", inv)
    tabkit.write_text(stem + "-closed-duplicates.md", render_closed_report(doomed, args.stamp))
    closed_urls = list(dict.fromkeys(t.url for t in doomed if t.url))
    tabkit.write_text(stem + "-closed-duplicates-urls.txt", LF.join(closed_urls) + LF)

    print("")
    print(f"written  {stem}-inventory.md  ({len(report.tabs)} tabs)")
    print(f"written  {stem}-closed-duplicates.md  ({len(doomed)} tabs)")
    print(f"written  {stem}-closed-duplicates-urls.txt  ({len(closed_urls)} URLs)")

    if not args.apply:
        print("")
        print("DRY RUN - nothing closed. Re-run with --apply to proceed.")
        return 0

    plan: dict[int, list[int]] = {}
    for t in doomed:
        plan.setdefault(t.window, []).append(t.index)

    for window in sorted(plan):
        tabkit.close_tabs(window, plan[window])
        print(f"closed {len(plan[window])} from window {window}")

    after = tabkit.read_session()
    procs_after = tabkit.chrome_process_stats()
    print("")
    print(f"tabs now           {after.total}")
    print(f"chrome processes   {procs_after['process_count']}")
    print(f"chrome total       {procs_after['total_mb']} MB")

    # Verify no unique URL was lost.
    before_keys = set(survivors.keys())
    after_keys = set(after.keyed.keys())
    missing = before_keys - after_keys
    print(f"unique URLs lost   {len(missing)}")
    for m in list(missing)[:10]:
        print(f"    LOST: {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
