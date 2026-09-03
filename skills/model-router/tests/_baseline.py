"""Load the vendored 1.12.1 router as a separate module (design §4 T3, T19).

Isolation rules: the snapshot's `from policy_digest import ...` binds to the
vendored copy even when the live one is already in `sys.modules`; the
snapshot's `CONFIG_PATH` resolves inside the snapshot directory by construction
(it walks from `__file__`); `plugin_manifest_version()` would walk parents up
to the LIVE repo manifest, so the loader seeds the snapshot's own version cache
with the oracle version before any route runs — the snapshot code itself is
never edited. Nothing here reads a module-level name off the snapshot
(`__getattr__` would load config).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).resolve().parent / "fixtures" / "baseline-1.12.1"
BASELINE_VERSION = "1.12.1"
BASELINE_POLICY_SHA = "f8197c6d039b415fcecb2e90f4b907df588fd9b3bf8255c2e15310193c6f4942"
_MODULE_NAME = "baseline_route_task"
_loaded = None


def load_baseline():
    global _loaded
    if _loaded is not None:
        return _loaded
    scripts = SNAPSHOT_DIR / "scripts"
    saved = sys.modules.pop("policy_digest", None)
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, scripts / "route_task.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = mod
        spec.loader.exec_module(mod)
        sys.modules.pop("policy_digest", None)     # keep the vendored digest bound to the snapshot only
    finally:
        sys.path.remove(str(scripts))
        if saved is not None:
            sys.modules["policy_digest"] = saved
    # `plugin_manifest_version()` keys its cache by the resolved file path of
    # route_task.py and consults the cache before walking parents.
    mod._PLUGIN_VERSION_CACHE[str((scripts / "route_task.py").resolve())] = BASELINE_VERSION
    _loaded = mod
    return mod


_cfg = None


def baseline_cfg(mod) -> dict:
    """One parsed config for the whole session ([P2-opus-F11]): a fresh dict per
    call would re-digest the YAML and grow the snapshot's Policy cache."""
    global _cfg
    if _cfg is None:
        _cfg = mod.load_config(mod.CONFIG_PATH)
    return _cfg


def pair_tasks(live_mod, base_mod, **kwargs):
    """One canonical kwargs record, two Task instances (design §4 T3)."""
    return live_mod.Task(**kwargs), base_mod.Task(**kwargs)
