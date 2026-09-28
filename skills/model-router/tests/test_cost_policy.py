"""Part B (1.17.0) cost-policy rules, one example group per rule
(design 2026-09-25 DD-B1..DD-B8; the grid-wide ledger is test_policy_rules.py).

Every route here goes through RouteRequestV1 — the Part B fields exist only
there — and through the same exit mapping the CLI uses.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import route_task as rt  # noqa: E402

CFG = rt.load_config()
ID = lambda key: CFG["models"][key]["id"]                       # noqa: E731
TIER = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
FAMILY = {m["id"]: m["family"] for m in CFG["models"].values()}


def _req(task_class, dims, runtime="claude_code", flags=(), **extra):
    c, u, b, r = dims
    req = {"route_schema_version": 1, "task_class": task_class, "complexity": c,
           "uncertainty": u, "blast_radius": b, "reversibility": r, "runtime": runtime,
           "flags": list(flags)}
    req.update(copy.deepcopy(extra))
    return req


def _exit(out):
    if out["terminal"]:
        return 1
    if out["requires_human_confirmation"]:
        return CFG["human_in_the_loop"]["human_gate_exit_status"]
    return 4 if out["human_confirmation_deferred"] else 0


def _route(req, cfg=None):
    """(exit, route) — (2, None) for input the router refuses."""
    try:
        out = rt.route(rt.task_from_request_v1(copy.deepcopy(req)), cfg or CFG)
    except rt.ValidationError:
        return 2, None
    return _exit(out), out


def _without(req, key):
    return {k: v for k, v in req.items() if k != key}


# --------------------------------------------------------------------------
# B1 — the implementer of completed work (DD-B1, C6)
# --------------------------------------------------------------------------

def test_b1_a_declared_implementer_is_reviewed_at_band_across_families():
    """The defect this closes (1.15.0 memory, audit F8): the work was done by
    the claude senior seat, the caller restricted review to openai + xai to
    force a cross-family review, and the router assumed its OWN worker had
    done the work — seating it as the implementer and a tier-1 reviewer
    below the band."""
    req = _req("IMPLEMENTATION", (2, 2, 2, 2), flags=["security_sensitive"],
               local_policy={"allowed_families": ["openai", "xai"]},
               implementer={"model_id": ID("claude_senior")})
    _, before = _route(_without(req, "implementer"))
    assert "review_below_band" in before["human_control_causes"]

    code, out = _route(req)
    assert out["selected_model"] == ID("claude_senior")
    assert out["worker_seat_state"] == "already_executed"
    assert out["implementer_declared"] is True
    assert out["implementer_source"] == "caller_declared"
    tiers = [TIER[m] for m in out["review"]["reviewer_models"]]
    assert len(tiers) == 2 and min(tiers) >= 2, out["review"]["reviewer_models"]
    assert {FAMILY[m] for m in out["review"]["reviewer_models"]} <= {"openai", "xai"}
    assert "review_below_band" not in out["human_control_causes"]
    assert out["review"]["review_depth_reduced"] == []
    # The worker seat already ran: nothing to dispatch for it.
    seats = out["dispatch_seats"]
    assert [s["seat"] for s in seats if s["seat"] != "judge"] == ["reviewer-1", "reviewer-2"]
    assert ID("claude_senior") not in {s["model_id"] for s in seats}
    assert [s["model_id"] for s in seats if s["seat"] != "judge"] == out["review"]["reviewer_models"]
    assert any("caller-declared" in n for n in out["notes"])
    assert code == 3          # CRITICAL band: the human gate stays


def test_b1_a_weaker_implementer_is_gated_and_a_hotfix_does_not_defer_it():
    # HIGH x NORMAL: the policy's worker is worker_balanced (tier 1).
    req = _req("IMPLEMENTATION", (2, 2, 1, 1), implementer={"model_id": ID("openai_worker_fast")})
    _, free = _route(_without(req, "implementer"))
    assert TIER[free["selected_model"]] == 1 and _exit(free) == 0
    code, out = _route(req)
    assert code == 3, out["human_control_causes"]
    assert out["human_control_causes"] == ["implementer_below_worker_tier"]
    assert rt.CAUSE_REASONS["implementer_below_worker_tier"] in out["rationale"]
    # A live incident defers the band's own gate, never this one: the review
    # is sized for the worker the policy asked for, not for the one that ran.
    code, out = _route({**req, "flags": ["production_hotfix"]})
    assert code == 3 and not out["human_confirmation_deferred"], out["notes"]
    # At the worker's tier or above there is nothing to confirm.
    code, out = _route({**req, "implementer": {"model_id": ID("xai_frontier")}})
    assert code == 0 and out["human_control_causes"] == []


@pytest.mark.parametrize("task_class", ["REVIEW", "INVESTIGATION"])
def test_b1_only_write_classes_take_an_implementer(task_class):
    """REVIEW already names its source's author (`review_context`); a
    read-only class's worker produces a judgement, not completed work."""
    code, _ = _route(_req(task_class, (1, 1, 1, 1), implementer={"model_id": ID("claude_senior")}))
    assert code == 2


@pytest.mark.parametrize("bad", [{"model_id": "no-such-model"}, {}, "claude", {"model_id": 3},
                                 {"model_id": "__ID__", "effort": "HIGH"}])
def test_b1_the_declaration_is_strict(bad):
    if isinstance(bad, dict) and bad.get("model_id") == "__ID__":
        bad = {**bad, "model_id": ID("claude_senior")}
    assert _route(_req("IMPLEMENTATION", (1, 1, 1, 1), implementer=bad))[0] == 2


def test_b1_a_superseded_id_is_a_valid_implementer():
    """The work may have finished before the registry moved on."""
    history = next(m["id"] for m in CFG["models"].values() if m.get("history_of") == "claude_senior")
    code, out = _route(_req("IMPLEMENTATION", (2, 1, 1, 1), implementer={"model_id": history}))
    assert code == 0 and out["selected_model"] == history
    assert out["worker_seat_state"] == "already_executed"
    assert history not in out["review"]["reviewer_models"]


def test_b1_availability_binds_the_review_seats_not_the_implementer():
    """The worker already executed: withholding its model, or its whole
    family, or losing the bridge, changes who can REVIEW it."""
    senior = ID("claude_senior")
    for extra in ({"availability_snapshot": {"unavailable_models": [senior]}},
                  {"local_policy": {"allowed_families": ["openai"]}},
                  {"flags": ["bridge_down"], "runtime": "codex"}):
        req = {**_req("IMPLEMENTATION", (2, 1, 1, 1), implementer={"model_id": senior}), **extra}
        code, out = _route(req)
        assert code in (0, 3), (extra, out and out["terminal"])
        assert out["selected_model"] == senior, extra
        assert senior not in out["review"]["reviewer_models"], extra
        assert not any(f.startswith(out["selected_role"] + ":") for f in out["fallbacks_applied"]), extra


def test_b1_without_a_declaration_nothing_changes_shape():
    _, out = _route(_req("IMPLEMENTATION", (2, 1, 1, 1)))
    assert out["worker_seat_state"] == "to_dispatch"
    assert out["implementer_declared"] is False and out["implementer_source"] is None
    assert "dispatch_seats" not in out


# --------------------------------------------------------------------------
# B2 — uncertainty is counted once (DD-B2, C1-iii)
# --------------------------------------------------------------------------

def test_b2_uncertainty_that_already_raised_the_band_does_not_promote_it_again():
    """Audit F3: `DOCUMENTATION c0u3` scores 6 (MEDIUM) only because
    uncertainty weighs 2; with weight 1 it is LOW. 1.16.1 then charged the
    same uncertainty again as a 0.20 confidence penalty and promoted the
    review to HIGH — two frontier reviewers on a luna documentation edit."""
    code, out = _route(_req("DOCUMENTATION", (0, 3, 0, 0)))
    assert out["risk_band"] == "MEDIUM" and out["review"]["band"] == "MEDIUM", out["review"]["band"]
    assert not any(o.startswith("low_routing_confidence") for o in out["band_overrides_applied"])
    # The reported confidence still carries the penalty; only the promotion
    # decision drops it.
    assert out["routing_confidence"] == 0.75 and code == 0


def test_b2_other_signals_still_promote_a_band_uncertainty_raised():
    # c0u2: 4 (MEDIUM), 2 with weight 1 (LOW). Unknown root cause (0.10) and
    # a fallback (0.06) take the uncertainty-free confidence to 0.79.
    req = _req("DEBUGGING", (0, 2, 0, 0), flags=["unknown_root_cause"],
               availability_snapshot={"unavailable_models": [ID("openai_worker_fast")]})
    _, out = _route(req)
    assert out["review"]["band"] == "HIGH"
    assert out["routing_confidence"] == 0.71


def test_b2_a_band_an_override_set_is_not_raised_by_uncertainty():
    # The critical-domain override puts c0u3 at HIGH with or without the
    # double weight, so the penalty is a separate fact and still promotes.
    _, out = _route(_req("IMPLEMENTATION", (0, 3, 0, 0), flags=["security_sensitive"]))
    assert out["review"]["band"] == "CRITICAL"
    assert "low_routing_confidence_raised_review_to_CRITICAL" in out["band_overrides_applied"]


def test_b2_escalate_routing_stays_reachable():
    """The rejected alternative (drop the penalty) left the lowest reachable
    confidence at 0.64, so ESCALATE_ROUTING could never fire."""
    _, out = _route(_req("DEBUGGING", (0, 3, 0, 0), flags=["unknown_root_cause", "bridge_down"]))
    assert out["terminal"] == "ESCALATE_ROUTING" and out["routing_confidence"] < 0.60


def test_b2_the_rule_is_one_config_key():
    cfg = copy.deepcopy(CFG)
    cfg["router"]["confidence"]["skip_uncertainty_penalty_when_band_raised"] = False
    _, out = _route(_req("DOCUMENTATION", (0, 3, 0, 0)), cfg)
    assert out["review"]["band"] == "HIGH"          # 1.16.1's promotion
    cfg["router"]["confidence"]["skip_uncertainty_penalty_when_band_raised"] = "yes"
    with pytest.raises(rt.ConfigError):
        _route(_req("DOCUMENTATION", (0, 3, 0, 0)), cfg)


@pytest.mark.parametrize("runtime", ["claude_code", "codex", "grok"])
@pytest.mark.parametrize("task_class", ["IMPLEMENTATION", "REFACTORING", "DEBUGGING"])
def test_b2_counting_uncertainty_once_never_costs_the_worker_a_tier(task_class, runtime):
    """DD-B2's worker drop. c3u3b0r1 scores 10 (HIGH), 7 (MEDIUM) with weight
    1. Without the promotion the lower-tier table plan and the frontier
    execution-cell plan stop sharing a review band, so the execution-cell
    guard yielded the stronger worker — sol fell to a tier-1 seat. The guard
    now weighs the two plans with the promotion 1.16.1 made, and only the
    adopted plan is re-planned without the double-counted penalty."""
    req = _req(task_class, (3, 3, 0, 1), runtime=runtime, reasoning_centric=True)
    cfg = copy.deepcopy(CFG)
    cfg["router"]["confidence"]["skip_uncertainty_penalty_when_band_raised"] = False
    _, before = _route(req, cfg)
    _, out = _route(req)
    assert out["selected_model"] == before["selected_model"], (before["selected_model"], out["selected_model"])
    assert TIER[out["selected_model"]] == 2
    assert out["review"]["band"] == "HIGH" and before["review"]["band"] == "CRITICAL"


# --------------------------------------------------------------------------
# B3 — the MEDIUM reviewer fits the floor (DD-B5, C4)
# --------------------------------------------------------------------------

def test_b3_a_tier_1_worker_gets_the_cheapest_adequate_cross_family_reviewer():
    """Audit F6: a grok (tier 1) worker's MEDIUM review went to sol (tier 2)
    through a per-implementer preference; the band asks for tier 1."""
    _, out = _route(_req("IMPLEMENTATION", (3, 0, 1, 0)))      # risk 5 MEDIUM, exec 9 NORMAL
    assert out["selected_model"] == ID("xai_frontier")
    assert out["review"]["reviewer_models"] == [ID("claude_worker_balanced")]
    assert out["review"]["reviewers"] == ["worker_balanced_alt"]
    assert out["cross_family_review"] is True and out["human_control_causes"] == []


def test_b3_the_requirement_is_the_implementer_tier_when_that_is_higher():
    # A declared tier-2 implementer: a tier-1 reviewer would review work
    # stronger than itself, so a tier-2 cross-family seat is taken.
    _, out = _route(_req("IMPLEMENTATION", (2, 1, 1, 1),
                         implementer={"model_id": ID("claude_senior")}))
    assert out["review"]["band"] == "MEDIUM"
    [model] = out["review"]["reviewer_models"]
    assert TIER[model] == 2 and FAMILY[model] != "claude"


def test_b3_no_cross_family_candidate_falls_back_within_the_family_at_the_requirement():
    _, out = _route(_req("IMPLEMENTATION", (2, 0, 1, 0), flags=["bridge_down"]))
    assert out["review"]["band"] == "MEDIUM"
    assert out["review"]["reviewer_models"] == [ID("claude_worker_balanced")]
    assert out["cross_family_review"] is False
    assert out["human_control_causes"] == []


def test_b3_a_same_family_fallback_below_the_implementer_is_disclosed_and_gated():
    """The fallback carries the same requirement, and the shortfall check
    counts the implementer: a tier-2 implementer reviewed by the only
    tier-1 seat left is a review below its band."""
    req = _req("IMPLEMENTATION", (2, 1, 1, 1), runtime="codex", flags=["bridge_down"],
               implementer={"model_id": ID("openai_reasoning")},
               availability_snapshot={"unavailable_models": [ID("openai_frontier")]})
    code, out = _route(req)
    assert out["review"]["band"] == "MEDIUM"
    [short] = out["review"]["review_depth_reduced"]
    assert short["band_requires"] == 2 and short["capability_tier"] == 1
    assert "review_below_band" in out["human_control_causes"] and code == 3


def test_b3_a_binding_only_role_resolves_to_its_own_seat_only():
    policy = rt.Policy.of(CFG)
    task = rt.Task(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, blast_radius=1,
                   reversibility=1)
    task.validate(policy)
    resolver = rt.Resolver(task, policy)
    assert resolver._candidates("worker_balanced_alt") == ["claude_worker_balanced"]
    task = rt.Task(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, blast_radius=1,
                   reversibility=1, unavailable_models=[ID("claude_worker_balanced")])
    task.validate(policy)
    assert rt.Resolver(task, policy).peek("worker_balanced_alt") is None


def test_b3_the_preference_table_is_gone():
    assert "preferred_by_implementer" not in CFG["review"]["MEDIUM"]
    assert "worker_balanced_alt" in CFG["review"]["MEDIUM"]["candidates"]


# --------------------------------------------------------------------------
# B4 — LOW effort cap (DD-B3, C2) and LOW review by deterministic checks
# (DD-B4, C3). REVIEW-class LOW and the seat matrix's REVIEW row are B6.
# --------------------------------------------------------------------------

def test_b4_a_low_risk_task_does_not_get_the_class_tables_high_effort():
    """Audit F4: `DEBUGGING c1` routed luna at HIGH on a LOW-risk task."""
    _, out = _route(_req("DEBUGGING", (1, 0, 0, 0)))
    assert out["risk_band"] == "LOW"
    assert out["selected_effort"] == out["selected_effort_effective"] == "MEDIUM"
    assert any(n.startswith("effort cap: band LOW capped effort HIGH at MEDIUM") for n in out["notes"])


@pytest.mark.parametrize("why,extra", [
    ("unknown root cause: the effort is doing the diagnosing",
     dict(flags=["unknown_root_cause"])),
    ("a retry after a capability failure",
     dict(attempt_outcomes=[{"attempt_id": "a1", "model_id": "__FAST__", "kind": "capability_failure",
                             "evidence_sha256": "1" * 64}])),
])
def test_b4_the_cap_skips_diagnosis_and_retries(why, extra):
    extra = copy.deepcopy(extra)
    for row in extra.get("attempt_outcomes", []):
        row["model_id"] = ID("openai_worker_fast")
    _, out = _route(_req("DEBUGGING", (1, 0, 0, 0), **extra))
    assert out["risk_band"] == "LOW"
    assert out["selected_effort"] in ("HIGH", "MAX"), why
    assert not any(n.startswith("effort cap:") for n in out["notes"]), why


def test_b4_floors_and_local_minimums_still_win_and_the_note_goes_with_the_cap():
    # Execution HARD at LOW risk: the HARD floor lifts the capped effort back.
    _, hard = _route(_req("DEBUGGING", (3, 0, 0, 0),
                          flags=["unfamiliar_codebase", "tool_heavy", "cross_service_change"]))
    assert hard["risk_band"] == "LOW" and hard["execution_band"] == "HARD"
    assert hard["selected_effort"] == "HIGH"
    assert not any(n.startswith("effort cap:") for n in hard["notes"])
    _, local = _route(_req("DEBUGGING", (1, 0, 0, 0), local_policy={"minimum_effort": "HIGH"}))
    assert local["selected_effort"] == "HIGH"
    assert not any(n.startswith("effort cap:") for n in local["notes"])


def test_b4_a_low_review_is_the_deterministic_checks():
    """U-6. 78% of LOW routes re-read the worker's work with the worker's own
    model in a second process; the checks now are the review."""
    code, out = _route(_req("IMPLEMENTATION", (1, 0, 1, 0)))
    rv = out["review"]
    assert rv["band"] == "LOW" and code == 0
    assert rv["reviewers"] == [] and rv["reviewer_models"] == []
    assert rv["effort"] is None and rv["judge"] is None
    assert rv["mode"] == "deterministic_checks"
    assert rv["required_checks"] == ["tests", "lint"]
    assert rv["review_independence"] == "not_applicable"
    assert "deterministic checks (tests, lint) must pass" in out["rationale"]
    assert rt.Policy.of(CFG).band_reviewer_floor["LOW"] is None


@pytest.mark.parametrize("extra,reason", [
    (dict(flags=["review_disagreement"]), "review_disagreement"),
    (dict(availability_snapshot={"checks_available": False}), "checks_unavailable"),
    (dict(local_policy={"minimum_reviewers": 1}), "minimum_reviewers"),
    (dict(local_policy={"minimum_provider_families": 2}), "minimum_provider_families"),
])
def test_b4_what_checks_cannot_carry_leaves_low_for_medium(extra, reason):
    code, out = _route(_req("IMPLEMENTATION", (1, 0, 1, 0), **extra))
    assert out["risk_band"] == "LOW"
    assert out["review"]["band"] == "MEDIUM", out["band_overrides_applied"]
    assert f"low_band_{reason}_raised_review_to_MEDIUM" in out["band_overrides_applied"]
    # Settled off LOW: a model review, never advertised as the checks.
    assert out["review"]["mode"] == "model_review" and len(out["review"]["reviewers"]) == 1
    assert out["review"]["required_checks"] == []
    assert out["terminal"] is None


def test_b4_a_reviewer_floor_takes_the_lowest_band_that_can_meet_it():
    _, two = _route(_req("IMPLEMENTATION", (1, 0, 1, 0), local_policy={"minimum_reviewers": 2}))
    assert two["review"]["band"] == "HIGH" and two["terminal"] is None     # 1.16.1: terminal
    assert len(two["review"]["reviewers"]) == 2
    _, three = _route(_req("IMPLEMENTATION", (1, 0, 1, 0), local_policy={"minimum_reviewers": 3}))
    assert three["terminal"] == "UNSATISFIABLE_LOCAL_POLICY"
    assert not any(o.startswith("low_band_") for o in three["band_overrides_applied"])
    _, fams = _route(_req("IMPLEMENTATION", (1, 0, 1, 0), local_policy={"minimum_provider_families": 3}))
    assert fams["review"]["band"] == "HIGH"


def test_b4_the_escape_only_raises_and_only_from_low():
    # A CRITICAL route with a floor it already meets stays where it is, and
    # its confidence promotion is not undone by a second pass.
    _, out = _route(_req("IMPLEMENTATION", (2, 2, 2, 2), local_policy={"minimum_reviewers": 1}))
    assert out["review"]["band"] == "CRITICAL"
    assert not any(o.startswith("low_band_") for o in out["band_overrides_applied"])
    # The escape runs before the fixed point: a confidence promotion can still
    # add its one band on top of it (unknown root cause + a fallback = 0.79).
    req = _req("INVESTIGATION", (1, 0, 0, 0), flags=["unknown_root_cause"],
               availability_snapshot={"checks_available": False,
                                      "unavailable_models": [ID("xai_frontier")]})
    _, out = _route(req)
    assert out["review"]["band"] == "HIGH", out["band_overrides_applied"]
    assert "low_band_checks_unavailable_raised_review_to_MEDIUM" in out["band_overrides_applied"]
    assert "low_routing_confidence_raised_review_to_HIGH" in out["band_overrides_applied"]


def test_b4_a_confidence_promotion_off_low_is_a_model_review():
    """`mode` is read off the settled band: a route promoted from LOW that
    advertised deterministic checks would have its host skip its reviewer."""
    req = _req("IMPLEMENTATION", (1, 0, 1, 0), flags=["unknown_root_cause"],
               availability_snapshot={"unavailable_models": [ID("openai_worker_fast")]})
    _, out = _route(req)
    assert out["review"]["band"] == "MEDIUM" and out["review"]["mode"] == "model_review"


def test_b4_only_the_lowest_band_may_seat_no_reviewer():
    for band in ("MEDIUM", "HIGH", "CRITICAL"):
        cfg = copy.deepcopy(CFG)
        cfg["review"][band] = {"reviewers": [], "effort": None, "independent": False,
                               "required_checks": ["tests"]}
        with pytest.raises(rt.ConfigError):
            rt.Policy(cfg)
    for broken in ({"effort": "MEDIUM"}, {"required_checks": []}, {"independent": True}):
        cfg = copy.deepcopy(CFG)
        cfg["review"]["LOW"].update(broken)
        with pytest.raises(rt.ConfigError):
            rt.Policy(cfg)


def test_b4_the_sentinel_is_never_read_as_zero_elsewhere():
    policy = rt.Policy.of(CFG)
    task = rt.Task(task_class="IMPLEMENTATION", complexity=1, uncertainty=0, blast_radius=1,
                   reversibility=0)
    task.validate(policy)
    resolver = rt.Resolver(task, policy)
    review = {"band": "LOW", "reviewers": ["worker_balanced"], "effort": "HIGH", "independent": False}
    with pytest.raises(rt.RouterInvariantError):
        rt._seat_judge(review, "worker_fast", "principal_architect", policy,
                       type("R", (), {"peek": lambda self, role, write=False: None})())


def test_b4_checks_unavailable_on_the_cli():
    import subprocess
    script = HERE.parent / "scripts" / "route_task.py"
    proc = subprocess.run([sys.executable, str(script), "--class", "IMPLEMENTATION",
                           "--complexity", "1", "--uncertainty", "0", "--blast-radius", "1",
                           "--reversibility", "0", "--checks-unavailable", "--format", "json"],
                          capture_output=True, text=True, timeout=60)
    import json
    out = json.loads(proc.stdout)
    assert out["review"]["band"] == "MEDIUM" and proc.returncode == 0


def test_b4_the_consumer_contract_is_written_down():
    text = " ".join((HERE.parent / "references" / "control-loop.md").read_text().split())
    assert "exit 0 with a non-empty `required_checks` means the consumer owes those checks" in text
    assert "deep-loop does not read `required_checks`" in text
