#!/usr/bin/env python3
"""session_audit.py -- read-only audit and archive of a live Chrome session.

Usage
-----
    python3 session_audit.py                      # report only, changes nothing
    python3 session_audit.py --export out.json    # also dump raw tab records
    python3 session_audit.py --archive out.md     # also write a markdown archive
    python3 session_audit.py --board out.html     # also write a restore board
    python3 session_audit.py --json               # machine-readable report

This script is deliberately non-destructive. Nothing is closed, killed or moved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_board  # noqa: E402
import tabkit  # noqa: E402


def human_report(report: tabkit.Report, procs: dict, swap: float, free: int) -> str:
    s = report.summary()
    lines = []
    lines.append("=" * 66)
    lines.append("CHROME SESSION AUDIT")
    lines.append("=" * 66)
    lines.append("")
    lines.append("Tabs")
    lines.append("-" * 66)
    lines.append(f"  total tabs         {s['total_tabs']:>8}")
    lines.append(f"  windows            {s['windows']:>8}")
    lines.append(f"  unique URLs        {s['unique_urls']:>8}")
    lines.append(f"  duplicate URLs     {s['duplicate_urls']:>8}")
    lines.append(f"  redundant tabs     {s['redundant_tabs']:>8}   ({s['duplicate_rate'] * 100:.1f}% of all tabs)")
    lines.append(f"  already suspended  {s['suspended_tabs']:>8}")
    lines.append("")
    lines.append("Processes / memory")
    lines.append("-" * 66)
    lines.append(f"  chrome processes   {procs['process_count']:>8}")
    lines.append(f"  chrome total       {procs['total_mb']:>8} MB")
    lines.append(f"  of which renderers {procs['renderer_mb']:>8} MB")
    lines.append(f"  swap used          {swap:>8.0f} MB")
    lines.append(f"  memory free        {free:>8} %")
    if procs["process_count"]:
        per = procs["total_mb"] / procs["process_count"]
        lines.append(f"  avg per process    {per:>8.1f} MB")
    lines.append("")
    lines.append("Largest domains")
    lines.append("-" * 66)
    for dom, count in s["top_domains"][:15]:
        bar = "#" * min(40, int(count / max(1, s["total_tabs"]) * 120))
        lines.append(f"  {count:>5}  {dom:<34}{bar}")
    lines.append("")

    # The interpretation that matters: process count, not bytes.
    if procs["process_count"]:
        susp_ratio = s["suspended_tabs"] / max(1, s["total_tabs"])
        lines.append("Diagnosis")
        lines.append("-" * 66)
        if susp_ratio > 0.6 and procs["process_count"] > 400:
            lines.append("  Most tabs are ALREADY suspended, yet the process count is high.")
            lines.append("  Suspension does not release the renderer process, only shrinks it.")
            lines.append("  The bottleneck here is tab/process COUNT, not tab weight.")
            lines.append("  Archiving and closing (not suspending) is the effective remedy.")
        elif s["redundant_tabs"] > 100:
            lines.append(f"  {s['redundant_tabs']} redundant tabs share URLs already open elsewhere.")
            lines.append("  Closing duplicates is the cheapest memory win available.")
        else:
            lines.append("  No dominant pathology detected.")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", metavar="JSON", help="write raw tab records")
    ap.add_argument("--archive", metavar="MD", help="write a markdown archive")
    ap.add_argument("--board", metavar="HTML", help="write an interactive restore board")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--stamp", default=date.today().isoformat(), help="date stamp for filenames")
    ap.add_argument(
        "--shareable",
        metavar="MD",
        help="write a redacted archive safe to show to others; hosts listed in "
             "privacy.redact_hosts and local account paths are replaced",
    )
    ap.add_argument("--config", metavar="TOML", help="explicit config file to use")
    args = ap.parse_args(argv)

    # An explicit --config wins over auto-discovery and fails loudly if absent,
    # so a typo cannot silently fall back to defaults.
    if args.config:
        try:
            tabkit.use_config_file(args.config)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    for warning in tabkit.config_warnings():
        print(f"config: {warning}", file=sys.stderr)

    report = tabkit.read_session()
    if not report.tabs:
        print("No Chrome tabs found. Is Chrome running?", file=sys.stderr)
        return 1

    procs = tabkit.chrome_process_stats()
    swap = tabkit.swap_used_mb()
    free = tabkit.memory_free_percent()

    if args.json:
        payload = {
            "summary": report.summary(),
            "processes": procs,
            "swap_used_mb": swap,
            "memory_free_percent": free,
        }
        payload["summary"]["top_domains"] = [list(x) for x in payload["summary"]["top_domains"]]
        print(json.dumps(payload, indent=2))
    else:
        print(human_report(report, procs, swap, free))

    if args.export:
        tabkit.write_text(args.export, json.dumps([t.as_dict() for t in report.tabs], indent=1))
        print(f"exported  {args.export}  ({len(report.tabs)} tabs)")
    if args.archive:
        md = tabkit.render_markdown(report, f"Tab Archive - {args.stamp}")
        tabkit.write_text(args.archive, md)
        print(f"archive   {args.archive}  ({os.path.getsize(args.archive)} bytes)")
    if args.shareable:
        md, redacted = tabkit.render_shareable_markdown(report, f"Session audit {args.stamp}")
        tabkit.write_text(args.shareable, md)
        print(
            f"shareable {args.shareable}  ({os.path.getsize(args.shareable)} bytes, "
            f"{redacted} hosts redacted)"
        )
        if redacted == 0:
            print(
                "  note: nothing was redacted. Set privacy.redact_hosts in your "
                "config before sharing this file.",
                file=sys.stderr,
            )
    if args.board:
        build_board.write_board(
            report.tabs,
            args.board,
            title=f"Tab Restore Board - {args.stamp}",
        )
        print(f"board     {args.board}  ({os.path.getsize(args.board)} bytes)")

    if args.export or args.archive or args.board:
        urls = tabkit.unique_urls(report)
        stem = os.path.splitext(args.export or args.archive or args.board)[0]
        plain = stem + "-urls.txt"
        tabkit.write_text(plain, "\n".join(urls) + "\n")
        print(f"url list  {plain}  ({len(urls)} unique URLs)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
