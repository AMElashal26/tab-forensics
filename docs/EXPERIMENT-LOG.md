# Experiment Log

Chronological record of the reference run, including dead ends. Measurements are real; the tab
listings are abbreviated.

---

## Phase 1 — Diagnosis

### 1.1 First look at memory

```
memory_pressure                         → System-wide memory free percentage: 22%
vm.swapusage                            → total 17408 MB   used 16662 MB   free 746 MB
Pages free                              → 4,022 pages (~63 MB)
System-wide memory free percentage      → 22%
```

**Reading:** not merely low on RAM — pinned against the swap ceiling. This is the state that
produces beachballing and the "out of application memory" dialog.

### 1.2 Top consumers

| Process | Memory |
|:---|:---|
| Safari | 7.1 GB |
| Google Chrome (main) | 5.8 GB |
| Finder | 2.5 GB |
| Chrome Helpers (top 8) | ~6.1 GB |
| WindowServer | 1.6 GB |
| Terminal | 1.4 GB |
| CloudD | 0.9 GB |

### 1.3 Tab inventory

```
Window  1:  51 tabs     Window 10: 118 tabs     Window 19:  37 tabs
Window  2:  46 tabs     Window 14: 242 tabs     Window 22:  88 tabs
Window  3:  49 tabs     Window 15: 189 tabs     Window 26:  91 tabs
Window  4:  91 tabs     Window 18: 192 tabs     Window 27:  95 tabs
...
CHROME TOTAL: 1666 tabs in 27 windows
```

### 1.4 First falsification

An early read showed window 1 with **46** tabs, then **22** on a re-read minutes later.

**Lesson:** a browser under memory pressure silently drops and reorders tabs. Any snapshot is
immediately stale, so plans must never be computed from an old file. This directly caused the
"snapshot immediately before mutating" rule.

---

## Phase 2 — Building the archive

### 2.1 Dead end: per-tab AppleScript

```
tell application "Google Chrome"
    repeat with wi from 1 to 27
        repeat with ti from 1 to (count of tabs of window wi)
            title of tab ti of window wi   -- O(n) round trips
            URL of tab ti of window wi
        end repeat
    end repeat
end tell
```

**Result:** `TimeoutError` twice, both attempts exceeding 120 seconds.

**Fix:** bulk property coercion — `title of every tab` / `URL of every tab`. Two round trips per
window instead of hundreds. Completed all 27 windows in well under a minute.

### 2.2 Discovery: the URLs were wrappers

First successful export produced entries like:

```
https://workona.com/suspended/#title=Raycast%20Manual&url=https%3A%2F%2Fmanual.raycast.com%2Fai
```

Not the real destination. Almost every tab was recorded as a suspender placeholder.

**Impact had it gone unnoticed:** an archive of thousands of identical useless wrapper URLs — the
worst outcome, because it *looks* like a successful backup.

### 2.3 Discovery: double nesting

While resolving, some entries resolved to *another* suspender URL:

```
workona.com/suspended → chrome-extension://noogafoofpebimajpfpamcfhoaifemoa/suspended.html → real URL
```

Two suspenders installed simultaneously, each wrapping the other's output.

**Fix:** recursive resolution with a visited-set to guarantee termination.
**Operational follow-up:** run one suspender, not two.

---

## Phase 3 — First cleanup (duplicates)

### 3.1 Planning

Fresh snapshot captured immediately before acting:

| Metric | Value |
|:---|:---|
| Tabs | 1,665 |
| Unique URLs | 1,418 |
| Duplicate URLs | 173 |
| Redundant tabs | 244 |

### 3.2 Safety net written first

Three files written and `fsync`'d **before** any tab was closed:

* `tabs-2026-10-20-inventory.md` (167 KB) — full pre-close snapshot
* `tabs-2026-10-20-closed-duplicates.md` (22 KB) — exactly what will be closed
* `tabs-2026-10-20-closed-duplicates-urls.txt` (12 KB) — reopenable URLs

### 3.3 Execution

Closed descending by index, per window:

```
WIN 14 closed 43     WIN 18 closed 32     WIN 26 closed 20
WIN 15 closed 48     WIN 22 closed 16     WIN 10 closed 15
...
CLOSE_ATTEMPTS: 244
```

### 3.4 A false alarm, handled correctly

Immediately after closing, a verification read returned:

```
AFTER_WINDOWS: 27   AFTER_TABS: 0
```

**Zero tabs** — apparently catastrophic. Rather than trusting one read, each window was re-queried
individually:

```
w1 rc: 0 tabs: 40
w2 rc: 0 tabs: 48
w4 rc: 0 tabs: 86
```

Cross-checked against the plan (46→40, 51→48, 91→86) — **exactly as intended.** The `0` was a
transient read during thrashing.

**Lesson:** an implausible measurement is a measurement problem, not necessarily a system problem.
Verify by a second independent path before reacting.

### 3.5 Verification

```
NOW_TABS: 1421
UNIQUE_URLS_LOST: 0
REMAINING_REDUNDANT: 0
EXPECTED: 1421
```

Clean.

---

## Phase 4 — The pivotal measurement

After cleanup, Chrome was still at 5.6 GB and swap barely moved. Suspicious. Full process census:

| Category | Memory |
|:---|:---|
| renderer | **12,654 MB** |
| browser-main | 1,400 MB |
| network service | 132 MB |
| gpu | 79 MB |
| **Total processes** | **1,263** |

Largest single renderer: **83 MB.** Average ~10 MB.

Meanwhile:

```
TABS_NOW: 1527
SUSPENDED_FLAG_COUNT: 1497      ← 98% already suspended
```

**This reframed the entire problem.**

98% of tabs were already suspended, yet 1,263 processes held 12.6 GB. Suspension had shrunk each
tab to ~10 MB but released nothing.

> **The binding constraint was process count, not memory.**
> Suspension cannot reduce process count. Only closing can.

This also explained the "35 GB Chrome" in the system dialog — the sum of a thousand small
processes, with no single culprit to point at.

---

## Phase 5 — The HTML board, and a silent failure

### 5.1 First board: dead buttons

The board rendered perfectly. Every button did nothing. Browser console:

```
SyntaxError: Unexpected EOF                       tab-restore-board.html:13184
ReferenceError: Can't find variable: grp          onclick  line 3537
ReferenceError: Can't find variable: copyUrls     onclick  line 216
```

### 5.2 Root cause

Braces and parentheses balanced. The break was character-level — a stray backslash inside a
JavaScript string literal, which terminated the string early and killed the **entire** script block:

```
intended:  'li[data-g="' + g + '"]'      →  emitted:  'li[data-g=\+g+\]'
intended:  u.join('\n')                  →  emitted:  a real line break (fatal)
```

Consequence: **no functions defined at all**, so every handler threw `ReferenceError`. The page
looked correct while being completely inert — the worst failure mode, because nothing signals it.

### 5.3 Fixes applied

1. Emit newlines via `String.fromCharCode(10)`, never `"\n"`.
2. Select groups by attribute comparison instead of a quote-bearing CSS selector.
3. Assert the generated script contains **no backslash**.
4. Gate the build on `node --check` — invalid script cannot be written to disk.

### 5.4 Secondary catch

Reconciling the master URL list against all archives revealed **165 tabs that existed in only one
file**. Final archive: **1,587 unique destinations**, up from 1,419.

**Lesson:** cross-check archives against each other. A single file can silently under-report.

---

## Phase 6 — Outcome

| Metric | Before | After |
|:---|:---|:---|
| Tabs | 1,666 | 1,421 |
| Redundant duplicates | 303 | **0** |
| Chrome processes | 1,263 | 2 *(after quit)* |
| Chrome memory | 13.7 GB | 17 MB |
| **Swap used** | **16,662 MB** | **1,741 MB** |
| Swap file size | 12,288 MB | 3,072 MB |
| **System memory free** | **22%** | **61%** |
| Unique URLs preserved | — | **1,587** |
| Unique URLs lost | — | **0** |

Swap did not merely shrink; macOS **shrank the swap file itself** (12 GB → 3 GB), which is the
reliable signal that pressure genuinely eased rather than merely relocating.

---

## Dead ends and discarded approaches

| Approach | Why rejected |
|:---|:---|
| Chrome extension popup ("send all to OneTab") | UI frozen; required clicks that could not land |
| Per-tab AppleScript reads | Timed out at 242 tabs |
| Browser `chrome.tabs` API directly | Needs a loaded extension; unavailable when browser is wedged |
| Relying on Chrome's session restore | Would re-create the exact 1,263-process explosion |
| Trusting one post-action read | Produced a false `0 tabs` reading |
| Hand-built HTML with manual escaping | Shipped a silently dead page |

## Open risks

* **Incognito tabs are unarchiveable** via the scripting bridge. Documented, not solved.
* **Unsaved text in form fields is lost** — outside what a DOM-level read can recover.
* **Near-duplicates in SPAs** (different anchors, same app) are treated as distinct URLs; conservative
  but imperfect.
