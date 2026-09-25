# Security Policy

## Supported versions

Security fixes are delivered through the latest release of deep-model-router.
Check the current version with `jq -r .version .claude-plugin/plugin.json`.

## Reporting a vulnerability

Please report security issues **privately** via
[GitHub Security Advisories](https://github.com/Sungmin-Cho/deep-model-router/security/advisories/new)
rather than opening a public issue.

We aim to acknowledge reports within a few days and will coordinate a fix and a
disclosure timeline with you.

## Scope

deep-model-router ships a skill, a YAML policy, local Python CLIs, and one
SessionStart hook.

- `route_task.py` is a deterministic scorer. It reads the bundled policy, the
  caller's classification and — when present — the local model state under
  `committed/` of `$DEEP_MODEL_ROUTER_STATE_DIR` (default
  `$XDG_STATE_HOME/deep-model-router` or `~/.local/state/deep-model-router`);
  it does not call a model provider. State files are opened relative to one
  verified root fd (`O_NOFOLLOW`, 0700 directories, 0600 single-linked
  regular files owned by this uid, 256 KiB cap, strict JSON), and a damaged
  committed state fails closed (`MODEL_STATE_UNAVAILABLE`). These checks keep
  out accidental damage, path substitution and other users; they are not an
  authentication of the files against the same uid, which can rewrite the
  plugin cache and CLI binaries alike.
- `model_sync.py` reads the local CLI model catalogs, verifies a successor id
  with contained read-only probes through `dispatch_agent.py`, and publishes
  the result into that state directory. It never seats a write-capable CLI
  on its own; `probe-maker` does, once, only after a person confirms on a TTY.
- `dispatch_agent.py` starts a local child process (a host CLI) under a
  wall-clock deadline and a TERM-then-KILL process-group ladder. Prompts enter
  the child by file on stdin; argv is executed without a shell. Receipt paths
  derived from `--attempt-id` must stay inside `--receipt-dir`.
- The locator refuses personal skill symlinks and sibling source checkouts so
  a nearby tree cannot shadow the installed plugin.
- The plugin ships one `hooks/hooks.json` SessionStart hook (`startup`,
  `async`, 10 s). Its command is a string the host shell interprets:
  `r="${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}"; [ -n "$r" ] && python3 "$r/skills/model-router/scripts/model_sync.py" tick --detach; exit 0`
  — the root is quoted, an empty root runs nothing, and the hook always
  exits 0. The tick is offline and inference-free; it starts a detached probe
  run only when a successor is due (`DEEP_MODEL_ROUTER_AUTOUPGRADE=0` or
  `model_sync.py disable` turns it off). NOT covered: the host shell itself
  and the content of the script the hook runs — the trust boundary is the
  installed plugin code, as for every other plugin hook; Codex's hash
  approval covers the command string only. The plugin keeps no network
  service.

When reporting, please indicate which runtime (Claude Code / Codex / Grok) is
affected, and whether the issue is in routing, location, or dispatch.
