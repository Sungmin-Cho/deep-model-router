**English** | [한국어](./CHANGELOG.ko.md)

# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] — 2026-08-25 (grok seat integrity)

### Added

- Dispatches can declare a grok stdout envelope (`--output-envelope grok-headless-json-v1`): the turn's `stopReason` is graded, and only `end_turn` can be a success — a cancelled turn exits 0 and could previously be recorded as SUCCEEDED.
- Dispatches can require the files a seat was supposed to produce (`--require-artifact`, `--artifact-root`, `--require-artifact-sha256`, `--require-artifact-allow-unchanged`), proving existence, containment and a digest before SUCCEEDED — and proving the file is this attempt's work, not a leftover, against a baseline captured before launch.
- Dispatches can read the grok session directory (`--session-evidence`, `--session-id`) so a receipt records the effective agent, sandbox profile and served model alongside the requested ones, bound to the attempt by session id and launch time.
- `--expect-effective-agent` and `--expect-sandbox-profile` refuse success when the effective agent or sandbox differs from what the seat asked for, so a write-capable seat cannot silently inherit a read-only default.
- Receipts carry `result.invalid_reasons`, naming why an attempt was rejected — a cancelled turn and an unusable evidence set are now distinguishable from a model that failed the work.
- `transports.*.to_xai` now carries a verified read-only grok reviewer seat recipe whose tool surface excludes the terminal, MCP meta-tool and web search.

### Changed

- `verify-evidence` rejects a `to_xai` receipt that carries no envelope or session evidence, and any receipt whose envelope did not end `end_turn`. Receipts written by 1.4.x are affected: verify an older evidence set with the version that produced it.
- A dispatch whose `--transport-id` ends `.to_xai` must declare the envelope and session evidence; a partial declaration is refused before the attempt starts.
- Grading now re-checks the deadline immediately before recording success, and the artifact hash is abandoned rather than allowed to run past it.

### Security

- Evidence files are opened without following symlinks and without blocking on a FIFO, and the artifact containment root is pinned before launch, so a supervised child cannot redirect the supervisor's reads or its own proof of work outside that root.

### Removed

- The single `transports.*.to_xai.mechanism` string is replaced by per-seat recipes. No write-capable grok seat recipe ships in this release: measured on grok 1.0.5, a hard link inside the working directory defeats both the path-scoped write rules and `--sandbox workspace`, so a write through it reaches files outside the directory. Route write-capable work away from the xai worker until that is closed.

## [1.4.0] — 2026-08-20 (RouteObservationV1)

### Added

- A validator for RouteObservationV1 records: it checks the observation schema and can verify referenced files and dispatch receipts without copying raw producer output.

## [1.3.0] — 2026-08-20 (latency-sensitive binding)

### Added

- `latency_sensitive` now decides the same binding as `large_context`: a task carrying it routes `worker_balanced` to the Claude balanced seat. The 2026-08-20 B.1 repeat eval tied quality 15/18 and recorded lower sonnet median latency in every task type; default bindings and capability_tier are unchanged.

## [1.2.1] — 2026-08-20 (balanced-seat repeat eval)

### Changed

- The worker_balanced pair (grok-4.6 vs sonnet-5) was re-measured on 6 multi-file tasks × 3 repeats: quality remains tied at 15/18 each, and sonnet's median latency was lower in every task type, so a latency-sensitive routing rule is now a design candidate — not implemented; a 180K/230K follow-up cell (2 tasks × 1 × 2) also tied on quality. Bindings and capability_tier are unchanged.

## [1.2.0] — 2026-08-19 (evidence linkage)

### Added

- Every route now emits `request_sha256` and a deterministic `decision_fingerprint`; dispatch receipts can carry them (`--decision-fingerprint`, `--policy-sha256`, `--transport-id`, `--host-cli-version`) and `verify-evidence` checks them with `--expect-fingerprint` / `--expect-models`.
- Receipts declare requested-vs-served identity explicitly: `observed_model_id` / `observed_model_source` slots (honest defaults — no transport can observe the served model yet).
- `routing_confidence_kind` labels the confidence value as a heuristic gate score, not a calibrated probability.
- The registry records the OpenAI GPT-5.6 272K long-context tier with an explicit boundary vocabulary, standard cached-input rates for all seats, and ledger entries for the pricing boundary and Fable 5's provider-side substitution.
- Routes seating a model with a declared served-model caveat disclose the substitution possibility in their notes.

### Fixed

- `policy_sha256` now digests the policy actually in use, so a config injected through the library API no longer reports the on-disk digest, and a config mutated in place between routes is re-read rather than answered from a stale cache — one fingerprint identifies one decision.
- Model profiles no longer describe worker bindings' quality as unmeasured; the effort table no longer lists work types the policy removed; the xAI long-context boundary reads "at or above 200K".

## [1.1.1] — 2026-08-18 (binding quality probed)

### Changed

- The worker_fast and worker_balanced bindings' quality is now measured, not merely assumed: a discriminating 446-node hidden-test head-to-head found haiku marginally ahead of luna and a tie at ceiling for grok-4.6 vs sonnet-5. Both bindings and every `capability_tier` are unchanged — they continue to rest on the verified price advantage — and the verification ledger records the scores, failure modes, and latency.

## [1.1.0] — 2026-08-18 (design and pricing audit)

### Added

- `large_context` now decides a binding: a task carrying it routes `worker_balanced` to the Claude balanced seat, because the xAI seat re-bills the whole request at double rate past 200K input tokens and its window ends 500K earlier.
- The registry records the xAI long-context price tier and context window, and the model profile states the comparison as the conditional one it is.

### Fixed

- `local_policy` values are validated, not only their key names: an unknown effort level was reported as an applied floor while being silently ignored, and a non-numeric tier crashed with the status reserved for internal errors. Both are invalid input (exit 2) now.
- The CLI locator resolves in its documented order — env, root, Claude cache, Codex cache — instead of letting `.codex` win by alphabetical accident, picks the highest version by number rather than by string (`1.9.0` used to beat `1.10.0`), and refuses a source checkout on every tier rather than only the first.
- `dispatch_agent.py status` on an unknown attempt id answers with one sentence and exit 2, like `cancel`, instead of a traceback.
- Documentation drift against the policy file: the REVIEW/CRITICAL worker, the missing `concurrency_sensitive` override, and the missing `termination_unconfirmed` operational flag.
- README now states the PyYAML requirement and that the human-gate exit status is configurable over 3..255.

### Removed

- `review.MEDIUM.reviewer_count` and `review.MEDIUM.prefer_cross_family`: both were read and neither had any effect. The behaviour they described is unchanged and now documented as the constant it always was.
- Three `effort_by_work` entries nothing selected, and an unused `implementation_role` task field.

## [1.0.1] — 2026-08-17

### Fixed

- Corrected Claude Sonnet 5 list price to $2 / $10 after that introductory rate became the permanent published price.

## [1.0.0] — 2026-08-17

### Added

- First public release of the shared decision plane for Claude Code, Codex, and Grok.
- Skill-driven classification plus a deterministic scorer that emits a RouteDecisionV1 (band, worker, effort, review policy, honest independence).
- RouteRequestV1 file input, local-policy merge, and availability-aware fallbacks that never drop a HIGH or CRITICAL floor.
- Background dispatch supervisor with a wall-clock deadline, process-group kill ladder, and a completion receipt distinct from isolation evidence.
- Host-neutral CLI locator that refuses sibling source trees and personal skill symlinks.
- Public plugin surface matching the rest of deep-suite: bilingual README and CHANGELOG, CONTRIBUTING, SECURITY, and LICENSE.
