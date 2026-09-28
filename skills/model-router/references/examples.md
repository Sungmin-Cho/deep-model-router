# Worked routing decisions

Every output below is real — produced by `scripts/route_task.py` against
`config/model-routing.yaml`, not written by hand. Registry keys replace
model ids in the pasted stdout (and in any command that had to name one),
because concrete identifiers live in the registry and nowhere else; resolve
them there.

`SKILL_DIR` is the skill's base directory (the directory containing
`SKILL.md`); commands below are written against it. Assign it once before
the first command below, substituting the real absolute path announced when
the skill loaded:

```bash
SKILL_DIR=<skill-base-directory announced when the skill loads>
```

If you change the policy and these stop matching, the examples are wrong, not
the policy. Regenerate rather than edit by hand.

## Contents

- [Hard but isolated](#hard-but-isolated)
- [The cheap path](#the-cheap-path)
- [Small task, critical domain](#small-task-critical-domain) ← regression case
- [Debugging with an unknown root cause](#debugging-with-an-unknown-root-cause) ← regression case
- [Payments architecture](#payments-architecture) ← regression case
- [Score exactly 10](#score-exactly-10) ← regression case
- [Reasoning-centric investigation](#reasoning-centric-investigation)
- [Save-data migration](#save-data-migration)
- [After a failed attempt](#after-a-failed-attempt)
- [When a model is unreachable](#when-a-model-is-unreachable)
- [When the retry budget is spent](#when-the-retry-budget-is-spent)
- [What these cases are meant to teach](#what-these-cases-are-meant-to-teach)

### Host below the ask — auth debugging on a fast-tier host

```bash
$SKILL_DIR/scripts/route_task.py --class DEBUGGING \
    --complexity 2 --uncertainty 2 --blast-radius 2 --reversibility 1 \
    --flags auth_sensitive \
    --host-model claude-haiku-4-5-20251001 --host-effort HIGH
```

<!-- no-transcript -->

(`--host-model` and the echoed `declared.model` keep the concrete id — the
registry-key substitution rule above applies to seat bindings only.)

The result is `risk_score: 11`, `band CRITICAL`, with
`policy_ask {tier 1, effort MAX, raised_by [orchestrator_critical_u2,
orchestrator_blast_high]}`; `model_comparison below` and
`effort_comparison below (HIGH < MAX)` produce `upgrade_recommended`. The note
is `host seat below orchestrator ask: model tier 0 < 1; effort HIGH < MAX [...]`.
The route itself — worker, reviewers, and exit — is unchanged by the
declaration.

---

## Hard but isolated

**Task:** implement a new scheduling algorithm behind a feature flag in one
module. Nobody else calls it yet. `c3 u0 b0 r0` — algorithmically complex,
fully specified, no blast radius.

```
$SKILL_DIR/scripts/route_task.py --class IMPLEMENTATION \
    --complexity 3 --uncertainty 0 --blast-radius 0 --reversibility 0
```

```
risk_score:  3
risk_band:   LOW
exec_score:  9
exec_band:   NORMAL
overrides:   (none)
worker:      worker_balanced  ->  xai_frontier
effort:      MEDIUM  (native: medium)
review:
  band:            LOW
  reviewers:       (none — deterministic checks)
  required:        independent=False
  actual:          not_applicable
  checks:          tests, lint
cross_family_review: False
fallbacks:   (none)
confidence:  0.95
notes:
  - worker_balanced: xai write seat on claude_code requires dispatch_agent --seat-profile grok-maker-v1
  - effort cap: band LOW capped effort HIGH at MEDIUM
  - execution band NORMAL raised worker from worker_fast to worker_balanced

IMPLEMENTATION scored 3/18 (c=3 u=0 b=0 r=0) -> band LOW; execution 9/18 -> NORMAL. Worker worker_balanced at MEDIUM effort. Review band LOW: no model reviewer — deterministic checks (tests, lint) must pass before the work is accepted. No fallbacks applied.
```

Risk `3` is `LOW`, so the review is the deterministic checks — `tests` and
`lint` must pass before the work is accepted, and no model re-reads it — and
the class table's `HIGH` effort is capped at `MEDIUM` (1.17.0). Execution `9`
is `NORMAL`, so the worker rises from `worker_fast` to `worker_balanced` — the
route says so in `notes` (`execution band NORMAL raised worker …`). Before
1.13.0 this task went to the cheapest model and escalated only after failing.

---

## The cheap path

**Task:** rename a symbol across 12 files. `c0 u0 b0 r0`

```
python3 "$SKILL_DIR"/scripts/route_task.py --class MECHANICAL \
    --complexity 0 --uncertainty 0 --blast-radius 0 --reversibility 0
```

```
risk_score:  0
risk_band:   LOW
exec_score:  0
exec_band:   EASY
overrides:   (none)
worker:      worker_fast  ->  openai_worker_fast
effort:      LOW  (native: low)
review:
  band:            LOW
  reviewers:       (none — deterministic checks)
  required:        independent=False
  actual:          not_applicable
  checks:          tests, lint
cross_family_review: False
fallbacks:   (none)
confidence:  0.95

MECHANICAL scored 0/18 (c=0 u=0 b=0 r=0) -> band LOW; execution 0/18 -> EASY. Worker worker_fast at LOW effort. Review band LOW: no model reviewer — deterministic checks (tests, lint) must pass before the work is accepted. No fallbacks applied.
```

Twelve files sounds like a lot, and it routes to the cheapest model at the
lowest effort — correctly. The workload is large; the *task* is trivial.

Note `not_applicable` rather than `degraded`: a `LOW` band does not ask for
independence, so there is nothing to fail to enforce. Since 1.17.0 it seats no
model reviewer at all (`review.mode: deterministic_checks`): exit 0 means the
route is dispatchable, and the checks it names are the review the caller owes.

---

## Small task, critical domain

**Task:** add a scope check to an authorization path. `c1 u0 b1 r0`,
`auth_sensitive`

```
python3 "$SKILL_DIR"/scripts/route_task.py --class IMPLEMENTATION \
    --complexity 1 --uncertainty 0 --blast-radius 1 --reversibility 0 \
    --flags auth_sensitive --runtime grok
```

```
risk_score:  3
risk_band:   HIGH
exec_score:  3
exec_band:   EASY
overrides:   ['critical_domain']
worker:      worker_balanced  ->  xai_frontier
effort:      HIGH  (native: high)
review:
  band:            HIGH
  reviewers:       senior_engineer, reasoning_specialist
  models:          claude_senior, openai_reasoning
  effort:          HIGH
  required:        independent=True
  actual:          degraded
cross_family_review: True
fallbacks:   (none)
confidence:  0.95
notes:
  - band HIGH floored effort at HIGH

IMPLEMENTATION scored 3/18 (c=1 u=0 b=1 r=0) -> band HIGH; execution 3/18 -> EASY. Overrides applied: critical_domain. Critical-domain flags: auth_sensitive. Worker worker_balanced at HIGH effort. Review band HIGH: senior_engineer, reasoning_specialist, independence_required=True, review_independence=degraded. No fallbacks applied.
```

**The raw score is 3, which is `LOW`. The emitted band is `HIGH` with dual
independent review.**

Every dimension is honestly small — it really is a simple, reversible,
well-understood change — and it still must not ship on a single lightweight
review, because the consequence of being wrong in an auth path is not
proportional to the size of the diff.

`review_independence=degraded` here is not a failure; it is the honest default
— nobody established whether isolation is possible for this session, so the
router declines to say either way. `--isolation available` moves it to
`planned`; only distinct per-reviewer session ids supplied after dispatch move
it to `enforced`. A capability attestation is not evidence that the capability
was used.

The worker is `xai_frontier`. HIGH is at or below that model's ceiling, so
requested and effective effort stay equal.

`--runtime grok` is load-bearing here and in the next example for the
effort ceiling, not for write authorization. `IMPLEMENTATION` and
`DEBUGGING` are `write` classes. `claude_code.to_xai` now ships a maker
seat, so a Claude Code host also names `xai_frontier` for write work; the
Codex-hosted direction stays unverified. On the grok host that same seat
is native — the host session itself does the work — and it is the one
effort ceiling below `MAX` that these two examples exist to teach.

---

## Debugging with an unknown root cause

**Task:** users are intermittently logged out; nobody knows why.
`c2 u2 b1 r0`, `auth_sensitive`, `unknown_root_cause`

```
python3 "$SKILL_DIR"/scripts/route_task.py --class DEBUGGING \
    --complexity 2 --uncertainty 2 --blast-radius 1 --reversibility 0 \
    --flags auth_sensitive,unknown_root_cause --runtime grok
```

```
risk_score:  8
risk_band:   HIGH
exec_score:  10
exec_band:   NORMAL
overrides:   ['critical_domain', 'low_routing_confidence_raised_review_to_CRITICAL']
  already satisfied by another rule: ['critical_domain']
worker:      worker_balanced  ->  xai_frontier
effort:      MAX -> VERY_HIGH  (native: xhigh)
review:
  band:            CRITICAL
  reviewers:       senior_engineer, reasoning_specialist
  models:          claude_senior, openai_reasoning
  effort:          MAX
  required:        independent=True
  actual:          degraded
  checks:          security, edge_cases, rollback, test_adequacy, specification_compliance
  judge:           principal_architect -> claude_architect
cross_family_review: True
fallbacks:   (none)
confidence:  0.77
human:       CONFIRMATION REQUIRED
notes:
  - confirm/on_any_critical_review: a CRITICAL review cannot be accepted automatically

DEBUGGING scored 8/18 (c=2 u=2 b=1 r=0) -> band HIGH; execution 10/18 -> NORMAL. Overrides applied: critical_domain, low_routing_confidence_raised_review_to_CRITICAL. Overrides that fired but were already satisfied: critical_domain. Critical-domain flags: auth_sensitive. Worker worker_balanced at VERY_HIGH effort (MAX was requested; the model's ceiling is lower). Review band CRITICAL: senior_engineer, reasoning_specialist, independence_required=True, review_independence=degraded. Judge: principal_architect. Required checks: security, edge_cases, rollback, test_adequacy, specification_compliance. No fallbacks applied. Human control: a CRITICAL review cannot be accepted automatically. Requires human confirmation before proceeding.
```

**The policy asked for `MAX`.** `unknown_root_cause` maps there directly,
above the band's `HIGH` floor: when you do not know what is wrong, thinking
harder is the only lever that reliably helps.

**The worker receives `VERY_HIGH`.** `xai_frontier` has a ceiling one step
below `MAX`. `selected_effort` stays `MAX` — that is what was asked for —
and `selected_effort_effective` is `VERY_HIGH`, native `xhigh`. The worker
floor is `HIGH`, so the cap does not break a floor and `effort_below_floor`
does not fire. Exit status is 3 because the review is `CRITICAL`.

**Review is `CRITICAL`, one band above the risk band.** Confidence came out at
0.77, below the 0.80 threshold, so the review band was raised. The router is
saying: *I am not fully confident I classified this, so check it harder than my
own classification suggests.*

**And a judge is bound.** The judge follows the *review* band, not the risk
band — a review promoted to `CRITICAL` needs adjudication just as much as one
that scored there. An earlier version keyed the judge off the risk band and
produced a `CRITICAL` review with all five required checks and no adjudicator:
half a control, which is worse than none because it looks whole.

---

## Payments architecture

**Task:** design the payment-processing subsystem. `c3 u3 b3 r2`,
`financial_sensitive`

```
python3 "$SKILL_DIR"/scripts/route_task.py --class ARCHITECTURE \
    --complexity 3 --uncertainty 3 --blast-radius 3 --reversibility 2 \
    --flags financial_sensitive
```

```
risk_score:  17
risk_band:   CRITICAL
exec_score:  15
exec_band:   VERY_HARD
overrides:   ['critical_domain', 'critical_irreversible']
  already satisfied by another rule: ['critical_domain', 'critical_irreversible']
worker:      principal_architect  ->  claude_architect
effort:      MAX  (native: max)
review:
  band:            CRITICAL
  reviewers:       senior_engineer, reasoning_specialist
  models:          claude_senior, openai_reasoning
  effort:          MAX
  required:        independent=True
  actual:          degraded
  checks:          security, edge_cases, rollback, test_adequacy, specification_compliance
  judge:           worker_balanced -> openai_frontier
cross_family_review: True
fallbacks:   (none)
confidence:  0.75
human:       CONFIRMATION REQUIRED
notes:
  - jointly allocated eligible models across the review and judge seats
  - confirm/on_any_critical_review: a CRITICAL review cannot be accepted automatically

ARCHITECTURE scored 17/18 (c=3 u=3 b=3 r=2) -> band CRITICAL; execution 15/18 -> VERY_HARD. Overrides applied: critical_domain, critical_irreversible. Overrides that fired but were already satisfied: critical_domain, critical_irreversible. Critical-domain flags: financial_sensitive. Worker principal_architect at MAX effort. Review band CRITICAL: senior_engineer, reasoning_specialist, independence_required=True, review_independence=degraded. Judge: worker_balanced. Required checks: security, edge_cases, rollback, test_adequacy, specification_compliance. No fallbacks applied. Human control: a CRITICAL review cannot be accepted automatically. Requires human confirmation before proceeding.
```

`critical_irreversible` fired because a critical-domain flag met
`reversibility >= 2` — the worst case the policy knows how to describe:
expensive to get wrong, and you cannot simply undo it.

**The judge is unavailable.** The implementer already holds
`principal_architect`, the only seat at that capability tier. An adjudicator
must be a model no party holds and no weaker than any of them — including the
implementer. There is no such model, so a human settles disagreement. Binding
the architect as judge of its own work would be the implementer wearing a
second label.

---

## Score exactly 10

**Task:** anything whose dimensions sum to 10. `c2 u2 b2 r0`

Ran once per task class. Every class emitted `risk_score: 10` / `risk_band:
HIGH`. Representative output (`MECHANICAL`):

```
python3 "$SKILL_DIR"/scripts/route_task.py --class MECHANICAL \
    --complexity 2 --uncertainty 2 --blast-radius 2 --reversibility 0
```

```
risk_score:  10
risk_band:   HIGH
exec_score:  10
exec_band:   NORMAL
overrides:   (none)
worker:      worker_balanced  ->  xai_frontier
effort:      HIGH  (native: high)
review:
  band:            HIGH
  reviewers:       senior_engineer, reasoning_specialist
  models:          claude_senior, openai_reasoning
  effort:          HIGH
  required:        independent=True
  actual:          degraded
cross_family_review: True
fallbacks:   (none)
confidence:  0.87
notes:
  - worker_balanced: xai write seat on claude_code requires dispatch_agent --seat-profile grok-maker-v1
  - band HIGH floored effort at HIGH

MECHANICAL scored 10/18 (c=2 u=2 b=2 r=0) -> band HIGH; execution 10/18 -> NORMAL. Worker worker_balanced at HIGH effort. Review band HIGH: senior_engineer, reasoning_specialist, independence_required=True, review_independence=degraded. No fallbacks applied.
```

Workers differ by class, as the table says they must. The band does not:

| Class | Worker |
|---|---|
| `MECHANICAL` `IMPLEMENTATION` `DEBUGGING` `REFACTORING` `TESTING` `DOCUMENTATION` | `worker_balanced` |
| `ARCHITECTURE` `MIGRATION` `REVIEW` `OPERATIONS` | `senior_engineer` |
| `INVESTIGATION` | `reasoning_specialist` |

This looks like a non-example and is in the test suite for a reason. An earlier
policy compared `score >= 10` in one section and configured `high_risk_max: 10`
in another, so a score of exactly 10 banded differently depending on which
branch reached it. Boundary bugs of that kind stay invisible until the one task
that lands on the boundary goes wrong.

---

## Reasoning-centric investigation

**Task:** prove whether a lock-ordering change can deadlock.
`c2 u2 b2 r0`, `reasoning_centric=true`

```
python3 "$SKILL_DIR"/scripts/route_task.py --class INVESTIGATION \
    --complexity 2 --uncertainty 2 --blast-radius 2 --reversibility 0 \
    --reasoning-centric
```

```
risk_score:  10
risk_band:   HIGH
exec_score:  10
exec_band:   NORMAL
overrides:   (none)
worker:      reasoning_specialist  ->  openai_reasoning
effort:      HIGH  (native: high)
review:
  band:            HIGH
  reviewers:       senior_engineer, principal_architect
  models:          claude_senior, claude_architect
  effort:          HIGH
  required:        independent=True
  actual:          degraded
cross_family_review: False
fallbacks:   (none)
confidence:  0.87

INVESTIGATION scored 10/18 (c=2 u=2 b=2 r=0) -> band HIGH; execution 10/18 -> NORMAL. Worker reasoning_specialist at HIGH effort. Review band HIGH: senior_engineer, principal_architect, independence_required=True, review_independence=degraded. Reviewer slot substituted: reasoning_specialist -> principal_architect (would have shared a model with the implementer). No fallbacks applied. cross_family_review=false — reviewers share a family; weigh the second verdict accordingly.
```

Same score as the previous example, different worker. The dimensions do not
distinguish these two tasks — `reasoning_centric` does. Here the hard part is
establishing what is true, not writing code.

HIGH's configured reviewers are `senior_engineer` and `reasoning_specialist`.
The implementer already holds the second of those, so de-confliction
substitutes `principal_architect`. Both seated reviewers are then claude, and
`cross_family_review` is false. That is a recorded substitution, not a
fallback — the shipped roles changed, the models those roles would have
resolved to did not need replacing.

---

## Save-data migration

**Task:** migrate the user save-data format. `c3 u2 b3 r3`, `migration`,
`data_integrity_sensitive`

```
python3 "$SKILL_DIR"/scripts/route_task.py --class MIGRATION \
    --complexity 3 --uncertainty 2 --blast-radius 3 --reversibility 3 \
    --flags migration,data_integrity_sensitive
```

```
risk_score:  16
risk_band:   CRITICAL
exec_score:  13
exec_band:   HARD
overrides:   ['critical_domain', 'critical_irreversible', 'migration_data_integrity']
  already satisfied by another rule: ['critical_domain', 'critical_irreversible', 'migration_data_integrity']
worker:      principal_architect  ->  claude_architect
effort:      MAX  (native: max)
review:
  band:            CRITICAL
  reviewers:       senior_engineer, reasoning_specialist
  models:          claude_senior, openai_reasoning
  effort:          MAX
  required:        independent=True
  actual:          degraded
  checks:          security, edge_cases, rollback, test_adequacy, specification_compliance
  judge:           worker_balanced -> openai_frontier
cross_family_review: True
fallbacks:   (none)
confidence:  0.87
human:       CONFIRMATION REQUIRED
notes:
  - jointly allocated eligible models across the review and judge seats
  - confirm/on_any_critical_review: a CRITICAL review cannot be accepted automatically

MIGRATION scored 16/18 (c=3 u=2 b=3 r=3) -> band CRITICAL; execution 13/18 -> HARD. Overrides applied: critical_domain, critical_irreversible, migration_data_integrity. Overrides that fired but were already satisfied: critical_domain, critical_irreversible, migration_data_integrity. Critical-domain flags: data_integrity_sensitive. Worker principal_architect at MAX effort. Review band CRITICAL: senior_engineer, reasoning_specialist, independence_required=True, review_independence=degraded. Judge: worker_balanced. Required checks: security, edge_cases, rollback, test_adequacy, specification_compliance. No fallbacks applied. Human control: a CRITICAL review cannot be accepted automatically. Requires human confirmation before proceeding.
```

Three overrides fire independently and agree. Each encodes a different reason
this is dangerous, and any one alone would produce the right band.

`rollback` is a required check, not an optional finding. For a migration this
irreversible, "we have a rollback plan" is part of the deliverable.

The judge is unavailable for the same reason as the payments case: the
implementer already holds the architect seat.

---

## After a failed attempt

**Task:** an ordinary feature; the fast worker already failed once.
`c1 u1 b1 r0`, `prior_failures=1`

`--prior-models` takes that worker's concrete registry id (the identifier
lives in the config):

```
python3 "$SKILL_DIR"/scripts/route_task.py --class IMPLEMENTATION \
    --complexity 1 --uncertainty 1 --blast-radius 1 --reversibility 0 \
    --prior-failures 1 --prior-models <openai_worker_fast id>
```

```
risk_score:  5
risk_band:   MEDIUM
exec_score:  5
exec_band:   EASY
overrides:   (none)
worker:      worker_balanced  ->  xai_frontier
effort:      MEDIUM  (native: medium)
review:
  band:            MEDIUM
  reviewers:       worker_balanced_alt
  models:          claude_worker_balanced
  effort:          HIGH
  required:        independent=True
  actual:          degraded
cross_family_review: True
fallbacks:   (none)
excluded:    ['openai_worker_fast'] (already failed)
confidence:  0.9
notes:
  - escalated above capability tier 0
  - worker_balanced: xai write seat on claude_code requires dispatch_agent --seat-profile grok-maker-v1

IMPLEMENTATION scored 5/18 (c=1 u=1 b=1 r=0) -> band MEDIUM; execution 5/18 -> EASY. Worker worker_balanced at MEDIUM effort. Review band MEDIUM: worker_balanced_alt, independence_required=True, review_independence=degraded. No fallbacks applied. Excluded as already-failed: openai_worker_fast.
```

Without the failure this routes to `worker_fast`. With it, the router refuses
to hand the task back to the capability tier that already failed. The first
escalation is `worker_balanced`. On a Claude Code host that now ships the
xai maker, the seat is `xai_frontier`. On Codex it remains the claude
balanced model: that host direction is still unverified for write.

**The escalation requires the history.** `--prior-failures 1` on its own is
`RETRY_HISTORY_REQUIRED`:

```
python3 "$SKILL_DIR"/scripts/route_task.py --class IMPLEMENTATION \
    --complexity 1 --uncertainty 1 --blast-radius 1 --reversibility 0 \
    --prior-failures 1
```

```
risk_score:  5
risk_band:   MEDIUM
exec_score:  5
exec_band:   EASY
overrides:   (none)
TERMINAL:    RETRY_HISTORY_REQUIRED  — no executable bindings emitted
review (policy only — not dispatchable):
  band:            MEDIUM
  reviewers:       worker_balanced
  required:        independent=True
  actual:          degraded
cross_family_review: True
fallbacks:   (none)
confidence:  0.9
human:       CONFIRMATION REQUIRED
notes:
  - retry history required: 1 prior failure(s) but 0 concrete model id(s) supplied — pass --prior-models with one model id per failure

IMPLEMENTATION scored 5/18 (c=1 u=1 b=1 r=0) -> band MEDIUM; execution 5/18 -> EASY. TERMINAL: RETRY_HISTORY_REQUIRED — no executable bindings emitted; routing confidence 0.9 after 1 prior failure(s). Surface to a human with what was tried, what evidence accumulated, and the blocking uncertainty. Review band MEDIUM: worker_balanced, independence_required=True, review_independence=degraded. No fallbacks applied. Requires human confirmation before proceeding.
```

Five rounds of inferring which model a previous attempt ran produced five
different defects, so the router asks the one party that knows. Feed back the
`selected_model` of each failed attempt — it is already in the route you
dispatched from.

---

## When a model is unreachable

**Task:** a security-sensitive change with the OpenAI reasoning role
unavailable. `c2 u1 b2 r1`, `security_sensitive`,
`--unavailable reasoning_specialist`

```
python3 "$SKILL_DIR"/scripts/route_task.py --class IMPLEMENTATION \
    --complexity 2 --uncertainty 1 --blast-radius 2 --reversibility 1 \
    --flags security_sensitive --unavailable reasoning_specialist
```

```
risk_score:  9
risk_band:   HIGH
exec_score:  8
exec_band:   EASY
overrides:   ['critical_domain']
  already satisfied by another rule: ['critical_domain']
worker:      worker_balanced  ->  xai_frontier
effort:      HIGH  (native: high)
review:
  band:            HIGH
  reviewers:       senior_engineer, reasoning_specialist
  models:          claude_senior, openai_frontier
  effort:          HIGH
  required:        independent=True
  actual:          degraded
cross_family_review: True
fallbacks:   ['reasoning_specialist: openai_reasoning unavailable -> openai_frontier']
confidence:  0.89
notes:
  - worker_balanced: xai write seat on claude_code requires dispatch_agent --seat-profile grok-maker-v1

IMPLEMENTATION scored 9/18 (c=2 u=1 b=2 r=1) -> band HIGH; execution 8/18 -> EASY. Overrides applied: critical_domain. Overrides that fired but were already satisfied: critical_domain. Critical-domain flags: security_sensitive. Worker worker_balanced at HIGH effort. Review band HIGH: senior_engineer, reasoning_specialist, independence_required=True, review_independence=degraded. Fallbacks: reasoning_specialist: openai_reasoning unavailable -> openai_frontier.
```

The route still emits because a usable fallback remains.

`--unavailable reasoning_specialist` withholds that role's default reasoning
model. The role falls back to `openai_frontier`, and the route records that
substitution. The senior reviewer remains Claude and the reasoning reviewer
remains OpenAI, so `cross_family_review` is true. Isolation is still degraded
until the caller supplies evidence; distinct families alone do not prove it.

There are three runtimes and fifteen (role × runtime) fallback paths. A
fallback is recorded only when the emitted model differs. The rule exists
because, when there were two runtimes, five of the ten paths used to
substitute the same model back and record a downgrade that never happened —
a recorded degradation with no degradation, the most misleading state a
metric can be in.

---

## When the retry budget is spent

**Task:** anything, after four failed attempts. `prior_failures=4`

```
python3 "$SKILL_DIR"/scripts/route_task.py --class IMPLEMENTATION \
    --complexity 1 --uncertainty 1 --blast-radius 1 --reversibility 1 \
    --prior-failures 4 \
    --prior-models <openai_worker_fast id>,<openai_worker_fast id>,<openai_worker_fast id>,<openai_worker_fast id>
```

```
risk_score:  6
risk_band:   MEDIUM
exec_score:  5
exec_band:   EASY
overrides:   (none)
TERMINAL:    HUMAN_REQUIRED  — no executable bindings emitted
review (policy only — not dispatchable):
  band:            MEDIUM
  reviewers:       worker_balanced_alt
  required:        independent=True
  actual:          degraded
cross_family_review: True
fallbacks:   (none)
excluded:    ['openai_worker_fast'] (already failed)
confidence:  0.8
human:       CONFIRMATION REQUIRED
notes:
  - escalated above capability tier 0
  - worker_balanced: xai write seat on claude_code requires dispatch_agent --seat-profile grok-maker-v1
  - retry budget spent: 4 attempt(s) against a cap of 4 — stop retrying and surface what was tried to a human

IMPLEMENTATION scored 6/18 (c=1 u=1 b=1 r=1) -> band MEDIUM; execution 5/18 -> EASY. TERMINAL: HUMAN_REQUIRED — no executable bindings emitted; routing confidence 0.8 after 4 prior failure(s). Surface to a human with what was tried, what evidence accumulated, and the blocking uncertainty. Review band MEDIUM: worker_balanced_alt, independence_required=True, review_independence=degraded. No fallbacks applied. Excluded as already-failed: openai_worker_fast. Requires human confirmation before proceeding.
```

**No executable bindings are emitted at all, and the CLI exits nonzero.**

Nulling only `selected_model` was not enough: a consumer could still read the
reviewer models out of a route whose own rationale said it must not be
executed. A terminal result now carries the review *policy* — which band, which
roles — with every concrete binding withheld.

Exhausting the retry budget is a normal terminal state, not an error — but a
terminal state that still hands back a runnable model is not a stop, it is a
suggestion. The same applies below 0.60 routing confidence, which terminates as
`ESCALATE_ROUTING`.

The stop must carry three things to the human: what was tried, what evidence
accumulated, and what the blocking uncertainty is. The third is the valuable
one — someone picking up a stalled task wants to know where the wall is, not to
re-derive the attempt history.

---

## What these cases are meant to teach

**Hard but isolated** — difficulty moves the worker; it never moves the review.

**Size is not risk.** The rename touches 12 files and routes cheapest. The auth
scope check touches a handful of lines and routes to dual independent review.

**Overrides must be unconditional.** Three of these are cases an earlier policy
version got wrong, and all three failed the same way: a class-specific branch
returned before the flag check ran. Structure, not diligence, prevents that.

**Review depth is not the worker's business.** Because review is a function of
the band alone, no worker-selection path can weaken it — which is what makes
the invariants testable across every class and dimension combination rather
than on a few hand-picked examples.

**Say what you enforced, not what you asked for.** `independence_required` is
policy; `review_independence` is evidence. `selected_effort` is the ask;
`selected_effort_effective` is what runs. `fallbacks_applied` records only
real substitutions. `terminal` withholds the model rather than emitting one you
must not run. Every one of those pairs exists because collapsing it produced a
claim the system could not back.
