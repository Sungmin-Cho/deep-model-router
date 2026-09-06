**English** | [한국어](./CHANGELOG.ko.md)

# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- GPT-6 Astra for frontier reasoning, upper OpenAI-only roles, and architect fallback, with Sol retained for senior work and fallback.
- Model-specific native effort mappings so models without disabled reasoning receive `low` for a `MINIMAL` request.

## [1.13.0] — 2026-09-03 (two-axis routing)

### Added

- An execution-difficulty axis: `execution_score` (3×complexity + 2×uncertainty +
  `unfamiliar_codebase` + `tool_heavy` + `cross_service_change`) and `execution_band`
  (EASY / NORMAL / HARD / VERY_HARD) are computed alongside the risk score and
  emitted on every route, terminal ones included.
- `execution_selection`, a class × execution-band worker table. The worker is the
  stronger of that cell and what the class × risk-band table would have selected,
  compared on the resolved model's capability tier; the risk-band selection remains
  a floor, and the execution cell yields whenever adopting it would change the
  review or control contract for the worse.
- Effort floors `execution_HARD: HIGH` and `execution_VERY_HARD: VERY_HIGH`.
- `router.bands`, `router.score_weights`, `execution.bands` and `execution.score_weights`
  are validated for shape and contiguity at load.

### Changed

- A technically hard but isolated task now escalates its worker without touching
  its review depth: review depth still follows the risk band alone. No route
  receives a weaker worker than in 1.12.1, and on any route 1.12.1 could already
  route, none gains a terminal state or a human control it did not have. A route
  1.12.1 could *not* route can become routable — when the stronger worker meets a
  `local_policy` capability floor, or when it frees an independent reviewer pair
  that `INDEPENDENCE_UNAVAILABLE` had blocked. Such a route is newly executable
  rather than newly restricted, and it may carry controls the terminal one never
  reported (a terminal route states no review contract at all).
- On a MEDIUM route whose worker rose from the fast tier, the single reviewer is
  now chosen for that stronger worker by the existing cross-family rule.
- `unfamiliar_codebase` and `tool_heavy` are consumed by the execution axis; they
  were accepted and ignored before. `cross_service_change`, which already chose the
  `multi_system_refactoring` effort for REFACTORING, now feeds execution difficulty
  for every class.
- The annotated route inventory moved from `SKILL.md` to `references/control-loop.md`,
  which is now its only owner. The Codex plugin description names both axes.

## [1.12.1] — 2026-09-03 (an unambiguous citation)

### Fixed

- The D-14 ledger row names the directory its acceptance receipt lives in. Two
  receipts share that attempt id with opposite outcomes, and the failing one
  sits at the shallower path, so a reader chasing the bare id met it first.

## [1.12.0] — 2026-09-02 (grok-hosted Claude reviewer seat)

### Added

- `transports.grok.to_claude.mechanism_reviewer`: a separately verified,
  read-only Claude reviewer recipe using `--permission-mode plan`,
  `--allowedTools Read,Glob,Grep,LS`, and `--strict-mcp-config`. The general
  mechanism remains unchanged and write-capable.

### Changed

- The dispatch contract records how a headless grok host can launch the
  supervisor without mistaking a bare-echo allow path for proof, and why a
  sandboxed outer host can hide the Claude child's keychain.
- Claude bridge boot savings are documented as environment-dependent: the
  strict flag removed about 80% when global MCP connectors actually
  handshook, while an earlier grok-host probe saved about 10% because those
  schemas were absent. That earlier ~10% result was not reproduced — the
  re-probe from a grok host measured the 80% figure instead — and its
  original cause remains unresolved.

### Fixed

- Claude transport ledger matching and documentation fences are now
  direction- and seat-specific, so a general mechanism row cannot silently
  vouch for a reviewer mechanism whose argv differs.

## [1.11.1] — 2026-09-02 (why the artifact pin stays)

### Changed

- The dispatch contract records that the artifact identity pin also refuses a
  second laundering sequence needing no hard link — write outside the root,
  rename in — and that no pre-spawn audit of the child's tree can stand in for
  it. The verification ledger carries why a content-certification mode was
  designed, implemented, reviewed and withdrawn, so the standing guidance is
  unchanged: certify a Claude seat's output by a caller-side content hash
  beside the receipt, and do not declare `--require-artifact` for such a seat.

## [1.11.0] — 2026-09-02 (Claude envelope, stricter verdicts)

### Added

- `--output-envelope claude-print-json-v1` grades a `claude -p --output-format
  json` document through the same gate as the grok format, reading that
  format's own key names and requiring its `type`, `subtype` and `is_error`
  discriminators to agree that the turn finished.
- A receipt's envelope carries `usage`, the child's own token accounting, so a
  caller measuring context or boot cost reads the receipt instead of scraping
  the retained stdout. Both formats carry counts; only finite non-negative
  numbers are kept.
- A receipt records the `verdict` it parsed and whether it was
  `verdict_recovered`; `verify-evidence` notes a recovered one.

### Fixed

- `verdict: PASS | PASS_WITH_CHANGES | FAIL` — the format a review prompt
  quotes — is no longer accepted as a verdict, including at line start where it
  always was. A seat that echoed the instructions and reviewed nothing no
  longer grades as having reviewed.
- A verdict that a headless format ran into the narration in front of it is no
  longer graded as no verdict at all, on the envelope and plain-stdout paths
  alike. The last unanchored verdict counts when the schema's second field is
  on the next line and in range.
- A `.to_xai` dispatch may no longer declare another vendor's envelope format,
  and `verify-evidence` refuses a receipt that did.

## [1.10.1] — 2026-09-02 (registry-id provenance)

### Changed

- The verification ledger records that `claude-haiku-4-5-20251001` and
  `claude-haiku-4-5` resolve to different served models, so the registry keeps
  the dated pin rather than dropping the suffix.

## [1.10.0] — 2026-09-02 (honest promotion record, bound write seats)

### Fixed

- A route promoted for low routing confidence no longer contradicts itself.
  Promoting the review band reseats reviewers, which can retire the fallback
  whose penalty triggered the promotion; the route now carries a note saying the
  promotion was decided on the pre-promotion plan and that the reported
  confidence is the promoted plan's, naming both numbers. The band still stands.

### Added

- A direction that declares `write_verified: true` without a maker seat now
  needs a verified ledger row naming it, and the router refuses to load a config
  where one is missing. Four directions were authorising write dispatch on
  nothing but a non-empty recipe string.
- The dispatch contract records that a Claude seat's output cannot be certified
  with `--require-artifact` — its file tools install content on a new inode,
  while an OpenAI seat truncates in place — and says to certify such a seat by a
  content hash recorded beside the receipt instead.

## [1.9.0] — 2026-09-02 (Fable 5.1, lean Claude bridge seats)

### Changed

- The grok host's Claude bridge seats — reviewers and workers alike — no
  longer load the user's global MCP servers (`--strict-mcp-config`); a caller
  that needs one adds `--mcp-config <file>` immediately after `-p`. The same
  recipe was probed from a nested `codex exec` turn under grok, not from an
  interactive Codex session.
- `principal_architect` is Claude Fable 5.1. Claude Fable 5 stays valid as
  history input (`--prior-models`, `--unavailable-models`, `--host-model`)
  and is never seated.
- A single fallback no longer raises the review band by itself. When both
  the old and new penalty still record a fallback, `routing_confidence` is
  higher by 0.04.

### Added

- A route whose declared host model is not in the registry carries a note
  saying the registry may be stale.

## [1.8.0] — 2026-09-01 (grok maker seat)

### Changed

- Claude Code write routes seat `xai_frontier` again. The skip note is
  replaced by a disclosure that dispatch must use
  `--seat-profile grok-maker-v1`. Codex-hosted write routes still skip xai.

### Added

- **Claude Code → xAI write-capable maker seat**, under supervisor
  prevention. `transports.claude_code.to_xai.mechanism_maker` plus
  `write_verified: true` plus a `verified` ledger entry. The argv is not
  containment: path-based sandbox still cannot tell a hard link from the
  file it names. Dispatch MUST use `--seat-profile grok-maker-v1`.
- `dispatch_agent.py`: `--child-cwd` / `--require-single-linked-cwd` (refuse
  spawn if any regular file in the child tree has `st_nlink > 1`);
  `--grok-home` / `--grok-auth-seed` (attempt-private home, auth copied onto
  a new inode); `--expect-sandbox-enforced` (grade
  `$GROK_HOME/sandbox-events.jsonl` `ProfileApplied.enforced`);
  `--seat-profile grok-maker-v1` (all of the above, plus envelope and
  session evidence, or pre-spawn refusal).
- The same maker argv is mirrored on `codex.to_xai` but that direction
  stays `verified: false` / `write_verified: false`.

### Security

- Hard-link escape remains possible at the grok argv: a planted alias
  inside cwd still overwrites the outside inode, including under a custom
  profile that denies the outside path (grok 1.0.13 / Darwin arm64). The
  shipping claim is supervisor prevention plus a tools whitelist that
  cannot `ln`, a per-attempt `GROK_HOME` (workspace write grants follow
  `$GROK_HOME`, not `~/.grok`), a fail-closed custom profile, and a deny
  rule on the events log so the model cannot forge `ProfileApplied`.

## [1.7.0] — 2026-09-01 (write-seat routing)

### Changed

- **A route whose worker has to write is no longer staffed by a model this
  host has no write-capable recipe for.** The router now reads the
  `transports` table: a direction that ships only a read-only seat cannot fill
  a write seat. On the Claude Code and Codex hosts this moves the xai worker
  off write-capable work, which callers were previously doing by hand with
  `--unavailable-models` — stop doing that for this purpose, it withholds the
  model from the review seats too. Nothing changes for a host's own family
  (the native seat needs no recipe), for read-only seats, or on routes
  declared `read_only`.
- Restoring a direction takes three things together: the maker recipe, the
  direction's `write_verified: true`, and a verification-ledger entry recording
  the probe. The coupling is a lookup, not a rule about a provider — but a
  recipe added without the probe authorizes nothing.

### Added

- `task_write_seat`: the class default for whether a route's worker writes.
  Fail-closed — only the two classes whose worker output is a judgement
  (`REVIEW`, `INVESTIGATION`) default to `read_only`.
- `--worker-seat write|read_only` and RouteRequestV1 `worker_seat` override
  that default per route, in both directions. An undeclared seat hashes
  exactly as it did before the field existed.
- `worker_seat` block on every route: the kind applied, where it came from,
  and the families this host can dispatch write work to. When the requirement
  moved the seat, an id-free note records it — in `notes`, as the binding
  decision it is, never in `fallbacks_applied`, so it does not spend routing
  confidence or promote a review band.
- `write_verified` on each transport direction: the sole authorization for
  cross-family write dispatch. `verified` attests the direction — for `to_xai`
  it attests the reviewer seat while the ledger records the maker seat as not
  shipped — so it never authorized write work on its own. Policy refuses a
  `write_verified: true` that no write-capable recipe backs or that the
  verification ledger contradicts, and refuses a `degraded_binding` that makes
  a bridged family look native.

### Security

- The grok maker seat stays unshipped, now on measured 1.0.13 evidence rather
  than 1.0.5's. Re-probed 2026-09-01 on grok 1.0.13 / darwin with the same
  shipping-candidate argv: a hard link inside the working directory still
  overwrites the external inode it aliases, and still does so under
  `--sandbox strict` — a path-based sandbox cannot tell a hard link from the
  file it names, and the kernel records no violation because the path used is
  genuinely inside the workspace. Two further containment facts are now on
  record: `~/.grok` is writable under both profiles (config, sandbox and
  trusted-folder state included; only the hooks paths are protected, and by a
  cancelled turn rather than a kernel denial), and the session summary reports
  a requested sandbox profile with no evidence it was enforced.

## [1.6.0] — 2026-09-01

### Added

- Optional `host_seat` declaration (`--host-model` / `--host-effort`,
  RouteRequestV1 `host_seat`) — the router reports how the host session
  compares to the orchestrator profile the policy asks for.
- `host_seat_advisory` block on every route: the orchestrator ask
  (tier/effort/raised_by) and declared-host comparisons; an
  `upgrade_recommended` advisory and an id-free note when the host is
  below the ask. The route itself never changes.

### Changed

- `router.default_orchestrator` / `default_orchestrator_effort` are now
  read and validated by the router (previously caller guidance only).

## [1.5.1] — 2026-08-29 (grok-hosted bridge verification)

### Changed

- `grok.to_claude` and `grok.to_openai` are now verified from a Darwin grok
  host: trivial round-trip, the reviewer-seat models addressed, and two
  concurrent read-only reviewer seats completing with schema-valid verdicts
  under the dispatch supervisor, with distinct receipt attempt ids as the
  isolation evidence. The verification ledger records the host platform,
  CLI versions, and date. A grok-hosted dual review over these bridges is
  no longer built on an assumption.
- Grok native subagent isolation remains unverified and is deliberately
  untouched by this probe: a grok-native dual review is still degraded
  unless both reviewers run as separate processes.

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
