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


def _advisory(out):
    return out["host_seat_advisory"]


def _ask(**over):
    return _advisory(route(_t(**over), CFG))["policy_ask"]


def test_default_ask_is_worker_fast_nominal_at_high():
    assert _ask() == {"tier": 0, "effort": "HIGH", "raised_by": []}


def test_uncertainty_3_raises_ask_to_worker_balanced_tier():
    ask = _ask(uncertainty=3)
    assert ask["tier"] == 1
    assert "orchestrator_uncertainty_3" in ask["raised_by"]


def test_critical_domain_with_u2_raises_tier():
    ask = _ask(uncertainty=2, flags=["auth_sensitive"])
    assert ask["tier"] == 1
    assert ask["raised_by"] == ["orchestrator_critical_u2"]


def test_architecture_high_band_uses_post_override_band():
    # ARCHITECTURE + all dimensions 0 + critical-domain flag -> override band HIGH
    ask = _ask(task_class="ARCHITECTURE", complexity=0, uncertainty=0,
               blast_radius=0, reversibility=0, flags=["auth_sensitive"])
    assert ask["tier"] == 2
    assert ask["raised_by"] == ["orchestrator_architecture_high"]


def test_architecture_ambiguity_reaches_principal_tier():
    assert _ask(task_class="ARCHITECTURE", uncertainty=3)["tier"] == 3


def test_raised_by_is_definition_ordered():
    # c1 u3 b2 r1 = 12 -> CRITICAL, so architecture_high also fires
    ask = _ask(task_class="ARCHITECTURE", uncertainty=3, blast_radius=2)
    assert ask["raised_by"] == [
        "orchestrator_uncertainty_3",
        "orchestrator_architecture_high",
        "orchestrator_architecture_ambiguity",
        "orchestrator_blast_high",
    ]


def test_blast_radius_2_raises_effort_to_max_strictly():
    assert _ask(blast_radius=2)["effort"] == "MAX"
    assert _ask(blast_radius=1)["effort"] == "HIGH"


def test_low_confidence_escalates_and_raises_ask_effort():
    # 0.95 - u3 0.20 - unknown_root_cause 0.10 - prior>=2 0.15 = 0.50 < 0.60
    # (yaml:47-63 / route_task.py:1399-1421 — P1-F1's measured arithmetic)
    out = route(_t(task_class="DEBUGGING", uncertainty=3,
                   flags=["unknown_root_cause"], prior_failures=2,
                   prior_models=["gpt-5.6-luna", "grok-4.6"]), CFG)
    assert out["routing_confidence"] == 0.50
    assert out["terminal"] == "ESCALATE_ROUTING"
    ask = _advisory(out)["policy_ask"]
    assert ask["effort"] == "MAX"
    assert "orchestrator_low_confidence" in ask["raised_by"]


def test_confidence_boundary_at_060_is_not_low():
    # 0.95 - 0.20 - 0.15 = 0.60; strict '<' means no escalation
    out = route(_t(task_class="DEBUGGING", uncertainty=3, prior_failures=2,
                   prior_models=["gpt-5.6-luna", "grok-4.6"]), CFG)
    assert out["routing_confidence"] == 0.60
    assert out["terminal"] != "ESCALATE_ROUTING"
    assert ("orchestrator_low_confidence"
            not in _advisory(out)["policy_ask"]["raised_by"])


def test_ask_is_immune_to_bridge_down_and_unavailability():
    base = dict(task_class="ARCHITECTURE", uncertainty=3)
    a = route(_t(**base), CFG)
    b = route(_t(**base, flags=["bridge_down"]), CFG)
    c = route(_t(**base, unavailable_models=["claude-fable-5"]), CFG)
    assert (_advisory(a)["policy_ask"] == _advisory(b)["policy_ask"]
            == _advisory(c)["policy_ask"])


def test_advisory_block_is_emitted_on_every_route_with_no_declaration():
    adv = _advisory(route(_t(), CFG))
    assert adv["declared"] is None
    assert adv["model_comparison"] == "undeclared"
    assert adv["effort_comparison"] == "undeclared"
    assert adv["advisory"] == "none"
    assert "policy_ask" in adv
