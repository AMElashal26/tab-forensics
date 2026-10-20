# tab-forensics — Engineering Writeup

*A case study in diagnosing a macOS memory crisis, and why the obvious explanation was wrong.*

---

## 1. Symptom

A Mac with **245 GB of storage and a small RAM budget** produced the system dialog:

> **Your system has run out of application memory.**
> To avoid problems with your computer, quit any applications you are not using.

The dialog listed a single culprit: **Google Chrome — 35.08 GB.** Everything else looked normal.
The machine was thrashing, the browser was unresponsive to clicks, and the standard advice
("close some tabs") was useless because the browser was too busy to be clicked.

## 2. Initial hypothesis, and why it was wrong

The intuitive reading: *tabs are heavy, there are a lot of them, so close some.* That leads to the
usual advice — install a tab suspender, let it unload inactive tabs, reclaim the memory.

Investigation quickly broke that model. Instrumenting Chrome's helper processes:

| Observation | Value |
|:---|:---|
| Tabs in the session | 1,666 |
| Windows | 27 |
| Tabs **already suspended** by extensions | **1,497 (98%)** |
| Chrome processes still alive | **1,263** |
| Largest single renderer | **83 MB** |
| Aggregate renderer memory | **12.6 GB** |
| Average per process | ~10 MB |

The session was **98% suspended** and *still* dying. Not one renderer was large. Memorably, the
heaviest single Chrome process was the **browser main process at 1.4 GB** — the controller, not any
page.

### The actual mechanism

A "suspended" tab is not a closed tab. Suspension swaps a heavy renderer heap for a light one — it
does **not** release the renderer process. Each suspended tab therefore still costs:

* one OS process (process table entry, kernel structures, Mach port rights),
* one renderer with baseline overhead (~10 MB here),
* per-process bookkeeping spread across the whole system.

Multiply by 1,500. The machine did not run out of *bytes*; it ran out of **process-table headroom**
and address-space bookkeeping. That is precisely why the dialog names no single large app — the
cost is distributed across a thousand small ones.

> **Conclusion: the bottleneck was tab *count*, not tab *weight*.**
> Suspension cannot fix a count problem, because suspension does not reduce count.
> Only archiving-and-closing does.

## 3. The constraint that shaped the solution

Chrome's UI was frozen. Any approach requiring the user to click — extension popups, "send all tabs
to OneTab", even closing tabs by hand — was out.

But Chrome maintains a **scripting bridge** (the AppleScript dictionary) that is independent of the
renderer loop. It kept answering while the windows were unresponsive. That made an automated,
non-interactive pipeline possible.

It also meant the archive could live **on disk**, where nothing could lose it — strictly safer than
an in-browser extension whose state dies with the process.

## 4. Engineering challenges

### 4.1 AppleScript round-trip explosion

The naive read is one call per tab:

```applescript
repeat with ti from 1 to tc
    set t to title of tab ti of window wi   -- O(n) round trips
    set u to URL of tab ti of window wi     -- O(n) more
end repeat
```

This **timed out** on a 242-tab window — two separate attempts exceeded 120 seconds.

**Fix: bulk property access.** AppleScript can coerce an entire tab collection's property at once:

```applescript
set tL to title of every tab of window wi   -- one round trip
set uL to URL of every tab of window wi     -- one round trip
```

`O(n)` round trips collapse to `O(1)` per window. Combined with per-window incremental reads, a
single pathological window can no longer blow up the whole session read.

### 4.2 Suspender extensions corrupt the data

This was the highest-value discovery. Tab suspenders do not just unload a page — they **navigate
the tab to their own extension page** and stash the real URL, percent-encoded, in the query string:

```
https://workona.com/suspended/#title=Raycast%20Manual&url=https%3A%2F%2Fmanual.raycast.com%2Fai
chrome-extension://noogafoofpebimajpfpamcfhoaifemoa/suspended.html#ttl=...&uri=https://...
```

A naive export records the **wrapper**, not the destination. The archive would have been thousands
of useless `workona.com/suspended` links — technically complete, practically worthless.

**Fix:** `resolve_target()` detects the known suspender hosts and extension IDs, extracts the
`uri`/`url`/`ttl` carrier parameter, decodes it, and recurses.

**Then it got worse — and more interesting.** Some tabs were wrapped **twice**, because two
suspenders were installed simultaneously: a Great Suspender URL nested inside a Workona URL. The
recursive resolver handles arbitrary depth, and `test_double_nested_two_suspenders` locks the
behaviour in permanently.

This also produced an operational recommendation: **run one suspender, not two.**

### 4.3 A mutated session during measurement

Chrome, under memory pressure, silently drops and reorders tabs. Observed in practice:

| Read | Window 1 tab count |
|:---|:---|
| First audit | 46 |
| Second read (minutes later) | 22 |
| Before closing | 40 |

Any plan computed from a stale snapshot can close the **wrong tab**.

**Fix: snapshot-then-mutate in one process.** The closing routine re-reads the session immediately
before acting, so indices are always derived from current state — never from an earlier file.

### 4.4 Generated JavaScript failing silently

The interactive HTML board shipped broken. The browser console showed:

```
SyntaxError: Unexpected EOF                       tab-restore-board.html:13184
ReferenceError: Can't find variable: grp          onclick
ReferenceError: Can't find variable: copyUrls     onclick
```

Root cause: a single stray backslash inside a JavaScript string literal. That one character made
the **entire** `<script>` block fail to parse, so *no* functions were defined and *every* button on
the page was dead — while the page rendered flawlessly. A textbook silent failure: the artifact
looked correct and did nothing.

Two structural defences were added:

1. **Backslash-free code generation.** Newlines are emitted as `String.fromCharCode(10)` rather
   than `"\n"`; group selection uses attribute comparison instead of a CSS selector containing
   quotes. `build_javascript()` then *asserts* the output contains no backslash.
2. **A build gate.** `validate_javascript()` pipes the emitted script through `node --check`
   before writing any HTML. A broken board is now impossible to produce.

### 4.5 Duplicate detection across suspension states

A suspended tab and a live tab pointing at the same page are the same page. Deduplication therefore
had to run on the **resolved, normalized** URL, not the raw one:

* unwrap suspender layers first,
* lowercase scheme and host,
* collapse a trailing slash (including one before a query string),
* treat query strings and fragments as significant,
* discard meaningless pages (`chrome://newtab/`, empty suspend placeholders).

**Measured result: 303 of 1,666 tabs (18%) were redundant duplicates** — the same GitHub and
YouTube pages open three or four times across different windows.

### 4.6 Choosing which copy survives

Closing the wrong copy destroys state. The survivor is chosen by a deliberate preference order:

1. **non-suspended before suspended** — a live tab proves the page still loads;
2. **lower window number** — survivors cluster in earlier windows, disturbing workspace shape
   minimally;
3. then index order, for determinism.

### 4.7 Process-table verification, not guesswork

After acting, the tool re-reads the session and compares key sets against the pre-close inventory,
printing an explicit **`unique URLs lost`** count. The reference run: **0**.

The same verification caught a false alarm mid-session: an intermediate tab count read as `0`
while Chrome was thrashing. Rather than trusting it, per-window counts were re-queried
individually, confirming the operation had been precise (window 1: 46 → 40 as planned).

## 5. Results

| Metric | Before | After |
|:---|:---|:---|
| Tabs | 1,666 | 1,421 |
| Redundant duplicates | 303 | **0** |
| Chrome processes | 1,263 | 2 *(after quit)* |
| Chrome memory | 13.7 GB | 17 MB |
| Swap used | 16,662 MB | **1,741 MB** |
| System memory free | 22% | **61%** |
| Unique destinations archived | — | **1,587** |
| Unique URLs lost | — | **0** |

Swap collapsed from 16.6 GB to 1.7 GB, and macOS shrank the swap file itself from 12 GB to 3 GB — a
reliable signal that pressure genuinely eased rather than merely moving around.

## 6. Technology stack

| Layer | Technology | Role |
|:---|:---|:---|
| Orchestration | **Python 3.11** (stdlib only) | Pipeline, data model, reporting |
| Browser control | **AppleScript** via `osascript` | Tab read/close/open bridge |
| System metrics | `ps`, `sysctl vm.swapusage`, `memory_pressure` | Process and swap instrumentation |
| Frontend | **HTML + CSS + vanilla JS**, zero dependencies | Offline single-file restore board |
| Build validation | **Node.js** (`node --check`) | Syntax gate on generated JavaScript |
| Data formats | JSON, Markdown, plain text | Four independent archive copies |
| VCS | **git** + **GitHub CLI** | History and publication |

**Zero third-party dependencies.** Everything is standard library or shipped with macOS. The tool
runs on a fresh machine with no install step — a deliberate choice for a recovery utility that may
be needed precisely when a system is too degraded to install anything.

## 7. Design principles extracted

1. **Measure the right quantity.** Process count, not memory, was the binding constraint. Profiling
   per-process and per-category is what exposed it.
2. **Suspend ≠ close.** Unloading contents is not releasing resources. Distinguish them explicitly.
3. **Archive to disk before destroying.** Durably (`flush` + `fsync`), and in more than one format,
   so recovery never depends on one tool still working.
4. **Snapshot immediately before mutating.** External state changes under you; recompute, never
   reuse.
5. **Verify with an invariant, not a feeling.** "Unique URLs lost = 0" is checkable. "Seems fine"
   is not.
6. **Generate code that cannot silently break, then gate the build.** Assert the invariant, and
   validate with a real parser before writing.
7. **Dry run by default.** Destructive action requires an explicit flag.
8. **Prefer failing loudly.** One syntax error disabled an entire page's behaviour without a visible
   symptom — the worst class of bug, and worth engineering against.

## 8. Limitations

* **macOS only** — AppleScript is the control channel.
* **Incognito windows are invisible** to the scripting bridge; their tabs cannot be archived.
* **Unsaved text is unrecoverable.** Drafts in form fields, email composers or chat inputs are not
  part of the DOM dump this tool reads. Documented, not solved.
* **Chromium-family only.** Firefox/Safari would need different bridges.
* **URL-level deduplication.** Two different anchors into the same single-page app are treated as
  distinct, which is conservative but can miss near-duplicates.

## 9. What I would build next

* **Incremental archival daemon** — periodically snapshot new tabs so no session can ever grow
  unbounded again.
* **Semantic clustering** — group tabs by topic (not just domain) using embeddings, so the restore
  board suggests meaningful batches.
* **Session-preserving close** — capture scroll position and form state where reachable, to make
  archiving lossless.
* **Linux/Windows bridges** via CDP, keeping the same core library.

---

*All figures in this document are from the author's own reference run. Personal browsing data is
deliberately excluded from this repository; fixtures under `data/sample/` are synthetic.*
