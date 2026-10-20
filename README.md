# tab-forensics

Audit, archive and de-bloat a runaway Chrome tab session — **without losing a single URL.**

Built after a real session reached **1,666 tabs across 27 windows**, consumed **16.6 GB of swap**,
spawned **1,263 Chrome processes** and produced macOS's *"Your system has run out of application
memory"* dialog. The recovery pipeline in this repo preserved **1,587 unique destinations**,
removed **547 surplus tabs**, and cut swap from **16.6 GB to 1.7 GB** with **zero URLs lost**.

---

## The finding that matters

Everyone's instinct is that heavy tabs cause memory pressure. The data disagreed:

| Observation | Value |
|:---|:---|
| Tabs already suspended by extensions | **1,497 of 1,527 (98%)** |
| Chrome processes still running | **1,263** |
| Largest single renderer | only **83 MB** |
| Aggregate renderer memory | **12.6 GB** |

Ninety-eight percent of tabs were suspended, and the machine was *still* dying. The reason:

> **Suspension does not release the renderer process — it only shrinks its contents.**

One suspended tab still costs one process. At 1,500 tabs that is ~1,500 processes, each with
baseline overhead. macOS exhausts process-table headroom long before it exhausts bytes, which is
why "out of application memory" appears with no single large application to blame.

**The bottleneck was tab *count*, not tab *weight*.** The only fix that reduces process count is
archiving tabs to disk and *closing* them — which is what this toolkit does safely.

---

## What it does

| Script | Purpose |
|:---|:---|
| `session_audit.py` | Read-only audit: tab counts, duplicate rate, process/memory profile, diagnosis |
| `close_duplicates.py` | Close redundant duplicate tabs, archive-first, with verification |
| `open_batch.py` | Reopen archived tabs in paced batches with live process monitoring |
| `tabkit.py` | Library: suspender unwrapping, normalization, Chrome bridge, instrumentation |
| `build_board.py` | Generate a self-contained interactive HTML restore board |

---

## Quickstart

No dependencies. Python 3.9+ and macOS only.

```bash
# 1. Look first — changes nothing
python3 scripts/session_audit.py

# 2. Archive everything to disk (markdown + plain URLs + interactive board)
python3 scripts/session_audit.py \
    --archive ~/Documents/tabs.md \
    --board   ~/Documents/tabs.html

# 3. Dry run: see what duplicate closing would do
python3 scripts/close_duplicates.py

# 4. Apply it (writes safety-net files BEFORE closing anything)
python3 scripts/close_duplicates.py --apply

# 5. Reopen a domain, in controlled batches
python3 scripts/open_batch.py --file ~/Documents/tabs-urls.txt --list
python3 scripts/open_batch.py --file ~/Documents/tabs-urls.txt \
    --domain github.com --limit 25 --apply

# Tests
python3 -m unittest discover -s tests -v
```

---

## The two problems worth knowing about

### 1. Suspender extensions hide your real URLs

Tab suspenders (`Workona`, `The Great Suspender`, `Marvelous Suspender`) do not merely unload a
tab — they **navigate it to their own extension page** and percent-encode the real URL into the
query string. A naive export therefore records rubbish:

```
https://workona.com/suspended/#title=Raycast%20Manual&url=https%3A%2F%2Fmanual.raycast.com%2Fai
```

`resolve_target()` unwraps that back to `https://manual.raycast.com/ai`.

And if **two** suspenders are installed at once, they nest — one extension's page wrapped inside
the other's. This genuinely occurred in the session that motivated this project, and it is the
case that `test_double_nested_two_suspenders` locks down.

### 2. Generated JavaScript is one backslash away from silently dying

During development the generated board shipped broken. The browser console reported:

```
SyntaxError: Unexpected EOF                          tab-restore-board.html:13184
ReferenceError: Can't find variable: grp             onclick
ReferenceError: Can't find variable: copyUrls        onclick
```

A single stray backslash inside a JavaScript string literal caused the **entire** `<script>` block
to fail parsing. No handlers were defined, so *every* button on the page was dead — while the page
itself looked perfectly fine.

Two defences now prevent this permanently:

* `build_javascript()` uses `String.fromCharCode(10)` instead of `"\n"` and asserts the emitted
  script contains no backslash at all.
* `validate_javascript()` pipes the script through `node --check` **before** any HTML reaches disk.
  A broken board cannot be written.

Both are enforced by tests (`TestBoardGeneration`).

---

## Safety model

Destroying 1,500 tabs is irreversible, so every mutation is gated:

1. **Fresh snapshot immediately before mutating.** A tab session is read again at the moment of
   action, never reused from an earlier run — a stressed Chrome silently drops and reorders tabs
   (window sizes were observed changing between reads).
2. **Archive first, fsync'd.** Markdown, a plain URL list, and a record of exactly what will be
   closed are written *and flushed to disk* before the first tab closes.
3. **Volume sanity check.** Aborts rather than proceeding if it would close an implausible
   fraction of the session.
4. **Descending index order.** Closing tab 40 cannot shift tab 12 out from under itself.
5. **Post-run verification.** After closing, the session is re-read and compared against the
   pre-close key set; the script reports `unique URLs lost`. The reference run reported **0**.
6. **Dry run by default.** `--apply` is required for anything destructive.

---

## Architecture

```
                     ┌──────────────────┐
                     │   Chrome (macOS) │
                     └────────┬─────────┘
                              │ AppleScript ("osascript")
                              │ 2 bulk property reads per window
                     ┌────────▼─────────┐
                     │     tabkit.py    │
                     │  ─ resolve_target│  unwrap nested suspenders
                     │  ─ normalize_url │  dedupe key
                     │  ─ read_session  │  incremental per window
                     │  ─ process_stats │  ps/sysctl/memory_pressure
                     └───┬──────────┬───┘
                         │          │
        ┌────────────────▼──┐   ┌───▼───────────────┐
        │ close_duplicates  │   │ build_board.py    │
        │ open_batch        │   │ session_audit     │
        └───────────────────┘   └───────────────────┘
```

**Why a Python → `osascript` bridge instead of a Chrome extension?** When Chrome's UI is frozen,
an AppleScript connection still answers — the scripting bridge is separate from the renderer loop.
It also needs no permissions, no install step, and no user interaction. The trade-off is that
AppleScript is slow, which drives the two design rules below.

**Why two bulk property reads per window?** Fetching `title` and `URL` per tab is `O(n)` AppleScript
round trips and reliably timed out on a 242-tab window. Reading `title of every tab` and
`URL of every tab` as two list properties collapses it to `O(1)` round trips per window.

**Why incremental per-window reads?** One pathological window cannot blow the timeout for the whole
session.

---

## Results from the reference run

| Metric | Before | After |
|:---|:---|:---|
| Tabs | 1,666 | 1,421 |
| Redundant duplicate tabs | 303 | **0** |
| Chrome processes | 1,263 | 2 *(after quit)* |
| Chrome memory | 13.7 GB | 17 MB *(after quit)* |
| Swap used | 16,662 MB | **1,741 MB** |
| System memory free | 22% | **61%** |
| Unique URLs preserved | — | **1,587** |
| Unique URLs lost | — | **0** |

---

## Repository layout

```
tab-forensics/
├── scripts/
│   ├── tabkit.py            # library: resolution, normalization, Chrome bridge, metrics
│   ├── session_audit.py     # read-only audit + archive
│   ├── close_duplicates.py  # archive-first duplicate removal
│   ├── open_batch.py        # paced batch reopening
│   └── build_board.py       # HTML restore board generator
├── tests/
│   └── test_tabkit.py       # 43 tests
├── docs/
│   ├── WRITEUP.md           # engineering narrative and technical notes
│   └── EXPERIMENT-LOG.md    # chronological record with measurements
├── data/
│   └── sample/              # synthetic, anonymised fixtures (safe to commit)
├── CHANGELOG.md
└── Makefile
```

---

## Privacy note

This tool reads **every open tab**, so its output is sensitive by nature. The repo therefore ships
only **synthetic** fixtures under `data/sample/`. Real exports are excluded by `.gitignore`.
The author's own reference dataset was deliberately never committed.

---

## License

MIT — see `LICENSE`.
