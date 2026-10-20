# Interview notes: tab-forensics

Talking points for discussing this project. [`CASE-STUDY.md`](CASE-STUDY.md) is the narrative;
this file is the Q&A version, organised by the question likely to prompt it.

Numbers here are real and reproducible; all fixtures in the repo are synthetic.

---

## The 30-second version

> A browser session reached 1,666 tabs and the machine hit macOS's "out of application memory"
> dialog. I built a toolkit that archived every tab to disk, removed 547 surplus tabs, and cut swap
> from 16.6 GB to 1.7 GB — losing zero of 1,587 unique destinations. The interesting part was that
> the obvious explanation for the memory pressure turned out to be wrong.

---

## "Walk me through a hard debugging problem"

**Use the shadowed-class bug.** It is the best story in the project because the failure was silent.

**Situation.** I read tabs from Chrome in bulk for speed — fetching titles and URLs one at a time over
AppleScript is O(n) round trips and times out on a 200-tab window. So I built each row as
`title <TAB> url`.

**Problem.** The audit reported **879 tabs with 0 URLs**. No exception. The domain breakdown came back
as a single `(empty)` bucket.

**The subtlety.** My variable was `tb`, assigned from the tab character:

```applescript
tell application "Google Chrome"
    set tb to tab        -- did NOT do what I intended
    set lf to linefeed
```

Inside a `tell application "Google Chrome"` block, **`tab` resolves to Chrome's own `tab` class**,
not the tab character. AppleScript coerced the class reference to an empty string rather than
raising. Every row was emitted as `title||url` with no separator, so the parser found no separator
and stored the whole line as a title.

**Why spot-checks didn't catch it.** Reading a single tab by index worked fine — the bug was specific
to the bulk path. The output also *looked* fine at a glance: titles were present and plausible. The
tell was a domain column that was uniformly empty, which is statistically improbable for real data.

**Resolution.** Explicit character ids — `ASCII character 9` / `ASCII character 10`. Then three
regression tests: the generated script must contain explicit character ids, must not contain
`set tb to tab`, and the parser must treat a separator-less line as title-only.

**The lesson I'd draw out.** The dangerous bugs weren't the ones that threw. Two of the three serious
defects in this project returned plausible output with no error. So my countermeasure isn't "test
more" — it's *invariants that fail loudly*: `node --check` gating generated JS, a post-run
verification that zero unique URLs were lost, and tests named after the bug they prevent.

---

## "Tell me about a technical decision you made"

Two good ones; pick by what they're probing.

### Choosing archive-first over close-first

**The trade-off.** Closing tabs is the only action that reduces process count, so it's the goal. But
closing is destructive and irreversible, and the browser was *already* unstable — tabs could be
dropped or reordered at any moment.

**The decision.** Every close operation writes a full markdown archive, a plain URL list, and a close
manifest — `fsync`'d — *before* the first tab is closed. Then it re-reads the session and verifies
zero unique URLs were lost.

**What I gave up.** Disk space and runtime. The archives are 165 KB and 105 KB for a session of that
size — trivially cheap. The verification pass costs a second full session read.

**Why it was right.** It converts an irreversible operation into a reversible one. If the archive step
fails, nothing closes. If verification fails, I know immediately and still have the archive.

**The generalisable principle:** when an operation is destructive and the environment is unreliable,
establish the recovery artifact first and prove it before acting.

### Reading rules from config instead of code

**The problem.** The first version hard-coded which suspender extensions existed and which sites to
redact. That's site-specific knowledge in source — bad for a shared repo, and it meant a fresh
checkout carried a list of the sites one particular person had browsed.

**The decision.** Move wrapper detectors, carrier parameters, dedupe rules, redaction hosts, pacing
and safety limits into TOML. Nothing site-specific remains in the code.

**The property I insisted on.** A missing or malformed config is **never** fatal — it falls back to
defaults and prints a warning. But an *explicitly requested* config that's absent fails loudly with
exit code 2. Rationale: a tool that refuses to audit your browser because of a typo in an optional
file is worse than one that runs with slightly different rules. But silently ignoring a config you
deliberately passed is worse still.

**What it caught.** Four real bugs, which I'd raise unprompted:

* unwrapping only read the URL fragment, so a wrapper carrying its target in the *query string*
  silently failed to resolve;
* `redact_hosts` returned early when no hosts were configured, which skipped local-path redaction —
  leaking the account name in every `file://` URL;
* redaction covered link URLs but not **titles**, and a suspended tab's title is
  `Title · https://realsite/...`, so the host was in plain text on 136 lines while a URL-only leak
  check passed;
* making `www.` stripping default-on would have silently changed dedupe keys — tabs that previously
  both survived could start colliding. That's a behaviour change, not a bug fix, so I made it opt-in.

The last one is the same category as the separator bug, and I'd say so: correct-looking, quietly
different behaviour.

---

## "How do you handle risk when the operation is destructive?"

Walk the safety model — it's five concrete gates, not a vibe:

| Gate | Protection |
|:---|:---|
| 1 | Fresh snapshot read immediately before closing; never reused from an earlier run |
| 2 | Full archive + URL list written and `fsync`'d *before* the first close |
| 3 | Dedicated close manifest, so the decision is auditable and reversible |
| 4 | Volume sanity check — abort if the close exceeds a configurable fraction of the session |
| 5 | Descending index order, so closing tab 40 can't shift tab 12 |

Plus: **dry-run is the default** and `--apply` is required. Auditing a stressed browser must not risk
making it worse, so only two functions in the library mutate state and both are explicit.

---

## "Tell me about a time you were wrong"

Genuinely the strongest answer available here, because I falsified my own hypothesis.

**My first conclusion.** After measuring the original session I concluded: *"suspension shrinks a
renderer's contents but never releases its process — the bottleneck is tab count, not tab weight."*
That was well-supported: 98% of tabs were suspended, yet 1,263 processes were alive and the largest
single renderer was only 83 MB.

**Why it was incomplete.** Later I measured a second profile restored from the same archive — 879 tabs,
same scale — and processes-per-tab had collapsed from **0.83 to 0.026**. Chrome memory was 3.4 GB
instead of 13.7 GB.

**The correction.** Tab count wasn't the constraint either. The real variable is **whether pages ever
went live in that process.** A session restored into *suspended* tabs spawns almost no renderers,
because Chrome doesn't create a renderer for a tab that never loaded. The original profile had
accumulated tabs as live pages over days.

**What I changed as a result.** `open_batch.py` reopens in paced batches with live process monitoring,
and refuses to open everything at once — because opening all of them live is exactly what creates the
problem. That design decision came from the corrected model, not the original one.

**How I'd frame it.** The first answer was defensible and wrong in an important way, and I only found
out by measuring a second instance instead of stopping when the first result agreed with my
hypothesis. Confirmation felt good; the additional measurement was what actually taught me something.

---

## Technical depth questions

### "How do you count memory usage accurately?"

Not by looking at Activity Monitor's headline number. I split Chrome processes by category from `ps`:

* `--type=renderer` — one per live tab (the cost that matters)
* `browser-main`
* `gpu`
* `utility:network.mojom.NetworkService`
* `utility:storage.mojom.StorageService`

Summing renderers separately is what exposed the real finding: 12.6 GB of aggregate renderer memory
across 1,263 processes, with no single renderer above 83 MB. That pattern — many small costs, no big
one — points at per-process overhead rather than any heavy page, which is the diagnosis that led to
the right fix. A single aggregate number would have supported the wrong conclusion.

### "Why AppleScript instead of a headless browser or CDP?"

The browser was actively unresponsive and the UI was frozen. There was no opportunity to click
anything, and driving a session through a page that won't repaint is hopeless. AppleScript talks to
the application's scripting interface directly, so it works when the UI doesn't. It was also the only
route that needed no new install while the machine was under memory pressure.

The cost: AppleScript is slow and fragile for bulk data, which is exactly why the separator bug bit
me. I mitigated it with bulk property reads (`title of every tab`) rather than per-tab loops.

### "How do you deduplicate URLs correctly?"

Normalisation, and being deliberate about what must *not* be normalised:

* lowercase scheme and host; strip trailing slash
* collapse a trailing slash *before* a query string, so `/a/?b=1` and `/a?b=1` match
* **keep** query strings and fragments — they're usually significant (`#readme` differs from the page)
* drop genuinely meaningless pages (`chrome://newtab`) so they can never become a group's survivor
* prefer a live tab as survivor over a suspended one, then lower window index, so the workspace shape
  is disturbed as little as possible

Critical detail: **redaction must never touch the dedupe key.** Hiding the account segment of local
paths would make two distinct files collide and one would be silently discarded. Redaction belongs at
export time only. There's a test for that.

---

## "What would you do differently?"

Three honest answers:

1. **I'd measure before concluding.** I have a documented instance of stopping at the first confirming
   result. The second profile measurement is what produced the actual insight.
2. **I'd treat generated code as untrusted from the start.** The restore board rendered perfectly and
   was completely inert because one stray backslash made the whole `<script>` block a syntax error.
   The fix — generate backslash-free JS and gate every build on `node --check` — should have been
   there before the first board was ever written.
3. **I'd design the redaction boundary earlier.** Discovering that titles leak URLs came from a leak
   check that passed. A security-relevant property should have tests written alongside the feature,
   not after a near-miss.

---

## Questions I'd ask them

Signals I actually care about, worth asking:

* How do you decide when something is worth automating versus doing by hand?
* When a bug turns out to be silent rather than loud, what changes about how you test?
* Who owns the decision to pause a project that's technically working but solving the wrong problem?
* What does "done" mean here — merged, deployed, monitored, or adopted?

---

## Traps to avoid when telling this story

* **Don't lead with the tab count.** It sounds trivial. Lead with the falsified hypothesis, or with the
  silent-failure debugging.
* **Don't claim it was hard because the number was big.** The difficulty was correctness under
  uncertainty and an unstable environment.
* **Don't present the first conclusion as final.** The revision is more interesting than a clean story.
* **Don't mention the specific sites.** The technique is the point, and the redaction machinery exists
  precisely so the work can be shown without publishing where anyone went.
* **Don't oversell the scale.** ~2,700 lines of Python with 59 tests. Say that. It's a focused tool
  that solved a real problem, not a distributed system.
