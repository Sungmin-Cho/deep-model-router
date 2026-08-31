"""Tests for Task 1: router.default_orchestrator{,_effort} gain a consumer.

Policy initialization now validates these two keys against role_tiers,
role_bindings.default, and effort_levels. Before this task both keys were
listed in DOCUMENTED_BUT_UNREAD as "guidance for the calling agent, not the
router" — a claim the router falsifies the moment it starts reading them.

Run:  python3 -m pytest skills/model-router/tests/ -q
"""

import copy
import json as _json
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    ConfigError,
    Task,
    ValidationError,
    load_config,
    main,
    route,
    request_sha256_of,
    task_from_request_v1,
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


# ---------------------------------------------------------------------------
# Task 3 — declared host seat: comparison, validation, identity, and inputs.
# ---------------------------------------------------------------------------

def _route_with_host(model, effort=None, over=None):
    task = _t(**(over or {}))
    task._host_seat = {"model": model, "effort": effort}
    return route(task, CFG)


# --- comparison and advisory [DD-3] ---

def test_haiku_vs_default_ask_is_at_and_silent():
    adv = _advisory(_route_with_host("claude-haiku-4-5-20251001"))
    assert adv["model_comparison"] == "at"
    assert adv["effort_comparison"] == "undeclared"
    assert adv["advisory"] == "none"


def test_model_below_alone_triggers_upgrade():
    adv = _advisory(_route_with_host("claude-haiku-4-5-20251001",
                                     over=dict(uncertainty=3)))
    assert adv["model_comparison"] == "below"
    assert adv["advisory"] == "upgrade_recommended"


def test_effort_below_alone_triggers_upgrade_by_index_not_string():
    adv = _advisory(_route_with_host("claude-fable-5", "HIGH",
                                     over=dict(blast_radius=2)))
    assert adv["model_comparison"] == "above"
    assert adv["effort_comparison"] == "below"
    assert adv["advisory"] == "upgrade_recommended"


def test_above_on_both_axes_stays_none():
    adv = _advisory(_route_with_host("claude-fable-5", "MAX"))
    assert adv["model_comparison"] == "above"
    assert adv["effort_comparison"] == "above"
    assert adv["advisory"] == "none"


def test_unrecognized_model_still_compares_effort():
    adv = _advisory(_route_with_host("claude-nova-6", "HIGH",
                                     over=dict(blast_radius=2)))
    assert adv["model_comparison"] == "unrecognized"
    assert adv["effort_comparison"] == "below"
    assert adv["advisory"] == "upgrade_recommended"


def test_effort_undeclared_with_max_ask_stays_none_in_decision_layer():
    adv = _advisory(_route_with_host("claude-fable-5",
                                     over=dict(blast_radius=2)))
    assert adv["effort_comparison"] == "undeclared"
    assert adv["advisory"] == "none"


# --- validation [DD-1] ---

def test_family_mismatch_with_runtime_is_loud():
    with pytest.raises(ValidationError):
        _route_with_host("gpt-5.6-sol")


def test_ceiling_violation_is_loud():
    with pytest.raises(ValidationError):
        _route_with_host("grok-4.6", "MAX", over=dict(runtime="grok"))


def test_unrecognized_model_skips_family_and_ceiling_checks():
    _route_with_host("claude-nova-6", "MAX")


def test_empty_model_and_bad_effort_are_loud():
    with pytest.raises(ValidationError):
        _route_with_host("")
    with pytest.raises(ValidationError):
        _route_with_host("claude-fable-5", "ULTRA")


# --- hash and output contract [IA-1b] ---

def test_declared_host_seat_changes_both_hashes():
    a = route(_t(), CFG)
    b = _route_with_host("claude-fable-5", "MAX")
    assert a["request_sha256"] != b["request_sha256"]
    assert a["decision_fingerprint"] != b["decision_fingerprint"]


def test_undeclared_preserves_legacy_hash_by_key_omission():
    legacy = request_sha256_of(_t())
    task = _t()
    task._host_seat = None
    assert request_sha256_of(task) == legacy


def test_model_only_equals_model_with_null_effort():
    t1 = _t()
    t1._host_seat = {"model": "claude-fable-5", "effort": None}
    t2 = _t()
    t2._host_seat = {"model": "claude-fable-5"}
    out1, out2 = route(t1, CFG), route(t2, CFG)
    assert out1["request_sha256"] == out2["request_sha256"]
    assert (out1["host_seat_advisory"]["declared"]
            == out2["host_seat_advisory"]["declared"]
            == {"model": "claude-fable-5", "effort": None})


# --- RouteRequestV1 / CLI ---

def _req(**extra):
    return {"route_schema_version": 1, "task_class": "IMPLEMENTATION",
            "complexity": 1, "uncertainty": 1, "blast_radius": 1,
            "reversibility": 1, **extra}


def test_request_v1_accepts_optional_host_seat():
    task = task_from_request_v1(_req(host_seat={"model": "claude-fable-5",
                                                "effort": "MAX"}))
    assert task._host_seat == {"model": "claude-fable-5", "effort": "MAX"}


def test_request_v1_host_seat_type_and_keys_are_strict():
    for bad in ("claude-fable-5", ["claude-fable-5"],
                {"model": "claude-fable-5", "mode": "x"}):
        with pytest.raises(ValidationError):
            task_from_request_v1(_req(host_seat=bad))
    assert task_from_request_v1(_req(host_seat=None))._host_seat is None


def test_cli_host_effort_without_model_exits_2():
    rc = main(["--class", "IMPLEMENTATION", "--complexity", "1",
               "--uncertainty", "1", "--blast-radius", "1",
               "--reversibility", "1", "--host-effort", "MAX"])
    assert rc == 2


def test_legacy_json_rejects_host_seat():
    payload = dict({"task_class": "IMPLEMENTATION", "complexity": 1,
                    "uncertainty": 1, "blast_radius": 1,
                    "reversibility": 1},
                   host_seat={"model": "claude-fable-5"})
    rc = main(["--json", _json.dumps(payload)])
    assert rc == 2


def test_request_json_wins_over_host_flags(tmp_path, capsys):
    p = tmp_path / "req.json"
    p.write_text(_json.dumps(_req()))
    rc = main(["--request-json", str(p), "--host-model", "claude-fable-5",
               "--format", "json"])
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["host_seat_advisory"]["declared"] is None


def test_cli_host_flags_declare_on_flags_path(capsys):
    rc = main(["--class", "IMPLEMENTATION", "--complexity", "1",
               "--uncertainty", "1", "--blast-radius", "1",
               "--reversibility", "1",
               "--host-model", "claude-haiku-4-5-20251001",
               "--host-effort", "HIGH", "--format", "json"])
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["host_seat_advisory"]["declared"] == {
        "model": "claude-haiku-4-5-20251001", "effort": "HIGH"}
