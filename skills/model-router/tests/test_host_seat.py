"""Tests for Task 1: router.default_orchestrator{,_effort} gain a consumer.

Policy initialization now validates these two keys against role_tiers,
role_bindings.default, and effort_levels. Before this task both keys were
listed in DOCUMENTED_BUT_UNREAD as "guidance for the calling agent, not the
router" — a claim the router falsifies the moment it starts reading them.

Run:  python3 -m pytest skills/model-router/tests/ -q
"""

import copy
import json as _json
import re
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
ID = lambda key: CFG["models"][key]["id"]                        # noqa: E731
ARCHITECT_ID = CFG["models"]["claude_architect"]["id"]
REGISTRY_IDS = {model["id"] for model in CFG["models"].values()}


def _whole_token(needle: str, text: str) -> bool:
    return re.search(r"(^|[^A-Za-z0-9._-])" + re.escape(needle) + r"([^A-Za-z0-9._-]|$)", text) is not None

EXPECTED_ASK_ROWS = [
    f"| default | {CFG['router']['default_orchestrator']} nominal / {CFG['router']['default_orchestrator_effort']} |",
    "| uncertainty == 3 | ≥ worker_balanced |",
    "| critical-domain flag AND uncertainty >= 2 | ≥ worker_balanced |",
    "| ARCHITECTURE AND band(HIGH+) | ≥ senior_engineer |",
    "| ARCHITECTURE AND uncertainty == 3 | ≥ principal_architect |",
    f"| routing_confidence < {CFG['router']['confidence']['escalate_below']:.2f} | effort MAX |",
    "| blast_radius >= 2 | effort MAX |",
]


def test_routing_policy_ask_table_matches_code_cell_by_cell():
    md = (SKILL / "references" / "routing-policy.md").read_text()
    start = md.index("<!-- ask-table:start -->")
    end = md.index("<!-- ask-table:end -->")
    block = md[start:end]
    rows = [l.strip() for l in block.splitlines()
            if l.strip().startswith("|") and "---" not in l
            and not l.strip().startswith("| condition")]
    assert rows == EXPECTED_ASK_ROWS


def test_recommendation_total_order_is_well_defined():
    def recommend(family, tier_req):
        rows = sorted((m["capability_tier"], key)
                      for key, m in CFG["models"].items()
                      if m["family"] == family
                      and m.get("dispatchable", True)
                      and m["capability_tier"] >= tier_req)
        return rows[0][1] if rows else None
    assert recommend("claude", 0) == "claude_worker_fast"
    assert recommend("claude", 1) == "claude_worker_balanced"
    assert recommend("claude", 3) == "claude_architect"
    assert recommend("xai", 2) is None


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
                   prior_models=[ID("openai_worker_fast"), ID("xai_frontier")]), CFG)
    assert out["routing_confidence"] == 0.50
    assert out["terminal"] == "ESCALATE_ROUTING"
    ask = _advisory(out)["policy_ask"]
    assert ask["effort"] == "MAX"
    assert "orchestrator_low_confidence" in ask["raised_by"]


def test_confidence_boundary_at_060_is_not_low():
    # 0.95 - 0.20 - 0.15 = 0.60; strict '<' means no escalation
    out = route(_t(task_class="DEBUGGING", uncertainty=3, prior_failures=2,
                   prior_models=[ID("openai_worker_fast"), ID("xai_frontier")]), CFG)
    assert out["routing_confidence"] == 0.60
    assert out["terminal"] != "ESCALATE_ROUTING"
    assert ("orchestrator_low_confidence"
            not in _advisory(out)["policy_ask"]["raised_by"])


def test_ask_is_immune_to_bridge_down_and_unavailability():
    base = dict(task_class="ARCHITECTURE", uncertainty=3)
    a = route(_t(**base), CFG)
    b = route(_t(**base, flags=["bridge_down"]), CFG)
    c = route(_t(**base, unavailable_models=[ARCHITECT_ID]), CFG)
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
    adv = _advisory(_route_with_host(ID("claude_worker_fast")))
    assert adv["model_comparison"] == "at"
    assert adv["effort_comparison"] == "undeclared"
    assert adv["advisory"] == "none"


def test_model_below_alone_triggers_upgrade():
    adv = _advisory(_route_with_host(ID("claude_worker_fast"),
                                     over=dict(uncertainty=3)))
    assert adv["model_comparison"] == "below"
    assert adv["advisory"] == "upgrade_recommended"


def test_effort_below_alone_triggers_upgrade_by_index_not_string():
    adv = _advisory(_route_with_host(ARCHITECT_ID, "HIGH",
                                     over=dict(blast_radius=2)))
    assert adv["model_comparison"] == "above"
    assert adv["effort_comparison"] == "below"
    assert adv["advisory"] == "upgrade_recommended"


def test_above_on_both_axes_stays_none():
    adv = _advisory(_route_with_host(ARCHITECT_ID, "MAX"))
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
    adv = _advisory(_route_with_host(ARCHITECT_ID,
                                     over=dict(blast_radius=2)))
    assert adv["effort_comparison"] == "undeclared"
    assert adv["advisory"] == "none"


# --- validation [DD-1] ---

def test_family_mismatch_with_runtime_is_loud():
    with pytest.raises(ValidationError):
        _route_with_host(ID("openai_reasoning"))


def test_ceiling_violation_is_loud():
    with pytest.raises(ValidationError):
        _route_with_host(ID("xai_frontier"), "MAX", over=dict(runtime="grok"))


def test_unrecognized_model_skips_family_and_ceiling_checks():
    _route_with_host("claude-nova-6", "MAX")


def test_empty_model_and_bad_effort_are_loud():
    with pytest.raises(ValidationError):
        _route_with_host("")
    with pytest.raises(ValidationError):
        _route_with_host(ARCHITECT_ID, "ULTRA")


# --- hash and output contract [IA-1b] ---

def test_declared_host_seat_changes_both_hashes():
    a = route(_t(), CFG)
    b = _route_with_host(ARCHITECT_ID, "MAX")
    assert a["request_sha256"] != b["request_sha256"]
    assert a["decision_fingerprint"] != b["decision_fingerprint"]


def test_undeclared_preserves_legacy_hash_by_key_omission():
    # Pinned from the pre-host_seat canonical payload for `_t()` at this
    # policy/configuration. An unconditional `"host_seat": null` changes this
    # digest, so this is a regression test for key omission rather than two
    # equivalent instances of the current implementation.
    assert request_sha256_of(_t()) == (
        "c92c316c148058bee7609995a276c8607b5dd0eb822189de0686b5c085b3204e")


def test_whitespace_padded_registered_model_is_normalized_for_lookup_and_hash():
    plain = _route_with_host(ID("claude_worker_fast"))
    padded = _route_with_host(" \tclaude-haiku-4-5-20251001\n ")
    assert padded["host_seat_advisory"]["declared"] == {
        "model": ID("claude_worker_fast"), "effort": None}
    assert padded["host_seat_advisory"]["model_comparison"] == "at"
    assert padded["request_sha256"] == plain["request_sha256"]


def test_whitespace_padded_registered_model_cannot_bypass_policy_validation():
    with pytest.raises(ValidationError, match="family"):
        _route_with_host(f" {ID('openai_reasoning')} ")
    with pytest.raises(ValidationError, match="ceiling"):
        _route_with_host(f"\t{ID('xai_frontier')}\n", "MAX", over=dict(runtime="grok"))


def test_model_only_equals_model_with_null_effort():
    t1 = _t()
    t1._host_seat = {"model": ARCHITECT_ID, "effort": None}
    t2 = _t()
    t2._host_seat = {"model": ARCHITECT_ID}
    out1, out2 = route(t1, CFG), route(t2, CFG)
    assert out1["request_sha256"] == out2["request_sha256"]
    assert (out1["host_seat_advisory"]["declared"]
            == out2["host_seat_advisory"]["declared"]
            == {"model": ARCHITECT_ID, "effort": None})


# --- Task 4 — id-free advisory surfaces and terminal path precision. ---

def _below_note(out):
    notes = [n for n in out["notes"] if n.startswith("host seat below")]
    assert len(notes) <= 1
    return notes[0] if notes else None


def test_below_note_is_id_free_and_pinned_both_axes():
    out = _route_with_host(ID("claude_worker_fast"), "HIGH",
                           over=dict(uncertainty=3, blast_radius=2))
    note = _below_note(out)
    assert "model tier 0 < 1" in note and "effort HIGH < MAX" in note
    assert not any(_whole_token(i, note) for i in REGISTRY_IDS)


def test_below_note_clauses_per_axis():
    # Model only: MAX effort at the MAX ask. Clauses join with "; "; the
    # raised_by suffix contains only codes, so the prose has no effort clause.
    out = _route_with_host(ID("claude_worker_fast"), "MAX",
                           over=dict(uncertainty=3, blast_radius=2))
    note = _below_note(out)
    assert "model tier 0 < 1" in note
    assert "effort" not in note.split("[")[0]
    # Effort only.
    out2 = _route_with_host(ARCHITECT_ID, "HIGH",
                            over=dict(blast_radius=2))
    note2 = _below_note(out2)
    assert "effort HIGH < MAX" in note2 and "model tier" not in note2
    # An unrecognized model still has an effort-only shortfall.
    out3 = _route_with_host("claude-nova-6", "HIGH",
                            over=dict(blast_radius=2))
    note3 = _below_note(out3)
    assert "effort HIGH < MAX" in note3 and "model tier" not in note3


def test_no_note_when_at_above_or_undeclared():
    for out in (route(_t(), CFG),
                _route_with_host(ARCHITECT_ID, "MAX")):
        assert _below_note(out) is None


def test_ia1_route_is_invariant_to_host_seat():
    over = dict(task_class="DEBUGGING", uncertainty=2,
                flags=["auth_sensitive"])
    a = route(_t(**over), CFG)
    t = _t(**over)
    t._host_seat = {"model": ID("claude_worker_fast"), "effort": "LOW"}
    b = route(t, CFG)
    volatile = {"host_seat_advisory", "request_sha256",
                "decision_fingerprint", "notes", "rationale"}
    assert ({k: v for k, v in a.items() if k not in volatile}
            == {k: v for k, v in b.items() if k not in volatile})
    assert a["notes"] == [n for n in b["notes"]
                          if not n.startswith("host seat")]


def test_terminal_keeps_declared_and_is_path_precise():
    t = _t(task_class="ARCHITECTURE", uncertainty=3,
           unavailable_models=[m["id"] for m in CFG["models"].values()])  # Exhaust fallbacks too.
    t._host_seat = {"model": ID("claude_worker_balanced"), "effort": "HIGH"}
    out = route(t, CFG)
    assert out["terminal"] is not None
    adv = out["host_seat_advisory"]
    assert adv["declared"]["model"] == ID("claude_worker_balanced")
    assert isinstance(adv["policy_ask"]["tier"], int)
    assert adv["model_comparison"] == "below"  # Tier 1 < 3.
    # Path precision: after scrubbing only declared.model, that id is absent
    # everywhere except top-level caller-input echoes.
    scrubbed = copy.deepcopy(out)
    scrubbed["host_seat_advisory"]["declared"]["model"] = None
    dumped = _json.dumps(scrubbed)
    echoed = set(out["unavailable_models"]) | set(out["excluded_prior_failures"])
    assert not ({i for i in REGISTRY_IDS if _whole_token(i, dumped)} - echoed)


def test_text_advisory_line_is_conditional_and_id_free(capsys):
    argv = ["--class", "IMPLEMENTATION", "--complexity", "1",
            "--uncertainty", "1", "--blast-radius", "2",
            "--reversibility", "1"]
    rc = main(argv + ["--host-model", ID("claude_worker_fast"),
                      "--host-effort", "HIGH"])
    assert rc == 0  # HIGH band, no gate.
    txt = capsys.readouterr().out
    line = next(l for l in txt.splitlines()
                if l.startswith("host-seat advisory"))
    assert "upgrade recommended" in line
    assert not any(_whole_token(i, line) for i in REGISTRY_IDS)
    rc2 = main(argv)  # Undeclared: no advisory line.
    assert rc2 == 0
    assert "host-seat advisory" not in capsys.readouterr().out


# --- RouteRequestV1 / CLI ---

def _req(**extra):
    return {"route_schema_version": 1, "task_class": "IMPLEMENTATION",
            "complexity": 1, "uncertainty": 1, "blast_radius": 1,
            "reversibility": 1, **extra}


def test_request_v1_accepts_optional_host_seat():
    task = task_from_request_v1(_req(host_seat={"model": ARCHITECT_ID,
                                                "effort": "MAX"}))
    assert task._host_seat == {"model": ARCHITECT_ID, "effort": "MAX"}


def test_request_v1_host_seat_type_and_keys_are_strict():
    for bad in (ARCHITECT_ID, [ARCHITECT_ID],
                {"model": ARCHITECT_ID, "mode": "x"}):
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
                   host_seat={"model": ARCHITECT_ID})
    rc = main(["--json", _json.dumps(payload)])
    assert rc == 2


def test_request_json_wins_over_host_flags(tmp_path, capsys):
    p = tmp_path / "req.json"
    p.write_text(_json.dumps(_req()))
    rc = main(["--request-json", str(p), "--host-model", ARCHITECT_ID,
               "--format", "json"])
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["host_seat_advisory"]["declared"] is None


def test_cli_host_flags_declare_on_flags_path(capsys):
    rc = main(["--class", "IMPLEMENTATION", "--complexity", "1",
               "--uncertainty", "1", "--blast-radius", "1",
               "--reversibility", "1",
               "--host-model", ID("claude_worker_fast"),
               "--host-effort", "HIGH", "--format", "json"])
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["host_seat_advisory"]["declared"] == {
        "model": ID("claude_worker_fast"), "effort": "HIGH"}


UNRECOGNIZED_NOTE = "host seat model is not in the registry"


def test_unrecognized_host_model_leaves_a_note_and_changes_nothing_else():
    out = _route_with_host("claude-nova-6", "HIGH")
    assert out["host_seat_advisory"]["model_comparison"] == "unrecognized"
    notes = [n for n in out["notes"] if UNRECOGNIZED_NOTE in n]
    assert len(notes) == 1 and "model_comparison=unrecognized" in notes[0]
    assert not any(m["id"] in notes[0] for m in CFG["models"].values())
    assert "claude-nova-6" not in notes[0]
    plain = route(_t(), CFG)
    for key in ("selected_model", "risk_band", "terminal", "requires_human_confirmation"):
        assert out[key] == plain[key], key


def test_registered_host_model_leaves_no_unrecognized_note():
    out = _route_with_host(ARCHITECT_ID, "HIGH")
    assert not any(UNRECOGNIZED_NOTE in n for n in out["notes"])


def test_architect_host_on_grok_runtime_is_a_family_mismatch():
    with pytest.raises(ValidationError):
        _route_with_host(ARCHITECT_ID, "HIGH", over=dict(runtime="grok"))
