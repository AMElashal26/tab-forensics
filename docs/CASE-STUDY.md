# Case study: recovering a machine from a 1,666-tab browser session

**Context.** A macOS machine became unusable: 16.6 GB of swap committed, sustained high CPU, and
macOS reporting *"Your system has run out of application memory."* The browser had accumulated
1,666 tabs across 27 windows over months of research. The goals were to reduce memory pressure and
to lose **no** saved research in the process. Every tab had to be recoverable.

**Outcome.** Swap 16.6 GB → 1.7 GB. Chrome processes 1,263 → 2. 547 surplus tabs removed across
two phases. **1,587 unique destinations preserved, 0 lost.** A complete, self-contained toolkit was
built to do it reproducibly, with a 59-test suite.

---

## The problem worth solving

The interesting part was not "close some tabs." It was that the obvious model of the problem was
wrong, and acting on it would have failed.

The natural assumption is that heavy tabs cause memory pressure, so the fix is to suspend or
discard them. Instrumenting the session falsified that assumption:

| Observation | Value |
|:---|:---|
| Tabs already suspended by extensions | 1,497 of 1,527 (**98%**) |
| Chrome processes still alive | **1,263** |
| Largest single renderer | **83 MB** |
| Aggregate renderer memory | **12.6 GB** |

Ninety-eight percent of tabs were *already* suspended and the machine was still dying. No single
renderer was large. So the cost was not in the contents of any tab — it was in the **existence** of
1,263 processes, each carrying baseline overhead.

> **Suspension shrinks a renderer's contents; it never releases the renderer process.**

One suspended tab still costs one process. macOS exhausts process-table headroom long before it
exhausts bytes, which is precisely why "out of application memory" appears with no large application
to blame. **The binding constraint was tab count, not tab weight.** The only action that reduces
process count is archiving to disk and closing — which means the real engineering problem is doing
that *without losing anything*.

That reframing is what the whole toolkit is built around.

---

## Why this is not a one-liner

Reading open tabs from a browser is a two-line AppleScript. Doing it correctly turned out to involve
five separate hazards, each of which silently produces wrong output rather than an error.

### 1. Suspender extensions replace your URL with theirs

A suspender does not unload a page — it *navigates the tab* to its own extension page and stashes
the real URL in a query parameter:

```
chrome-extension://<id>/suspended.html#ttl=Raycast%20Manual&pos=0&uri=https%3A%2F%2Fmanual.raycast.com%2Fai
```

Export naively and the entire archive is worthless: hundreds of tabs pointing at the same extension
page, with the destinations you cared about buried inside percent-encoding.

Worse, **two suspenders installed at once nest**. The session contained Great Suspender URLs wrapped
inside Workona URLs. Unwrapping once yields another wrapper. `resolve_target()` unwraps recursively
with cycle detection.

### 2. A class name shadowed a string, and every URL vanished silently

This is the one worth an interview conversation.

The bulk read built its output with a tab separator. AppleScript:

```applescript
tell application "Google Chrome"
    set tb to tab        -- ...
    set lf to linefeed   -- ...
```

Inside a `tell application "Google Chrome"` block, **`tab` resolves to Chrome's own `tab` class**,
not the tab character. AppleScript coerced the class reference to an empty string. Nothing raised.

The result was that every row was emitted as `title||url` with no separator, so the parser read
**879 tabs with 0 URLs**. Total data loss, zero error messages, and it even *looked* plausible,
because the failure surfaced only as an empty domain column. Individual per-tab reads behaved
correctly, so casual spot-checks passed.

Fixed by using explicit character ids (`ASCII character 9`). Guarded by regression tests asserting
the script contains no `set tb to tab` and does contain explicit character ids.

**The transferable lesson:** the dangerous bugs were not the ones that threw. They were the ones that
returned something plausible. Two of the three significant defects in this project were of exactly
that shape.

### 3. Generated JavaScript is one backslash from silently dead

The first restore board rendered perfectly and was completely inert — every button did nothing.

A single stray backslash inside a generated JS string literal made the whole `<script>` block a
syntax error. The page still displayed, so it looked finished.

Two defences: generate JS without backslashes (newlines via `String.fromCharCode(10)`), and gate
every build on `node --check` so a broken script cannot be written to disk at all.

### 4. Suspension state changes the memory arithmetic

Measuring the same session before and after tells two different stories:

| | Loaded live over days | Restored already-suspended |
|:---|:---|:---|
| Processes per tab | **0.83** | **0.026** |
| Chrome memory, 879 tabs | 13.7 GB | 3.4 GB |

The original session held ~1,263 renderers. A session restored into *suspended* tabs holds almost
none — Chrome does not spawn a renderer for a tab that never went live.

**So the cost is not tab count either, in the end. It is whether pages ever went live in that
process.** That is a sharper and more actionable finding than the first conclusion, and it was only
visible by measuring the second profile. It is also why the toolkit reopens tabs in paced batches
and refuses to open everything at once.

### 5. Redaction that missed the obvious place

The shareable export replaced configured hosts in link URLs. A leak check passed.

It was still leaking. A suspended tab's **title** is `<page title> · <full URL>` — the host was
spelled out in plain text next to the sanitised link, on 136 lines. Redaction had to cover titles,
and a test now asserts it.

---

## Engineering approach

**Archive first, always.** `close_duplicates.py` writes a full markdown archive, a plain URL list,
and a close manifest, `fsync`'d, *before* closing a single tab. It then verifies afterwards that
**zero unique URLs were lost**, and aborts if the close volume exceeds a configurable fraction of the
session. Dry-run is the default; `--apply` is required.

**Rules live in config, not code.** Wrapper detectors, carrier parameters, dedupe rules, redaction
hosts, pacing and safety limits are all TOML. This keeps site-specific knowledge out of the source
and means the repository ships no list of the sites anyone browses. A missing or malformed config is
never fatal; an explicitly requested one that is absent fails loudly.

**Read-only by default.** Only two functions mutate state (`close_tabs`, `open_urls`) and both are
explicit. Auditing a stressed browser must not risk making it worse.

**Tests encode the non-obvious failures.** 59 tests, and the important ones document *why* they
exist — the shadowed-class separator bug, title-level redaction, nested unwrapping, and the property
that the toolkit runs correctly with no config file at all.

---

## Stack

| Area | Used |
|:---|:---|
| Language | Python 3.11 (stdlib only — no dependencies) |
| OS integration | AppleScript via `osascript`, driven from Python |
| Process/memory instrumentation | `ps`, `sysctl vm.swapusage`, `memory_pressure` |
| Data handling | `urllib.parse`, `dataclasses`, `collections.Counter` |
| Config | TOML (`tomllib`, with a strict stdlib fallback parser) |
| Frontend artifact | Self-contained HTML + vanilla JS, no framework, no network |
| Build validation | `node --check` as a hard gate on generated JS |
| Testing | `unittest`, 59 tests |
| Tooling | `make`, `git`, GitHub CLI |

No third-party packages. The HTML board is a single self-contained file with no external requests,
so it works offline and can be archived indefinitely.

---

## Numbers

| Metric | Before | After |
|:---|:---|:---|
| Open tabs | 1,666 | 1,119 |
| Windows | 27 | 27 |
| Redundant duplicate tabs | 303 | 0 |
| Chrome processes | 1,263 | 2 |
| Chrome memory | 13.7 GB | 17 MB |
| Swap used | 16,662 MB | 1,741 MB |
| System memory free | 22% | 61% |
| **Unique URLs lost** | — | **0** |

A later and separate profile, restored from the archive, was audited with the same toolkit: 879 tabs,
147 redundant, 24 windows. Applying the dedup pass closed 182 tabs, left 740, and lost 0 unique URLs.

---

## What I would say in an interview

* **I falsified my own hypothesis.** The obvious explanation (heavy tabs) was wrong, and I only found
  the real one (process count) by instrumenting `ps` by category instead of trusting the story.
* **I treat silent wrongness as the primary risk.** Two of the three serious defects returned
  plausible output with no error. The countermeasure is invariants that fail loudly — `node --check`,
  post-run lost-URL verification, and tests named after the bug they prevent.
* **I build for reversal.** Archive-then-act, dry-run defaults, and a verification step that proves
  nothing was lost.
* **I separate policy from mechanism.** Everything site-specific is configuration, which is both good
  engineering and the reason this repository can be public at all.

---

## Reproducing

```bash
git clone <this repo> && cd tab-forensics
python3 -m unittest discover -s tests -v     # 59 tests, no dependencies
python3 scripts/session_audit.py             # read-only; changes nothing
python3 scripts/close_duplicates.py          # dry run by default
```

All fixtures under `data/sample/` are synthetic. Real sessions are never committed.
