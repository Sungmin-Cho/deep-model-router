# Router CLI locator

Consumers must not import `../deep-model-router` or a personal
`~/.claude/skills/model-router` symlink.

Order:

1. `DEEP_MODEL_ROUTER_CLI` if it is an executable `route_task.py`
2. `$DEEP_MODEL_ROUTER_ROOT/skills/model-router/scripts/route_task.py`
3. Claude cache `~/.claude/plugins/cache/**/deep-model-router/<ver>/.../route_task.py`
4. Codex cache `~/.codex/plugins/**/deep-model-router/**/.../route_task.py`

Missing → treat as router unavailable (consumer §11.3).
Python reference: `skills/model-router/scripts/locate_router.py`.
Node consumers copy the same order; they do not import the Python file.

## Local model state directory

The router and `model_sync.py` resolve one state root, first match wins:

1. `$DEEP_MODEL_ROUTER_STATE_DIR` (non-empty)
2. `$XDG_STATE_HOME/deep-model-router`
3. `~/.local/state/deep-model-router`

The router reads only `committed/` under it; a missing root means no overlay.
`DEEP_MODEL_ROUTER_OVERLAY=off` ignores overlay entries (revocations still
apply); `DEEP_MODEL_ROUTER_AUTOUPGRADE=0` stops the tick and publication.
Python reference: `state_root_path` in `skills/model-router/scripts/model_state.py`.
