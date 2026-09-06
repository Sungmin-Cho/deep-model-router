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
