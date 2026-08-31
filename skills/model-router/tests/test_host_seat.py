"""Tests for Task 1: router.default_orchestrator{,_effort} gain a consumer.

Policy initialization now validates these two keys against role_tiers,
role_bindings.default, and effort_levels. Before this task both keys were
listed in DOCUMENTED_BUT_UNREAD as "guidance for the calling agent, not the
router" — a claim the router falsifies the moment it starts reading them.

Run:  python3 -m pytest skills/model-router/tests/ -q
"""

import copy
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    ConfigError,
    Task,
    load_config,
    route,
)

CFG = load_config()


def _t(**kw):
    kw.setdefault("task_class", next(iter(CFG["worker_selection"])))
    kw.setdefault("complexity", 0)
    kw.setdefault("uncertainty", 0)
    kw.setdefault("blast_radius", 0)
    kw.setdefault("reversibility", 0)
    return Task(**kw)


def _cfg_with(path: list, value):
    cfg = copy.deepcopy(CFG)
    node = cfg
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return cfg


def test_unknown_default_orchestrator_role_is_a_config_error():
    cfg = _cfg_with(["router", "default_orchestrator"], "worker_fastt")
    with pytest.raises(ConfigError):
        route(_t(), cfg)


def test_default_orchestrator_missing_from_default_binding_is_a_config_error():
    cfg = copy.deepcopy(CFG)
    cfg["role_tiers"] = cfg["role_tiers"] + ["ghost_role"]
    cfg["router"]["default_orchestrator"] = "ghost_role"
    with pytest.raises(ConfigError):
        route(_t(), cfg)


def test_invalid_default_orchestrator_effort_is_a_config_error():
    cfg = _cfg_with(["router", "default_orchestrator_effort"], "ULTRA")
    with pytest.raises(ConfigError):
        route(_t(), cfg)
