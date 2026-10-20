#!/usr/bin/env python3
"""open_batch.py -- reopen archived tabs in controlled batches.

Chrome thrashes when a few hundred tabs appear at once. This reopens them in
paced batches so the renderer count climbs gradually and stays observable.

Usage
-----
    python3 open_batch.py --list                       # show domain counts
    python3 open_batch.py --domain youtube.com --limit 20 --apply
    python3 open_batch.py --file board-urls.txt --limit 30 --apply
    python3 open_batch.py --domain github.com --dry-run

Dry run by default.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tabkit  # noqa: E402


def load_urls(path: str) -> list[str]:
    urls = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            u = line.strip()
            if u and not u.startswith("#"):
                urls.append(u)
    return urls


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="file containing one URL per line")
    ap.add_argument("--domain", help="only reopen URLs on this domain")
    cfg_open, cfg_limits = tabkit.config()["open"], tabkit.config()["limits"]
    ap.add_argument("--limit", type=int, default=cfg_open["default_batch"],
                    help="max URLs to open this run (config: open.default_batch)")
    ap.add_argument("--batch", type=int, default=1, help="tabs per window")
    ap.add_argument("--delay", type=float, default=cfg_open["per_tab_delay_seconds"],
                    help="seconds between tabs (config: open.per_tab_delay_seconds)")
    ap.add_argument("--list", action="store_true", help="list available domains and exit")
    ap.add_argument("--apply", action="store_true", help="actually open (default: dry run)")
    args = ap.parse_args(argv)

    if not args.file:
        print("--file is required (a URL list from session_audit.py)", file=sys.stderr)
        return 1

    urls = load_urls(args.file)
    if not urls:
        print(f"no URLs found in {args.file}", file=sys.stderr)
        return 1

    if args.list:
        counts = Counter(tabkit.domain_of(u) for u in urls)
        print(f"{len(urls)} URLs across {len(counts)} domains")
        print("")
        for dom, n in counts.most_common(60):
            print(f"  {n:>5}  {dom}")
        return 0

    if args.domain:
        urls = [u for u in urls if tabkit.domain_of(u) == args.domain]
        if not urls:
            print(f"no URLs matched domain {args.domain}", file=sys.stderr)
            return 1

    selection = urls[: args.limit]
    print(f"matched            {len(selection)} URLs")
    print(f"domain filter      {args.domain or '(none)'}")
    print(f"limit              {args.limit}")
    print("")

    for u in selection[:12]:
        print(f"  {u[:110]}")
    if len(selection) > 12:
        print(f"  ... and {len(selection) - 12} more")

    if not args.apply:
        print("")
        print("DRY RUN - nothing opened. Re-run with --apply to proceed.")
        return 0

    print("")
    procs_before = tabkit.chrome_process_stats()
    print(f"processes before   {procs_before['process_count']}")

    opened = 0
    for u in selection:
        tabkit.open_urls([u], batch=args.batch, delay=args.delay)
        opened += 1
        if opened % 10 == 0:
            p = tabkit.chrome_process_stats()
            print(f"  opened {opened:>3}  processes {p['process_count']:>5}  mem {p['total_mb']:>6} MB")

    time.sleep(2)
    procs_after = tabkit.chrome_process_stats()
    print("")
    print(f"opened             {opened}")
    print(f"processes after    {procs_after['process_count']}")
    print(f"memory after       {procs_after['total_mb']} MB")
    print(f"swap used          {tabkit.swap_used_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
