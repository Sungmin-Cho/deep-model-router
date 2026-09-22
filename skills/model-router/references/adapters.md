# Runtime adapters

Read this when a model is unavailable, when you are invoking across the
provider bridge, or when you need the concrete command syntax.

## Contents

- [Two layers](#two-layers)
- [Capability preflight](#capability-preflight)
- [Effort mapping](#effort-mapping)
- [Transports](#transports)
- [Dispatch contract](#dispatch-contract)
- [Fallback matrices](#fallback-matrices)
- [Disclosing degradation](#disclosing-degradation)

## Two layers

**Layer A — routing policy.** Provider-neutral. Contains no command, no model
identifier, no effort string. Takes a classified task, returns a role, an
effort level, a review policy, an escalation policy, a rationale, and a
confidence.

**Layer B — this file.** Maps role aliases to concrete models and conceptual
effort to provider values. Owns invocation syntax, session management, and
isolation mechanics.

Keeping the layers apart is what lets the same policy drive every runtime and
survive model renames. Any provider-specific string that leaks into Layer A is
a bug in the policy, not a shortcut.

## Capability preflight

Before the first route in a session, establish what you can actually invoke:

```yaml
capabilities:
  runtime: claude_code | codex | grok | unknown
  available_families: [claude, openai, xai]
  available_models: []          # resolved ids only
  effort_control: native | approximate | none
  effort_values: []             # values the CLI actually accepts
  cross_provider: available | unavailable | untested
  subagent_isolation: available | unavailable
```

Probe in this order, stopping at the first success per field:

1. **Runtime-native listing** — whatever the host exposes for enumerating models.
2. **Single cheap call** — one minimal-token request per candidate id; a
   resolution error marks the alias unavailable.
3. **Static allowlist** — the `verified: true` registry entries as a floor.

Rules:

- An alias whose model does not resolve is **unavailable** and falls back. It
  must never cause a hard failure.
- Cache the probe result for the session. Re-probe if any call fails with a
  model-resolution error.
- If `cross_provider` is `unavailable` or `untested`, use the single-provider
  binding. Never emit a route you cannot execute.
- If `subagent_isolation` is `unavailable`, degrade dual review per
  `review-policy.md` and say so in the rationale.

This step exists because the most consequential failure a router can have is
naming a model nobody verified exists. The route looks fine; it just cannot run.

## Effort mapping

Conceptual levels are ordered:

```
MINIMAL < LOW < MEDIUM < HIGH < VERY_HIGH < MAX
```

The map is keyed by the **model's family**, not by the host runtime. The value
is the token that family's CLI will accept. A session hosted on one runtime
routinely dispatches a model of another family; looking the spelling up under
the host used to emit a token the target CLI would reject.

### claude family

Accepted values: `low | medium | high | xhigh | max`.

```yaml
MINIMAL:   low        # no distinct minimal tier; collapses upward
LOW:       low
MEDIUM:    medium
HIGH:      high
VERY_HIGH: xhigh
MAX:       max
```

### openai family

Accepted values: `none | low | medium | high | xhigh | max`. Verified by probing
each value against the installed CLI — **`minimal` is rejected**, `none` is
accepted.

```yaml
MINIMAL:   none
LOW:       low
MEDIUM:    medium
HIGH:      high
VERY_HIGH: xhigh
MAX:       max
```

These are family defaults. Overlay a resolved model's `effort_map` from the
registry before dispatch (or use `Policy.native_effort(model, effort)`). The
OpenAI frontier maps `MINIMAL` upward to `low` because it cannot disable
reasoning. Apply this after the model's effort ceiling, including when the
model fills a fallback role; use `selected_effort_native` for the worker.

Set it with `-c model_reasoning_effort=<value>`.

### xai family

Accepted values: `low | medium | high | xhigh`. There is no minimal or none
tier — reasoning cannot be disabled — and the CLI rejects `max`. The family
tops out at `xhigh`.

```yaml
MINIMAL:   low
LOW:       low
MEDIUM:    medium
HIGH:      high
VERY_HIGH: xhigh
MAX:       xhigh      # unreachable: the clamp runs before this lookup and
                      # the only xai model's ceiling is VERY_HIGH
```

### When effort control is unavailable

If `effort_control: none`, approximate in this order of preference:

1. Route one model tier higher.
2. Give explicit reasoning instructions in the prompt.
3. Decompose the task into smaller verified steps.
4. Increase verification depth — more checks, not more retries.
5. Constrain the token budget.

Record `effort_control: approximate` in the metrics so later analysis can
account for it.

## Transports

Each runtime has a native subagent and CLI bridges to the other families.
Verification is per direction, not a blanket "the bridges work":

- Claude Code → openai (`codex exec`) — verified
- Codex → claude (`claude -p`) — verified
- Claude Code → xai (the grok reviewer seat profile) — verified
- grok → claude, grok → openai (`claude -p` / `codex exec` from a grok
  host) — verified (darwin grok host, 2026-08-29; see the ledger)
- Codex → xai — assumed (same mechanism, the hosted direction was not
  probed)

**Passing the prompt.** A reviewer's prompt contains the diff, and diffs
contain quotes, backticks, `$`, and newlines. Build argv programmatically —
never interpolate a prompt into a shell string. Prefer file delivery: write
the prompt to a file and feed it to the child's stdin (`codex exec` reads
the instruction from stdin when the prompt argument is `-`;
`scripts/dispatch_agent.py --prompt-file` does this for any transport). A
CLI that only takes a positional prompt gets it as a single argv element.

**Pin a non-interactive permission mode before a background launch.** A
background bridge has no TTY, so an approval prompt is a hang that looks
exactly like a slow model. Decide the mode up front, pass it explicitly
(`--permission-mode` / `-s <sandbox>` / the grok approval flags), and record
it in the dispatch receipt. The far side of a bridge keeps its own
sandbox/approval config — verify the effective mode at preflight. For grok
the mode is the weakest of these levers, and "Grok seat profiles" below
explains what carries the control instead.

**Fences mirror the YAML.** Every command fence in this section is Layer B's
prose rendering of the matching `transports` mechanism in
`config/model-routing.yaml` — the same argv shape, token-for-token (modulo
line wrapping for readability); a `codex exec` fence carries `-s <sandbox>`
and `--skip-git-repo-check` wherever the YAML mechanism does, and a mismatch
between the two is a bug, not an intentional variant.

### Claude Code

**Native:** the `Agent` tool. Context isolation is the enforcement boundary for
independent review.

**To openai models:**

```bash
codex exec -m <id> \
    -c model_reasoning_effort=<effort> \
    -s <sandbox> \
    --skip-git-repo-check \
    "<prompt>"
```

`<sandbox>` is `read-only` or `workspace-write`.

Runs as a separate process with a fresh session — isolation holds by
construction.

**To xai models:** one string per SEAT, not one per transport — see "Grok
seat profiles" below for why. The reviewer seat is what this release ships:

```bash
grok --no-auto-update -m <id> --effort <native-effort> \
    --output-format json -s <fresh-uuid> \
    --permission-mode plan \
    --tools read_file,list_dir,grep --deny MCPTool \
    --disable-web-search --sandbox read-only \
    --prompt-file /dev/stdin
```

`<native-effort>` is one of `low`, `medium`, `high`, `xhigh` — the family's
native token from the effort map above, never the conceptual level.
`<fresh-uuid>` is the same value the supervisor gets as `--session-id`.

The maker seat string in this release is the `mechanism_maker` on
`claude_code.to_xai` (mirrored on `codex.to_xai` but not authorized there).
Argv alone is not containment. Dispatch MUST use
`--seat-profile grok-maker-v1` so the supervisor applies the disposable
single-linked `--child-cwd`, the per-attempt `--grok-home` / `--grok-auth-seed`,
the custom `dmr-maker-v1` profile, and `--expect-sandbox-enforced`. See
"Grok seat profiles".

Also a separate process with a fresh session.

**Aliases float; the registry does not.** The Claude Code `Agent` tool takes
only family aliases (`fable`, `opus`, `sonnet`, `haiku`) and always serves
the newest model of that family, so a native seat can run a newer model than
the registry key it was routed as. The registry id is honoured only over the
`claude -p` bridges. When a bridge receipt's `served_models` (grok envelope)
or the host advisory's `model_comparison: unrecognized` disagrees with the
registry, the registry is stale: bump the id, re-probe, and keep the retired
id as a non-dispatchable history row. Never register an alias — price, tier,
effort ceiling and the verification ledger are per concrete model.

### Codex

**Native:** the `multi_agent` feature (stable, enabled). Its context-isolation
semantics have **not** been verified to match the Claude `Agent` tool's. Until
they are, either confirm isolation on the first dual review of a session, or
run both reviewers as separate `codex exec` processes.

**To claude models:**

```bash
claude -p --model <id> \
    --effort <effort> \
    --permission-mode <mode> \
    --strict-mcp-config \
    "<prompt>"
```

Also a separate process with a fresh session. `--effort` is required: without
it the band's level does not cross the bridge.

**To xai models:** the same seat strings as above — argv does not depend on
the host. Verification does: the seat probes ran from a Claude Code host, so
the Codex-hosted direction stays assumed and the YAML keeps `verified: false`
for it.

### Grok seat profiles

Grok is the one bridge where the transport needs **two** machine strings, and
where the permission mode is not the control. What follows is the probe
ledger's summary; it was measured on grok 1.0.5 (5115b46bc909), darwin,
2026-08-25. These findings are machine- and platform-specific — re-probe on
any other platform before relying on them.

**A cancelled turn exits 0.** A headless grok run has no TTY, so a tool call
that needs approval it cannot prompt for is *cancelled*, ending the turn with
`stopReason: "cancelled"` — and the process still exits 0. Nothing about the
exit status distinguishes that from a finished turn, which is why every grok
dispatch declares `--output-envelope grok-headless-json-v1` and the
supervisor grades the stdout document instead. `end_turn` alone is a success
candidate; everything else fails closed.

**Cancellation is not a function of the mode alone.** It is a function of
(mode x tool surface x the built-in read-only command list x allow/deny rules
x which tool the model happens to reach for). In the same `plan` mode `ls`
was approved from the built-in list while `echo` cancelled the turn. No
recipe can promise "this never cancels", which is exactly why detection lives
in the supervisor and not in the recipe.

**Deny survives; ask cancels.** A call blocked by a `--deny` rule returns a
refusal to the model and the turn continues. A call with no matching rule has
to ask, cannot, and kills the turn. So the reviewer strategy is not to deny
the dangerous tools but to remove them from the model's view entirely with a
`--tools` whitelist — and `--deny MCPTool` closes the MCP meta-tool that
`--tools` leaves behind.

**`--permission-mode` is accepted for Claude compatibility.** Treat it as a
label, not a boundary. The documented `acceptEdits` ("file edits without a
prompt") did not authorize a headless write in 1.0.5 — an explicit
`--allow` rule did — and the documented "deny and continue" of `dontAsk`
cancelled the turn instead. Where the docs and the CLI disagreed, this
ledger follows the CLI.

**The prompt leaves argv.** `--prompt-file /dev/stdin` alone triggers
headless mode (no `-p` needed), and the supervisor wires its own
`--prompt-file` to the child's stdin. Note this was confirmed for a
regular-file fd, not only a pipe — the official docs state a piped stdin is
*not* read as a prompt, and the production path is a regular file.

**The reviewer seat ships.** `--tools read_file,list_dir,grep` is sufficient
for a review prompt, and the full recipe including `--deny MCPTool`,
`--disable-web-search` and `--sandbox read-only` completes with a verdict.

**The maker seat ships on `claude_code.to_xai` only, under supervisor
prevention.** Path-based sandbox still cannot tell a hard link from the
file it names (1.0.5 and 1.0.13, workspace and strict, custom deny of the
outside path included). What closed the three axes on darwin/arm64 grok
1.0.13:

- **A.** `--require-single-linked-cwd` on a disposable `--child-cwd` refuses
  spawn if any regular file has `st_nlink > 1`. The shipped argv's
  `--tools` whitelist has no `run_terminal_cmd`, so the child cannot `ln`;
  Write/Edit create a regular file, not a hard link.
- **C.** `--grok-home` injects an attempt-private home. Workspace write
  grants follow `$GROK_HOME`, not `~/.grok`. Seed `auth.json` onto a new
  inode with `--grok-auth-seed`.
- **D.** `--sandbox dmr-maker-v1` is a custom profile (fail-closed if it
  cannot apply). `--expect-sandbox-enforced` grades
  `ProfileApplied.enforced == true`.
  `--deny "Write(<grok-home>/…/sandbox-events.jsonl)"` (and Edit) blocks
  model forgery of that log while the grok logger still appends. Measured,
  per location: the root path on 1.0.13, both paths on 1.0.40. The deny is a
  permission-policy control, not a kernel one.

**The events log moved, so it has two addresses.** grok 1.0.13 wrote
`$GROK_HOME/sandbox-events.jsonl`; 1.0.40 writes
`$GROK_HOME/sessions/sandbox-events.jsonl`. The supervisor reserves both
before spawn and reads both at grading, taking a disagreement between two
present records as a failure rather than a vote. Every location it reads is
also denied in the argv below: a log the supervisor grades but leaves
writable is a forgery channel, so the denied set and the supervisor's
location table are held equal by test. A future move degrades to
`sandbox_event_missing` — the seat fails closed, which is how 1.0.40 was
found.

**Reserving the name is not reserving the log.** Each reservation is pinned
by `(st_dev, st_ino)`, as `--require-artifact` pins its own, because the deny
rule is a permission-policy control that stops an append and not an
unlink-and-recreate: a fresh single-linked regular file in the same name
reads exactly like the logger's. A location that is gone at grading is
`sandbox_event_missing`; one that is there but is not the reserved inode is
`sandbox_event_identity_replaced`. Two present records that disagree are
`sandbox_event_disagreement`, with both records in the receipt. grok 1.0.40
appends to the reserved inode, so the pin costs a real run nothing.

```bash
grok --no-auto-update -m <id> --effort <native-effort> \
    --output-format json -s <fresh-uuid> \
    --agent general-purpose --permission-mode acceptEdits \
    --tools read_file,list_dir,grep,search_replace \
    --disallowed-tools Agent --no-subagents \
    --allow "Write(./**)" --allow "Edit(./**)" \
    --deny MCPTool \
    --deny "Write(<grok-home>/sessions/sandbox-events.jsonl)" \
    --deny "Edit(<grok-home>/sessions/sandbox-events.jsonl)" \
    --deny "Write(<grok-home>/sandbox-events.jsonl)" \
    --deny "Edit(<grok-home>/sandbox-events.jsonl)" \
    --disable-web-search --sandbox dmr-maker-v1 \
    --prompt-file /dev/stdin
```

`--seat-profile grok-maker-v1` is what binds the supervisor flags to this
argv. A raw paste of the child command without those flags is outside the
shipping claim. Substitute `<grok-home>` in the deny rules with the same
absolute path passed as `--grok-home`; a literal `<grok-home>` token does
not protect the events file. `codex.to_xai` carries the same argv and stays
`write_verified: false`. The ledger row names
`transports.claude_code.to_xai.mechanism_maker` — it does not authorize
the Codex direction.

**Rule arguments must be quoted.** `Write(./**)` unquoted is a shell syntax
error — the parentheses are metacharacters. The YAML and these fences carry
the quotes for that reason.

**`bypassPermissions` / `--always-approve` are not recipe defaults.** Both
bypass approval wholesale, and both can be disabled machine-wide by an
administrator lock (`disable_bypass_permissions_mode`), so a YOLO fallback is
not even available everywhere. Use them only in a disposable isolated
worktree where the caller explicitly accepts the risk.

**A headless grok host must authorize the supervisor command itself.** On
grok 1.0.13, `acceptEdits` and `dontAsk` cancelled before spawning
`python3 dispatch_agent.py`; a bare `echo` was an auto-approved special case
and therefore not a valid control. The controller invocation that carried the
full supervised grok-to-Claude probe used `bypassPermissions` with sandboxing
off. That is what was run, not what is required: short `python3` probes on the
same host also passed the same gate under `auto`, and under `acceptEdits` with
`--allow "Bash(python3)"`. Neither narrower mode was exercised end-to-end with
the full supervisor, so they are a measured opening rather than a recommended
recipe. Wrapping that outer host in the built-in workspace sandbox blocked the
Claude child's keychain and produced `Not logged in` even without `--bare`.

The child's receipt does not observe the outer host's permission mode — see
"the effective permission mode is not observable at all" below — so no receipt
here proves which mode the controller ran under. Nor was the acceptance run
contained: its `--child-cwd` was a throwaway `/private/tmp/d14-supervisor-made-unsandboxed`,
not a worktree, and it ran with `require_single_linked_cwd: false`, so the
pre-spawn single-link audit never executed. This is a host-launch recipe, not
a transport mechanism and not a containment claim.

#### Deriving the session evidence directory

The supervisor never derives this path — the caller declares it as
`--session-evidence grok-session-v1:<dir>`. The derivation is Layer B
knowledge, and the layout is officially documented:

```
$GROK_HOME/sessions/<URL-encoded-cwd>/<session-uuid>/
```

`$GROK_HOME` defaults to `~/.grok`. The **cwd is the grok child's cwd**.
With `--child-cwd` the supervisor passes that directory as `Popen(cwd=)`;
without it the child inherits the supervisor's cwd. Maker dispatches must
pass `--child-cwd` so the session path and the nlink audit name the same
tree. Encode the child's realpath (`/tmp` vs `/private/tmp` on macOS).
`<session-uuid>` is the value given to grok's `-s` — pass the same value to
`--session-id`, which is what binds the evidence to the attempt.

**Keep the dispatch cwd short.** If the URL-encoded cwd exceeds 255 bytes the
layout falls back to a slug+hash directory with an inner `.cwd` file, and
simple derivation stops working. Orchestrator scratch and worktree paths can
get long, so treat "encoded cwd ≤ 255 bytes" as a Layer B preflight rule.
Past that, discover the directory by matching `.cwd` inside the group
directory after the turn, or leave the seat fail-closed — a failed derivation
surfaces as `session_evidence_unreadable`, never as a silent success.

#### What the evidence can and cannot show

`summary.json` carries `agent_name` (an officially documented field) and
`info.id`; `sandbox_profile` is unofficial but present in practice, and the
supervisor records it and gates on it only when the caller declares
`--expect-sandbox-profile`. `events.jsonl` is **not** in the documented
layout, so its terminal `turn_ended` is recorded opportunistically and never
gates.

Session evidence is **recorded on every terminal receipt, graded on one.**
`FAILED`, `TIMED_OUT` and `TERMINATION_UNCONFIRMED` are decided by exit
status or termination alone, and nothing read from the session directory
relabels them — but that is precisely where the effective agent, the
effective sandbox profile and the turn's cancellation category explain what
happened, so the receipt carries them there too. Collection is bounded and
best-effort: a session directory that was never written leaves the same
always-present shape full of nulls rather than costing the attempt its
receipt. So does one that could not be READ — a post-open read error, or a
`json` parse that raises something other than a decode error, is an absence
of evidence and never an attempt outcome. `session_evidence.unreadable` is
how the receipt tells those two absences apart; it records, and like the
rest of this collection it gates nothing.

The **effective permission mode is not observable at all.** grok 1.0.5
records it nowhere — not in stdout, not in `summary.json`, not in
`events.jsonl`. The effective agent name, the effective sandbox profile and
the cancellation event are the most a receipt can prove about applied policy,
and that is the boundary of the "requested vs effective" evidence this
adapter can offer.

### grok

**Native:** the CLI's `--agents` / `--no-subagents` surface. Isolation
semantics have **not** been verified. Until they are, treat a grok-native dual
review as degraded unless both reviewers run as separate processes.

**To claude models:** two `claude -p` strings, one per seat. The general
write-capable seat keeps the permission-mode slot (`write_verified` hangs
on this string):

```bash
claude -p --model <id> \
    --effort <effort> \
    --permission-mode <mode> \
    --strict-mcp-config \
    "<prompt>"
```

The reviewer seat is read-only: `--permission-mode plan` and
`--allowedTools Read,Glob,Grep,LS`, closed by `--strict-mcp-config` so the
variadic list cannot swallow the positional prompt:

```bash
claude -p --model <id> \
    --effort <effort> \
    --permission-mode plan \
    --allowedTools Read,Glob,Grep,LS \
    --strict-mcp-config \
    "<prompt>"
```

`--strict-mcp-config`
drops every MCP server the user's global config would otherwise load into the
seat (measured at ~80% of a headless seat's boot context); reviewers and
workers alike lose user-global MCP servers. A caller that needs one adds
`--mcp-config <file>` **before another option, never last** — it is variadic,
so it consumes values until the next option, and left open at the end it eats
the positional prompt (`-p` is the natural place) — and that call is a variant
the ledger does not vouch for. Verified from a darwin grok host (2026-08-29
transport; 2026-09-02 this string) and from a nested Codex host (2026-09-02).
A positional prompt placed after a variadic `claude` flag (`--allowedTools`,
`--add-dir`) is swallowed by that flag; deliver via `--prompt-file` or place
the prompt after only fixed-arity flags. Dispatched under `dispatch_agent.py`
with `--output-schema review` and a read-only permission mode; separate
process by construction, and the receipt `attempt_id` is the
isolation-evidence id.

The savings are environment-dependent because the flag can remove only MCP
schemas that actually loaded: the Claude Code host and an unsandboxed grok
host measured about 80% (64,012 to 13,490 tokens in the latter), while an
earlier grok-host probe measured about 10% because those schemas were absent.
That earlier ~10% figure was **not reproduced**. The 2026-09-02 re-probe from
a grok host landed on the 80% result instead, and nothing since has produced
the 10% one again, so its original cause is recorded rather than explained:
read the low number as a possibility this flag has on some hosts, not as a
second measurement standing beside the first.

**To openai models:**

```bash
codex exec -m <id> \
    -c model_reasoning_effort=<effort> \
    -s <sandbox> \
    --skip-git-repo-check \
    "<prompt>"
```

Verified from a darwin grok host (2026-08-29): dispatched under
`dispatch_agent.py` with `--output-schema review` and `-s read-only`;
separate process by construction, and the receipt `attempt_id` is the
isolation-evidence id.

A grok-hosted dual review is certifiable exactly when both reviewers run
over these two bridges as supervised separate processes with distinct
receipt attempt ids; the native `--agents` surface remains outside that
claim and a review built on it stays degraded.

### Confirming a transport

Before setting `cross_provider: available`, confirm three things:

1. The transport exists and can execute a trivial round-trip.
2. You know which models it can address.
3. Reviewer isolation survives the bridge — a delegation that shares
   conversation state breaks independence.

**Never emit a route naming a model you cannot invoke.** A route that depends on
an unconfirmed transport must be rewritten through the fallback matrix before
emission.

### Permission asymmetry

Worth knowing: a bridged worker runs under the *other* runtime's sandbox and
approval settings, not the current session's. A `codex exec` spawned from
Claude Code obeys Codex's `sandbox_mode` and `approval_policy`. That is a real
operational difference — surface it rather than assuming the caller's
permission posture carries across.

## Dispatch contract

Layer B owns the time axis after a route is emitted. Every background
dispatch that uses a subprocess/CLI bridge transport (`codex exec`,
`claude -p`, `grok -p`) — worker or reviewer — runs under
`scripts/dispatch_agent.py`, which enforces the rules below. A foreground
dispatch a human is actively watching may skip the supervisor; it may never
skip the rules.

Native in-process transports (the Claude Code Agent tool, Codex
`multi_agent`) are outside `dispatch_agent.py`'s mechanical scope — the
supervisor's only execution primitive is `subprocess.Popen(argv)`, and an
in-process host mechanism cannot be spawned as an argv subprocess. A native
seat's supervision (deadline, cancellation) belongs to the host runtime that
launched it, not to `dispatch_agent.py`. The same receipt-state vocabulary
still classifies a native seat's outcome: a native seat that returns nothing
is `NO_RESPONSE`, exactly as for a bridged seat, but no receipt is
fabricated for a native dispatch that `dispatch_agent.py` never ran.

**Linkage scope.** The fingerprint chain (`--decision-fingerprint` on `run`,
`--expect-fingerprint`/`--expect-models` on `verify-evidence`) proves
decision-class linkage for **supervised subprocess seats**: this route's
decision, these declared models, completed review receipts. It does not
identify the task instance (two tasks with identical classification share a
fingerprint — `prompt_sha256` is the instance-level audit field), and it
does not cover native in-process seats, which write no receipt — so an
evidence-bearing review runs both seats as supervised CLIs. `model_id`
is the caller's **declared** value; the supervisor never cross-checks it
against argv — the raw argv recorded in the receipt is what an auditor
checks instead. One deliberate convergence: `effective_policy.allowed_families`
echoes the caller's list verbatim (order and duplicates included) while the
fingerprint canonicalises it, because the router consumes that list purely as
a membership set. Two requests differing only in that echo are one decision
and share one fingerprint. The four `run` arguments are all caller-supplied and probed
by nothing: `--decision-fingerprint` and `--policy-sha256` are copied from
the route JSON's same-named fields, `--transport-id` is the path key in the
config `transports` table (e.g. `claude_code.to_openai`), and
`--host-cli-version` is passed only when the caller already knows it.

**Declaration consistency, and its documented edge.** A `--transport-id`
ending `.to_xai` must also declare `--output-envelope` and
`--session-evidence`; the supervisor refuses a partial set **before spawn**
(exit 2, no receipt, no attempt-id consumed), and `verify-evidence` refuses a
complete absence at the moment such a receipt would become review evidence.
The suffix is the whole trigger: `--runtime` names the HOST everywhere in
this skill, so keying off `--runtime grok` would refuse grok-*hosted*
dispatches out to claude/codex — which produce no envelope — while doing
nothing for the case that matters (a Claude Code host dispatching *into*
grok).

What remains outside is a dispatch that declares **nothing**. The supervisor
could only tell that child is grok by parsing its argv, and it never parses
argv — argv is recorded in the receipt for an auditor to check instead. That
residue is owned by the Layer B recipe and the evidence chain, and it is
stated here so it is a documented boundary rather than a silent one.
Applying a contract the caller *did* declare is a different thing entirely,
and the same thing `--output-schema review` has always done.

**Artifact paths are attempt-exclusive — a caller contract.** Every
`--require-artifact` path and the `--artifact-root` that fences them belong
to exactly one attempt. Never point two concurrent attempts at the same path
or the same root. The supervisor proves the file *changed* since its
pre-spawn baseline; it cannot prove *this attempt* is what changed it, so a
shared path lets one attempt's work be recorded as another's proof. A
mechanical per-attempt lease is deliberately deferred (design §7); until it
exists, this paragraph is the whole of the guarantee.

**A required artifact must be the only name for its inode.** Containment
fences a *path*, but a write lands on an *inode*, so a second hard link
inside `--artifact-root` pointing at a file outside it passes every path
check there is — and a write through the in-root name overwrites the outside
file. The supervisor therefore requires `st_nlink == 1`, taken from the same
descriptor the digest is taken from: pre-spawn a multi-linked declaration is
refused outright (exit 2, no receipt), and a link the child creates during
the attempt lands as `artifact_multiply_linked:<path>` with no digest
recorded — a hash there would read as proof a *contained* file holds that
content. Each artifact record carries `nlink` so the receipt shows what was
checked. This is the supervisor-side counterpart to the grok 1.0.5 finding
in the 1.5.0 changelog: hard links defeat that CLI's own path-scoped write
rules, so the evidence layer refuses to certify one.

**A required artifact must still be the inode that was pinned.** `st_nlink`
is sampled twice — the pre-spawn baseline and grading — and the child owns
everything in between. A child can hard-link an outside inode at the required
path, write through it, unlink that name, and drop a fresh single-linked file
before it exits: both samples read `1`, the outside file is overwritten, and
the receipt records `contained: true`, `changed: true`, `SUCCEEDED`. So the
supervisor pins an *identity* rather than sampling a property. Before spawn
every required path is bound to one `(st_dev, st_ino)` — an existing artifact
to its own inode, an **absent** one to a reservation the supervisor creates
with `O_CREAT|O_EXCL` — and a descriptor on that inode is held (non-inheritable,
released on every exit path) for the whole attempt, which is what keeps the
inode number from being recycled under the comparison. At grading the required
path must still name that inode, or the attempt is
`artifact_identity_replaced:<path>` with no digest recorded. Each artifact
record carries `identity_pinned`.

Two caller-visible consequences, both deliberate:

- **A required artifact must be written IN PLACE.** `os.replace`/`rename` over
  a required path installs a new inode and is refused — at grading time it is
  indistinguishable from the laundering sequence above, since both end with a
  fresh single-linked file and no way to say what the inode it replaced was
  also called. The same refusal covers a second sequence that needs no link at
  all: write the required bytes to a path outside the root and rename it in.
  Neither is visible afterwards, which is why the identity is pinned rather
  than the property sampled — and why a pre-spawn audit of the child's tree
  cannot stand in for the pin, however thorough (verification ledger, "Content
  certification cannot replace the artifact identity pin"). Seats that atomically publish elsewhere should keep doing so and
  declare the *final* path as the required artifact only if they write it
  directly.
- **An absent required path is created before the child runs**, holding a short
  line of text that says so, and is **withdrawn** if the child never writes it
  — so a required artifact that was never produced is still `artifact_missing`
  with `exists: false`, exactly as before, and a supervisor that crashed does
  not leave a stray file where the caller declared there was none. Missing
  parent directories under `--artifact-root` are created with it.

**A Claude seat's output cannot be certified with `--require-artifact`, and
that is a property of the seat rather than of the contract.** Measured
2026-09-02 on Claude Code 2.1.258 / Darwin: a `claude -p` seat at
`--permission-mode acceptEdits`, asked to overwrite a pre-existing
single-linked file, writes exactly the content it was asked for and installs it
on a **new inode** — with and without `--strict-mcp-config`, so this is not a
property of any recipe, and only the tool that prompt drove was exercised. The
same prompt through `codex exec -s workspace-write` truncates in place and
keeps its inode, so the artifact contract certifies that seat normally; the
limitation is the Claude file-writing path, not the check. A Claude attempt
under `--require-artifact` therefore grades `INVALID_OUTPUT` /
`artifact_identity_replaced:<path>` with no digest recorded, so the receipt
proves nothing about the content either way. Asking that seat for a shell that
truncates in place does not rescue it: `acceptEdits` does not auto-approve the
terminal tool, and an absent reserved path then grades `artifact_missing` (a
pre-existing one that the child never touches grades `artifact_unchanged`).
So: certify a Claude seat's output by a **content hash recorded beside the
receipt**, and if a workflow needs supervisor-certified artifacts, do not seat
a Claude worker for it. `write_verified: true` on a direction says that seat
may be given write work — the routing question — never that the supervisor can
certify what it wrote. The verification ledger carries both measurements.

**The supervisor's own I/O is checked like anyone else's.** Two consequences
a caller can see:

- **A reservation that cannot be written WHOLE is a pre-spawn refusal** —
  exit 2, no receipt, no claim, and the path the supervisor created for it
  removed again. A short write is completed rather than accepted, because the
  digest recorded for a reservation describes the whole body: fewer bytes on
  disk than that would make the leftover marker unrecognisable at grading and
  hand a child that produced nothing a `changed: true` artifact.
- **A withdrawal that fails is reported, not assumed.** If the reservation
  cannot be removed, the artifact record keeps what grading actually saw
  (`exists: true`, its size, `sha256: null` — those are supervisor bytes, not
  the child's) and the receipt carries
  `artifact_reservation_cleanup_failed:<path>` next to the unchanged
  `artifact_missing:<path>`. A receipt never says a path is absent while it
  is still on disk.

Cleanup is also never an outcome: releasing the pins runs after the terminal
receipt is written, per entry, and an error in it cannot change the state, the
exit status, or whether the remaining descriptors are given back.

What this does **not** claim: a supervisor cannot stop an unconfined child
from writing outside its root. The contract is about proof — no attempt whose
required path stopped naming the pinned inode receives a successful receipt.

**A baseline is absent only when absence is confirmed.** The pre-spawn
baseline open accepts exactly one failure as "the file is not there yet":
`ENOENT`. Every other errno — `EACCES`, `EIO`, `ESTALE`, `ENOTDIR` — is
refused before spawn (exit 2, no receipt). An unreadable baseline and a
missing one both leave `baseline_sha256` null, and grading reads null as
"changed", so accepting the first would hand an attempt a freshness proof it
never earned.

**Compatibility with 1.4.x receipts.** A `to_xai` receipt written before this
contract existed carries no envelope and no session evidence. It was valid
under its own contract and it will fail today's `verify-evidence` — an
intended, narrow, fail-closed window. Verify an older evidence set with the
`verify-evidence` of the version that produced it.

### Launch is not completion

A background spawn returns a handle. The result exists only when the
attempt's receipt reaches a terminal state:

```
STARTING --start failure--> START_FAILED
   |
   v
RUNNING --exit 0, output valid----> SUCCEEDED
   |    \--exit != 0--------------> FAILED
   |     \--exit 0, output bad----> INVALID_OUTPUT
   |
   +--deadline--> TERM -> grace -> KILL -> confirmed? --yes--> TIMED_OUT
   |                                                   \-no--> TERMINATION_UNCONFIRMED
   +--cancel----> (same ladder) -> CANCELLED | TERMINATION_UNCONFIRMED
```

One empty poll is not a failure: while the receipt says `RUNNING` and the
deadline has not expired, keep waiting. From outside, a model reasoning
silently is indistinguishable from a hang — which is why the deadline is
wall-clock and generous, never inactivity-based and clever.

### Per-seat deadlines

| Seat | Effort | Deadline |
|---|---|---|
| worker | up to HIGH | 10 min |
| worker | VERY_HIGH / MAX | 20 min |
| reviewer | HIGH | 10 min |
| reviewer | MAX | 20 min |
| judge | any | 10 min |

Defaults, not law — scale by task size, tool use, and write permission.
What is law: every dispatch names a deadline (`--deadline-seconds`), and
expiry means TERM, a grace period, KILL, then confirmation of the whole
process group. `TERMINATION_UNCONFIRMED` blocks every write-capable retry:
re-route with `route_task.py --flags termination_unconfirmed` and the
route holds for a human.

### Invoking the supervisor

```bash
python3 "$SKILL_DIR"/scripts/dispatch_agent.py run \
    --attempt-id r1-a7f3 --receipt-dir receipts/ \
    --deadline-seconds 600 --grace-seconds 15 \
    --seat reviewer-1 --runtime claude_code \
    --model-id <resolved-id> --effort-native <native-effort> \
    --permission-mode read-only \
    --decision-fingerprint <route decision_fingerprint> \
    --policy-sha256 <route policy_sha256> \
    --transport-id claude_code.to_openai \
    --prompt-file r1-prompt.txt --output-schema review \
    -- codex exec -m <resolved-id> -c model_reasoning_effort=<native-effort> \
       -s read-only --skip-git-repo-check -
```

For a grok seat, the same invocation additionally declares the envelope, the
session evidence and (for any seat shipping a `--sandbox` flag) the expected
effective profile:

```bash
python3 "$SKILL_DIR"/scripts/dispatch_agent.py run \
    --attempt-id r1-b2c9 --receipt-dir receipts/ \
    --deadline-seconds 600 --seat reviewer-1 --runtime claude_code \
    --model-id <resolved-id> --effort-native <native-effort> \
    --transport-id claude_code.to_xai \
    --output-envelope grok-headless-json-v1 \
    --session-evidence grok-session-v1:$HOME/.grok/sessions/<enc-cwd>/<uuid> \
    --session-id <uuid> \
    --expect-sandbox-profile read-only \
    --prompt-file r1-prompt.txt --output-schema review \
    -- grok --no-auto-update -m <resolved-id> --effort <native-effort> \
       --output-format json -s <uuid> --permission-mode plan \
       --tools read_file,list_dir,grep --deny MCPTool \
       --disable-web-search --sandbox read-only \
       --prompt-file /dev/stdin
```

`<uuid>` is one value used three times — grok's `-s`, the supervisor's
`--session-id`, and the last path segment of the evidence directory. A seat
that produces files adds `--require-artifact` / `--artifact-root` — except a
Claude seat, whose output that contract cannot certify (above) — and a
write-capable seat adds `--expect-effective-agent` so a silently inherited
read-only default agent cannot be recorded as success.

`--prompt-file` feeds the child's stdin (here `codex exec`'s `-` prompt);
with no prompt file, stdin is /dev/null, so a stdin wait is structurally
impossible. `status --attempt-id <id> --receipt-dir <dir>` polls;
`cancel` kills from outside with the same confirmation ladder;
`verify-evidence` checks receipt ids before they become
`--isolation-evidence` (see `review-policy.md`, "Where the evidence id
comes from"). For seats dispatched from a route, bind the decision as well —
`verify-evidence ... --expect-fingerprint <route decision_fingerprint>
--expect-models <route review.reviewer_models, comma-separated>` — since without those two the
check cannot tell this decision's receipts from any other completed review's.

**Receipt storage is a trust boundary.** Keep the receipt directory outside
the child's enforced write authority. Mode `0700` excludes other UIDs; it
does not isolate a child with the same UID. The supervisor's in-memory result
owns completion. A disk `SUCCEEDED` cannot override its observed failure;
only a matching conservative cancellation may affect finalization, and it
cannot turn unconfirmed termination into confirmed termination. The existing
read/replace race between cooperating terminal writers remains; this change
does not claim atomic cancellation reconciliation or filesystem authentication.

`status`, `cancel`, and `verify-evidence` check any claimed success for an
absent claim sentinel, integer exit status zero, confirmed termination, valid
schema, completion timing, and matching stdout digest. The same bounded bytes
are decoded again to check the envelope and the recorded review verdict.
`FAIL` is a completed review, not approval. Incomplete or still-claimed
success is refused (`status`/`cancel` exit 2; evidence verification exit 1), so
retry status after publication completes. Older incomplete synthetic receipts
must be regenerated. These consistency checks cannot authenticate arbitrary
post-completion edits when the child can write the entire evidence store.

### Output is a contract

`--output-schema review` requires a parseable `verdict:` line. Exit 0 with
empty stdout *inside the deadline* is `INVALID_OUTPUT`, not success. Output
written after the deadline is never graded regardless of content: a
truncated `PASS` after a timeout kill is `TIMED_OUT`, never `INVALID_OUTPUT`
— that label is reserved for an in-deadline exit-0 attempt that failed to
produce a parseable verdict. Either way the review "did not run" —
re-dispatch it per `review-policy.md` ("A seat that returns no verdict");
never grade its fragments.

Stdout and stderr must be new paths: a pre-existing regular file, FIFO,
symlink, or hardlink causes `START_FAILED` without spawning or truncation.
Plain stdout and receipt reads, like envelope reads, are bounded to 4 MiB and
refuse symlinks and nonregular files without blocking. Oversized or unreadable
plain stdout is invalid output; a nonregular receipt is not completion proof.

**The envelope is a contract too, and it is graded first.** With
`--output-envelope grok-headless-json-v1` declared, stdout must be one JSON
object whose `stopReason` is `end_turn`; `cancelled`, `refusal`,
`max_tokens`, `max_turn_requests`, any unknown value, an absent field and
anything that is not a single JSON object are all `INVALID_OUTPUT`. Because
it is graded *before* the output schema, a cancelled turn that happens to
have emitted a well-formed verdict is still a cancelled turn — and under
`review` the verdict grammar applies to the envelope's `text` field, not to
the raw JSON carrying it. Under schema `none` an empty `text` is fine:
`--require-artifact` is what proves such a seat finished, since exit 0 with
some prose on stdout never did.

**Two formats, one gate.** `--output-envelope claude-print-json-v1` grades a
`claude -p --output-format json` document the same way, reading that format's
own key names: the finishing reason is `stop_reason` (not `stopReason`), the
answer the verdict grammar applies to is `result` (not `text`), and the failure
discriminator is `subtype` on a document whose `type` is always `result`. All
three are graded, not merely recorded: a document is a finished turn only when
`type` is `result`, `subtype` is `success`, and `is_error` is false, so a
mis-declared or foreign object fails closed instead of passing on null fields.
Missing or non-boolean `is_error` is invalid; only literal false can pass.
`is_error` outranks the finishing reason — a turn can end `end_turn` and still
declare it failed — and every reason that applies is reported, because the stop
reason is what tells a recipe defect from a model failure.

**Both formats carry `usage`**, and the receipt's envelope carries it too, so a
caller measuring boot or context cost reads the receipt instead of scraping the
retained stdout. The key sets differ — grok adds `reasoning_tokens` and
`total_tokens` — and the receipt's own `output_envelope` says which format
produced it. Only counts survive: a value that is not a finite non-negative
number is dropped, because a child's document is untrusted input and `NaN`
would write a receipt no strict JSON reader can parse. A document that carries
no counts at all leaves the key null rather than absent.

**To use it**, add `--output-format json` to the child and
`--output-envelope claude-print-json-v1` to the supervisor; under
`--output-schema review` the verdict grammar then applies to the document's
`result` field, exactly as it applies to a grok document's `text`. A
`.to_xai` dispatch may not declare this format: that direction requires the
grok envelope, because what it exists to catch is a grok cancellation.

**A verdict that ran into the narration in front of it is recovered, and the
repair is recorded.** Measured 2026-09-02: a grok headless document's `text`
can join the model's progress notes to its final answer with no newline
between them, so the line-anchored grammar reports nothing on a turn that
produced a verdict and the seat is re-dispatched over a formatting artifact.

The same rules apply to **both** the envelope and the plain-stdout path — the canonical
reviewer recipe declares no envelope, so a net that covered only one would
miss the seat most reviews actually use.

- `verdict: PASS | PASS_WITH_CHANGES | FAIL` is the format this document's own
  prompt quotes, not an answer, and it is refused wherever it appears —
  including at line start, where it was accepted for as long as the grammar
  existed. A seat that echoed the instructions and reviewed nothing does not
  grade as having reviewed.
- Prefer an explicit final section: put `=== REVIEW ===` on its own line,
  followed by the final `verdict:`. Only the last such section is parsed;
  earlier progress notes or historical verdicts cannot determine its result.
  The marker may be concatenated to Grok's preceding narration; the newline
  after it must remain. Put a blank line after quoted paragraphs before the
  final section.
- Markdown fenced code and blockquotes cannot supply a verdict. The final
  section must have exactly one verdict; conflicting or repeated verdicts are
  invalid. A verdict token followed by other prose on the same line is invalid.
- Without a final marker, accept a leading verdict. An anchored verdict buried
  after historical prose is refused. The existing run-in recovery remains for
  exactly one unanchored verdict with an adjacent in-range `confidence:` line;
  it is a formatting repair, not semantic proof that the surrounding prose is
  a review. Use the explicit marker to remove that legacy ambiguity.

The receipt records both the `verdict` it parsed and whether it was
`verdict_recovered`, and `verify-evidence` prints a note for a recovered one.
The output did need repair, and the recipe that produced it should be fixed —
asking for the final answer on a new line remains the right instruction. This
is the net under it.

The cause lands in `result.invalid_reasons`. Reasons naming a cancellation or
unusable evidence mean the recipe killed the turn, not that the model failed
— fix the recipe, re-dispatch the same model once, and keep it out of
`--prior-failures` (`review-policy.md`, "A silence caused by the recipe").

## Fallback matrices

Fallbacks are mandatory, not advisory. A missing model degrades the route; it
never fails it.

### Claude Code runtime

| Unavailable | Fallback |
|---|---|
| `worker_fast` (openai) | `claude_worker_fast`, then `claude_worker_balanced` |
| `worker_balanced` (xai) | `claude_worker_balanced`, then `openai_worker_balanced`, then `claude_senior` |
| `senior_engineer` (claude) | `openai_reasoning`, then `claude_architect`, then `openai_frontier` |
| `reasoning_specialist` (openai) | `openai_reasoning`, then `openai_frontier`, then `claude_senior`, then `claude_architect` |
| `principal_architect` (claude) | `openai_frontier`, then `claude_senior`, then `openai_reasoning` |
| Cross-family reviewer | Strongest available same-family reviewer; set `cross_family_review: false` |

### Codex runtime

| Unavailable | Fallback |
|---|---|
| `worker_fast` (openai) | `openai_worker_balanced`, then `claude_worker_fast` |
| `worker_balanced` (xai) | `claude_worker_balanced`, then `openai_worker_balanced`, then `openai_reasoning` |
| `senior_engineer` (claude) | `openai_reasoning`, then `claude_senior`, then `openai_frontier` |
| `reasoning_specialist` (openai) | `openai_reasoning`, then `openai_frontier`, then `claude_senior` |
| `principal_architect` (claude) | `openai_frontier`, then `openai_reasoning`, then `claude_architect` |
| Cross-family reviewer | Strongest available same-family reviewer; set `cross_family_review: false` |

### grok runtime

| Unavailable | Fallback |
|---|---|
| `worker_fast` (openai) | `claude_worker_fast`, then `openai_worker_balanced` |
| `worker_balanced` (xai) | `claude_worker_balanced`, then `openai_worker_balanced` |
| `senior_engineer` (claude) | `claude_senior`, then `openai_reasoning`, then `openai_frontier` |
| `reasoning_specialist` (openai) | `openai_reasoning`, then `openai_frontier`, then `claude_senior` |
| `principal_architect` (claude) | `openai_frontier`, then `claude_architect`, then `claude_senior` |
| Cross-family reviewer | Strongest available same-family reviewer; set `cross_family_review: false` |

**Write-capable xai on Claude Code only.** `claude_code.to_xai` ships
`mechanism_maker` with `write_verified: true` and a `verified` ledger
entry. `codex.to_xai` carries the same argv but stays unverified for that
host direction, so a Codex-hosted write route still will not name grok as
the worker. No `--unavailable-models` route-around is needed for this, and
none should be used for it: it withholds the model from the review seats too.

Which routes write is a class default in `task_write_seat`, overridable per
route with `--worker-seat write|read_only` (RouteRequestV1 `worker_seat`). The
emitted route carries a `worker_seat` block — the kind applied, where it came
from, and which families this host can dispatch write work to — and, when the
requirement moved the seat, an id-free note saying so. It is a binding
decision, not scarcity: it is not recorded in `fallbacks_applied` and does not
spend routing confidence.

Three things this deliberately does not do. It does not touch the **host's own
family** — `transports.<runtime>.native` is the host session writing, not a
bridge, so a grok host still implements with the xai seat. It does not touch
**read-only seats**: the verified xai reviewer is seated on write routes
exactly as before. And it is not an xai rule — it is a lookup. Restoring a
direction takes three things together, and no fewer: the maker recipe, the
direction's own `write_verified: true`, and a verification-ledger entry
recording that the maker seat was probed. Policy fails closed when they
disagree, so a recipe added without the probe authorizes nothing.

One limit worth knowing: a resolved role holds one model per route, so when
the worker sits on the role that binds the skipped seat, that model is absent
from the whole route rather than falling through to a reviewer seat.

### Degraded bindings

When a bridge is down entirely, fall back to the single-provider binding:

**This one IS scarcity, and is recorded as a fallback.** The alt-seat swap
above is a binding decision because nothing became unavailable — the caller
stated a fact about the task and the policy picked a different seat on merit.
`bridge_down` is the opposite: a whole provider is unreachable, the route names
models the default binding would not have named, and the confidence it reports
should say so. Moving it into `notes` and out of `fallbacks_applied` was
proposed and declined for that reason: it would make a degraded route report a
healthy route's confidence, against the rule at the end of this file that the
metrics must let someone reconstruct what was *available* when a route was
decided. The cost that motivated the proposal — a review band promoted on the
most common profile at the moment capacity is scarcest — was fixed at its root
instead, by re-deriving the penalty (1.9.0).

Registry keys, not model ids — resolve them through `config/model-routing.yaml`,
which is the only place a concrete identifier appears:

```yaml
claude_only:
  worker_fast:          claude_worker_fast
  worker_balanced:      claude_worker_balanced
  senior_engineer:      claude_senior
  reasoning_specialist: claude_senior          # at MAX effort
  principal_architect:  claude_architect

openai_only:
  worker_fast:          openai_worker_fast
  worker_balanced:      openai_worker_balanced
  senior_engineer:      openai_reasoning
  reasoning_specialist: openai_frontier
  principal_architect:  openai_frontier

xai_only:
  worker_fast:          xai_frontier
  worker_balanced:      xai_frontier
  senior_engineer:      xai_frontier
  reasoning_specialist: xai_frontier          # at VERY_HIGH; the model's ceiling
  principal_architect:  xai_frontier          # at VERY_HIGH; the model's ceiling
```

Both single-provider bindings lose family diversity — set
`cross_family_review: false` and weigh the second verdict accordingly.
`claude_only` shares a model between senior and reasoning roles;
`openai_only` now has separate tier-2 senior and tier-3 frontier models.
Each has enough distinct models to seat two reviewers for a balanced worker,
but stronger workers and unavailable models can still reduce review depth or
leave no judge. Inspect the emitted shortfall and confirmation fields.

`xai_only` is not that pattern. One model fills every role, so any band that
requires independent review is `INDEPENDENCE_UNAVAILABLE` — terminal, not
merely thin. That is the honest answer: there is no way to run an independent
review against a single model. Only `LOW` (independence not required) stays
executable.

## Disclosing degradation

Every applied fallback appears in `fallbacks_applied` and is named in the
rationale. When a fallback reduces review independence, the degradation rules in
`review-policy.md` apply.

The rule underneath all of this: the metrics should let someone reconstruct not
just what was decided, but what was *available* when it was decided. A route
that looks weak in hindsight is a very different problem depending on whether
the strong option existed at the time.


### Terminal publication and cancellation

`dispatch_agent` serializes each attempt's terminal read/reconcile/write/claim
release with a stable `.lock` inode. Lock acquisition is bounded; lock files
remain after completion and must not be removed while supervisors or cancelers
may still be active. This coordinates cooperating tools, not hostile same-UID
processes.

Cancellation intent is persisted before signaling. The supervising run polls
for it, so requester death cannot turn an accepted cancellation into success.
Unconfirmed termination dominates both writers. Signals and process waits occur
outside the publication lock.

Exit **8** means receipt publication failed, including an identity mismatch,
unreadable current receipt, lock timeout, failed atomic write, or failed claim
release. Known unconfirmed termination takes precedence and still returns
exit5 with the publication error on stderr. The claim is retained; a terminal receipt with a claim is unpublished
and cannot be used by status, cancel, or observation evidence checks. Recover
storage/authority before retrying; classify this as the operational
`attempt_outcomes` kind `publication_failure`, never `capability_failure`.
A post-spawn crash still cleans up the group and exits9 unless termination
is unconfirmed, which always retains exit5; if RUNNING was never
published, only the supervisor's privately retained exact STARTING snapshot
can authorize publishing that launch's cleanup result.


### Darwin receipt-store guard

When stored receipts will be promoted to trusted completion evidence on macOS,
run with `--receipt-guard darwin-sandbox-v1` and verify with
`verify-evidence --require-receipt-guard`. The default `none` preserves existing
cross-platform launches and makes no receipt-store protection claim. A requested
but unavailable guard is `START_FAILED`; it never silently launches unprotected.

Before claiming an attempt, the supervisor pins the receipt directory to its
`F_GETPATH` kernel path; all later lock, polling, output, and publication paths
use it. A final symlink root is rejected. Orchestrators should retain a canonical
store path rather than a mutable alias. After creating output files, the
supervisor obtains their kernel paths and passes paths as Seatbelt parameters, and probes an actual denied write
before launching the target under that same policy. It denies receipt-root reads
and writes, mutation of every canonical ancestor node, and all new hard links.
Pre-existing store symlinks, special files, and multiply linked regular files
are refused; use a fresh store if the bounded 10,000-entry inspection is exceeded.
Only writes and metadata inspection of this attempt's stdout/stderr are allowed
in the store. Metadata access lets Node initialize its write-only inherited
streams; it does not permit reading stream data or opening the publication lock.
A prompt descriptor into the protected store is refused; keep prompt files
outside the receipt directory so inherited stdin cannot bypass read denial.
Workspace reads/writes and normal Git add/commit remain available. Signals are
limited to the same sandbox; privileged task ports, AppleEvents, and launchd job
creation are denied. Descendants inherit the guard. Admission is checked again
immediately before target launch; an expired deadline never starts the target.

The receipt's `receipt_guard` is null when undeclared; otherwise it progresses
through `requested`, `prepared`, and `launched`. A prepared/launched record binds
`mechanism`, `profile_sha256`, and `protected_root` to the parameterized recipe.
The verifier requires `launched` and recomputes that binding from current kernel
paths. This checks recipe consistency; a self-described JSON file is not
cryptographic authentication. Keep the receipt store under trusted supervisor
control for its lifetime and apply the guard to every untrusted child that could
reach it. Unrelated unsandboxed same-UID processes and external service deputies
are outside this direct-process-tree boundary. This is not workspace containment or verifier/runtime integrity protection;
use a trusted verifier installation/runtime that the child cannot alter (or
validate its pinned integrity through a trusted controller before invoking it).
Existing maker-seat sandbox and `--require-single-linked-cwd` obligations remain.

Accepted cancellation optionally records `result.cancel_requested_at`; this
control intent is owned by the supervisor/canceler, not the child output.

Guard/prelaunch failures use the registered operational reasons
`receipt_guard_unavailable` and `deadline_expired_before_launch` with
`START_FAILED`. These are launch/admission failures, not model capability failures.
Root validation errors before an attempt is claimed return invalid usage without
creating an attempt receipt.
