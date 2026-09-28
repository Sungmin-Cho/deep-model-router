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


# --- the 1.16.1 oracle (Part B, plan B0) --------------------------------------
#
# Part B changes the review policy on purpose, so the release it is measured
# against is the last one before it: 1.16.1, whose routing is 1.16.0's with the
# two OpenAI ids promoted. Its import closure is six modules, not two, and a
# sibling bound to a LIVE module would move the oracle with the implementation.
# The vendored files are renamed `baseline_1_16_1_*` with their sibling imports
# rewritten, so nothing in the snapshot can resolve to the live scripts
# whatever `sys.modules` already holds.

SNAPSHOT_1161_DIR = Path(__file__).resolve().parent / "fixtures" / "baseline-1.16.1"
BASELINE_1161_VERSION = "1.16.1"
BASELINE_1161_POLICY_SHA = "9597ed2cd30f40319b283740cb9858f4d92b7cbc45415f3f70d56124131994bd"
SNAPSHOT_1161_PREFIX = "baseline_1_16_1_"
_loaded_1161 = None
_cfg_1161 = None


def load_baseline_1161():
    global _loaded_1161
    if _loaded_1161 is not None:
        return _loaded_1161
    scripts = (SNAPSHOT_1161_DIR / "scripts").resolve()
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(
            f"{SNAPSHOT_1161_PREFIX}route_task", scripts / f"{SNAPSHOT_1161_PREFIX}route_task.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(scripts))
    for name, loaded in list(sys.modules.items()):
        if name.startswith(SNAPSHOT_1161_PREFIX):
            if Path(loaded.__file__).resolve().parent != scripts:
                raise AssertionError(f"{name} resolved outside the snapshot: {loaded.__file__}")
    # Same reason as the 1.12.1 loader: `plugin_manifest_version()` would walk
    # up to the LIVE manifest and report the release under test.
    mod._PLUGIN_VERSION_CACHE[str(scripts / f"{SNAPSHOT_1161_PREFIX}route_task.py")] = \
        BASELINE_1161_VERSION
    _loaded_1161 = mod
    return mod


def baseline_1161_cfg() -> dict:
    """The snapshot's own parsed config, once per session."""
    global _cfg_1161
    if _cfg_1161 is None:
        mod = load_baseline_1161()
        _cfg_1161 = mod.load_config(mod.CONFIG_PATH)
    return _cfg_1161
