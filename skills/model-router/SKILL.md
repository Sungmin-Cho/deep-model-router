---
name: model-router
description: Choose which model and reasoning-effort level should do a piece of software-engineering work, and how thoroughly that work must be reviewed, based on complexity, uncertainty, blast radius, reversibility, task type, and prior failures. Use this whenever you are about to delegate implementation, debugging, refactoring, architecture, migration, investigation, or review work to a subagent or another model — and especially before touching authentication, authorization, payments, database schemas, data migrations, concurrency, or anything else where a mistake is expensive or hard to undo. Also use it when a first attempt has failed and you are deciding whether to retry or escalate, when two reviewers disagree, or when someone asks which model to use for a task. Also use it when dispatching routed work to a background agent or bridge process, and when a dispatched worker or reviewer timed out, returned nothing, or cannot be confirmed dead. Also when the host session itself may be under- or over-provisioned.
---

# Model Router

You are deciding two things about a piece of work: **who should do it**, and
**how hard it should be checked**. Those are separate decisions, and keeping
them separate is the point of this skill.

Measured diagnostics and price freshness are documented in
`references/evaluation.md`. Their small fixed suite does not establish global
optimality; routing confidence is a policy heuristic, not a probability.

The cheap model does the volume. Escalation happens on evidence, not on hunches.
Review depth tracks risk, not the worker you happened to pick. And you never
claim a safety property you did not actually enforce.

## When this applies

Route when you are about to **delegate** work — to a subagent, to another
model, to a fresh session. Delegation already costs you a context window, so
choosing well is nearly free at that point.

Do not route work you are simply going to do inline in your own turn. There is
no choice to make there: you are the executor. A one-line fix, a file read, a
question you can answer — just do it.

Skip routing entirely for trivial single-file edits with no ambiguity. The
routing overhead would exceed the task.

## Step 1 — Classify

This is the judgment part, and it is yours. Nothing downstream can be better
than this step, so spend real attention here.

**Task class** — exactly one:

```
MECHANICAL   IMPLEMENTATION  DEBUGGING    REFACTORING
ARCHITECTURE INVESTIGATION   MIGRATION    REVIEW
TESTING      DOCUMENTATION   OPERATIONS
```

**Four dimensions, each 0–3:**

| | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| **complexity** | rename a symbol | add a simple endpoint | refactor state across modules | redesign a distributed model |
| **uncertainty** | implementation is known | minor ambiguity | several plausible approaches | requirements or root cause unclear |
| **blast_radius** | isolated | one subsystem | broad product impact | critical system / user / business |
| **reversibility** | trivial rollback | easy rollback | difficult rollback | effectively irreversible |

Naturally high blast radius: authentication, authorization, payments, database
schema, save-data format, production deploys, concurrency, security,
irreversible migrations, shared protocols, public APIs.

**Route on uncertainty and blast radius, not on size.** Generating 20 similar
components is a large workload with near-zero uncertainty — cheap model.
Changing 5 lines in the auth path is a tiny workload with critical blast radius
— strong model, dual review. Token counts and file counts may inform how you
decompose the work; they must not drive who does it. Complexity moves the
*worker* through the execution band; it never moves the review.

**Flags** — detect all that apply:

```
critical-domain (force overrides):
  security_sensitive  auth_sensitive  financial_sensitive  data_integrity_sensitive

elevating (each one has an observable effect — see the table below):
  concurrency_sensitive  migration  public_api_change
  production_hotfix  unknown_root_cause  review_disagreement

context (inform decomposition and model choice, never the band; three feed the execution axis):
  unfamiliar_codebase  cross_service_change  long_horizon
  large_context  latency_sensitive  tool_heavy

operational (state of the runtime, not of the task):
  bridge_down  termination_unconfirmed
```

What each elevating flag actually does — a flag with no consumer is a promise
the system does not keep, so this table is enforced by test:

| Flag | Effect |
|---|---|
| `production_hotfix` | band floor `HIGH` |
| `concurrency_sensitive` | band floor `MEDIUM` |
| `public_api_change` | band floor `MEDIUM` |
| `migration` | with `data_integrity_sensitive` → band `CRITICAL` |
| `unknown_root_cause` | effort `MAX`, worker promotion, confidence penalty |
| `review_disagreement` | routes to the disagreement path and binds a judge |

Two context flags also have a deterministic effect, on the model rather than
the band: `large_context` and `latency_sensitive` each bind `worker_balanced`
to `worker_balanced_alt`. The first is the caller's statement that the prompt
is at or past the primary's 200K whole-request price line; the second, that
first-escalation wall-clock latency outweighs output price. Numbers and the
latency evidence: `references/model-profiles.md`.

**`reasoning_centric`** — one boolean that decides between the two frontier
roles:

- `true` when the bottleneck is deciding **what is correct**: logical
  verification, edge-case enumeration, spec consistency, concurrency and state
  reasoning, discriminating between root-cause hypotheses.
- `false` when the bottleneck is producing **correct code**: multi-file edits,
  API surface work, refactoring mechanics, framework idiom, tool orchestration.

Default to `false` when genuinely torn. Code-centric routing is cheaper and
recovers more gracefully from a wrong guess.

## Step 2 — Compute the route

Hand your classification to the scorer. It is deterministic, so the band, the
overrides, and the review policy come out the same every time:

`SKILL_DIR` below is this skill's base directory — the path announced when
the skill loads, i.e. the directory containing this file. Every command in
this file and in `references/examples.md` is written against it, because a
subagent's working directory is the project root, not the skill root. Assign
it once before the first command below, substituting the real absolute path
announced when the skill loaded:

```bash
SKILL_DIR=<skill-base-directory announced when the skill loads>
```

```bash
python3 "$SKILL_DIR"/scripts/route_task.py --class DEBUGGING \
    --complexity 2 --uncertainty 3 --blast-radius 2 --reversibility 1 \
    --flags auth_sensitive,unknown_root_cause
```

Other inputs worth knowing:

| Flag | Use |
|---|---|
| `--format json` | machine-readable route |
| `--runtime claude_code\|codex\|grok` | the host and its degraded binding. Effort spelling uses the selected model's family map plus any per-model override |
| `--worker-seat write\|read_only` | override the class default for whether this route's worker needs a write-capable dispatch recipe |
| `--prior-failures N` | after a failed attempt; `--prior-models` must then name **one concrete model id per failure** |
| `--unavailable <role>` / `--unavailable-models <id>` | a specific role or model does not resolve |
| `--flags bridge_down` | the whole cross-provider transport is unreachable — switches to the degraded single-provider binding |
| `--isolation available\|unavailable` / `--isolation-evidence <ids>` | whether isolation *can* be achieved this session, and one **distinct** session id per dispatched reviewer |
| `--request-json <file>` | RouteRequestV1 file (`route_schema_version`, `local_policy`, `availability_snapshot`). Wins over `--json` and flags |

**Independence has five states, and only one of them is a claim.** A route is
computed before any reviewer runs, so nothing known at routing time can prove
isolation happened:

| State | Meaning |
|---|---|
| `not_applicable` | the band does not ask for independence |
| `degraded` | nobody established whether isolation is possible — the default |
| `unavailable` | positive evidence it *cannot* be achieved here |
| `planned` | attested achievable, not yet demonstrated |
| `enforced` | one distinct session id per reviewer was supplied afterwards |

`unavailable` and `degraded` are deliberately distinct: a confirmed gap and an
unchecked one call for different responses. **`enforced` does not unlock
anything** — an isolation receipt is a string the caller passed in, bound to no
real dispatch, so `CRITICAL` always asks a human and `enforced` reports the
claim without treating it as proof. Making the strongest control in the policy
openable by typing would be the exact failure this skill is about.

Exit status is the part of this contract a shell can act on, so every outcome
needing a person is nonzero: **0** dispatchable, **1** terminal, **2** invalid
input, **4** dispatchable with a confirmation owed afterwards (a production
hotfix — the review runs at full depth, the human is asked after the fix
ships), and `human_in_the_loop.human_gate_exit_status` (**3** by default) for a
route executable only after a human confirms — `requires_human_confirmation` is
a boolean in a JSON blob, and a caller reading success as authorisation walks
straight through it.

The terminal states are normal outcomes that need a human, not routes to
execute:

| Terminal | Meaning |
|---|---|
| `HUMAN_REQUIRED` | the retry budget is spent; no executable route is emitted |
| `ESCALATE_ROUTING` | routing confidence fell below 0.60 — re-classify at higher effort or ask a human; for a host orchestrator, use user `/effort` or delegate to a higher-effort seat |
| `INDEPENDENCE_UNAVAILABLE` | the band requires independent review and it cannot be had — no distinct-model assignment exists, or the caller reported isolation unavailable |
| `RETRY_HISTORY_REQUIRED` | `--prior-failures N` without one concrete model id per failure. The router does not guess what ran |
| `OPERATIONAL_RECOVERY_REQUIRED` | typed operational attempt history lacks recovery evidence; repair the execution/adapter issue before retrying |
| `TERMINATION_UNCONFIRMED` | a previous process may still write; confirm termination and reroute before another attempt |
| `SUPPLY_EXHAUSTED` | no usable model remains for a role the route needs — an operational shortage, not a bad request |
| `UNSATISFIABLE_LOCAL_POLICY` | `local_policy` cannot be met (empty `allowed_families`, empty intersection, or a floor the binding cannot seat) |
| `MODEL_STATE_UNAVAILABLE` | local model state is unreadable or its root fails admission, or a `policy_pin` cannot be reproduced — no model is named |

`judge_unavailable` is deliberately **not** terminal: independence failing means
the review cannot happen as specified, so nothing is safe to dispatch, whereas a
missing adjudicator leaves the review runnable and only hands a human the job of
settling a disagreement.

**Every strength comparison reads the resolved model's `capability_tier`, never
a role's position in the ladder** — judge-vs-party, reviewer-vs-band-floor,
substitute-vs-replaced, critical-domain floor, and retry escalation alike. Under
scarcity a role holds whatever model is left, so `worker_fast` can end up on the
frontier model and `worker_balanced` on a weaker one; ranking by role label
ranks the assignment backwards exactly when scarcity makes it matter.

Shortfall reporting — `review_depth_reduced`, `band_floor_unsatisfiable`, `effort_below_floor` — and the seating rules are in `references/review-policy.md`.

If you cannot run the script, compute it by hand from the tables below — the
script reads `config/model-routing.yaml`, and this file describes the same
policy, so the two must agree.

### The pipeline

Eight stages, and **no stage returns early**. That constraint is not stylistic:
an earlier version of this policy dispatched on task class with early returns
and checked critical-domain flags afterwards, so debugging an auth bug and
designing a payments architecture silently bypassed mandatory dual review. The
fix was to stop fusing "who does it" and "how it's reviewed" into one decision.

```
1 NORMALIZE  → class, 4 dimensions, flags, reasoning_centric
2 SCORE      → risk_score → band; execution_score → execution_band
3 OVERRIDE   → band adjusted by flags        [unconditional]
4 WORKER     → role, by class × execution band, never below class × risk band;
               yields if the review would suffer
5 EFFORT     → conceptual effort level
6 REVIEW     → policy, by BAND ONLY          [depth independent of stage 4]
7 RESOLVE    → aliases → available models, with fallbacks
8 EMIT       → route + rationale + confidence + metrics
```

Before acting on a route, check these five — the suite asserts them too, so if
one looks false something is genuinely broken:

- [ ] **I1** Every task reached review selection — no branch skipped it.
- [ ] **I2** A critical-domain flag put the review band at `HIGH` or above, for *every* task class.
- [ ] **I3** A critical-domain flag put the worker at `worker_balanced`'s capability tier or above.
- [ ] **I4** The route names only models confirmed available.
- [ ] **I5** The rationale names the band, the triggering flags, and any fallbacks.

### Score and bands

```
risk_score = complexity + 2×uncertainty + 2×blast_radius + reversibility     (0–18)
```

Uncertainty and blast radius carry double weight: complexity and reversibility
describe what the change *is*, the other two approximate what it might *cost*.

| Band | Score |
|---|---|
| `LOW` | 0 – 3 |
| `MEDIUM` | 4 – 7 |
| `HIGH` | 8 – 10 |
| `CRITICAL` | 11 – 18 |

Bands are contiguous and exhaustive, and every downstream rule is written in
bands. A raw-score comparison anywhere else is a bug — that ambiguity is what
made score 10 route differently depending on which branch you arrived through.

### Execution score and bands

```
execution_score = 3×complexity + 2×uncertainty
                + unfamiliar_codebase + tool_heavy + cross_service_change      (0–18)
```

| Band | Score |
|---|---|
| `EASY` | 0 – 8 |
| `NORMAL` | 9 – 11 |
| `HARD` | 12 – 14 |
| `VERY_HARD` | 15 – 18 |

This axis decides **who implements** and floors the worker's effort. It never
moves the risk band, the review, or a human control.

### Overrides

Applied **after** the band, **unconditionally**, for every task class:

```
any critical-domain flag                     → band = max(band, HIGH)
any critical-domain flag AND reversibility≥2 → band = CRITICAL
migration AND data_integrity_sensitive       → band = CRITICAL
production_hotfix                            → band = max(band, HIGH)
public_api_change                            → band = max(band, MEDIUM)
concurrency_sensitive                        → band = max(band, MEDIUM)
review_disagreement                          → disagreement path, regardless of band
```

Overrides only raise a band, never lower it.

### Worker by class and band

The class × risk-band table is the **floor** — the weakest worker that risk
tolerates:

| Class | LOW | MEDIUM | HIGH | CRITICAL |
|---|---|---|---|---|
| `MECHANICAL` | worker_fast | worker_fast | worker_balanced | senior_engineer |
| `DOCUMENTATION` | worker_fast | worker_fast | worker_balanced | worker_balanced |
| `TESTING` | worker_fast | worker_fast | worker_balanced | senior_engineer |
| `IMPLEMENTATION` | worker_fast | worker_fast | worker_balanced | ‡ |
| `REFACTORING` | worker_fast | worker_balanced | worker_balanced | ‡ |
| `DEBUGGING` | worker_fast | worker_fast | worker_balanced | ‡ |
| `INVESTIGATION` | worker_fast | worker_balanced | reasoning_specialist | reasoning_specialist |
| `MIGRATION` | worker_balanced | worker_balanced | senior_engineer | principal_architect † |
| `ARCHITECTURE` | worker_balanced | worker_balanced | senior_engineer | principal_architect |
| `REVIEW` | worker_fast | worker_balanced | senior_engineer | senior_engineer |
| `OPERATIONS` | worker_fast | worker_balanced | senior_engineer | senior_engineer |

**‡** `reasoning_specialist` if `reasoning_centric`, else `senior_engineer`.
**†** architecture phase only; implementation runs at worker_balanced / senior_engineer.

A second table, class × execution band (`references/routing-policy.md`,
"Execution difficulty"), names the worker difficulty asks for. The router
finishes the risk chain first (table, class promotions, critical floor, retry
ladder), then adopts the execution cell only if its resolved model is
**strictly** stronger by `capability_tier` and seating it leaves the settled
review and control contract no worse — otherwise it yields, and says so in
`notes` (`execution band … raised worker …` / `… yielded …`).

Then apply, in order:

```
ARCHITECTURE, uncertainty==3 or long_horizon  → principal_architect
DEBUGGING, unknown_root_cause & ≥2 failures   → at least the ‡ role
INVESTIGATION, unknown_root_cause             → at least worker_balanced
any critical-domain flag                      → at least worker_balanced
prior_failures ≥ 1                            → at least one tier above what failed
```

Role tiers, low to high: `worker_fast` → `worker_balanced` →
`senior_engineer` → `reasoning_specialist` → `principal_architect`.

Role profiles and what each is actually good at: `references/model-profiles.md`.

### Effort

```
MINIMAL < LOW < MEDIUM < HIGH < VERY_HIGH < MAX
```

| Work | Effort |
|---|---|
| formatting, rename, boilerplate | `LOW` |
| straightforward implementation | `MEDIUM` |
| multi-file feature, debugging, refactoring, architecture, standard review | `HIGH` |
| multi-system refactoring | `VERY_HIGH` |
| complex architecture, unknown root cause, adversarial review | `MAX` |

A `LOW`-risk task's table effort is capped at `MEDIUM` (`effort_caps`) unless it
has an unknown root cause or a capability failure on record; the floors below
still win.

Floors override the table, never the reverse:

```
band HIGH                 → effort ≥ HIGH
band CRITICAL             → effort ≥ VERY_HIGH
any critical-domain flag  → effort ≥ HIGH
execution band HARD       → effort ≥ HIGH
execution band VERY_HARD  → effort ≥ VERY_HIGH
```

`selected_effort` is what the policy asked for and never changes meaning.
`selected_effort_effective` is what the worker's model actually receives —
equal to the ask when the model has no ceiling, lower when it does. A cap that
breaks a floor (`effort_below_floor`) keeps the route executable and asks a
human. The native CLI token is the effective level looked up under the worker
model's family.

**As orchestrator, default to `worker_fast` at `HIGH`.** Classifying, building
the task graph, and detecting escalation conditions are well served by high
effort; `MAX` is for when the dependency graph is genuinely complex, five or
more subtasks interlock, requirements conflict, routing confidence is below
0.60, or failure would have `HIGH`+ blast radius.

### Existing-artifact reviews

For a read-only REVIEW of existing work, pass RouteRequestV1 `review_context`
with its target SHA-256 and known author model IDs or families. Do not infer
source authorship from your host model. See `references/review-policy.md`.
When this context is present, dispatch **only `dispatch_seats`**, once per entry.
`selected_*` identifies its lead reviewer; it is not an additional worker to
spawn. Keep peer contexts independent and collect one session ID per reviewer.
An empty dispatch list is not permission to invent a replacement route.

## Step 3 — Review, by band alone

Review depth does not depend on which worker you picked. That independence is
exactly what makes I1 and I2 checkable: no worker-selection branch can quietly
weaken the review.

| Band | Reviewers | Effort | Independent |
|---|---|---|---|
| `LOW` | none — deterministic checks (`tests`, `lint`) | — | no |
| `MEDIUM` | one stronger role, cross-family preferred | `HIGH` | yes |
| `HIGH` | senior_engineer + reasoning_specialist | `HIGH` | yes |
| `CRITICAL` | senior_engineer + reasoning_specialist | `MAX` | yes |

A reviewer's effective effort is its `effort_ceiling_applied` entry's
`capped_at` when one exists, otherwise `review.effort`. The CLI token is that
level looked up in `effort_map` under the reviewer model's family.

`CRITICAL` additionally requires every one of these to appear as an explicit
finding category — including when the answer is "checked, nothing found":

```
security   edge_cases   rollback   test_adequacy   specification_compliance
```

A `CRITICAL` review that silently omits one is invalid and must be re-run.

### Making independence real

Two reviews are independent only if reviewer B's input contains no token
derived from reviewer A's output. Stated as prose alone, this requirement is
violated by default — the natural implementation, asking one conversation for
two reviews in sequence, leaks the first into the second.

Each reviewer gets exactly: the diff, the task spec and acceptance criteria,
the relevant source, and the band's checklist. Not the other reviewer's
verdict, findings, confidence, or any paraphrase — and not even a hint that
another review is happening.

Mechanically: in Claude Code, dispatch each reviewer as a separate `Agent`
subagent, all in one message so they run concurrently and none can observe
another; in Codex, one non-interactive execution per reviewer with a fresh
session id, never reused. A cross-family reviewer reached over the bridge
(`codex exec` / `claude -p`) spawns a fresh process, so isolation holds by
construction. `references/review-policy.md` has the per-runtime detail.

When the execution band seats a frontier worker, the HIGH / CRITICAL pair loses
that model and `_deconflict` substitutes — `self_review_avoided` discloses it.
If the substitute would leave the review shallower than the risk-band worker
allowed, the execution cell yields instead.

If you cannot achieve real isolation, **do not claim it**. Run sequentially with
the second reviewer forming its verdict first, record `review_independence:
degraded`, and treat `PASS + PASS` on `CRITICAL` as `PASS_WITH_CHANGES` pending
human confirmation. Claiming independence you did not enforce is the most
damaging thing this skill can produce: it converts a control into an assurance
that is false.

### Reading verdicts

Reviewers return `verdict` (`PASS` / `PASS_WITH_CHANGES` / `FAIL`),
`confidence`, `findings`, `missing_tests`, `uncertainties`.

Reason about content, not the verdict token. A `PASS` carrying a
`critical`-severity finding is a contradiction — treat it as `FAIL` pending
clarification. A `PASS` with confidence below 0.5 is `PASS_WITH_CHANGES`.

Disagreement resolution and judge selection: `references/review-policy.md`.
The default judge is `principal_architect`; strongly code-local disputes may
use `senior_engineer` instead.

## Step 4 — Escalation and stopping

Escalate on **evidence**: a failed acceptance check, a stated low confidence,
an unstable plan, a reviewer finding. Not on a hunch, and not merely because a
stronger model exists.

A retry must reach a **strictly higher `capability_tier` than the model that
ran**, and never a weaker one than the same task with no failures. **The router
does not reconstruct what ran — it asks.** `--prior-failures N` requires
`--prior-models` to name N concrete model ids (repeat one that failed twice);
anything else is `RETRY_HISTORY_REQUIRED`. `route()` is stateless while this
rule is historical, so the party that knows — the caller, which dispatched them
— supplies it. Every route emits `selected_model`; keep it. The same-tier
budgets below are real, but the router will not route one: it reports
exhaustion and asks a human.

```
same_model_same_effort:            1
same_model_higher_effort:          1
stronger_model:                    2
max_total_implementation_attempts: 4
max_review_rounds:                 3
max_judge_invocations:             1
```

**Re-score mid-task when scope grows.** A task that starts `MEDIUM` and drifts
into auth-adjacent territory must be re-scored and re-routed, review policy
included. This is mandatory, not optional.

Exhausting the retry budget is a **normal terminal state**, not an error. Stop
and tell the human what was tried, what evidence accumulated, and what the
blocking uncertainty is. Silent looping is the error.

Emit your own routing confidence, 0.0–1.0: 0.80+ execute as routed; 0.60–0.79
execute but raise the review band one level; below 0.60 escalate the routing
decision itself — re-classify at higher effort or ask a human. A band raised at
0.60–0.79 stays raised even if the promoted plan then resolves at 0.80+, and
the route notes both numbers.

## Step 5 — Emit the route

Every route reports the two scores and bands (`risk_*`, `execution_*`), the
selected role / model / effort (requested, effective, native), the review
block (band, reviewers, independence policy *and* evidence, shortfalls,
judge), every fallback, compensation, ceiling and human control that fired,
`routing_confidence`, `decision_fingerprint`, and a rationale naming the
band, the flags and every fallback. The full annotated inventory — one owner,
checked against the emitted keys by test — is `references/control-loop.md`
("Observability").

Two pairs are deliberately not collapsed. **`independence_required` vs
`review_independence`:** the first is policy, the second is evidence, and
reporting policy as evidence is how a control becomes a false assurance.
- **`selected_model` vs `terminal`.** A terminal state names *no* concrete model
  anywhere except where the caller put one — not the worker, not the reviewers,
  not the judge, not inside `review_depth_reduced`. Enumerating the fields to
  null re-opens this every time the schema grows, so the property is what holds.
  A `self_review_avoided` record that names a reviewer who is not seated, or a
  `review_depth_reduced` entry whose `band_requires` is not the band's, is a
  contradiction rather than a note.

Optimize expected **total** task cost — correctness, engineering quality,
latency, money, and human intervention together. Not the unit price of one
call.

```
avoid:   cheap model × endless retries
avoid:   frontier model for every trivial task
prefer:  cheap model → one evidence-based retry → stronger model
```

## Dispatching the route

A route is a decision, not a result. A seat dispatched in the background
returns a handle; the result exists only as an execution receipt, and
`scripts/dispatch_agent.py` is what produces one — it owns the deadline,
the kill ladder, and termination confirmation. Read
`references/adapters.md` ("Dispatch contract") before the first background
dispatch of a session.

On Darwin, when promoting stored receipts to trusted completion evidence, use
`run --receipt-guard darwin-sandbox-v1` and
`verify-evidence --require-receipt-guard`. If the requested guard is unavailable,
keep protected authority unavailable; do not silently substitute an unguarded
launch. See `references/adapters.md` for the exact process-tree boundary and
external-writer limitations.

Two receipts are two different proofs, and neither substitutes for the
other: `--isolation-evidence` takes the `attempt_id`s of reviewer receipts
that reached `SUCCEEDED` — validate the set first with
`dispatch_agent.py verify-evidence` — and no evidence at all beats an
invented id. A seat whose receipt ended without a verdict did not review
(`references/review-policy.md`, "A seat that returns no verdict"). An
attempt whose process tree could not be confirmed dead blocks
write-capable retries: re-route with `--flags termination_unconfirmed` and
let the human gate hold it.

A grok seat also declares `--output-envelope`, `--session-evidence` and `--session-id`: a cancelled grok turn still exits 0 (`references/adapters.md`, "Grok seat profiles").

Host-seat advisory: emit `upgrade_recommended` once per decision. Recommend
`/model` only for `model_comparison: below`, using the lowest tier-satisfying
host-family model; downshift only after session-pattern hysteresis, once per
session. Full rules: `references/routing-policy.md`.

Routing-dispatched seats carry the decision with them: pass the route's
`decision_fingerprint` / `policy_sha256` to `dispatch_agent.py run`, then
check `verify-evidence --expect-fingerprint --expect-models` — the review
evidence chain, whose scope `references/adapters.md` ("Linkage scope") states.

## Before the first route in a session

Establish what you can actually invoke. A route naming a model you cannot call
is worse than no route. Check the runtime, which model families are reachable,
whether effort control exists, whether the cross-provider bridge works, and
whether subagent isolation is available.

Declare the host model id with `--host-model`; pass `--host-effort` only when
the environment supplied a reliable effort value. Conversion and utterance
detail are defined in `references/routing-policy.md`.

A model that does not resolve is unavailable — fall back per
`references/adapters.md` and record it; never a hard failure.

Run the offline model tick once: `python3 "$SKILL_DIR"/scripts/model_sync.py tick --detach`.

## References

Read these when needed; not for a routine route.

- **`references/routing-policy.md`** — dimensions, bands, overrides, and worker/effort selection.
- **`references/model-profiles.md`** — role purposes, authority limits, and bindings.
- **`references/review-policy.md`** — review bands, independence, judges, disagreements.
- **`references/control-loop.md`** — escalation, retries, routing confidence, and observability.
- **`references/adapters.md`** — runtime differences, effort mapping, dispatch, fallbacks.
- **`references/examples.md`** — worked routing decisions, including failures.
- **`references/observation.md`** — RouteObservationV1 and `validate_observation.py`.

Configuration lives in `config/model-routing.yaml`; model identifiers belong
there. Update the registry when models or prices change; roles stay portable.
