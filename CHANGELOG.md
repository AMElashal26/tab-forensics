# Changelog

All notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses date-based versions.

## [0.1.0] - 2026-10-20

Initial release. Extracted from a real incident in which a Chrome session reached
1,666 tabs and exhausted system swap.

### Added

**Core library (`scripts/tabkit.py`)**

* `resolve_target()` — recursively unwraps tab-suspender URLs (Workona, The Great Suspender,
  Marvelous Suspender) back to real destinations, including the case where two suspenders nest
  inside one another. Cycle-safe via a visited set.
* `normalize_url()` — stable deduplication key. Lowercases scheme/host, collapses trailing slashes
  (including one before a query string), preserves query strings and fragments as significant,
  and returns `None` for information-free pages (`chrome://newtab/`, empty suspend placeholders).
* `domain_of()` — grouping key with explicit handling of `chrome://` and `file://` schemes.
* `read_session()` / `read_window()` — Chrome access via an `osascript` bridge, using two bulk
  property reads per window instead of a per-tab loop, read incrementally per window.
* `close_tabs()` — closes by descending index so earlier closures cannot shift later targets.
* `chrome_process_stats()` — classifies Chrome helper processes by type and memory, the measurement
  that revealed process count (not bytes) was the binding constraint.
* `swap_used_mb()`, `memory_free_percent()` — system pressure instrumentation.
* `write_text()` — durable write (`flush` + `fsync`) for safety-net files.
* `render_markdown()` / `parse_markdown_archive()` — archive format and a tolerant parser that
  skips malformed lines rather than losing the record.

**CLI tools**

* `session_audit.py` — read-only audit with a diagnosis section; can also export JSON, a markdown
  archive, a plain URL list, and an HTML board.
* `close_duplicates.py` — duplicate removal with archive-first safety gating, volume sanity checks,
  a dry-run default, and post-run `unique URLs lost` verification.
* `open_batch.py` — paced reopening with domain filtering and live process monitoring; dry-run by
  default.
* `build_board.py` — generates a dependency-free, offline, interactive HTML restore board.

**Tests (`tests/test_tabkit.py`)**

* 43 tests covering suspend unwrapping (including double nesting), normalization, domain
  extraction, statistics, survivor selection, archive round-trips, and board generation invariants.

### Fixed

* **Silent board failure.** Generated JavaScript containing a stray backslash made the entire
  `<script>` block fail to parse, leaving every button on the page inert while the page rendered
  correctly. Fixed by generating backslash-free JavaScript (newlines via `String.fromCharCode(10)`,
  attribute-based group selection) and by asserting the invariant.
* **`domain_of()` misclassifying chrome-internal URLs.** `chrome://extensions/` was reported as the
  domain `extensions` because the generic host regex matched the scheme payload. Browser-internal
  schemes are now checked first. Caught by `test_chrome_internal`.
* **Trailing slash before a query string.** `/a/?b=1` and `/a?b=1` produced different
  deduplication keys, under-reporting duplicates.

### Known limitations

* macOS only (AppleScript is the control channel).
* Incognito windows are not visible to the scripting bridge and cannot be archived.
* Unsaved text in form fields is not recoverable by a DOM-level read.
* URL-level deduplication only; distinct anchors into the same single-page app are treated as
  separate destinations.

### Security / privacy

* `.gitignore` excludes all real tab exports. Only synthetic fixtures under `data/sample/` are
  committed.
* No network access anywhere in the toolkit.
* No third-party dependencies; Python standard library only.
