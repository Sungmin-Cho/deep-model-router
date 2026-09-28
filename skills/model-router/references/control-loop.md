# Control loop — escalation, retries, confidence, observability

## Escalation triggers

Escalate when any of these occurs:

1. The worker reports confidence below the configured threshold.
2. The worker cannot produce a stable implementation plan.
3. The same test fails after two **materially different** attempts.
4. The root cause remains unknown after reasonable investigation.
5. Requirements appear contradictory.
6. An architectural decision has multiple high-impact alternatives.
7. A critical-domain flag is detected.
8. A reviewer reports a `high` or `critical` correctness finding.
9. Independent reviewers disagree.
10. The worker requests scope beyond the original task.
11. The proposed change increases blast radius beyond the original estimate.
12. Required context exceeds the worker's reliable handling capacity.
13. A dispatched seat produced no result — its receipt ended `START_FAILED`,
    `TIMED_OUT`, `TERMINATION_UNCONFIRMED`, or `INVALID_OUTPUT`, or `FAILED` with no parseable verdict block. Silence is evidence about the
    *dispatch*; classify it (below) before treating it as evidence about the
    *model*.

Every one of these is **evidence**. None of them is "a stronger model exists and
I feel uneasy." That distinction is what keeps cost bounded: escalating on
availability rather than evidence means always escalating.

### Trigger 11 deserves its own paragraph

**Re-scoring mid-task is mandatory, not optional.** A task that begins at
`MEDIUM` and grows into auth-adjacent territory must be re-scored and re-routed
— including its review policy. The original classification was correct for the
task as understood at the time; it stops being correct the moment the task
changes shape.

This is the most commonly skipped rule in the whole policy, because by the time
scope has grown you are already deep in the work and re-routing feels like
losing progress. It isn't. Shipping an auth change through a `MEDIUM` review is
losing progress.

## Retry and loop limits

```yaml
same_model_same_effort:            1
same_model_higher_effort:          1
stronger_model:                    2
max_total_implementation_attempts: 4
max_review_rounds:                 3
max_judge_invocations:             1
require_new_evidence_on_same_tier: true    # "tier" = capability_tier of the
                                          # model that RAN
```

### The same-model retry (1.17.0)

The router reads `same_model_higher_effort` and
`require_new_evidence_on_same_tier` (design 2026-09-25 DD-B6, user decision
U-7). A capability failure is retried on the **same model at one effort
higher** instead of a stronger model when all of these hold — otherwise the
ladder climbs a tier as before:

1. The history is typed (`attempt_outcomes`), nothing else stops the route
   (budget, unconfirmed termination, unrecovered operational outcomes), no
   `implementer` is declared, and every capability failure is on ONE model.
2. That model has at most `same_model_higher_effort` capability failures — the
   integer is the budget: `2` retries it twice, the third failure climbs.
3. Its most recent capability failure declares `effort` (the conceptual level
   it actually ran at) and, while `require_new_evidence_on_same_tier` holds,
   `retry_evidence_sha256`: new evidence, different from every hash in the
   history (a repeat is exit 2).
4. The route without the history seats that model, and one level above the
   higher of every effort it failed at and the effort that route gives it
   exists and is within the model's ceiling. It is never clamped: a model that
   failed at its ceiling is not sent back at it.

The retry keeps the 0.05 failure penalty, lists the model as no longer
excluded, and says `same-model retry: <registry key> again at <effort>` in
`notes`. The fields are caller declarations like every attempt record — the
router reads no receipt — and are omitted from the request identity when
absent, so a 1.16 history still hashes as it did. Operational outcomes are
never its target: effort does not fix a timeout.

### Accounting for silent seats

Two questions the retry rules used to leave open, decided:

- **A re-dispatch consumes budget.** Re-running a `NO_RESPONSE` reviewer
  consumes one `max_review_rounds` round; re-running a silent worker
  consumes one implementation attempt. Silence is not free — unbounded
  re-dispatch is exactly the silent loop the terminal states exist to
  prevent.
- **What `--prior-models` records.** A seat counts as a failure *of the
  dispatched model id* only when its receipt proves the model ran and did
  not deliver: `TIMED_OUT` with `termination_confirmed: true`,
  `INVALID_OUTPUT`, or `FAILED`. `FAILED` enters the ladder with or without
  a parseable verdict block — the model ran and did not deliver either way,
  so a missing verdict is not an exemption from accounting. Environmental
  outcomes never enter the ladder: `START_FAILED` and permission stalls are
  transport problems — fix the command or the approval mode, or pass
  `--flags bridge_down` — and `TERMINATION_UNCONFIRMED` blocks retrying
  instead of recording anything: re-route with `--flags termination_unconfirmed`
  and the route holds for a human, because a
  possibly-live writer plus a retry is two writers on the same files.
  `CANCELLED` never enters the ladder — the orchestrator cancelled it,
  which is evidence about the orchestrator's schedule, not the model; the
  re-dispatch still consumes budget exactly like any other re-dispatch
  (previous bullet). That is also why `CANCELLED` is a `NO_RESPONSE` member
  (the seat did not review) but not one of trigger 13's causes above — it
  is never itself the thing being escalated. An attempt the orchestrator
  cancelled never enters `--prior-models` regardless of what state its
  receipt ends up in: the authority for what was cancelled is the
  orchestrator's own round log, not the receipt — `run`'s terminal write
  races the receipt against a `cancel` that landed in between (design doc
  DD-9), and a receipt that a late overwrite relabeled `FAILED` or
  `SUCCEEDED` must not be read back as evidence about the model.

Recording a hang as a model failure escalates the ladder for a reason that
was never about capability — the wrong model gets blamed and the wrong
model gets paid.

### Which model "ran"

The router does not work this out. It requires the caller to say.

Three readings were tried and each was wrong in a different way: what the role
resolves to *now* (the failure is excluded by then, so that is its replacement);
the role's nominal binding (if that model was withheld, the role fell back and
ran something else); and the candidate ladder (which missed one of the caller's
two withholding channels). Two further attempts to reconstruct the ladder from
`prior_failures` alone were wrong again — once by counting a promotion twice,
once by reading a partial history as complete.

The reason is structural, not a run of bad luck: `route()` is stateless while
this rule is historical, and availability can change between attempts, so no
amount of care recovers a fact the function cannot see. This policy already
conceded the same point one field over — `same_model_same_effort` and its
siblings are the *caller's* budgets, because one `route()` call cannot count
attempts. Reconstructing which models those uncounted attempts ran is the same
claim, and it does not become true by being disclosed.

So `--prior-failures N` requires `--prior-models` to carry **N concrete model
ids**, repeating one that legitimately failed more than once. A role alias does
not identify a model; a short list is not a history. Anything else is
`RETRY_HISTORY_REQUIRED`: terminal, no bindings, and a note naming what to
supply. The caller has the ids — every route this router emits contains
`selected_model`.

**A second attempt at the same tier must carry a changed hypothesis or new
evidence.** Without one, the attempt is not permitted and the router must
escalate instead.

The reason this rule is stated as a hard constraint rather than advice:
repeatedly asking the same model to try equivalent approaches is the dominant
cost-overrun mode in agent systems, and it does not feel like looping from the
inside. Each attempt looks like a fresh idea. The check is external and
mechanical on purpose — *what new information does this attempt have that the
last one didn't?* If the answer is "none", escalate.

**Exhausting the limits is a normal terminal state, not an error.** Stop and
surface the situation to a human with three things:

- what was tried,
- what evidence accumulated,
- what you believe the blocking uncertainty is.

That third item is the valuable one. A human picking up a stalled task wants to
know where the wall is, not to re-derive the attempt history.

Silent looping is the error. Silent stopping is nearly as bad.

## Routing confidence

Emit your own confidence in the **routing decision**, 0.0–1.0. This is separate
from the worker's confidence in its output. The emitted
`routing_confidence_kind: heuristic_policy_score` states what this number is —
a policy gate score, not a calibrated success probability.

| Confidence | Action |
|---|---|
| `>= 0.80` | Execute as routed |
| `0.60 – 0.79` | Execute, but raise the review band one level |
| `< 0.60` | Escalate the *routing decision itself* — re-classify at higher effort, or ask a human |

A band raised by the middle row stays raised even when the promoted plan then
resolves at `>= 0.80`: promoting reseats reviewers, and that can retire the
fallback whose penalty triggered the promotion. The route says so in a note
naming both numbers — the pre-promotion confidence the decision read, and the
promoted plan's confidence it reports.

Uncertainty is counted once (1.17.0, design 2026-09-25 DD-B2). It already
weighs 2 in the risk score; when that double weight alone lifted the band —
the band with uncertainty weighted once, overrides included, is lower — the
middle row's promotion is decided on the confidence **without** the
uncertainty penalty (`router.confidence.skip_uncertainty_penalty_when_band_raised`).
The reported `routing_confidence` and the `< 0.60` escalation keep the full
penalty, and every other signal (prior failures, unknown root cause,
fallbacks) still promotes. Where the execution cell offers a stronger worker,
the two plans are weighed with the promotion the penalty would have made, and
only the adopted plan is planned without it — counting uncertainty once never
costs the worker a tier.

Low routing confidence must never be silently ignored. Both the value and the
reason for it belong in the emitted rationale, because "the router wasn't sure"
is exactly the context a human needs when the route turns out wrong.

The scorer computes a conservative default: confidence drops with maximum
uncertainty, with repeated prior failures, with unknown root cause, and when
fallbacks were applied. Those are the conditions under which a confidently
wrong route costs the most.

## Human-in-the-loop

These situations stop or gate rather than proceeding:

| Situation | Why |
|---|---|
| Retry budget exhausted | Four attempts without success means the task is not what the classification said it was |
| Any `CRITICAL` review | The router cannot verify an isolation receipt's provenance, so it never treats one as proof; a human confirms |
| Independence could not be established | **Terminal.** Disclosure is not a control — a route whose reviewers cannot hold distinct models is one where the implementer reviews itself |
| No adjudicator could be seated | **Human confirmation.** The route is still dispatchable; what a human takes over is adjudicating a disagreement, should one arise |
| Routing confidence below 0.60 | The router does not trust its own classification, and classification errors propagate everywhere downstream |
| Local model state cannot be used | **Terminal** `MODEL_STATE_UNAVAILABLE` (exit 1, no model named; every key of "Every route emits" is present, null or `[]`). Either the state root itself fails admission — not a directory owned by this user with mode 0700, or a symlink — whether or not `committed/` exists (`state_reason: root_unadmitted`; the note names the check that failed and the `chmod 700 <root>` fix, and `model_sync.py status`/`repair` report it), or `committed/` exists but its pointer or generation fails admission, hashing or the schema (`state_reason: unreadable`; `model_sync.py repair` or deleting `committed/` fixes it). Seating anything while revocations are unknown could revive a revoked id, so `DEEP_MODEL_ROUTER_OVERLAY=off` does not bypass either. A `policy_pin` that cannot be reproduced ends the same way, with a `pin_*` reason (see Routing JSON inputs) |

## Routing JSON inputs

Both `--json` and `--request-json` reject duplicate object keys, nonfinite
numbers (including exponent overflow), malformed encodings, unpaired Unicode
surrogates, and excessive nesting as input errors (exit 2).

RouteRequestV1 requires integer `route_schema_version: 1` and a real boolean
`reasoning_centric` when supplied. Optional collections and objects retain
their null-as-omitted meaning; false, zero, and the wrong container type do
not mean omitted. Availability lists contain strings and `isolation` is
`available`, `unavailable`, or null. Nonempty isolation evidence still requires
the isolation key to be present; its existing null semantics are unchanged.

V1 `flags` accepts a string array or the existing comma-separated string
form. Legacy `--json` uses the Task contract, so its flags must remain an
array. Repeated prior model IDs represent repeated attempts and are retained.
An empty `allowed_families` remains an unsatisfiable policy; an empty declared
host seat remains invalid. Valid inputs keep the same policy and fingerprints.

`policy_pin` (RouteRequestV1, or `--policy-pin`) names the `policy_sha256` of
an earlier route; anything but 64 lowercase hex is exit 2, and it is never part
of `request_sha256` (the digest it selects already enters the fingerprint).
Equal to the current effective policy, it changes nothing. Otherwise the router
walks the committed generations' `parent_generation_sha256` chain (at most 256)
and recomputes each as current base + that generation's entries and history +
the **current** generation's revocations, before the request is validated; the
null parent at the chain's end is the bundled policy with no overlay (a route
taken before any `committed/` existed pinned it) under the same current
revocations. The first match routes with `model_overlay.status: pinned`. No match is the
`MODEL_STATE_UNAVAILABLE` terminal with one `state_reason`, decided in this
order: `pin_suppressed_by_off` (`DEEP_MODEL_ROUTER_OVERLAY=off` removed entries
the pin needs), `pin_revoked` (a later revocation — revert beats pin),
`pin_base_changed` (the bundled policy moved, e.g. a plugin update),
`pin_generation_missing`. A pin absorbs overlay replacements only; a route
given an explicit config cannot honour one and refuses it (exit 2).

## LOW review is the deterministic checks

Since 1.17.0 the LOW band seats no model reviewer (design 2026-09-25 DD-B4,
user decision U-6): `review.reviewers` is `[]`, `review.effort` is null,
`review.mode` is `deterministic_checks`, and `review.required_checks` names the
checks — today `tests` and `lint`. The router cannot see them run, so **exit 0
with a non-empty `required_checks` means the consumer owes those checks**:
dispatch the worker, run the checks, and accept the work only when they pass.
Exit 0 is "dispatchable", never "the checks passed". A repository that cannot
run them re-routes with `availability_snapshot.checks_available: false` (CLI
`--checks-unavailable`); the review then leaves LOW for MEDIUM and seats a
model. It is never replaced by the host's own judgement.

A LOW route also leaves LOW — raise only, once, before the confidence
promotion, which may still add its one band — on `review_disagreement` (to
MEDIUM), and on a `local_policy` floor the checks cannot meet: the lowest band
whose seat matrix supplies `minimum_reviewers` reviewers and
`minimum_provider_families` families (`minimum_reviewers: 2` → HIGH). No band
at all: `UNSATISFIABLE_LOCAL_POLICY`, as before. Each move is a
`band_overrides_applied` entry `low_band_<reason>_raised_review_to_<band>`.
`mode` and `required_checks` are read off the SETTLED band, so a route promoted
off LOW never advertises `deterministic_checks`.

deep-loop does not read `required_checks`: it dispatches `selected_model` and
verifies with its own checker, and it never dispatched the LOW reviewer seat
either, so for deep-loop this change is no change — and deep-loop does not
enforce the LOW checks (open item L-8).

## Quota readings

`availability_snapshot.family_quota: {"<family>": "ok" | "low" | "exhausted"}`
(CLI `--family-quota openai=low,xai=ok`) is the caller's reading of each
provider's remaining quota (design 2026-09-25 DD-B8). The router reads no
network and no user file for it; `model_sync.py quota` reports what the local
codex rollout records say, without running codex.

- `exhausted` withholds every model of the family from every seat, like
  `unavailable_models` with the reason quota (not echoed in that list — it is
  the caller's). A declared `implementer` is not unseated: it already ran.
- `low` moves only the **worker** seat, to the first same-tier model of another
  family when one exists — a binding choice with no confidence penalty, and no
  move at all without a same-tier seat. Review seats are unaffected. The
  execution cell is weighed without it, so a `low` reading never costs the
  worker a tier.
- `ok`, or no entry, changes nothing. Absent, the field leaves
  `request_sha256` unchanged.

A `quota_exhausted` attempt outcome still needs recovery evidence
(`OPERATIONAL_RECOVERY_REQUIRED`): the typed history records what happened to
one attempt; `family_quota` states the provider's standing.

## Declared implementer

RouteRequestV1 `implementer: {"model_id": "<registry id>"}` says this write
work has **already been done** by that model, so the route plans its review
rather than its dispatch (design 2026-09-25 DD-B1). Write classes only —
REVIEW names its source's author in `review_context`, and a read-only class
produces a judgement rather than work — anything else is exit 2. Any registry
id is accepted, history rows included.

The route keeps the worker role the policy would have seated (after the
execution cell) and puts the declared id in it: `selected_model` is the
implementer, `worker_seat_state: already_executed`, and `dispatch_seats` holds
only the reviewers and the judge. Every de-conflict, family comparison and
judge floor reads the declared id. `allowed_families`, `unavailable_*`,
`bridge_down` and `family_quota` bind the review seats only — the worker has
run. When the declared tier is below the worker the policy would have seated,
`implementer_below_worker_tier` gates the route (exit 3), and a
`production_hotfix` does not defer it: the review was sized for a stronger
worker than the one that ran. The declaration is caller input, exactly like
`review_context`; review independence rests on it. Omitted, it leaves
`request_sha256` unchanged.

## Typed attempt history

V1 optionally accepts `attempt_outcomes`, an ordered array for the executor
lineage being routed (not sibling reviewer/judge attempts). Each record has a
unique safe `attempt_id`, concrete registry/history `model_id`, `kind`, and
hex64 `evidence_sha256`; `recovery_sha256` is optional/null or a different hex64.
Since 1.17.0 a record may also carry `effort` (the level it ran at) and, on a
`capability_failure`, `retry_evidence_sha256` — see The same-model retry.
Do not combine it with nonempty legacy `prior_failures`.

Only `capability_failure` feeds the existing model exclusion, tier escalation,
and failure-confidence penalty. Operational kinds are `transport_failure`,
`launch_failure`, `resolution_failure`, `timeout`, `max_turns_partial`,
`no_artifact`, `invalid_output`, `authentication_failure`, `quota_exhausted`,
`publication_failure`,
`cancelled`, and `unknown`. They require recovery evidence before an ordinary
policy retry; unresolved records produce `OPERATIONAL_RECOVERY_REQUIRED` with
no executable bindings. A timeout here means termination was confirmed.
Never classify a live but buffered/silent process as a completed failure.

`termination_unconfirmed`, whether a legacy flag or typed record, always
prevents execution, including hotfixes and old notify-only settings. The old
`on_termination_unconfirmed` setting is removed; unconfirmed termination now
returns terminal exit 1 rather than an executable-after-confirmation exit 3.
A generic recovery hash cannot clear it. After
actual termination proof, the caller must update that attempt's observed
classification/evidence; duplicate IDs are refused.

Every record consumes the existing total attempt budget. `retry_count` reports
that total; `escalation_count` retains the capability-history count, not a count
of actually executed escalations. Typed history and a count summary are echoed
as declared input, including on terminal routes, and bound to request identity
before projection onto the legacy capability ladder. Omitted/null history keeps
legacy request identity at a fixed policy; the deliberate unconfirmed-termination
hardening changes that path and the updated policy digest changes fingerprints.

Classification and evidence hashes are caller declarations, not authenticated
receipt imports. Recovered operational failures use the current task/availability
policy; they do not pin the previously used model or add a capability floor.
Per-effort, review-round, continuation execution and backoff remain controller
responsibilities. Retained partial artifacts belong in the recovery evidence.


## Observability

An explicitly declared `review_context` binds an existing artifact's target hash
and source-author exclusions to a read-only REVIEW request. It is echoed only
when supplied, including on terminal routes, and is caller input rather than
an executable model binding. See `review-policy.md` for its contract.

Every route emits (this file is the only inventory; `SKILL.md` Step 5
summarises it):

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
execution_score:  execution_band:   # the second axis (DD-1); selects the
                               # worker, never the review
band_overrides_redundant: []   # fired, but another rule had already got there
route_path:                    # null, or "disagreement"
terminal:                      # null, or one of the terminal states in the
                               # table above
selected_role:  selected_model:  selected_effort:  selected_effort_effective:
selected_effort_native:
worker_seat_state:             # to_dispatch, or already_executed when the
                               # request declared an `implementer`
implementer_declared:  implementer_source:   # false/null, or true and
                               # caller_declared (the router does not
                               # authenticate the declaration)
review:
  band:  reviewers: []  reviewer_models: []  effort:   # effort null when the
                               # band seats no model reviewer (LOW)
  mode:                        # model_review, or deterministic_checks when
                               # the settled band seats no model reviewer
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
worker_seat:                   # kind + source + write_capable_families
host_seat_advisory:            # declared + policy_ask{tier,effort,raised_by} + comparisons + advisory
requires_human_confirmation:
human_confirmation_deferred:   # a production hotfix: dispatch now, confirm after
human_control_causes: []       # which human_in_the_loop controls fired, by
                               # cause code — the machine-checkable half of the
                               # reason strings in the rationale
notes: []                      # every promotion, floor, compensation and
                               # policy decision the route actually made
rationale:   # names the band, the triggering flags, and every fallback
model_overlay:                 # null without committed local model state; else
                               # {status (applied | partial | noop | pinned |
                               # unavailable), base_policy_sha256,
                               # generation_sha256, applied (registry keys),
                               # noop and rejected ([{key, reason}]),
                               # history_ids_synthesized, blocked_ids (a
                               # count), state_reason (unreadable,
                               # root_unadmitted or a pin_* reason)}. Keys and counts only,
                               # never a model id. Not a RouteObservationV1
                               # decision key
```

### Fields the router cannot know

`review_count` and `final_success` were listed above until 2026-08-15 with
no producer: `route_task.py` runs before any dispatch, so it can know
neither. They belong to the **execution receipt** written per dispatched
seat by `scripts/dispatch_agent.py`, which owns the time axis after launch:

```yaml
execution_receipt:            # one per attempt — see references/adapters.md,
  attempt_id:                 # "Dispatch contract", for the full schema
  result.state:  RUNNING | SUCCEEDED | FAILED | TIMED_OUT | CANCELLED |
                 START_FAILED | TERMINATION_UNCONFIRMED | INVALID_OUTPUT
  result.exit_status:
  result.schema_valid:
  result.termination_confirmed:
```

`review_count` is the number of reviewer-seat receipts with `result.state:
SUCCEEDED` this round. `final_success` is a statement about the last
implementation receipt *and* its review round together — only the
orchestrator that watched both can assert it, and it lives in the
orchestrator's summary, not in any single route or receipt.

Recommended additions where the runtime exposes them: `input_tokens`,
`output_tokens`, `latency_ms`, `estimated_cost`.

Without per-route cost visibility, every tuning decision downstream is guesswork
— you cannot tell an escalation that paid for itself from one that didn't.

## Cost guardrail

Optimize expected **total task cost**: correctness, engineering quality,
latency, money, and human intervention combined. Not the unit price of any
single call.

```
avoid:   cheap model × endless retries
avoid:   frontier model for every trivial task
prefer:  cheap model → one evidence-based retry → stronger model
```

The first failure mode is the more expensive one and the harder to notice,
because each individual call looks cheap. Six failed attempts on the cheap
model plus the human time to untangle the result costs far more than routing
correctly once.
