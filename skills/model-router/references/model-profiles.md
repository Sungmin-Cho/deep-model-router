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
Codex → claude, Claude Code → xai, grok → claude, and grok → openai have
been probed. Codex → xai uses the same command and is recorded as assumed.
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
tokens against the predecessor's $5 / $25. No quality comparison was run on
the new generation. `capability_tier` 2 comes from the lineage, not from a
measurement, and the ledger says so in its own row.

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
accepted and the low and max effort tokens. The write seat was not re-probed
through the maker recipe for it, and the ledger records that. The CLI echoing
the requested id is id acceptance, not proof of the served model. The
published prices were re-read from the provider's documentation: $2 / $10 per
million input / output tokens as before, with cached input halved to $0.10.
No quality comparison was run on the new id. `capability_tier` 2 comes from
the lineage, not from a measurement, and the ledger says so in its own row.

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
probe confirmed the id, the served model, and the low and max effort tokens.
The write seat was not re-probed through the maker recipe. The published
prices are unchanged at $2 / $10 per million input / output tokens with
$0.20 cached input, and the full 1M window is still billed at standard
rates. Both the 446-node head-to-head and the repeat eval measured the
predecessor, and so did the latency fact the `latency_sensitive` swap rests
on. This generation also keeps refusal classifiers, like the Claude senior
seat, and its row carries the same substitution disclosure.

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
replayed through the router over 33,792 routes: 16,896 fresh inputs plus
one retry each after its own worker failed. The ledger row "Tier and binding
counterfactuals after the 2026-09-30 bumps" has the counts.

- **The Claude balanced seat to tier 2** would cut the summed output price
  of the seated models by about 11%. It would also seat this model where a
  tier-3 reviewer sits today on 6,942 routes. On 4,082 retries after this
  seat fails, the next seat would be the tier-3 architect instead of the
  senior. The whole saving rests on this model being tier-2 strength. No
  measurement in this registry shows that, and even its tier-1 fitness is
  inherited from its predecessor.
- **The Claude balanced seat as the default `worker_balanced`** would raise
  that price sum slightly. It would also stop the first escalation from
  crossing family, which is the reason the xai seat holds that role.
- **The OpenAI reasoning seat to tier 3** would raise that price sum by about
  3%. On 1,018 retries after the Sol seat itself fails, the route would
  become terminal instead of climbing to the OpenAI frontier seat, because
  nothing sits above tier 3.

What would move the first of these is a discriminating measurement of the
Claude balanced seat against the two tier-2 seats on this router's review and
implementation work. A suite that finishes at its ceiling, like the 446-node
one, cannot provide it. Until then, a vendor's launch table is recorded as
`documented`, never as the reason a tier moved.
