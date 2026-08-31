# Routing policy — the runtime-neutral core

Everything here is provider-neutral. No command syntax, no model identifiers.
Those live in `config/model-routing.yaml` and `adapters.md`.

## Contents

- [Why these four dimensions](#why-these-four-dimensions)
- [Scoring the dimensions](#scoring-the-dimensions)
- [The derived signal: reasoning_centric](#the-derived-signal-reasoning_centric)
- [Flags](#flags)
- [Bands and overrides](#bands-and-overrides)
- [Implementation tiers](#implementation-tiers)
- [Effort ceilings](#effort-ceilings)
- [Architecture routing](#architecture-routing)
- [Debugging policy](#debugging-policy)
- [The remaining classes](#the-remaining-classes)
- [Orchestrator policy](#orchestrator-policy)

## Why these four dimensions

Most bad routing decisions come from using the wrong signal. Token count, file
count, and diff size are the tempting ones because they are easy to measure,
and they are close to useless: they measure how much typing the task involves,
not how likely you are to get it wrong or how much it costs when you do.

The four dimensions split that question in two. **Complexity** and
**reversibility** describe what the change *is* — observable properties you can
read off the task. **Uncertainty** and **blast radius** describe what the change
might *cost* — and that is why they carry double weight in the score. Together
they approximate risk-adjusted expected loss, which is the thing you actually
want to route on.

Uncertainty is the single strongest escalation signal in the system. A task
nobody understands yet will consume more of a weak model's attempts than a task
that is merely large.

## Scoring the dimensions

### complexity — how hard is the implementation itself?

| | Meaning | Example |
|---|---|---|
| 0 | Mechanical, trivial | Rename a symbol across files |
| 1 | Straightforward | Add a simple REST endpoint |
| 2 | Multi-component, non-trivial | Refactor state across modules |
| 3 | Algorithmically or architecturally complex | Redesign a distributed state model |

### uncertainty — how unclear is the correct solution?

| | Meaning |
|---|---|
| 0 | The exact implementation is known |
| 1 | Minor ambiguity |
| 2 | Several plausible approaches |
| 3 | Requirements or root cause fundamentally unclear |

### blast_radius — how damaging is a mistake?

| | Meaning |
|---|---|
| 0 | Isolated, local |
| 1 | Limited subsystem |
| 2 | Broad product or system impact |
| 3 | Critical system, user, or business impact |

Areas that are naturally high blast radius regardless of how small the diff
looks: authentication, authorization, payments, database schema, save-data
format, production deployment, concurrency, security, irreversible migrations,
shared protocols, public APIs.

### reversibility — how easy is rollback?

| | Meaning |
|---|---|
| 0 | Trivial rollback |
| 1 | Easy rollback |
| 2 | Difficult rollback |
| 3 | Effectively irreversible, or data-changing |

## The derived signal: reasoning_centric

Several cells in the worker table read "frontier role", and there are two
frontier roles. `reasoning_centric` is what picks between them.

```
true   the bottleneck is deciding WHAT is correct
       logical verification, edge-case enumeration, spec consistency,
       concurrency and state proofs, discriminating root-cause hypotheses

false  the bottleneck is producing correct CODE
       multi-file edits, API surface work, refactoring mechanics,
       framework idiom, tool orchestration
```

Default `false` when genuinely ambiguous. Code-centric routing is cheaper, and
when it turns out to be the wrong call the recovery is a normal escalation
rather than a wasted frontier-reasoning pass.

This signal exists because the selection rule ("code-centric → senior engineer,
reasoning-heavy → reasoning specialist") is useless without an input that
distinguishes the two. Stating a rule whose input you never collect is how a
policy ends up with an unreachable branch.

## Flags

**Critical-domain** — these force band overrides:

```
security_sensitive   auth_sensitive   financial_sensitive   data_integrity_sensitive
```

**Elevating** — these raise the band or force a specific path:

```
concurrency_sensitive  migration  public_api_change
production_hotfix  unknown_root_cause  review_disagreement
```

**Operational** — state of the runtime, not a property of the task:

```
bridge_down  termination_unconfirmed
```

**Context** — these inform how you decompose the work and which model fits, but
never the band:

```
unfamiliar_codebase  cross_service_change  long_horizon  large_context
latency_sensitive  tool_heavy
```

`large_context` and `latency_sensitive` are the two the router itself acts
on: each rebinds `worker_balanced` to `worker_balanced_alt`, a same-tier
seat. The first because the primary's price doubles and its window runs out
past 200K input tokens; the second because a quality-tied measurement found
the alt faster on wall-clock p50 in every probed task type. Same band, same
review, different model — which is exactly what a context flag is allowed
to move. Carrying both flags is the same swap, not a conflict.

The distinction matters. Context flags describe the working conditions; letting
them move the band would inflate risk assessments for tasks that are merely
awkward rather than dangerous.

## Bands and overrides

```
risk_score = complexity + 2×uncertainty + 2×blast_radius + reversibility
```

| Band | Score | Ordinal |
|---|---|---|
| `LOW` | 0 – 3 | 0 |
| `MEDIUM` | 4 – 7 | 1 |
| `HIGH` | 8 – 10 | 2 |
| `CRITICAL` | 11 – 18 | 3 |

Bands are contiguous, exhaustive, and ordered, so `max(band_a, band_b)` is well
defined. Every downstream rule is expressed in bands. This is not a style
preference: mixing raw-score comparisons across sections is how a score of
exactly 10 ends up banding differently depending on which code path reached it.

Overrides run after the band is computed, unconditionally, for every task class:

```
any critical-domain flag                      → band = max(band, HIGH)
any critical-domain flag AND reversibility≥2  → band = CRITICAL
migration AND data_integrity_sensitive        → band = CRITICAL
production_hotfix                             → band = max(band, HIGH)
public_api_change                             → band = max(band, MEDIUM)
concurrency_sensitive                         → band = max(band, MEDIUM)
review_disagreement                           → disagreement path, any band
```

Bands may only be raised. An override that could lower a band would let a
convenient reclassification erase a safety control.

## Implementation tiers

The band table gives the answer; these tiers explain the shape of it.

| Tier | Trigger | Worker | Effort |
|---|---|---|---|
| **0 — Mechanical** | Rename, formatting, boilerplate, generated tests, DTO/schema mapping, repetitive edits | `worker_fast` | `LOW`–`MEDIUM` |
| **1 — Standard** | Ordinary feature, known pattern, clear acceptance criteria, band `LOW`/`MEDIUM` | `worker_fast` | `MEDIUM` (`HIGH` if multi-file) |
| **2 — Advanced** | Multi-module behaviour, moderately unfamiliar code, tricky state flow, non-trivial refactor, or `worker_fast` produced something incomplete | `worker_balanced` | `MEDIUM`–`HIGH` |
| **3 — Difficult** | Hard debugging, concurrency, complicated state, difficult tool orchestration, significant architecture interaction | `worker_balanced` / `senior_engineer` / `reasoning_specialist` | `HIGH`–`MAX` |
| **4 — Frontier** | Unresolved root cause, critical architecture, catastrophic blast radius, failed prior escalations, reviewer disagreement on critical work | `senior_engineer` / `reasoning_specialist` / `principal_architect` | `MAX` |

At Tier 3, `reasoning_centric` picks the lane: `false` → `worker_balanced` or
`senior_engineer`; `true` → `reasoning_specialist`.

## Effort ceilings

A model may declare an `effort_ceiling`: the highest conceptual level its CLI
will accept. The router does not rewrite the request. It keeps
`selected_effort` as the level the policy asked for, writes
`selected_effort_effective` as the level the worker will receive, and records
every seat that was actually capped in `effort_ceiling_applied`.

A cap that lands below a floor — `effort_floors.*` for the worker (against
the risk band), `review.<band>.effort` for a reviewer or judge (against the
promoted review band) — keeps the route executable and asks a human
(`effort_below_floor`). A level that came from the effort table alone is a
preference, not a floor, so capping it does not gate.

## Architecture routing

| Condition | Route |
|---|---|
| Band `LOW`/`MEDIUM`, constrained implementation-oriented design | `worker_balanced` / `HIGH` |
| Band `LOW`/`MEDIUM`, meaningful judgment required | `senior_engineer` / `HIGH` |
| Band `HIGH` | `senior_engineer` / `MAX` |
| Band `CRITICAL` | `principal_architect` |
| `uncertainty == 3`, any band | `principal_architect` |
| `long_horizon`, any band | `principal_architect` |

Additional `principal_architect` triggers, independent of band: a major
subsystem boundary change, a major migration, several viable architectures with
major tradeoffs, a large unfamiliar codebase, or an architectural choice that
will be difficult to reverse.

For `CRITICAL` architecture, consider adding a `reasoning_specialist`
adversarial review of the design *before* decomposing into implementation
work. Finding the flaw in the plan is far cheaper than finding it in the code.

## Debugging policy

Debugging escalates on **root-cause uncertainty**, not on bug severity. A
severe bug with an obvious cause is a cheap fix; a mild bug nobody can explain
is expensive.

```
worker_fast / HIGH
   ↓ root cause still unclear
worker_balanced / HIGH
   ↓ still unclear
senior_engineer / HIGH   or   reasoning_specialist / HIGH
   ↓ hypotheses conflict
senior_engineer + reasoning_specialist, independent analysis
   ↓ disagreement remains
principal_architect as judge
```

A retry at the same tier must bring at least one of: new evidence (a log, a
repro, a bisect result, a failing assertion), a materially different
hypothesis, or a stronger model. Without one of those, the attempt is not
permitted — escalate instead.

A `DEBUGGING` task carrying any critical-domain flag inherits the override:
band ≥ `HIGH`, worker ≥ `worker_balanced`, dual independent review. This is the
case an earlier version of the policy silently dropped, because it dispatched
on task class before checking flags.

## The remaining classes

| Class | Notes |
|---|---|
| `REFACTORING` | Route by band. Multi-system refactoring implies `HIGH`+ effort. Behaviour-preservation review is mandatory at band `HIGH`+. |
| `INVESTIGATION` | Frequently `reasoning_centric`. The output is a written finding, not a diff — review checks evidence quality, not code. |
| `MIGRATION` | Never routes below `worker_balanced`. Rollback review and migration tests are mandatory at band `HIGH`+. |
| `TESTING` | Generation is cheap (`worker_fast`). *Adequacy review* of generated tests is not — it belongs to the reviewer of the code under test, who knows what should have been covered. |
| `DOCUMENTATION` | Lowest tier, unless it documents a public API contract, in which case `public_api_change` puts it at band ≥ `MEDIUM`. |
| `OPERATIONS` | `production_hotfix` forces band ≥ `HIGH`. A rollback plan is a required output, not an optional finding. |

## Orchestrator policy

### The orchestrator seat is host-bound

The orchestrator policy is an advisory ask for the host session. The plugin
controls dispatch seats; the host is user-owned, and only the user can apply
`/model` or `/effort`. This advisory consumes that gap without pretending to
control the host. The orchestrator still classifies and decomposes, and the
reasoning-centric ladder below remains the authoritative distinction.

The v1 ask is deliberately finite:

<!-- ask-table:start -->
| condition | ask |
|---|---|
| default | worker_fast nominal / HIGH |
| uncertainty == 3 | ≥ worker_balanced |
| critical-domain flag AND uncertainty >= 2 | ≥ worker_balanced |
| ARCHITECTURE AND band(HIGH+) | ≥ senior_engineer |
| ARCHITECTURE AND uncertainty == 3 | ≥ principal_architect |
| routing_confidence < 0.60 | effort MAX |
| blast_radius >= 2 | effort MAX |
<!-- ask-table:end -->

Deferred from v1: stable-graph enforcement, architecture-in-decomposition,
the five-or-more-subtasks/conflict refinement, and the
ARCHITECTURE×CRITICAL (uncertainty < 3) cap.

#### Native effort to conceptual effort

Convert a declared native effort through the host-local family's `effort_map`:
the map is historical and ordered; if native spellings overlap, choose the
lower conceptual level. Claude has no distinct native MINIMAL tier, so the
documented `MINIMAL -> low` pair is an upward-collapse approximation: inversion
selects LOW rather than treating native `low` as proof of MINIMAL. xAI `xhigh`
maps to VERY_HIGH. Unknown or unavailable
native values are not silently converted; ask the host to declare a reliable
conceptual value or leave effort undeclared.

#### Utterance policy

**A — upgrade.** Emit `upgrade_recommended` immediately, once per decision,
when either axis is below its ask. Recommend `/model` only when
`model_comparison: below`, selecting the first registry row satisfying
`family == local_family ∧ dispatchable != false ∧ capability_tier ≥ ask.tier`,
sorted `(capability_tier asc, registry key asc)`. Do not compare prices within a
tier. If no candidate exists, or the declared model is unrecognized, delegate
the dispatch and make no `/model` utterance. A higher effort for an orchestrator
host is applied by the user with `/effort` or by delegating to a higher-effort
seat.

**B — effort unknown.** Ask for a host effort declaration only when the
`raised_by` list contains an effort-raising rule; otherwise remain silent.

**Downshift.** Require a session pattern and hysteresis before recommending a
lower seat. It is MUST to apply the common five-decision guard; the
`(a-effort)` and `(a-model)` utterances are SHOULDs, not guarantees. The
constant 5 is a policy constant in this document.

The orchestrator is the thing doing the classifying and decomposing. Default it
to `worker_fast` at `HIGH` effort — its job (classify, build the task graph,
detect dependencies, select workers and reviewers, notice escalation
conditions) is well served by high effort and does not need the frontier.

**Promote the orchestrator to `MAX` effort when:**

- the dependency graph is complex,
- architecture decisions are embedded in the decomposition itself,
- five or more interdependent subtasks exist,
- requirements conflict,
- routing confidence is below 0.60,
- failure would carry `HIGH`+ blast radius.

**Escalate the orchestrator past `worker_fast` when:**

- `uncertainty == 3`,
- `ARCHITECTURE` class at band `HIGH` or above,
- any critical-domain flag combined with `uncertainty >= 2`,
- `worker_fast` cannot produce a stable task graph across two attempts.

```
worker_fast/HIGH → worker_fast/MAX → worker_balanced/HIGH or senior_engineer/HIGH
  → principal_architect    (architectural ambiguity)
  → reasoning_specialist   (reasoning-heavy ambiguity)
```
