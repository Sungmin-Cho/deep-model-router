# Review policy

## Contents

- [Depth follows the band, and only the band](#depth-follows-the-band-and-only-the-band)
- [The four bands](#the-four-bands)
- [Reviewer independence](#reviewer-independence)
- [Enforcing isolation per runtime](#enforcing-isolation-per-runtime)
- [Reporting what you actually got](#reporting-what-you-actually-got)
- [Reviewer output contract](#reviewer-output-contract)
- [Reading verdicts](#reading-verdicts)
- [Disagreement resolution](#disagreement-resolution)

## Depth follows the band, and only the band

Review depth does not depend on which worker was selected.

This is worth stating flatly because the alternative is so tempting: "we used
the strong model, so a lighter review is fine." That reasoning inverts the
control. The whole reason to review is that the implementer might be wrong, and
a strong implementer being wrong is not less dangerous than a weak one being
wrong — it is more dangerous, because the output is more plausible.

Making review a pure function of the band also makes the safety invariants
checkable. If review depended on the worker, then every branch in worker
selection would be a branch that could weaken review, and no test could cover
them all.

## The four bands

### `LOW`

```yaml
reviewers: [worker_fast]
effort: MEDIUM
independent: false
```

Formatting, isolated UI, mechanical refactor, generated tests.

### `MEDIUM`

```yaml
candidates: [worker_balanced, senior_engineer, reasoning_specialist]
effort: HIGH
independent: true
```

Exactly one independent reviewer, from a different family where available, and
at least as strong as the implementer. Both of those are constants in
`select_review`, not settings: a `reviewer_count` key and a
`prefer_cross_family` key used to sit in this block and neither was read —
raising the count to 3 or clearing the preference produced byte-identical
routes. Asking for a second reviewer means asking for `HIGH`, and the
cross-family preference is the reason this band names candidates rather than
one fixed reviewer.

"At least as strong", not "stronger": the two frontier roles are peers, so once
the implementer is already at the top of the ladder the reviewer can only be a
sibling. `reasoning_specialist` reviewing `senior_engineer` work — and the
reverse — is the intended pairing, and it is the *family* difference doing the
work there, not a tier difference. The same applies when a `MEDIUM`-band task
gets promoted to `principal_architect` by the architecture rule: its reviewer
is a peer, not a superior.

Pick the first candidate that is both available and a different family from the
implementer:

| Implementer | Preferred reviewer |
|---|---|
| `worker_fast` | `worker_balanced` |
| `worker_balanced` | `reasoning_specialist` |
| `worker_balanced_alt` | `senior_engineer` |
| `senior_engineer` | `reasoning_specialist` |
| `reasoning_specialist` | `senior_engineer` |
| `principal_architect` | `reasoning_specialist` |

If no cross-family reviewer is available, use the strongest available
same-family reviewer and record `cross_family_review: false`. A same-family
review is worth having; it is just worth less, and the metric should say so.

### `HIGH`

```yaml
reviewers: [senior_engineer, reasoning_specialist]
effort: HIGH
independent: true      # must be structurally enforced
```

### `CRITICAL`

```yaml
reviewers: [senior_engineer, reasoning_specialist]
effort: MAX
independent: true
required_checks:
  - security
  - edge_cases
  - rollback
  - test_adequacy
  - specification_compliance
disagreement_judge: principal_architect
```

Every `required_check` must appear as an explicit finding category in the
reviewer's output, **including when the verdict is `PASS`** — recorded as
"checked, no finding". A silent omission is indistinguishable from a check that
never ran, so a `CRITICAL` review missing one is invalid and must be re-run.

## A live incident

`production_hotfix` raises the band, which is right: a bad change during an
incident compounds. What it must not do is make the response slower than the
incident. The four dimensions score what a change *is* (complexity,
reversibility) and what it might *cost* (uncertainty, blast radius); none of
them represents the cost of **delay**, so without this rule a hotfix and an
unhurried architecture change get identical handling.

So the review does not move — same band, same depth, same two independent
reviewers, same required checks — and the **human confirmation does**:

| | ordinary CRITICAL | `production_hotfix` |
|---|---|---|
| reviewers | 2, independent | 2, independent |
| required checks | all five | all five |
| human | before dispatch (exit 3) | after the fix ships (exit 4) |

`human_in_the_loop.on_production_hotfix` chooses between the two, because a team
that would rather wait is making a legitimate call and not a mistake.

Deferral is scoped to the band's own review gate. A review that cannot be
trusted still blocks: `independence_compromised` means the reviewers are the
implementer under another label, `review_depth_reduced` means there are fewer
of them than the band asks, and `effort_below_floor` means a seated model
cannot reach the effort a floor required. An incident does not make an
untrustworthy review acceptable. `judge_unavailable` deliberately does not
block deferral — an adjudicator is needed only if the two reviewers disagree,
which is an event after the review, which is where the deferred confirmation
already is.

## Reviewer independence

### Reviewing an existing artifact with declared authors

RouteRequestV1 accepts optional `review_context` on read-only `REVIEW` tasks:

```json
{
  "target_sha256": "<64 lowercase hex characters>",
  "author_model_ids": ["<model id from the registry>"],
  "author_families": []
}
```

Declare at least one model ID or family. Exact model IDs, including retired
history IDs, exclude those IDs only; `author_families` additionally excludes
whole provider families. All executable seats for this REVIEW task are filtered,
including its lead reviewer, peer reviewers and judge. The actual
host remains advisory and is never used to infer who wrote the source artifact.

The context is normalized and included in request identity and the decision
fingerprint. Changing the target or authors changes that identity. Omitted/null
context retains worker-plus-review semantics and the same request identity. Invalid context, write-seat
overrides and other task classes are rejected. Exclusion is an eligibility rule,
not a model outage; genuine outages of eligible replacements remain recorded.

This is a caller declaration. The router does not read the artifact to verify
its digest, authenticate authorship, or certify the served provider model.
The context is echoed as input even on terminal routes. With this explicit
context, the selected executor is the lead reviewer, included once in the
band's reviewer count. Use `dispatch_seats` as the canonical execution list;
do not dispatch `selected_model` again alongside that list. Each entry gives
`seat`, `role`, `model_id`, `effort`, and `effort_native`. Terminal routes return
an empty list. Isolation evidence still needs one distinct session per reviewer.
The lead receives the higher of its selected effort and the review effort.

For a deficient ordinary slate, or an explicit source review, the router searches
eligible fallback candidates jointly. Ordinary workers stay fixed; a source
review's lead can rise to the final review floor. Reviewers must meet
the band's tier and effort floors; a judge must be distinct and at least as
capable as every party. Candidate preference breaks ties after provider diversity.
Intentional seating is not an outage penalty. If no complete assignment exists,
reviewer shortages retain conservative gates. If reviewers are feasible but
a judge is not, the review slate is retained with a human adjudication gate. This search does not establish empirical model quality.

Two reviews are independent if and only if reviewer B's input contains no token
derived from reviewer A's output, transitively.

Each reviewer's input is exactly:

- the diff or change under review,
- the task specification and acceptance criteria,
- the relevant source context,
- the review checklist for the band.

And explicitly excludes: the other reviewer's verdict, findings, confidence, or
any paraphrase or summary of them — and any hint that another review is in
progress at all. Even knowing a second opinion exists changes how a reviewer
calibrates.

**Stated as prose alone, this requirement is violated by default.** The natural
implementation — asking one conversation for two reviews in sequence — leaks
the first review into the second's context. Independence has to be a property
of the mechanism, not an instruction.

The execution axis makes the substitution above more common: a `VERY_HARD`
task can seat `senior_engineer` or `reasoning_specialist` as the worker, so the
HIGH / CRITICAL pair loses that model and de-confliction substitutes. Under
the default binding the substitute is the architect tier and depth does not
move. Where it would — a degraded binding, scarcity — the execution cell
yields to the risk-band worker instead (`routing-policy.md`, "How the two
tables combine").

## Enforcing isolation per runtime

Each runtime has a native subagent for a same-family reviewer and CLI bridges
for the others.

| Runtime | Native | Bridged |
|---|---|---|
| Claude Code | `Agent` tool subagent | `codex exec` / `grok -p` |
| Codex | `multi_agent` subagent | `claude -p` / `grok -p` |
| grok | native `--agents` subagent | `claude -p` / `codex exec` |

**Claude Code:** dispatch each reviewer as a separate subagent via the `Agent`
tool, and issue both dispatches **in a single message** so they run
concurrently and neither can observe the other's result. Never include reviewer
A's returned text in reviewer B's prompt. Subagent context isolation is the
intended enforcement boundary — and the verification ledger records it as
`assumed`, not verified: it is the Agent tool's documented behaviour, never
probed here. That is a stronger position than the Codex and grok natives
below, which are not documented to isolate at all, but it is not proof. Where
the independence claim has to hold, dispatch the reviewers as bridged
processes instead; those are isolated by construction.

**Codex:** invoke each reviewer as a separate non-interactive execution with a
fresh session. Do not reuse a session identifier across reviewers, and do not
pipe reviewer A's stdout into reviewer B's prompt construction.

**grok:** default HIGH and CRITICAL reviewers are the claude and openai
frontier seats, so a grok-hosted dual review is two bridged processes by
construction. The native subagent is for same-family review; under `xai_only`
any band that requires independent review is terminal, so that path does not
arise as an executable dual review.

**The bridge is isolated by construction.** `codex exec`, `claude -p`, and
`grok -p` each spawn a fresh process with a fresh session, so a bridged
reviewer cannot see the other's context even in principle. That is a nice
property to have on the reviewer that matters most.

**Codex and grok native subagents carry an unverified assumption.** Codex
`multi_agent` is enabled, and grok exposes `--agents` / `--no-subagents`, but
whether either isolates context the way independent review requires has not
been empirically confirmed. Until it has, verify isolation on the first dual
review of a session, or run both reviewers as separate processes, which is
isolated for certain.

## Reporting what you actually got

`independence_required` is what the band asks for. `review_independence` is
what was established. Keeping them apart is the whole point — collapsing them
converts a safety control into a false assurance, and false assurance is worse
than no assurance because nobody goes looking for the problem.

### Shortfalls

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

| State | Meaning |
|---|---|
| `not_applicable` | the band does not require independence |
| `degraded` | nobody established whether isolation is possible |
| `unavailable` | positive evidence that it cannot be achieved here |
| `planned` | attested achievable, not yet demonstrated |
| `enforced` | one distinct session identifier per reviewer, supplied after dispatch |

Two distinctions in that table are load-bearing.

**`unavailable` is not `degraded`.** One is a confirmed gap, the other is an
unchecked assumption, and they call for different responses. The config's own
ledger records Codex native subagent isolation as flag-verified but
semantics-unverified — exactly the case that needs its own state rather than
being folded into "unknown".

**`planned` is not `enforced`.** A route is computed before any reviewer runs,
so nothing available at routing time can prove isolation happened. A caller's
attestation that isolation is *achievable* is a capability claim; distinct
per-reviewer session identifiers, supplied afterwards, are the closest thing to
evidence the interface has.

**But `enforced` is still only a report.** The router cannot verify a receipt's
provenance — it is a caller-supplied string, unbound to any actual dispatch. A
`CRITICAL` review therefore always requires human confirmation regardless of
what independence reports. Anything else would let the policy's strongest
control be opened by typing two words, which is precisely the false assurance
this section exists to prevent.

### Where the evidence id comes from

`--isolation-evidence` wants one id per reviewer seat **that returned a
verdict** — completion evidence and isolation evidence meet here. The
canonical id is the dispatch receipt's `attempt_id`
(`scripts/dispatch_agent.py`), validated before routing:

```bash
python3 "$SKILL_DIR"/scripts/dispatch_agent.py verify-evidence \
    --receipt-dir receipts/ --ids r1-a7f3,r2-b2c9 --expect-count 2 \
    --expect-fingerprint <route decision_fingerprint> \
    --expect-models <route review.reviewer_models, comma-separated>
```

The two `--expect-*` options are optional in the grammar and **not optional
for a routed review**: without them the check accepts any two completed review
receipts, including receipts from a different decision or different models.
Omit them only when the seats were not dispatched from a route.

Per transport, the underlying session identifier is recorded in the receipt:
`grok -p` takes a caller-chosen `-s <fresh-uuid>`; `codex exec` and the
Claude Code `Agent` tool expose no caller-visible session id — which is why
the receipt's `attempt_id`, not an invented string, is the evidence. **If
there is no receipt, there is no id**: leave `--isolation-evidence` empty
and let the route report `planned` or `degraded`. Inventing an id is typing
the control open.

**Independence is also a property of the resolved models, not the roles.** Two
reviewer roles that resolve to the same model are one reviewer with two labels.
Under a degraded single-provider binding this is the normal case, so the router
substitutes on resolved-model collisions and reports
`independence_compromised` when no distinct model is available for a slot.

When real isolation cannot be achieved, run the reviewers sequentially with the
second explicitly instructed to form its verdict *before* being shown anything
else, record the honest state, and treat `PASS + PASS` at `CRITICAL` as
`PASS_WITH_CHANGES` pending a human.

## Reviewer output contract

```yaml
verdict: PASS | PASS_WITH_CHANGES | FAIL
confidence: 0.0 - 1.0
findings:
  - severity: critical | high | medium | low
    category: correctness | security | architecture | maintainability |
              performance | testing | specification
    description: <what is wrong>
    evidence: <file:line, a repro, or the reasoning chain>
    required_fix: <what must change>
missing_tests:
  - <description>
uncertainties:
  - <what the reviewer could not determine, and why>
```

The `uncertainties` field earns its place: a reviewer that could not evaluate
something is giving you different information from a reviewer that evaluated it
and found nothing, and collapsing the two loses the signal.

### A seat that returns no verdict

`NO_RESPONSE` is not a fourth verdict a reviewer can emit — it is the
orchestrator's classification of a seat whose dispatch receipt ended
`START_FAILED`, `TIMED_OUT`, `TERMINATION_UNCONFIRMED`, `INVALID_OUTPUT`, or
`CANCELLED` (orchestrator-initiated)
(`INVALID_OUTPUT` covers only output written *inside* the deadline with exit
0 that is empty or fails to parse; output written after the deadline is
never graded regardless of content, so a truncated `PASS` after a timeout
kill is `TIMED_OUT`, not a pass and not `INVALID_OUTPUT`), or `FAILED` with
no parseable verdict block.

- A seat classified `NO_RESPONSE` did not review. The band's review is
  incomplete; nothing that seat would have checked can be assumed checked.
- Re-dispatch the missing seat **once**, with a fresh session id, and with
  the finished reviewer's output kept out of the prompt. Leaking it to
  unblock a stall is the exact isolation failure these rules exist to
  prevent.
- The re-dispatch consumes one `max_review_rounds` round (see
  `control-loop.md`, "Accounting for silent seats").
- If the seat is silent again, the band's independent review cannot be
  completed as specified this session. Re-route with `--isolation
  unavailable` — the router reports the `INDEPENDENCE_UNAVAILABLE` terminal
  for independence-requiring bands and a human takes over; record
  `review_independence: degraded` with both attempts' receipts in the
  orchestrator's round log. One verdict is not a dual review.
- `MEDIUM` (single reviewer): same re-dispatch rule; a second silence goes
  to a human with whatever partial evidence exists.

**A silence caused by the recipe is not a model failure.** Check the
receipt's `result.invalid_reasons` before you attribute anything. Reasons
naming a cancelled turn (`envelope_stop_reason:cancelled`,
`session_terminal_event:cancelled`), a document that declared its own failure
(`envelope_reported_error`, whose `result.envelope.error_type` names which one
— `error_max_turns` and a permission-cancelled turn are both recipe defects),
or unusable evidence
(`session_evidence_unreadable`, `session_evidence_unbound`) say the seat's
transport recipe killed the turn — a headless CLI with no TTY cancels a
tool call it cannot prompt for, and the model never got to review anything.
Fix the recipe (usually a missing allow rule or an over-broad tool surface),
then re-dispatch **the same model once**; that is the ordinary single
re-dispatch above, not an extra round. Do **not** carry these forward in
`--prior-failures`: that argument reports models that failed at the work,
and routing away from a model that was never allowed to start degrades the
next route for no reason. Reasons naming missing or unchanged artifacts
(`artifact_missing:*`, `artifact_unchanged:*`) or an artifact the evidence
layer refuses to certify (`artifact_multiply_linked:*`, an inode with a
second name; `artifact_identity_replaced:*`, a path that stopped naming the
inode pinned before the attempt started) are the same class of finding for a
producing seat — a workspace or recipe defect, not a model that failed the
work. `artifact_identity_replaced:*` has one common innocent cause worth
knowing before you go looking for an attack: a seat whose writer does the
write-a-temp-file-and-rename dance. That idiom installs a new inode and is
refused by design (`adapters.md`, "A required artifact must still be the
inode that was pinned"); fix the seat to write in place and re-dispatch the
same model once. `artifact_reservation_cleanup_failed:*` is not about the
model at all: the supervisor could not remove the placeholder it created for
an absent required path, so that file is still there and the receipt says so.
Read it with the `artifact_missing:*` it accompanies — the seat produced
nothing — and clear the leftover before re-dispatching, or the next attempt's
baseline starts from the supervisor's own bytes. Who reports failure history
is always the caller; the router changes nothing here.

## Reading verdicts

**Reason about content, not the verdict token.** Models are quite capable of
writing `PASS` above a paragraph describing a serious problem.

- `PASS` carrying a `critical`-severity finding is a contradiction. Treat it as
  `FAIL` pending clarification.
- `PASS` with `confidence < 0.5` is `PASS_WITH_CHANGES`.
- `PASS` with unresolved entries in `uncertainties` at band `HIGH`+ is not a
  clean pass — resolve the uncertainty or escalate it.

## Disagreement resolution

Given two independent reviews R1 and R2:

| R1 | R2 | Resolution |
|---|---|---|
| `PASS` | `PASS` | Accept — unless an unresolved `critical`/`high` finding or an unresolved `uncertainties` entry exists |
| `PASS` | `PASS_WITH_CHANGES` | Apply changes, then one lightweight re-review |
| `PASS_WITH_CHANGES` | `PASS_WITH_CHANGES` | Apply the union of changes, then one lightweight re-review |
| `PASS` | `FAIL` | **Judge** |
| `FAIL` | `PASS` | **Judge** |
| `PASS_WITH_CHANGES` | `FAIL` | Judge if the `FAIL` cites `critical`/`high` severity; otherwise return for reimplementation |
| `FAIL` | `FAIL` | Return for reimplementation — no judge needed, they agree |
| any verdict | `NO_RESPONSE` | Not a disagreement — an incomplete review. Apply "A seat that returns no verdict"; never adjudicate a verdict against an absence |
| `NO_RESPONSE` | `NO_RESPONSE` | A dispatch failure, not a review failure: check the transport (`bridge_down`?) before spending review rounds |

### Judge selection

```
default                      → principal_architect
strongly code-local dispute  → senior_engineer      (when architect cost/latency is undesirable)
formal-reasoning dispute     → reasoning_specialist / MAX
```

The judge receives **both** reviews plus the original independent input, and
must produce a verdict *plus* an explicit statement of which reviewer was
correct on each contested finding. That second requirement matters: a judge
that only issues a verdict gives you no way to tell whether it engaged with the
disagreement or just picked a side.

The judge's verdict is final for that round, and it counts against
`max_review_rounds`.

### Who may hold the judge seat

The adjudicator must be a model that no party already holds, and no weaker than
any of them — the implementer included, since it is one side of any dispute
about its own work. Both conditions are checked on the **resolved model**, not
on the role label. Under scarcity a role holds whatever model is left, so
`worker_fast` can end up on the frontier model while `worker_balanced` holds a
mid one; ranked by role label that assignment looks well ordered and seats the
weaker model as judge of the stronger.

When the seats cannot be arranged to satisfy both conditions, one reviewer may
be re-seated to free a model — but never below the tier its band asks of a
reviewer. Review depth is not currency for buying a judge seat. If the judge
can only be seated by spending it, the judge is unavailable and a human settles
any disagreement, which is a shortage the caller can act on.

`LOW`'s self-review exemption is narrower than it looks: it permits the reviewer
the **band configured** to resolve onto the implementer's model. It is not a
licence for the router to *move* a reviewer there. Re-seating a distinct,
stronger reviewer onto the implementer in order to free a model for the judge
buys the adjudicator with the review — and at `LOW` neither the substitution
record nor the depth gate can report it, because the band's reviewer floor is
zero. So the implementer's model is barred from a *replacement* reviewer at
every band. With two models and three seats you cannot have both an independent
reviewer and an independent adjudicator; saying `judge_unavailable` is the
honest answer, not a false stop.

A fallback compensation may add a **bonus** reviewer (`compensating_reviewers`).
That seat never upgrades the band's own independence requirement: it is checked
for model collisions like any other, but a band that did not ask for
independence does not start requiring it because a compensation fired, and a
bonus reviewer that cannot be isolated must not terminate the task.
