---
name: model-router
description: Choose which model and reasoning-effort level should do a piece of software-engineering work, and how thoroughly that work must be reviewed, based on complexity, uncertainty, blast radius, reversibility, task type, and prior failures. Use this whenever you are about to delegate implementation, debugging, refactoring, architecture, migration, investigation, or review work to a subagent or another model — and especially before touching authentication, authorization, payments, database schemas, data migrations, concurrency, or anything else where a mistake is expensive or hard to undo. Also use it when a first attempt has failed and you are deciding whether to retry or escalate, when two reviewers disagree, or when someone asks which model to use for a task. Also use it when dispatching routed work to a background agent or bridge process, and when a dispatched worker or reviewer timed out, returned nothing, or cannot be confirmed dead.
---

# Model Router

You are deciding two things about a piece of work: **who should do it**, and
**how hard it should be checked**. Those are separate decisions, and keeping
them separate is the point of this skill.

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
decompose the work; they must not drive who does it.

**Flags** — detect all that apply:

```
critical-domain (force overrides):
  security_sensitive  auth_sensitive  financial_sensitive  data_integrity_sensitive

elevating (each one has an observable effect — see the table below):
  concurrency_sensitive  migration  public_api_change
  production_hotfix  unknown_root_cause  review_disagreement

context (inform decomposition and model choice, never the band):
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
| `--runtime claude_code\|codex\|grok` | the host, and the degraded binding that survives when that host's cross-provider bridge is down. Effort spelling comes from the selected model's family, not from this flag |
| `--prior-failures N` | after a failed attempt; `--prior-models` must then name **one concrete model id per failure** |
| `--unavailable <role>` / `--unavailable-models <id>` | a specific role or model does not resolve |
| `--flags bridge_down` | the whole cross-provider transport is unreachable — switches to the degraded single-provider binding |
| `--isolation available\|unavailable` / `--isolation-evidence <ids>` | whether isolation *can* be achieved this session, and one distinct session id per dispatched reviewer |
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
| `ESCALATE_ROUTING` | routing confidence fell below 0.60 — re-classify at higher effort or ask a human |
| `INDEPENDENCE_UNAVAILABLE` | the band requires independent review and it cannot be had — no distinct-model assignment exists, or the caller reported isolation unavailable |
| `RETRY_HISTORY_REQUIRED` | `--prior-failures N` without one concrete model id per failure. The router does not guess what ran |
| `SUPPLY_EXHAUSTED` | no usable model remains for a role the route needs — an operational shortage, not a bad request |
| `UNSATISFIABLE_LOCAL_POLICY` | `local_policy` cannot be met (empty `allowed_families`, empty intersection, or a floor the binding cannot seat) |

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

`review_depth_reduced` is non-empty when the seats that could be filled are
weaker than the band asks. The route stays executable and asks a human: the
router cannot restore the depth, and whether a thinner review is acceptable is a
judgement about the change. Review depth is never spent to buy a judge seat —
neither by demoting a reviewer below the band nor by re-seating one onto the
implementer's own model; the adjudicator is reported unavailable instead.
`band_floor_unsatisfiable` marks a shortage that will *not* clear by retrying,
and a compensation's bonus seat never upgrades the band's own independence
requirement. `effort_below_floor` fires when a seated model's ceiling is below
the effort a floor required; the route stays executable and asks a human.
`references/review-policy.md` has the seating rules.

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
2 SCORE      → risk_score → band
3 OVERRIDE   → band adjusted by flags        [unconditional]
4 WORKER     → role, by class + band         [never returns]
5 EFFORT     → conceptual effort level
6 REVIEW     → policy, by BAND ONLY          [independent of stage 4]
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

Floors override the table, never the reverse:

```
band HIGH                 → effort ≥ HIGH
band CRITICAL             → effort ≥ VERY_HIGH
any critical-domain flag  → effort ≥ HIGH
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

## Step 3 — Review, by band alone

Review depth does not depend on which worker you picked. That independence is
exactly what makes I1 and I2 checkable: no worker-selection branch can quietly
weaken the review.

| Band | Reviewers | Effort | Independent |
|---|---|---|---|
| `LOW` | worker_fast | `MEDIUM` | no |
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
decision itself — re-classify at higher effort or ask a human.

## Step 5 — Emit the route

Every route reports:

```yaml
task_class:  complexity:  uncertainty:  blast_radius:  reversibility:
route_schema_version:  router_plugin_version:  policy_sha256:
request_sha256:  decision_fingerprint:   # same request x policy x router
                               # version -> same fingerprint; carried into
                               # dispatch receipts and checked by
                               # verify-evidence --expect-fingerprint
effective_policy:  selected_capability_tier:  selected_families: []
local_policy_applied:
reasoning_centric:
risk_score:  risk_band:   band_overrides_applied: []   critical_flags: []
band_overrides_redundant: []   # fired, but another rule had already got there
route_path:                    # null, or "disagreement"
terminal:                      # null, or one of the terminal states in the
                               # table above
selected_role:  selected_model:  selected_effort:  selected_effort_effective:
selected_effort_native:
review:
  band:  reviewers: []  reviewer_models: []  effort:
  independence_required:       # what the band asks for
  review_independence:         # what was actually established
  independence_compromised:    # no distinct model was available for a seat
  judge_unavailable:           # no adjudicator at or above every party's tier
  review_depth_reduced: []     # [{reviewer, model, capability_tier, band_requires}]
  band_floor_unsatisfiable:    # the binding itself cannot supply that tier
  compensating_reviewers:      # extra seats added by a compensation
  self_review_avoided: []      # [{replaced, with, reason}]; `with` is always a
                               # role in `reviewers` above
  required_checks: []
  judge:  judge_model:         # null when judge_unavailable — a human adjudicates
cross_family_review: true | false
fallbacks_applied: []          # only recorded when the model actually changed
effort_ceiling_applied: []     # [{role, model, requested, capped_at,
                               # floor_broken, floor_requires}]; a seat whose
                               # model cannot receive the effort asked for
fallback_compensations_applied: []
unavailable_models: []
excluded_prior_failures: []    # models withheld because they already failed
escalation_count:  retry_count:
routing_confidence:  routing_confidence_kind:   # a heuristic gate score,
                               # not a calibrated success probability
requires_human_confirmation:
human_confirmation_deferred:   # a production hotfix: dispatch now, confirm after
human_control_causes: []       # which human_in_the_loop controls fired, by
                               # cause code — the machine-checkable half of the
                               # reason strings in the rationale
notes: []                      # every promotion, floor, compensation and
                               # policy decision the route actually made
rationale:   # names the band, the triggering flags, and every fallback
```

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

Routing-dispatched seats carry the decision with them: pass the route's
`decision_fingerprint` / `policy_sha256` to `dispatch_agent.py run`, then
check `verify-evidence --expect-fingerprint --expect-models` — the review
evidence chain, whose scope `references/adapters.md` ("Linkage scope") states.

## Before the first route in a session

Establish what you can actually invoke. A route naming a model you cannot call
is worse than no route. Check the runtime, which model families are reachable,
whether effort control exists, whether the cross-provider bridge works, and
whether subagent isolation is available.

A model that does not resolve is unavailable — fall back per
`references/adapters.md` and record it; never a hard failure.

## References

Read these when the situation calls for them; not needed for a routine route.

- **`references/routing-policy.md`** — dimensions, bands, overrides, worker and effort
  selection in full, with the reasoning behind each weight.
- **`references/model-profiles.md`** — the five roles, what each is for, what each must not
  be the sole authority on, and the current bindings.
- **`references/review-policy.md`** — review by band, independence mechanics per runtime,
  who may hold the judge seat, the reviewer output contract, disagreements.
- **`references/control-loop.md`** — escalation triggers, retry limits, which model "ran"
  and how that inference has gone wrong, routing confidence, observability.
- **`references/adapters.md`** — runtime differences, effort mapping, transports, the
  dispatch contract for background execution, and the fallback matrices. Read when a
  model is unavailable, before a background dispatch, or when you cross the bridge.
- **`references/examples.md`** — worked routing decisions, including the four cases an
  earlier version of this policy got wrong.
- **`references/observation.md`** — RouteObservationV1 (`validate_observation.py`).

Configuration lives in `config/model-routing.yaml`. Model identifiers appear
there and nowhere else — when models or prices change, update the registry and
recalibrate; the role system stays fixed. That separation is what keeps this
portable across runtimes and alive across model generations.
