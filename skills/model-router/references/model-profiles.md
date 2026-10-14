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
Codex → claude, Claude Code → xai, grok → claude, grok → openai and Claude
Code → meta have been probed. Codex → xai uses the same command and is
recorded as assumed. The meta family is offered on Claude Code hosts only.
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

`meta_worker_balanced` is a fourth family's tier-1 model, bound to no role. On
a Claude Code host it is the first fallback of `worker_balanced`, through a
read-only recipe and no write recipe, so it takes that role's seats that read
whenever the xai seat does not resolve for the route (`adapters.md`, "The meta
seat reads, on Claude Code only"). With the xai seat available and not the
declared author, no route changes: a replay of 16,896 grid points, each routed
fresh and after its own worker failed, moved none. Codex and grok hosts never
seat it.

**Its tier is provisional.** No eval in this repository has measured it.
Third-party benchmarks put it level with the previous Sonnet generation —
this registry's tier-1 anchor — and below the current Claude balanced seat, at
roughly a sixth of that seat's cost per task. The ledger row "meta_worker_balanced
tier evidence (external, not measured)" holds the figures. Until a measurement
like the one below exists, read tier 1 as an assumption, and do not bind the
seat to a role on it. While a caller withholds the xai seat, this seat is in
effect the tier-1 cross-family seat on Claude Code; a caller who wants neither
withholds this model's id as well (`--unavailable-models`, or an
`allowed_families` list without `meta`).

Where it seats, the same replay shows what changes. The counts are routes out
of 33,792 per scenario (out of 3,072 for the REVIEW-only author scenario):

- **Read-only leads.** With the xai seat out, the lead of a REVIEW or an
  INVESTIGATION route (its worker, which reads) moves from the Claude balanced
  seat to this one (142 fresh routes, 30 retries). So does a REVIEW whose
  declared author is the xai family (85), and an INVESTIGATION whose xai quota
  reads `low` (87). Equal tier, but by outside numbers a weaker model. Its own
  reviewer then only needs to be another family at tier 1, so on 69 of those
  routes the OpenAI reasoning seat gives way to the Claude balanced seat. A
  caller's read-only override does the same for a write class.
- **A fourth family for review under scarcity.** When OpenAI or Claude is also
  exhausted, the router gains a cross-family reviewer on 3,125 and 5,762
  routes respectively. It gains it by the existing rules: the lowest tier that
  meets the floor, cross-family first. So this tier-1 seat takes review seats a
  same-family tier-2 or tier-3 model held (Claude senior 310, OpenAI reasoning
  64, Claude architect 22). Where that leaves a seat below its band, the route
  still says so (`review_depth_reduced`) and the gate still holds.
- **A weaker worker to keep that review.** Seating the execution cell must not
  make the review contract worse. When this is the only cross-family reviewer
  left, the stronger same-family worker yields to the risk-table worker:
  Claude balanced → Claude fast with OpenAI exhausted, OpenAI balanced →
  OpenAI fast with Claude exhausted, mostly on IMPLEMENTATION and DEBUGGING.
- **Different retries.** A different first worker means a different retry. 37
  retries with OpenAI exhausted no longer pay a fallback penalty, so their
  review band is not promoted (HIGH → MEDIUM) and 28 `review_below_band` gates
  are not raised.
- **One more note.** Every write route that seats its worker on
  `worker_balanced` with the xai seat out now notes that the meta seat could
  not write there.

### The gemini reviewer seat

`gemini_worker_balanced` is the gemini family's first dispatchable model, a
tier-1 seat bound to no role. On a Claude Code host it is the second fallback
of `worker_balanced`, after the meta seat, through a read-only recipe and no
write recipe (`adapters.md`, "The gemini seat reads, on Claude Code only,
after the meta seat", and "Antigravity seat profile" for what contains it).
A role resolves to the first usable model, so this seat is reached only where
the xai seat AND the meta seat both fail to resolve for the route. While the
meta seat resolves, nothing moves: the same replay method as the meta seat's
(16,896 grid points, fresh and after a failed worker) changed no route with
everything available, with the xai seat withheld, with an xai quota of `low`,
with a declared implementer, with a read-only override, or with the bridge
down. Codex and grok hosts never seat it. The unbound gemini row the suite
names is untouched and still never seated.

**Its tier is provisional**, on the same footing as the meta seat: no eval in
this repository has measured it, and third-party benchmarks put it level with
the previous Sonnet generation and the meta seat and below the current Claude
balanced seat (ledger row "gemini_worker_balanced tier evidence (external, not
measured)"). To keep it out as well, withhold its id or leave `gemini` out of
an `allowed_families` list.

Where it seats, it changes what the meta seat changes: with the meta seat out
too, every count above recurs, the same kinds of moves in the same numbers,
because this seat is reached exactly where the meta seat would have been and is
a seat of the same tier and kind. One thing is new:

- **A lower effort ceiling.** The CLI accepts `low`, `medium` and `high` only,
  so the seat's ceiling is HIGH. Where it takes a seat whose effort floor is
  higher, the route records `effort_below_floor`, which asks for a human
  (`human_in_the_loop.on_effort_below_floor`). Almost always the route is a
  CRITICAL one that already asked: with the xai and meta seats out, the cause
  joins an existing gate on 1,712 routes with OpenAI also exhausted and 3,424
  with Claude exhausted, and on 88 with a read-only worker override. A gate is
  newly raised on 6 routes, all with that override, where this seat became a
  worker that a floor wanted at VERY_HIGH. The effort the seat runs at is
  capped, not silently mapped: the route shows HIGH.

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
ledger says so in its own row.

So the binding rests on the verified price advantage, not on a demonstrated
quality advantage — which is exactly what the "cheapest capable model carries
the volume" principle asks for. The 446-node probe did separate them, by 1.3pp
against this seat, and the registry line stayed as it was: a gap that small,
recoverable by the escalation ladder, does not outweigh a 5x price advantage.
A larger measured gap is what would move the binding, and moving it is one
line in the registry; the policy does not move either way.

The Claude fast tier (`claude_worker_fast`) remains bound as the `claude_only`
fallback, so a dead bridge costs latency and money, not capability.

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

**`capability_tier` is 1 on purpose.** Honesty first: the one measured
comparison there is finished at the suite's ceiling, which separates nothing —
promoting the model to tier 2 on it would treat an undiscriminating result as
a demonstrated one.
Side-effect second: MEDIUM's reviewer floor is the minimum nominal tier among
`worker_balanced`, `senior_engineer`, and `reasoning_specialist` in the
default binding. That minimum is currently 1. Raising this model to 2 would
lift the floor to 2 and start marking MEDIUM reviews as below band — a
band-policy change with nothing to do with adding a model. If a harder eval
later supports a raise, change the registry line and decide the floor
question at the same time.

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
  the measurement below says it is not there yet.
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
evidence rather than inherited evidence. The ledger row recording the balanced
seat's tier measurement has the method and its limits. A vendor's launch table is still recorded as `documented`, never as
the reason a tier moved.
