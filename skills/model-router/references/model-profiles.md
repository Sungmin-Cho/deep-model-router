# Roles and bindings

Routing logic addresses models by **role alias**. Concrete identifiers live in
`config/model-routing.yaml` and are repeated here only for orientation — if the
two disagree, the config wins.

The point of the indirection: when models, prices, or names change, you update
one registry and recalibrate thresholds. The role vocabulary stays fixed. That
separation is what keeps this policy alive across model generations and
portable between runtimes.

## The five roles

These are the permanent interface. They do not change when models change.

| Role | Meaning |
|---|---|
| `worker_fast` | High-volume worker and default orchestrator |
| `worker_balanced` | Balanced senior worker; the first meaningful escalation |
| `senior_engineer` | Senior implementation, difficult debugging, code-review judgment |
| `reasoning_specialist` | Frontier logical verification and adversarial analysis |
| `principal_architect` | System architecture, ambiguity resolution, final judge |

Two more names appear in the policy — `reviewer` and `judge` — but they are
**capacities, not models**. They bind to one of the five above at routing time.

### `worker_fast` — default worker and orchestrator

Use for: task decomposition, routine routing decisions, straightforward coding,
scoped bug fixes, test generation, mechanical refactoring, repetitive edits,
documentation, implementation from a clear specification, low-risk review.

Must not be the sole authority when architecture is highly ambiguous,
requirements conflict, the root cause is unknown after reasonable
investigation, blast radius is critical, any critical-domain flag is set, or
several architectural approaches carry major tradeoffs.

### `worker_balanced` — first escalation

Use for: moderately ambiguous implementation, multi-file changes with real
interaction between the parts, difficult debugging, unfamiliar codebases, work
where code quality outweighs throughput, tool-heavy agent work, medium-risk
review, complex refactoring that does not justify the frontier tier.

This is the **first code-centric escalation target**. If `worker_fast` is
unavailable in the active runtime, this becomes the default worker.

Since 1.13.0 `unfamiliar_codebase` and `tool_heavy` are routing inputs — they
raise the execution score — rather than advice the caller applies by hand.

### `senior_engineer` — senior engineering and code review

Use for: complex debugging, difficult code semantics, maintainability review,
architecture-compliance review, high-risk implementation review, complex
refactoring, and review of `worker_fast` / `worker_balanced` output.

Prefer this over `principal_architect` for routine high-end engineering. The
architect tier is for judgment about *systems*, not about code.

### `reasoning_specialist` — frontier reasoning and verification

Use for: difficult logical verification, edge-case analysis, specification
consistency, concurrency and state reasoning, hard root-cause investigation,
adversarial and critical review.

Especially valuable as the independent reviewer of `senior_engineer`-family
output, because it is drawn from a different model family — see below.

### `principal_architect` — architecture and adjudication

**Must not be the default worker.**

Reserve for: system-level architecture, major new subsystems, highly ambiguous
requirements, unfamiliar architecture with many valid solutions, long-horizon
planning, major migrations, reviewer disagreement requiring adjudication, and
severe uncertainty that survived all prior analysis.

## Why two frontier roles

`senior_engineer` and `reasoning_specialist` are both frontier-tier. Either
could, in principle, do the other's job. The pair exists for two reasons, and
the first is much more important than the second.

**1. Family diversity at the top tier.** Different model families have
different failure modes, and that difference is the entire value of a second
review. Bind both frontier roles to the same family and dual review degrades
into asking one model twice — expensive, and much less likely to catch the
thing the first pass missed. The `HIGH` and `CRITICAL` bands pair these two
roles specifically so the two reviewers come from different families.

**2. Task fit.** `reasoning_centric` routes work to whichever role suits the
bottleneck. This is a real but secondary effect.

If you ever find yourself binding both to one family — because a bridge is
down, say — that is a degradation. Record `cross_family_review: false` and
treat the second review as weaker than it looks.

## Current bindings

One binding table, not one per runtime. Roles are filled by whichever model
best fits the job regardless of provider. Provider choice is nearly free at
delegation time — you have already paid for a fresh context window — so it
should be made on merit.

Not every bridge is verified in every direction. Claude Code → openai,
Codex → claude, Claude Code → xai, grok → claude, grok → openai, Claude
Code → meta, Claude Code → gemini and Codex → gemini have been probed. Codex
→ xai uses the same command and is recorded as assumed. The meta family is
offered on Claude Code hosts only, the gemini family on Claude Code and Codex
hosts, and neither ever where the work writes.
A route that depends on an assumed edge still has to be invocable in the
session that emits it — see `adapters.md`.

Registry keys, not model ids. Resolve them through
`config/model-routing.yaml` — concrete identifiers appear there and nowhere
else, so this table cannot drift out of date when the registry changes:

```
worker_fast           openai_worker_fast       (openai)
worker_balanced       xai_frontier             (xai)
worker_balanced_alt   claude_worker_balanced   (claude)
senior_engineer       claude_senior            (claude)
reasoning_specialist  openai_reasoning         (openai)
principal_architect   claude_architect         (claude)
```

The escalation ladder alternates families by construction — openai → xai →
claude → openai → claude. The first escalation used to repeat a family
(openai → claude → claude → …). Binding the balanced worker to the xai
frontier model restores a family change on that first step. HIGH and CRITICAL
dual-review seats stay on the two frontier families, so review depth does not
move.

**`worker_balanced_alt` is the Claude balanced model, and it is not a
same-family fallback.** `worker_fast` is openai. The alt exists so the first
escalation still has a seat when `xai_frontier` is unreachable. Reaching for
it by default would skip the xai step the ladder was just given.

Degraded bindings for when a bridge is down are in `adapters.md`.

### The meta reviewer seat

`meta_worker_balanced` is a fourth family's tier-0 model, bound to no role. On
a Claude Code host it is the second fallback of `worker_balanced`, after the
Claude balanced model, through a read-only recipe and no write recipe
(`adapters.md`, "The meta seat reads, on Claude Code only"). A role resolves to
the first usable model in its list, so this seat takes that role's reading
seats only where neither the xai seat nor the Claude balanced model resolves:
both withheld, the Claude family exhausted or outside `allowed_families`, or a
`review_context` naming both families as authors. Codex and grok hosts never
seat it.

**Its tier is 0 since 1.22.0.** The 2026-10-14 real-review replay (below)
confirmed tier 1 against the previous Sonnet generation, a history row. Read
against the live tier-1 rows, as tiers are now measured (`evaluation.md`, "How
a tier is measured"), the same preregistered rule lowers it: its recall falls
more than five points under the OpenAI balanced model, the lowest measured live
tier-1 row, and is about a quarter of the Claude balanced seat's. That last
figure is why the Claude model leads the list again (user decision,
2026-10-14). The seat keeps its place in the list (user decision, 2026-10-15):
where it resolves the role, a review seat whose band asks for tier 1 or more
now goes to a model that meets the floor. 1.19.0 had put
this seat first, for a cross-family reviewer wherever the xai seat was out; a
caller who still wants that withholds the Claude balanced model's id for the
route.

What 1.21.0 moved back, against 1.20.0 (the replay compares 33,792 routes per
scenario, each fresh and after its own worker failed; 3,072 for REVIEW-only
author scenarios):

- **With the xai seat out** — the state of any host that withholds it — the
  lead of a REVIEW or INVESTIGATION route returns from this seat to the Claude
  balanced seat (142 fresh routes, 30 retries), as do 85 reviewer seats; on 69
  routes the Claude balanced seat, now the worker, hands its reviewer seat to
  the OpenAI reasoning seat. 1,349 write routes lose the note that this seat
  could not write there, because nothing is skipped any more.
- **With OpenAI exhausted** (the xai seat available), 578 routes seat the
  Claude balanced model where this seat reviewed beside the Claude architect:
  the cross-family review those routes had is given up for the stronger
  same-family reviewer.

What 1.22.0 moves, against 1.21.0 (the same 33,792-route grid per scenario):
only routes where this seat resolved the balanced role. On Claude Code with
the xai seat withheld and the Claude family exhausted, 5,956 routes change. On
700 a MEDIUM review seat goes from this seat to the OpenAI reasoning seat, and
on 138 the lead of a REVIEW route, which counts as its first reviewer, goes to
the OpenAI balanced model. Where this seat still sits beside a stronger
reviewer on a HIGH or CRITICAL route, which already asked a human, the
shortfall record now says tier 0. A retry after this seat fails is unchanged:
it reaches the OpenAI reasoning seat either way. Across
both hosts 2,018 routes lose the cross-family review this seat or the gemini
seat supplied; of 2,462 review seats that change, 2,426 go to a slate that
meets the band's floor (four of them LOW-band REVIEW leads, which have none)
and 36 to a slate the route records as below it (18 of those now ask a human,
18 already did; 24 routes stop asking). With the Claude family exhausted and the xai seat
available, 1,274 Claude Code routes change only the tier that record carries.

A reading lead is not a reviewer, so an INVESTIGATION still takes this model
where it resolves the role — now at tier 0, which the route's
`selected_capability_tier` reports — with two exceptions. A low Claude quota
no longer swaps a reading lead onto it, because that swap wants a model of the
same tier: the OpenAI balanced model takes it (174 Claude Code routes with the
xai seat withheld). And an INVESTIGATION with an unknown root cause, whose
promotion asks for at least the balanced role, skips the tier-0 seats on its
first attempt (since 1.24.0 the meta seat alone: the gemini seat is tier 1 and
meets the floor) unless no usable model meets that floor or meeting it would
leave no independent reviewer (the route notes either); its retry is held by
the retry ladder instead, and the seats stay available to its reviewers.

Routes carrying an unknown root cause lose some review depth too. With the
xai seat out and the Claude family exhausted, 1,200 drop from HIGH to MEDIUM
review — one reviewer instead of two — because their reviewer no longer comes
through a fallback, which used to cost enough routing confidence to raise the
band; on 88 of them, IMPLEMENTATION and DEBUGGING routes, the execution band's
raise then yields to keep the bands equal, and the OpenAI fast model stays the
worker where 1.21.0 raised it to the OpenAI balanced model. The execution band raises a worker only to a
strictly stronger tier, so a few routes no longer raise their fast worker to
this seat, and some that kept a fast worker for this seat's cross-family
review now raise it to the OpenAI balanced model.

That yield on 88 routes is the execution guard working as designed: a
stronger worker may not change the review's depth, a deeper review included
(issue #66, replayed in 1.23.0; `routing-policy.md`, "How the two tables
combine"). Since 1.23.0 the investigation promotion also never trades a
stronger model the fast role resolves to for the balanced role's (issue #67).
Under the deepest shortage replayed — the xai seat and the fast and frontier
OpenAI seats withheld, the Claude family exhausted — 32 Claude Code first
attempts move from GPT-5.6 Terra to GPT-6.1 Sol, the worker the same task gets
without the flag; their review then falls to the meta seat below its band and
asks a human, and their retries end at a human, nothing stronger being left.
With the frontier OpenAI seat available as well, 2 more move and Astra
reviews them.

### The gemini reviewer seat

`gemini_frontier` is the gemini family's dispatchable model since 1.24.0, a
tier-1 seat bound to no role: Google's frontier model, through the Antigravity
CLI. It holds the places the seat's first model held. On a Claude Code host it
is the third fallback of `worker_balanced`, after the Claude balanced model and
the meta seat; on a Codex host it is the second, right after the Claude
balanced model (`adapters.md`, "The gemini seat reads, after the Claude model",
and "Antigravity seat profile" for what contains it). It reads only: the Claude
Code direction ships a maker recipe, but its write authority is withheld
(below), and the Codex direction has none. Grok hosts never seat it. The first
model, `gemini_worker_balanced` (tier 0 from 1.22.0), is retired at the user's
request: it stays valid history input and is never seated. The unbound gemini
row the suite names is untouched and still never seated.

**Its tier is 1 since 1.24.0**, set by a measurement rather than the vendor's
claims. The 2026-10-27 real-review replay (the 1.22.0 method on the same three
packages, judges and preregistered margins, read for a row with no tier yet —
`evaluation.md`, "How a tier is measured") put its recall at 23.3%: above the
live tier-1 bar (11.8%), below the tier-2 bar (28.2%, five points under the
Sol seat's 33.2%), and between the OpenAI balanced seat (16.8%) and the Sol
seat — about 2.4 times the retired model's 9.8%. Per package it
ranged from 0% (two reviews of one implementation round that found nothing
where the reference reviewers had found nine defects) to 48%. No tier-3 row
could be measured beside it — the two tier-3 models are the judges — so this
replay could not have placed it above tier 2. Its sessions were audited from
the CLI's own conversation store: every tool call was a read inside its
package.

**The maker seat is still shipped without write authority.** It was re-probed
on the new model (the profile, the kernel fence and the before-spawn refusals
hold), but a seat that writes moves tier only when an implementation replay
agrees with the review replay, and that replay could not run: the account's
quota for this model was spent by the review replay. Until it runs,
`write_verified` stays false and the seat never writes.

**Its quota is small and its turns are long.** On a personal sign-in the
account's quota for this model ran out after about ten long agentic runs in
one afternoon — a review here made 150 to 220 file reads — and the CLI then
refused every run (HTTP 429, "Individual quota reached") until a reset a week
away; other models on the same account kept working. A refused attempt ends
FAILED with the CLI's text in its receipt. Withhold the model
(`--unavailable-models`) until the reset rather than retrying it. A review took
7 to 13 minutes at high effort: give the seat a generous deadline.

What 1.24.0 changes for this seat, against 1.23.1 — the 1.22.0 replay grid plus
ten scenarios (the senior and reasoning seats or whole families out, and the xai
quota low), 958,113 routes, fresh and after a failure:

- **62,082 routes change.** With every seat available nothing moves, unless the
  xai quota is low: then the `low` swap, which moves a reading lead to the first
  same-tier model of another family, reaches this seat — 335 investigations on
  both hosts move from the OpenAI balanced seat to it (176 of them, with an
  unknown root cause, run at the seat's HIGH effort ceiling rather than the MAX
  the flag asks for; no floor is broken). Every other change is where everything
  ahead of the seat is out, mostly with the xai seat withheld and the Claude
  family exhausted (on both hosts), or with OpenAI exhausted.
- **It now staffs seats whose floor is tier 1.** The retired tier-0 model could
  read and lead reading work but not take a review seat a MEDIUM band floors at
  tier 1; this seat can. Reviewer seating changes on 45,920 routes: where the
  retired model sat beside another reviewer it is replaced in place, and MEDIUM
  review leads and reviewer seats that went to the OpenAI balanced, reasoning or
  frontier seats go to this one. A cross-family review is gained on 5,338 routes
  and lost on 546.
- **The worker moves on 4,601 routes** (3,478 first attempts, 1,123 retries).
  Reading leads and promoted investigations take this seat (3,479), mostly where
  they took the OpenAI balanced seat; a retry after it fails climbs above tier
  1, and a retry after the tier-0 meta seat fails may now stop at this seat where
  it went on to the Sol seat. On 248 first attempts of work that writes, with xai withheld and the
  Claude family exhausted, the execution cell now keeps the OpenAI fast writer
  (tier 0) instead of raising it to the OpenAI balanced one, because raising it
  would give up this seat's cross-family review (`execution band NORMAL
  yielded worker_balanced: cross_family_review`) — the trade 1.19.0 disclosed
  for the meta seat, which tier 0 had ended. Their retries follow (186), as do
  288 retries of reading work whose first attempt moved; on 2 first attempts of
  reading work the lead lands a tier lower.
- **Gates and terminals.** A human gate changes on 886 routes: 246 gain one and
  224 lose one; a `review_below_band` cause is added to 380 and removed from
  224, and `effort_below_floor` (the seat's HIGH effort ceiling) joins 272 gates
  already raised. Ten first attempts carrying an unknown root cause, with xai
  withheld and the Claude family exhausted, newly end ESCALATE_ROUTING, and
  eight terminal routes change kind with the fast, xai and frontier OpenAI
  seats out and Claude exhausted (six INDEPENDENCE_UNAVAILABLE first attempts
  become ESCALATE_ROUTING, two SUPPLY_EXHAUSTED retries become
  INDEPENDENCE_UNAVAILABLE). 1,354
  routes have their review band raised by the confidence rule: the fallback
  that reaches this seat is charged like any other.
- The summed output price of the seated worker and reviewers rises 0.52%.

Two wider placements were replayed and not taken (user decision 2026-10-27):
listing the seat also last in the senior and reasoning fallback lists on both
hosts moves 88,703 routes (+1.01%; replayed without the two xai-quota
scenarios, 890,529 routes, where the adopted placement moves 61,747), and
listing it second there, right after
the Sol seat, moves 237,685 (+1.71%), seating a tier-1 model in tier-2 roles on
9,770 worker changes and ending 1,290 more routes ESCALATE_ROUTING. The ledger
row "1.24.0 gemini seat placement counterfactuals" has the numbers.

### The OpenAI frontier seat

`openai_frontier` fills the upper OpenAI-only roles and is the next reasoning
candidate when the default `openai_reasoning` seat is unavailable or has
failed. It is also an architect fallback. `openai_reasoning` remains the
default reasoning model, the tier-2 OpenAI-only senior, and the fallback when
the frontier is unavailable. A failed tier-2 OpenAI attempt can now escalate
to a stronger OpenAI model while the default role bindings remain intact.

The frontier is tier 3, alongside `claude_architect`. This is a policy
assignment informed by the provider's model positioning, not a measured
cross-provider ranking. It does not raise the configured HIGH/CRITICAL
reviewer floor, whose minimum remains the tier-2 senior role. Replacing the
default reasoning model would also upgrade MEDIUM reviewer seats; the
execution guard can then reject a stronger worker to preserve that reviewer
tier. Keeping the default binding avoids that regression. Measure complete
task cost and latency before adopting a broader default replacement.

The verification ledger separates the successful low-effort CLI probe from
documented effort support and reference API prices. This model's MINIMAL
request maps upward to native `low`; its per-model `effort_map` overlays the
family map. A model missing from a stale local catalog must be checked in the
actual runtime; hosts without access must report it in `unavailable_models`.

### The Claude senior seat

**Its tier is inherited, not current.** On 2026-09-25 the `claude_senior`
id moved to the next Opus generation. A contained probe confirmed the id,
the served model, the low and max effort tokens, and a write-seat run through
the maker recipe. The published prices were re-read from the provider's
documentation: the list price went down, $4 / $20 per million input / output
tokens against the predecessor's $5 / $25. The 2026-09-30 tier check of the
balanced seat scored this seat as a tier-2 reference (see "Tiers after the
2026-09-30 bumps"), but no measurement re-decided its own tier:
`capability_tier` 2 comes from the lineage, and the ledger says so in its own
row.

This generation cannot turn thinking off, and its own default effort is
medium. The router always passes an explicit `--effort` on this seat, so that
default never takes effect on a routed dispatch.

Like the architect seat, this generation keeps refusal classifiers, and the
provider's fallback can rerun a declined request on another Claude model.
The row therefore carries the same `security_sensitive` substitution
disclosure. It was missing from the 1.16.0 bump and was added in 1.17.1.

### The OpenAI reasoning seat

**Its tier is inherited, not current.** On 2026-09-26 the `openai_reasoning`
id moved to the next Sol generation, and on 2026-09-30 to that generation's
point release. For the current id a contained probe confirmed that the id is
accepted, the low and max effort tokens, and a write-seat run through the
maker recipe. The CLI echoing
the requested id is id acceptance, not proof of the served model. The
published prices were re-read from the provider's documentation: $2 / $10 per
million input / output tokens as before, with cached input halved to $0.10.
The 2026-09-30 tier check of the balanced seat scored this seat as a tier-2
reference, but no measurement re-decided its own tier: `capability_tier` 2
comes from the lineage, and the ledger says so in its own row.

The point release rejects the `none` effort token that the OpenAI family map
sends for MINIMAL, so this row maps MINIMAL to `low`, as the OpenAI frontier
seat does.

## Why `worker_fast` is bound to the OpenAI fast tier

Worth recording, because it is the one binding decision that rests on
measurement rather than structure. Prices below are quoted as of the probe date
in the config's verification ledger; the authoritative ids and rates live in
the registry.

**Price — verified.** The OpenAI fast tier is $0.20 / $1.20 per million
input/output tokens against the Claude fast tier's $1.00 / $5.00: five times
cheaper on input, 4.2× on output, and $0.02 vs $0.10 on cached input. For the
role that carries the volume, that difference compounds across every routine
task. Both rates are the standard (≤272K-input) tier; above 272K input tokens
the provider re-bills the whole request at 2×/1.5× — luna stays far cheaper
than haiku on both sides of that line, so the binding direction is unchanged.

**Quality — probed 2026-08-18, near ceiling.** A discriminating 446-node
hidden-test head-to-head (8 spec-dense stdlib tasks, one attempt per model,
the same CLI transports this router uses) scored haiku 442/446 and luna
436/446 — haiku marginally ahead (+1.3pp), each model's failures tracing to
two spec-edge root causes. Mean per-task wall time favoured luna 19.6s
against haiku 91.2s. One attempt per model and correlated node failures make
this suggestive, not decisive; the ledger records it as
`price_verified_quality_probed`. The binding still rests on the verified
price advantage, now with the measured quality cost on record.

**Those numbers are inherited, not current.** On 2026-09-26 the
`openai_worker_fast` id moved to the next Luna generation. A contained probe
confirmed that the id is accepted, the low and max effort tokens, and a
write-seat run through the maker recipe. The published prices were re-read:
$0.10 / $0.50 per million input / output tokens, half the input price and
under half the output price quoted above, so the price argument is stronger
than it was. The 446-node head-to-head was not re-run. Read its scores as the
predecessor generation's; `capability_tier` 0 comes from the lineage, and the
ledger says so in its own row. The 2026-10-14 real-review replay measured this
seat as a live tier-0 anchor: about a third of the Claude fast seat's review
recall, level with the previous Sonnet generation. It writes, so that moves
nothing on its own.

So the binding rests on the verified price advantage, not on a demonstrated
quality advantage — which is exactly what the "cheapest capable model carries
the volume" principle asks for. The 446-node probe did separate them, by 1.3pp
against this seat, and the registry line stayed as it was: a gap that small,
recoverable by the escalation ladder, does not outweigh a 5x price advantage.
A larger measured gap is what would move the binding, and moving it is one
line in the registry; the policy does not move either way.

The Claude fast tier (`claude_worker_fast`) remains bound as the `claude_only`
fallback, so a dead bridge costs latency and money, not capability.

**Its id moved to the next Haiku generation on 2026-10-14** (model_sync found
and probed it on 2026-10-08; the attended maker probe passed and promote moved
it into the registry). The published price fell to a tenth of its
predecessor's on input and output for prompts up to 100,000 tokens; a longer
prompt pays five times that for the whole request, recorded as the row's
`long_context` block. The context window grew to a million tokens. Its
`capability_tier` 0 is inherited from the lineage. One measurement exists: in
the 2026-10-14 real-review replay below, this seat — run as a reported subject
— recalled close to the Claude balanced seat and three times the previous
Sonnet generation. **It does not raise the tier** (1.22.0). The replay
separates this seat from neither the Claude balanced seat nor the tier-2 mean,
so if it supported tier 1 it would support tier 2 as well; and this seat
writes, so a review replay measures half of what its tier gates (`evaluation.md`,
"How a tier is measured"). What a raise would have moved was replayed first:
with every seat available, retries after the OpenAI fast seat fails would
climb to this seat instead of a balanced one on Claude Code and grok hosts
(1,860 routes), and with OpenAI exhausted it would take review seats from the
Claude balanced model on thousands more — all of it resting on a writing
strength nobody had measured. The implementation replay of 2026-10-15 has now
measured it, and it stays at tier 0: the raise misses the gate there by 0.6
points (see "Tiers measured on 2026-10-15").

## Why `worker_balanced` is bound to the xai frontier model

The same posture as `worker_fast`, pointed at the first escalation.

**Price — verified, and tiered.** Below 200K input tokens the xai frontier
model is priced as a balanced-tier seat ($2.00 / $6.00 per million
input/output tokens) against the Claude balanced model's $2.00 / $10.00, and
against a claimed frontier tier. Input is equal; the remaining advantage is
output price. For the first escalation that difference is the point of the
binding.

**At or above 200K input tokens the comparison inverts.** xAI re-bills the *entire*
request at $4.00 / $12.00 (cached $1.00) once the prompt reaches that line —
not just the tokens past it — while the Claude balanced model bills its full
1M window at flat standard rates. So above 200K this seat costs more on both
axes and runs out of window 500K earlier. The advantage is real and it is
conditional; stating only the first half is what made the previous version of
this section read as unconditional. (The OpenAI GPT-5.6 seats carry an
analogous whole-request tier above 272K input; that boundary is recorded in
the registry for reference and moves no binding — the `large_context` rule
remains keyed to the xAI 200K line.)

That is one deterministic fact, so it is a rule rather than advice: a task
flagged `large_context` binds `worker_balanced` to `worker_balanced_alt`
(the Claude balanced seat) instead. The router never sees a token count, so
the flag is the caller's statement about which side of the line the route is
on — set it when the prompt, the retrieved context, or the files in scope
plausibly reach 200K tokens. The swap is a binding choice, not a fallback:
nothing became unavailable and nothing is recorded as though it had. If the
alt seat is itself unavailable the primary comes back, and that *is* recorded
as a fallback. Under a degraded (single-provider) binding there is no alt seat
to prefer and the rule is a no-op.

The same alt-seat rule also fires for `latency_sensitive`. The 2026-08-20
B.1 repeat eval (6 multi-file tasks × 3 reps) tied quality 15/18 each and
recorded a lower Claude-balanced SUCCEEDED p50 in every task type. That is
not a quality win for the alt; it is a latency fact at a quality tie, so a
caller who values first-escalation wall-clock delay over output price can
opt into the same swap. Bindings and `capability_tier` stay where they are.

**Quality — probed 2026-08-18, tied at ceiling.** The same 446-node
head-to-head scored the xai frontier model 446/446 and sonnet-5 445/446 — a
tie at the suite's ceiling, one attempt per model (mean wall time: 103.2s for
the xai seat against sonnet-5's 38.6s). No regression detected against the
claimed frontier tier; a single suite at ceiling is not grounds to raise
`capability_tier`.

**That measurement is inherited, not current.** A newer xai generation was
observed as the CLI's default on 2026-09-22 and the registry id moved to it.
Id acceptance and the effort ceiling were re-probed on it; the published
prices and the context window were re-read from the provider's documentation.
All are unchanged, so the price argument above is current. Neither the
446-node head-to-head nor the repeat eval was re-run, and the ledger says so
in its own row. Read every quality number in this section as the predecessor
generation's until a fresh head-to-head exists — and note that the binding
and the tier below do rest on them, so what is inherited is the seat's
fitness itself, not merely a promotion nobody took.

**The alt seat's numbers are inherited too.** On 2026-09-30 the
`claude_worker_balanced` id moved to the next Sonnet generation. A contained
probe confirmed the id, the served model, the low and max effort tokens,
and a write-seat run through the maker recipe. The published
prices are unchanged at $2 / $10 per million input / output tokens with
$0.20 cached input, and the full 1M window is still billed at standard
rates. Both the 446-node head-to-head and the repeat eval measured the
predecessor, and so did the latency fact the `latency_sensitive` swap rests
on. This generation also keeps refusal classifiers, like the Claude senior
seat, and its row carries the same substitution disclosure. Its tier was
measured separately; see "Tiers after the 2026-09-30 bumps" below.

**`capability_tier` is 2 since 1.23.0, on two measurements.** A seat that
writes moves only when a review replay and an implementation replay point the
same way (`evaluation.md`, "How a tier is measured"). The real-review replay
of 2026-10-14 put this model level with the tier-2 rows, and the
implementation replay of 2026-10-15 — eight accepted findings from this
repository, each fixed from its pre-fix tree and graded by the tests its real
fix added — scored it 0.625, equal to GPT-6.1 Sol (the weaker live tier-2 row)
and against 0.6875 for the Claude senior seat and 0.3125 for the OpenAI
balanced model. Both replays read "raise" under the rule fixed before the
second ran, and the user applied it (2026-10-15). The ledger row
"Implementation replay of the writing rows (2026-10-15)" has the numbers and
the disclosures: the raise rests on the weaker tier-2 row as the anchor (with
the senior seat alone it would hold, one run short), and the reference fixes
the hidden tests come from were written by the senior seat's model.

MEDIUM's reviewer floor does not move: it is the minimum nominal tier among
`worker_balanced`, `senior_engineer` and `reasoning_specialist` in the
default binding, where the xai seat holds `worker_balanced` at tier 1. What
moves is every comparison that reads this model's resolved tier — the
"Tiers measured on 2026-10-15" section below lists it.

## Tiers after the 2026-09-30 bumps

The two generations promoted on 2026-09-30 moved further than a point release
usually does, by their vendors' own numbers. The new Sonnet generation lands
within a few points of the Claude senior seat on most of its launch table,
and ahead of the previous Sol generation on several. The new Sol point
release is reported level with the OpenAI frontier model on one coding
benchmark, at about a fifth of the cost per task. The ledger row
"Vendor-published benchmarks for the 2026-09-30 bumps" holds the figures.
Both vendors still place these models in their middle line.

No tier and no binding moved. Before deciding, each candidate change was
replayed through the router on 16,896 grid points. Each point was routed
fresh and once more after that configuration's own worker failed, and the
routes were compared point by point. The ledger row "Tier and binding
counterfactuals after the 2026-09-30 bumps" has the counts.

- **The Claude balanced seat to tier 2** would cut the summed output price
  of the seated models by about 11%. It would also seat this model where a
  tier-3 reviewer sits today on 6,942 routes. On 4,082 retries after this
  seat fails, the next seat would be the tier-3 architect instead of the
  senior. The whole saving rests on this model being tier-2 strength, and
  the measurement below says it is not there yet. (Measured again with live
  anchors and an implementation replay, and adopted in 1.23.0: see "Tiers
  measured on 2026-10-15".)
- **The Claude balanced seat as the default `worker_balanced`** would raise
  that price sum slightly. The step from the fast worker would still cross
  family, but the next step, from the balanced worker to the senior seat,
  would stay inside the Claude family. Keeping that step cross-family is
  why the xai seat holds the role.
- **The OpenAI reasoning seat to tier 3** would raise that price sum by
  under 2%. On 1,018 retries after the Sol seat itself fails, the route
  would become terminal instead of climbing to the OpenAI frontier seat,
  because nothing sits above tier 3.

**Measured on 2026-09-30.** The decision rule was fixed before each round
ran. The first suite used seeded-defect modules with hidden tests. Every
model fixed all 24 defects, and the three strongest also found all 24 in
review. The tier-1 anchor found 22, a gap too small to pass the rule's
validity gate, so this suite decided nothing.
The second replayed a real review from this repository against its original
target: twelve defects that review had found and the author had fixed.
Average recall over two runs:

| Model | Recall |
|---|---|
| Sol seat | 42% |
| Claude senior seat | 31% |
| This seat | 21% |
| Previous Sonnet generation (tier-1 anchor) | 8% |

This seat closes 44% of the distance from the anchor to the tier-2 seats,
short of the 75% the rule required. It stays at tier 1, now on measured
evidence rather than inherited evidence. Read under the method adopted in
1.22.0 (`evaluation.md`, "How a tier is measured"), that check anchored on a
history row and replayed a review for a seat that writes, so it is recorded and
decides nothing: the tier is 1 by lineage. The ledger row recording the balanced
seat's tier measurement has the method and its limits. A vendor's launch table is still recorded as `documented`, never as
the reason a tier moved.

**Measured on 2026-10-14 (issue #58).** The same method, preregistered with its
amendments before any grade: three real reviews from this repository, each
frozen when it ran (two implementation reviews and one design review, with 9,
9 and 11 accepted findings), every subject on its shipped reviewer recipe at
high effort, two runs each, graded blind by two judges from other families
(calibrated exactly on the original reviews). Pooled recall:

| Seat | Tier | Recall |
|---|---|---|
| Claude senior seat | 2 | 52% |
| Claude balanced seat | 1 | 46% |
| Claude fast seat | 0 | 40% |
| Sol seat | 2 | 33% |
| Previous Sonnet generation (tier-1 anchor) | 1 | 13% |
| Meta reviewer seat | 1 | 12% |
| Gemini reviewer seat | 1 | 10% |

The rule's bar for tier 1 is the anchor minus five points, so both reviewer
seats confirmed tier 1. What the rule could not say, the table does: the anchor
itself sits far below the current Claude balanced and fast seats. Acting on
that, the Claude balanced model now leads the balanced worker's fallback list
again, and the gemini maker seat stays without write authority. The ledger
rows "… tier measurement (real-review replay, 2026-10-14)" carry the limits:
the ground truth came from two of the reviewers measured here, findings outside
it are not credited, and the seats were compared with their own tool surfaces.

**Re-read against live anchors (1.22.0, issue #63).** The anchor above is a
history row: the previous Sonnet generation is never seated, so it says nothing
about the models the router treats as tier 1 today. Tiers are now measured
against the live rows that hold them, the lowest one measured setting the bar
(`evaluation.md`, "How a tier is measured"). The two live OpenAI rows the replay
had not covered were run on the same packages, judges and method:

| Seat | Tier | Recall |
|---|---|---|
| OpenAI balanced seat | 1 | 17% |
| OpenAI fast seat | 0 | 13% |

The live tier-1 bar is therefore the OpenAI balanced seat's 16.8%, still well
separated from tier 2 (42%); that seat's own tier is inherited, not measured,
and the xai seat, also live at tier 1, could not be measured. Five points under
the bar is 11.8%: the meta seat (11.7%) and the gemini seat (9.8%) fall below
it, so the same preregistered rule that confirmed them against the history row
lowers both, and they are tier 0. With one of the two judges alone the meta
seat would have been confirmed. The plan for these runs, written after the two
seats' numbers were known, had added a band that would have called both
inconclusive; the review found it, and the preregistered rule stands (user
decision, 2026-10-15). One judge shares the OpenAI rows' family; it agreed
with the other on every item.

What the table does not decide: review recall splits the measured rows into
two groups that ignore the tiers — the current Claude seats and the Sol seat
(33–52%, tiers 0 to 2) and everything else (10–17%, tiers 0 and 1). The
writing rows in both groups keep their tiers until an implementation replay
agrees with the review replay. That is why the Claude fast seat stays at tier 0
(see "Why `worker_fast` is bound to the OpenAI fast tier") and the Claude
balanced seat at tier 1: the replay separates neither from the tier-2 mean, and
a raise of the balanced seat would move about as many routes (13,368) as the
2026-09-30 counterfactual found. The ledger rows "Reviewer seat tiers against
live anchors (real-review replay, 2026-10-14)" and "Tier counterfactuals after
the 2026-10-14 replays" have the numbers.

## Tiers measured on 2026-10-15

**The implementation replay (issue #65).** Eight accepted findings from this
repository whose fix added a test, chosen before any run (four in the routing
code, one in the local model state, two in model-sync, one in the dispatch
supervisor). Each subject got the pre-fix tree without history and the finding
as its reviewer wrote it, worked through a write-capable headless recipe at
high effort in a disposable copy, twice, and was graded by the tests the real
fix added plus the pre-fix suite — no judge. Every session was audited for
reads outside its copy; none read an answer.

| Seat | Tier (before) | Pass rate |
|---|---|---|
| Claude senior seat | 2 | 69% |
| Sol seat | 2 | 63% |
| Claude balanced seat | 1 | 63% |
| Claude fast seat | 0 | 56% |
| OpenAI fast seat | 0 | 38% |
| OpenAI balanced seat | 1 | 31% |

The rule fixed before the runs reads each row against the others of its tier
and the tier above. The Claude balanced seat sits level with the Sol seat, the
weaker tier-2 row, in this replay as in the review replay: both say raise, and
it is **tier 2** (user decision, 2026-10-15). The Claude fast seat's raise
narrowly fails the gate here (the tier-1 mean sits 9.4 points above the other
tier-0 row, the OpenAI fast seat, where the gate needs 10), so it stays at
**tier 0**. The OpenAI rows hold: the gate cannot separate tier 1 from tier 2 while the Claude
balanced seat sat in tier 1, although the OpenAI balanced seat scored lowest
of all six.

**What the raise moves.** Every comparison that reads this seat's tier. With
every seat available, 4,082 retries after this seat fails climb to the tier-3
architect instead of the senior seat, 2,254 retries after the xai seat fails
take this seat instead of the senior one, the medium reviewer for a tier-2
worker is often this seat instead of the senior one, and high and
critical review pairs that substituted a tier-3 seat for the senior one when
the execution band had raised the worker onto it now keep the senior seat
(the band no longer raises this seat's work: the senior seat is no stronger).
With the xai seat withheld 15,246 routes move, and 3,888 critical routes that
had no adjudicator now have one. The summed output price of the seated models
falls 11% with every seat available and 8% with the xai seat withheld; with
OpenAI exhausted it rises 3%, and 1,024 routes that had no independent
reviewer become dispatchable. **1.23.0 as released** (historical numbers):
retries after the xai seat fails landed on this seat through the balanced
role's fallback list, which cost 0.06 routing confidence on top of the failure
they already paid for. In the **1.23.0 replay**, 454 retries sat at 0.60 and 264
first REVIEW attempts with the xai seat withheld and OpenAI exhausted sat at
0.65; the fallback charge reduced them to 0.54 and 0.59, respectively, below
the routing threshold. **1.23.1 (issue #70) repairs the retry charge:** a seat
reached because the binding's model *failed* is not itself a scarcity fallback,
so the retry downgrade is retained without charging that same failure twice.
Actual scarcity still counts when the failed model is also currently withheld,
or a successor is withheld; a retry-caused downgrade can coexist with that
outage fallback. The 1.23.1 replay restored 452 of the 454 retries to 0.60; two
remain below threshold because the large-context investigation has its XAI seat
withheld, OpenAI exhausted, and the Sonnet seat failed. The 264 first REVIEW
attempts remain fresh routes at 0.59: no model failed there, and two families
are unavailable. Because 0.60 and 0.80 both read the remaining fallbacks,
removing the retry-only charge can change the review band, and the existing
execution guard may then change the worker tier too; neither path goes below
the risk band or override floors. **The following cross-family review counts
are the 1.23.0 tier counterfactual, not the 1.23.1 retry replay:** review is
gained on 4,572 routes and lost on 264 with every seat available (4,548 and
528 with the xai seat withheld; 2,566 and 4,360 with OpenAI exhausted). A
`low` xai quota now swaps a reading lead to the OpenAI balanced model, the
same-tier model of another family, instead of this one. The host advisory that names a `/model` breaks the new tie
between this seat and the senior seat by role, so a Claude host below a tier-1
or tier-2 ask is still pointed at this model. The ledger rows "Implementation
replay of the writing rows (2026-10-15)" and "Tier counterfactuals after the
2026-10-15 implementation replay" have the numbers.

## Tiers measured on 2026-10-27

The gemini seat's model changed in 1.24.0 (Gemini 3.8 Flash retired at the
user's request), so its row had no tier. It was measured by the 1.22.0 review
replay — the same packages, judges, margins and live anchors, the judges'
calibration re-run first (12 of 12 exact) — read for a row with no tier yet:
the highest tier whose bar it clears on a suite that separates that tier from
the one below.

| Seat | Tier | Recall |
|---|---|---|
| Claude senior seat (anchor) | 2 | 52% |
| Claude balanced seat (anchor) | 2 | 46% |
| Sol seat (anchor, the tier-2 bar's row) | 2 | 33% |
| **Gemini reviewer seat** | **1** | **23%** |
| OpenAI balanced seat (anchor, the tier-1 bar's row) | 1 | 17% |
| Gemini reviewer seat, first model (retired) | 0 | 10% |

The tier-2 bar is 28.2% and the tier-1 bar 11.8%; tier 3 could not be decided,
because the two tier-3 models are the judges. Every reading of the
sensitivity — each tier-2 row alone as the bar, each judge alone — gives tier 1;
by package the seat would read tier 0, 1 and 2. The seat ships read-only, so
the review replay is its whole measurement; the implementation replay that
would let it write is recorded as not run (the account's quota for the model
was spent). With this seat at tier 1, a `low` xai quota swaps a reading lead to
it, the first same-tier model of another family in the list, on both hosts. The
ledger rows "gemini_frontier tier measurement (real-review replay,
2026-10-27)" and "gemini_frontier implementation replay (not run, 2026-10-27)"
have the numbers and limits.
