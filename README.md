**English** | [한국어](./README.ko.md)

# deep-model-router

![version](https://img.shields.io/github/package-json/v/Sungmin-Cho/deep-model-router?label=version)
![license](https://img.shields.io/github/license/Sungmin-Cho/deep-model-router)
[![part of deep-suite](https://img.shields.io/badge/part%20of-deep--suite-5b8def)](https://github.com/Sungmin-Cho/deep-suite)

Deterministic model / effort / review router for Claude Code, Codex, and Grok.

Classify a delegated software-engineering task, then let the scorer pick the worker from *difficulty*, and the review depth from *risk* (both axes floor the reasoning effort) — not from file count, token count, or which model you happen to have open. Review depth is a function of the risk band alone; the worker choice cannot quietly weaken it. A hard but isolated task gets a stronger worker; an easy but sensitive one keeps its deep review.

Part of the [deep-suite](https://github.com/Sungmin-Cho/deep-suite) ecosystem. [deep-work](https://github.com/Sungmin-Cho/deep-work) and [deep-loop](https://github.com/Sungmin-Cho/deep-loop) depend on this plugin as the shared decision plane. See the [CHANGELOG](CHANGELOG.md) for release history.

---

## Role in deep-suite

deep-model-router is the **decision plane**. Sibling plugins keep execution, durable state, and their own safety floors. This plugin answers two questions and keeps them separate:

1. **Who should do the work** — a role bound to an available model and an effort level, chosen by execution difficulty over the risk-band floor; both axes floor the effort.
2. **How hard it must be checked** — a review policy that follows the risk band, including whether independent review is required.

It does not implement the work, and it does not claim a control it did not enforce. `independence_required` is policy; `review_independence` is evidence. A missing router is a local fallback, not a reason to drop a HIGH or CRITICAL floor.

---

## Installation

### Option 1 — Marketplace (registered in deep-suite)

```text
# Claude Code
/plugin marketplace add Sungmin-Cho/deep-suite
/plugin install deep-model-router@claude-deep-suite

# Codex
codex plugin marketplace add Sungmin-Cho/deep-suite
codex plugin add deep-model-router@claude-deep-suite
```

### Option 2 — Local clone

```text
# Claude Code
claude plugin add https://github.com/Sungmin-Cho/deep-model-router.git

# Codex — add the local path as a plugin directory in your Codex config
```

Python 3 and **PyYAML** are required for the scorer and the dispatch supervisor — the policy is a YAML file, so the first route on a box without PyYAML fails. Install it with `python3 -m pip install pyyaml` if your interpreter does not already have it. The supervisor is POSIX-only (process-group control). There is no Node runtime dependency.

---

## Usage

### Claude Code

```text
/deep-model-router:model-router
```

### Codex

```text
$deep-model-router:model-router
```

Load the skill once per session to classify. Repeat decisions through the CLI — do not re-derive the band by hand:

```text
SKILL_DIR=<skill-base-directory announced when the skill loads>
python3 "$SKILL_DIR"/scripts/route_task.py --class IMPLEMENTATION \
    --complexity 1 --uncertainty 1 --blast-radius 1 --reversibility 0 \
    --format json
```

`SKILL_DIR` is the directory that contains `SKILL.md`. A background subagent inherits the project root, not the skill root — always prefix the script with that path.

RouteRequestV1 files win over flags:

```text
python3 "$SKILL_DIR"/scripts/route_task.py --request-json ./route-request.json --format json
```

Background dispatch is a separate step. A route is a decision; `scripts/dispatch_agent.py` owns the deadline, the kill ladder, and the completion receipt. Read `skills/model-router/references/adapters.md` before the first background dispatch.

---

## Skills

| Skill | Claude Code | Codex | Purpose |
|---|---|---|---|
| model-router | `/deep-model-router:model-router` | `$deep-model-router:model-router` | Classify delegated work and emit a RouteDecisionV1 |

Consumers must not import `../deep-model-router` or a personal `~/.claude/skills/model-router` symlink. Resolve the CLI as documented in [`docs/locator.md`](docs/locator.md).

---

## How routing works

You classify; the script scores. The cheap model does the volume. Escalation happens on evidence. Review depth tracks risk.

| You supply | The scorer returns |
|---|---|
| Task class, four 0–3 dimensions, flags | Risk band, worker role + model, effort, review policy |
| Runtime, availability, prior failures | Fallbacks, terminal states, human-gate exit codes |

```
risk_score = complexity + 2×uncertainty + 2×blast_radius + reversibility     (0–18)
LOW 0–3 · MEDIUM 4–7 · HIGH 8–10 · CRITICAL 11–18

execution_score = 3×complexity + 2×uncertainty + three context flags          (0–18)
EASY 0–8 · NORMAL 9–11 · HARD 12–14 · VERY_HARD 15–18   → the worker, and an effort floor
                                                        → never the review band or a human control
```

Critical-domain flags (auth, security, financial, data integrity) raise the band after scoring, for every task class. A small, well-understood change in an authorization path still gets a strong worker and independent review.

Review is sized to the band and no further. A `LOW` review is the deterministic checks the route names (`tests`, `lint`) — the caller owes them before accepting the work, and re-routes with `--checks-unavailable` where they cannot run. A `MEDIUM` review seats one cross-family reviewer at the implementer's tier, not above it. A `REVIEW` task's lead counts as one of its reviewers. For work that is already done, a RouteRequestV1 `implementer` plans the review against the model that actually wrote it.

The policy lives in `skills/model-router/config/model-routing.yaml`. Model identifiers are born in that registry or in a probed local overlay entry on your machine (below), and `model_sync.py promote` is the only way back into the registry. The skill body and `references/` describe the same rules the script executes.

Exit status is part of the contract: **0** dispatchable, **1** terminal, **2** invalid input, **3** needs confirmation first, **4** production hotfix (confirm after it ships), **5** internal error. Only 3 is configurable — it is `human_in_the_loop.human_gate_exit_status`, any value in 3..255, so a caller that already uses 3 for something else can move the gate. Read it from the config rather than hard-coding it; 0, 1 and 2 are taken and >255 truncates to a success code, which is why the range is validated at load time.

---

## Model auto-upgrade / local overlay

Vendors ship new model generations faster than releases. `skills/model-router/scripts/model_sync.py` follows each registry row's declared lineage on your machine: it reads the CLI model catalogs offline, verifies a successor id with contained read-only probes, and publishes it as a local overlay entry. The router then routes that row on the new id — tier inherited, price `unavailable`, and a note that the maker seat was not re-probed for this id. Nothing is edited in the plugin.

- **Trigger.** A SessionStart hook runs `model_sync.py tick --detach` (offline, milliseconds; it starts a detached probe run only when a successor is due). Codex asks once to trust the hook command. Grok, or any host without the hook, runs the same tick from the skill.
- **State.** `$DEEP_MODEL_ROUTER_STATE_DIR`, else `$XDG_STATE_HOME/deep-model-router`, else `~/.local/state/deep-model-router` (0700; tool-written, not hand-edited). The router reads only `committed/`. Deleting `committed/` resets to a fresh install, revocations included.
- **Commands.** `model_sync.py status` (current generation, deferrals, notices) · `revert <key>` (drop the entry and revoke its id) · `unblock <id>` · `disable` / `enable` (auto-upgrade; `disable` also cancels in-flight probes) · `repair [--to <generation> [--force]]` · `quota` (codex usage from local rollout records; no model call) · `promote --repo … --key … --price …` (move an entry into a repo checkout).
- **Off switches.** `DEEP_MODEL_ROUTER_AUTOUPGRADE=0` stops ticks and publication. `DEEP_MODEL_ROUTER_OVERLAY=off` is the emergency switch: the router ignores overlay entries but keeps revocations, and an overlay id it already seated stays valid retry history. Corrupt committed state still fails closed (`MODEL_STATE_UNAVAILABLE`) — use `repair`. So does a state root that fails admission (not a directory you own with mode 0700); the route's note names the `chmod 700` fix.
- **In-flight deep-loop runs.** A policy digest change — a plugin update or an overlay publication — stops an in-flight deep-loop run until deep-loop passes `policy_pin`. For long runs, set `DEEP_MODEL_ROUTER_AUTOUPGRADE=0`.

---

## deep-suite links

| Plugin | Role |
|---|---|
| [deep-model-router](https://github.com/Sungmin-Cho/deep-model-router) | This plugin — shared decision plane |
| [deep-work](https://github.com/Sungmin-Cho/deep-work) | Phased implementation orchestrator |
| [deep-review](https://github.com/Sungmin-Cho/deep-review) | Independent evaluator with an APPROVE verdict |
| [deep-loop](https://github.com/Sungmin-Cho/deep-loop) | Durable multi-session control plane |
| [deep-goal](https://github.com/Sungmin-Cho/deep-goal) | Goal condition compiler |
| [deep-evolve](https://github.com/Sungmin-Cho/deep-evolve) | Autonomous fitness-metric experiment loop |
| [deep-docs](https://github.com/Sungmin-Cho/deep-docs) | Document gardening agent |
| [deep-wiki](https://github.com/Sungmin-Cho/deep-wiki) | Knowledge base ingest and management |
| [deep-memory](https://github.com/Sungmin-Cho/deep-memory) | Cross-project semantic memory |
| [deep-dashboard](https://github.com/Sungmin-Cho/deep-dashboard) | Harness diagnostics and suite telemetry |
| [deep-suite (marketplace)](https://github.com/Sungmin-Cho/deep-suite) | Unified marketplace and harness matrix |

## Links

- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Locator](docs/locator.md)
- [deep-suite marketplace](https://github.com/Sungmin-Cho/deep-suite)

## License

MIT — see [LICENSE](LICENSE).
