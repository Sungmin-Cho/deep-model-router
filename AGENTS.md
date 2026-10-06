# deep-model-router — Agent Guide

Deterministic model / effort / review router. Claude Code, Codex, and Grok
share this file. The skill name is `model-router`; the plugin key is
`deep-model-router`.

Read the version from `.claude-plugin/plugin.json`. Do not hard-code it.

📄 Documentation in this repo follows `docs/DOCS_RULE.md` (local maintainer guide).

## Layout

```
skills/model-router/
  SKILL.md
  config/model-routing.yaml
  scripts/route_task.py
  scripts/dispatch_agent.py
  references/
  tests/
hooks/hooks.json          # SessionStart tick, shared by Claude Code and Codex
hooks/hooks.claude.json   # Claude Code only: names the one mod module
hooks/mods/               # the Claude Code mod (TypeScript) and its tests
types/index.d.ts          # the mod's $.state contract
```

Every path in the skill is `$SKILL_DIR`-relative. `$SKILL_DIR` is the
directory that contains `SKILL.md`.

The mod under `hooks/mods/` is a Claude Code visibility layer: it never
changes a route, a receipt or a review floor. A check every host needs goes
in the scripts. Keep `modules` out of `hooks/hooks.json` (Codex loads that
file, and a plugin gets one module).

## Invocation

- Claude: `/deep-model-router:model-router`
- Codex: `$deep-model-router:model-router`
- Repeated decisions: `python3 "$SKILL_DIR/scripts/route_task.py" --format json`
  or `--request-json <file>` (RouteRequestV1).

## Locator

Consumers must not import `../deep-model-router` or a personal
`~/.claude/skills/model-router` symlink. Resolve the CLI with
`DEEP_MODEL_ROUTER_CLI`, then `DEEP_MODEL_ROUTER_ROOT`, then the host
plugin cache. See `docs/locator.md`.

## Tests

```bash
python3 -m pytest skills/model-router/tests/ -q
claude plugin validate .
claude plugin test .        # the mod's hooks/mods/tests/*.test.ts
```
