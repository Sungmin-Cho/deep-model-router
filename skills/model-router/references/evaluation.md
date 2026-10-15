# Measured model diagnostics

Run the opt-in collector on macOS with a trusted Codex installation:

```bash
python3 "$SKILL_DIR/scripts/evaluate_models.py" run \
  --output-dir /absolute/path/to/new-run --repetitions 2 --effort LOW
```

This makes 24 real calls by default: four OpenAI registry models, three fixed
bundles, two repetitions. `--models` accepts registry keys; `--deadline` bounds
each attempt (maximum 600 seconds). A new directory is required. The collector
protects its manifest, fixed oracle, source snapshot, receipts and measurements
inside the Darwin receipt guard. Prompts and response schemas are outside it.
Model tools are disabled; an observed tool item invalidates the measurement.

Exit 0 means every planned attempt yielded a usable grade, native usage and a
fresh price quote, **not** that every answer was correct. Missing measurements
return nonzero. Unconfirmed termination and publication failure preserve exits 5
and 8 and stop further calls. Partial artifacts are not a completed evaluation.

The [fixed cases](../evals/diagnostic-cases.json) cover outcome/control boundaries,
small code-review defects and constrained seat assignment. Answers are checked
by deterministic oracles, not by an LLM grading itself. The JSONL parser requires
one fresh completed turn, no fatal events or tools, and strict native usage
fields. Cache reads and writes are input subsets; reasoning tokens are already
included in output tokens. Zero/missing/contradictory usage is unavailable.
The [Codex event contract](https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs)
and the observed CLI stream define that interface.

Elapsed time is **collector-measured supervisor wall time**, including launcher,
guard, CLI, provider work and receipt publication; it excludes the local grader.
It is not provider-only inference latency. Medians include every attempted
outcome, including failures. Built-in model instructions, cache state and normal
machine load are not controlled as in a dedicated model benchmark.

Quotes use the registry's dated standard API prices, not subscription charges,
service-tier surcharges or tool fees. Missing axes, verification older than 30
days, future verification and expired promotional windows produce no quote.
This is a freshness bound, not automatic online price verification. CLI usage
is aggregate: above a context-price boundary it cannot establish the individual
request tier, so no quote is emitted. Direct per-request callers can explicitly
supply that stronger evidence to the quote helper. The whole-request boundary
math is separately unit tested. [Official pricing](https://developers.openai.com/api/docs/pricing)
is the reference; Sol's current promotion is documented on its
[dated registry source](../config/model-routing.yaml).

## 2026-09-06 diagnostic result

[Machine-readable results and hashes](../evals/diagnostic-results-2026-09-06.json)
record 24 native calls with Codex CLI 0.153.4 at LOW effort. Each configuration
answered 26 cases twice. Primary served identity is unavailable; labels below
identify requested model configurations.

| Requested configuration | Fully correct bundles | Correct cases | Median attempt wall time | Standard API equivalent, all 6 calls |
|---|---:|---:|---:|---:|
| Luna | 4/6 | 50/52 | 10.156 s | $0.020805 |
| Terra | 5/6 | 51/52 | 7.593 s | $0.179551 |
| Sol | 6/6 | 52/52 | 7.584 s | $0.429044 |
| Astra | 6/6 | 52/52 | 9.592 s | $0.993110 |

Luna twice, and Terra once, proposed two same-family reviewers when the case
required two families and the eligible supply made that impossible. The correct
answer was unavailable. This illustrates why deterministic constraint checks
remain necessary around model-generated assignments.

Inputs ranged from 15,205 to 17,787 reported tokens; cache-write usage was zero.
These calls did not exercise long-context billing or nonzero cache-write billing.
The fixture is public and narrow; repetitions are not independent new tasks.
It does not validate repository implementation quality, MAX-effort behavior,
other providers or globally optimal routing. Sol and Astra tied on this suite;
there is no evidence here to justify replacing the existing routing defaults.
`routing_confidence` remains a policy heuristic, not a calibrated probability.

Collection used the preserved earlier collector snapshot identified in the JSON.
After control/storage fixes, every protected receipt and stdout was independently
regraded and requoted without mismatch. The final collector's failure exits and
protection of all measurement files were separately exercised by a real guarded
local fixture. Raw files stay local because they contain machine paths and
session identifiers; the published result contains counts and source hashes.

## How a tier is measured

`capability_tier` is read by every strength comparison the router makes, so a
tier moves only on a measurement, and the measurement has to compare the
right things. This section is the method; the results are in
[model-profiles.md](model-profiles.md) and the verification ledger.

**Anchors are live rows.** A tier is held by the dispatchable rows the
registry puts there, because those are the models the router seats as peers,
and a seat's tier claim is that it is as strong as they are. A history row
(`dispatchable: false`) is a predecessor generation: it may be reported beside
a result, never used as its anchor. Neither may the row whose tier a
verdict is deciding: when one measurement decides several rows, each
verdict's anchors leave out that verdict's own row only, and every verdict
reads the registry as it stood before the measurement. When a tier holds
several live rows, the anchor is the lowest one measured — a seat level with
the weakest model the router already treats as that tier is inside the range
the scale already accepts.
Each live row added to a measurement can only lower that bar, and the anchor's
own tier may itself be inherited rather than measured, so a result names the
row that set the bar and says how that row's tier was established. A live row
that could not be dispatched for the measurement is named as unmeasured, not
skipped silently.

**Verdicts, written down before the first dispatch.** The suite is *valid*
when the anchor sits at least 0.10 below the mean of the tier above it — it
separates the tiers. On a valid suite a seat no more than 0.05 below the
anchor is *confirmed*, and a seat further below is *lowered*, by one tier. On
a suite that is not valid the verdict is *inconclusive*: the tier stays where
it was and is recorded as provisional. A row is *raised* one tier by the same
rule one tier up: the suite must separate the row's tier from the one above
without the row (the mean of the tier above at least 0.10 over the lowest
other live row of the row's tier), and the row must sit no more than 0.05
below the lowest live row of the tier above. Thresholds compare unrounded
values. The subjects, margins and verdicts are fixed before any dispatch; a rule
changed after some results were seen is not applied without saying so, and
the result reports what the preregistered rule would have said.

**What a measurement can move.** A tier is read on both sides of a route. On
the review side it sets each band's reviewer floor, which candidate a MEDIUM
review seats and whether a judge outranks the parties. On the worker side it
sets the retry ladder (a retry needs a strictly higher tier than what failed),
whether the execution band may raise the worker, the critical-domain floor on
the worker, the floor an investigation's promotion puts on the balanced role,
the compensation a weaker architect substitute receives, and the same-tier swap
a low quota makes. A read-only seat — no write recipe on any
host, or `write_verified: false` — only reviews and leads reading work (a
retry after it fails is reading work too), so a real-review replay measures
its job and may raise or lower it. For a seat that writes, a review replay
measures half the job: its tier moves only when a review replay and an
implementation replay point the same way. A review replay alone is recorded
as a measurement and moves nothing.

**The two replays.** A *review replay* re-runs a real review from this
repository's history against its frozen target with the original prompt. The
ground truth is the findings the author accepted; judges that are not
subjects grade each review blind, after reproducing the original reviews'
expected sets, and a judge that shares a subject's family is disclosed with
the result. Suites of seeded defects reached their ceiling twice and decided
nothing, which is why the replay uses real reviews. An *implementation replay*
takes accepted findings whose fix added a test: the subject gets the pre-fix
tree (no history) and the finding, works through its maker recipe in a
disposable workspace, and is graded by the fix's tests and the existing suite —
deterministic, no judge. It carries the same validity gate, because a fix task
stated precisely can reach the ceiling too.
