**English** | [한국어](./CHANGELOG.ko.md)

# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] — 2026-08-25 (grok seat integrity)

### Added

- Dispatches can declare a grok stdout envelope (`--output-envelope grok-headless-json-v1`): the turn's `stopReason` is graded, and only `end_turn` can be a success — a cancelled turn exits 0 and could previously be recorded as SUCCEEDED.
- Dispatches can require the files a seat was supposed to produce (`--require-artifact`, `--artifact-root`, `--require-artifact-sha256`, `--require-artifact-allow-unchanged`), proving existence, containment and a digest before SUCCEEDED — and proving the file is this attempt's work, not a leftover, against a baseline captured before launch. A required artifact must be the only name for its inode, and a baseline that cannot be read is treated as absence only on a confirmed `ENOENT` — every other read error is refused before the attempt starts.
- Dispatches can read the grok session directory (`--session-evidence`, `--session-id`) so a receipt records the effective agent, sandbox profile and served model alongside the requested ones, bound to the attempt by session id and launch time. Failed, timed-out and unconfirmed-termination receipts carry the same evidence, recorded but never graded, so the audit trail survives the outcomes that most need it.
- `--expect-effective-agent` and `--expect-sandbox-profile` refuse success when the effective agent or sandbox differs from what the seat asked for, so a write-capable seat cannot silently inherit a read-only default.
- Receipts carry `result.invalid_reasons`, naming why an attempt was rejected — a cancelled turn and an unusable evidence set are now distinguishable from a model that failed the work.
- `transports.*.to_xai` now carries a verified read-only grok reviewer seat recipe whose tool surface excludes the terminal, MCP meta-tool and web search.

### Changed

- `verify-evidence` rejects a `to_xai` receipt that carries no envelope or session evidence, and any receipt whose envelope did not end `end_turn`. Receipts written by 1.4.x are affected: verify an older evidence set with the version that produced it.
- A dispatch whose `--transport-id` ends `.to_xai` must declare the envelope and session evidence; a partial declaration is refused before the attempt starts.
- Grading now re-checks the deadline immediately before recording success, and the artifact hash is abandoned rather than allowed to run past it.

### Security

- Evidence files are opened without following symlinks and without blocking on a FIFO, and the artifact containment root is pinned before launch, so a supervised child cannot redirect the supervisor's reads or its own proof of work outside that root.
- A required artifact with more than one hard link is refused — before launch as a usage error, and during grading as `artifact_multiply_linked` with no digest recorded. Containment fences a path, but a write lands on an inode: a second name inside the root for a file outside it would otherwise be overwritten through a path the receipt called contained.
- A required artifact must still be the inode it was pinned to before launch. Sampling the link count at launch and at grading left the whole attempt in between unwatched: a child could hard-link an outside file at the required path, write through it, remove that name and leave a fresh single-linked file behind, and both samples would read `1` under a `contained: true` / `changed: true` SUCCEEDED. Every required path is now bound to one `(st_dev, st_ino)` before launch — an absent one by a reservation the supervisor creates and withdraws if the child never writes it — held open for the attempt and re-checked at grading as `artifact_identity_replaced`. This means a required artifact must be written **in place**: `rename`/`os.replace` over one installs a new inode and is refused. A required artifact that was never produced is still `artifact_missing`, and the supervisor still cannot stop an unconfined child from writing outside its root — only from being certified for it.
- Reading a session evidence directory can no longer cost an attempt its outcome. A post-open read error on `events.jsonl` or a `json` parse that raised something other than a decode error escaped the best-effort terminal collection and reached the crash handler, which relabeled an already-decided FAILED or TIMED_OUT receipt as CANCELLED / TERMINATION_UNCONFIRMED and exited 9. Both reads are now individually guarded and produce the same always-present null shape, with a new `session_evidence.unreadable` flag distinguishing "could not be read" from "was never written".
- The supervisor's own file I/O can no longer manufacture or erase artifact evidence. A short but successful `write` of the pre-spawn reservation was recorded as if the whole body had landed, so a child that produced nothing could be graded against a truncated marker and certified for it; the reservation is now written in a complete loop, and anything short of the whole body fails before launch (exit 2, no receipt, no claim, no leftover file). An error while withdrawing a reservation aborted the release loop, leaking the current and later descriptors and returning exit 9 over an already-persisted FAILED or TIMED_OUT receipt; release is now per entry, closes every pin in its own `finally`, and can no longer change a persisted outcome. And a failed `unlink` was ignored while the record was rewritten to `exists: false`, so a receipt denied a file that was still on disk; the record now follows the unlink, and the leftover is named by a new `artifact_reservation_cleanup_failed:<path>` reason alongside the unchanged `artifact_missing:<path>`.

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
