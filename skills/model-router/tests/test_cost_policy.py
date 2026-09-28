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
