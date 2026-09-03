import copy
import itertools
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from route_task import Task, default_config, route

CFG = default_config()
TIER_OF = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}


def _raised(out):  return [n for n in out["notes"] if n.startswith("execution band") and " raised worker" in n]
def _yielded(out): return [n for n in out["notes"] if n.startswith("execution band") and " yielded " in n]
ID = lambda key: CFG["models"][key]["id"]          # noqa: E731
ARCHITECT_ID = ID("claude_architect")
CONF = CFG["router"]["confidence"]
EXTRA_REVIEW_BELOW = CONF["extra_review_below"]     # 0.80
ESCALATE_BELOW = CONF["escalate_below"]             # 0.60
BASE = dict(task_class="IMPLEMENTATION", complexity=1, uncertainty=1,
            blast_radius=1, reversibility=1, runtime="claude_code")


def _t(**over):
    return Task(**{**BASE, **over})


def _r(**over):
    return route(_t(**over), CFG)


LUNA, HAIKU = ID("openai_worker_fast"), ID("claude_worker_fast")

# (uncertainty, prior_models, flags, expected confidence AFTER the change,
#  expect review-band promotion, expect ESCALATE_ROUTING terminal)
BOUNDARY_ROWS = [
    (2, [],            [],                                   0.87, False, False),
    (2, [],            ["bridge_down"],                      0.81, False, False),   # the one intended change
    (1, [],            ["bridge_down"],                      0.89, False, False),
    (2, [LUNA],        ["bridge_down"],                      0.76, True,  False),
    (3, [],            [],                                   0.75, True,  False),
    (3, [],            ["bridge_down"],                      0.69, True,  False),
    (2, [],            ["unknown_root_cause"],               0.77, True,  False),
    (1, [],            ["unknown_root_cause", "bridge_down"], 0.79, True,  False),
    (1, [LUNA],        ["bridge_down"],                      0.84, False, False),
    (1, [LUNA, HAIKU], ["bridge_down"],                      0.74, True,  False),
    (3, [],            ["unknown_root_cause", "bridge_down"], 0.59, False, True),
    (3, [LUNA],        ["bridge_down"],                      0.64, True,  False),
    (2, [LUNA, HAIKU], ["unknown_root_cause", "bridge_down"], 0.56, False, True),
]


@pytest.mark.parametrize("u,priors,flags,expected,promoted,terminal", BOUNDARY_ROWS)
def test_boundary_table(u, priors, flags, expected, promoted, terminal):
    out = _r(uncertainty=u, prior_failures=len(priors), prior_models=list(priors), flags=list(flags))
    assert out["routing_confidence"] == expected, out["routing_confidence"]
    if terminal:
        assert out["terminal"] == "ESCALATE_ROUTING" and out["selected_model"] is None
        return
    assert out["terminal"] is None, out["terminal"]
    overrides = [o for o in out["band_overrides_applied"] if o.startswith("low_routing_confidence")]
    assert bool(overrides) == promoted, (out["band_overrides_applied"], out["review"]["band"])


def test_a_lone_fallback_never_promotes_at_modal_uncertainty():
    """DD-3 principle, reconstructed from INPUTS: no prior failures, no
    unknown_root_cause, uncertainty <= 2, and a recorded fallback -> the
    confidence stays at or above extra_review_below."""
    for u in (0, 1, 2):
        out = _r(uncertainty=u, flags=["bridge_down"])
        assert out["fallbacks_applied"], "bridge_down must record the degraded binding"
        assert out["routing_confidence"] >= EXTRA_REVIEW_BELOW, (u, out["routing_confidence"])
        assert not any(o.startswith("low_routing_confidence") for o in out["band_overrides_applied"])


def test_u3_unknown_root_cause_fallback_stays_terminal():
    """The 0.60 gate on the riskiest profile must survive the penalty change."""
    out = _r(uncertainty=3, flags=["unknown_root_cause", "bridge_down"])
    assert out["routing_confidence"] < ESCALATE_BELOW
    assert out["terminal"] == "ESCALATE_ROUTING"
    assert out["selected_model"] is None


from test_invariants import (DIMENSIONS, FLAG_SETS, PRIOR_HISTORY, RUNTIMES,  # noqa: E402
                             SCARCITY, TASK_CLASSES)

OLD, NEW = 0.10, 0.06
FULL = os.environ.get("DMR_FULL_SWEEP") == "1"
SCARCITY_DOMAIN = SCARCITY if FULL else [[], [ARCHITECT_ID], [ID("claude_worker_balanced"), ID("openai_worker_fast")]]
HISTORY_DOMAIN = PRIOR_HISTORY if FULL else [([], 0), ([ID("claude_senior")], 1), ([HAIKU, LUNA], 2)]
GRID = [  # penalty-relevant axes on three band corners; the fallback origin is bridge_down OR one withheld model
    dict(complexity=c, uncertainty=u, blast_radius=b, reversibility=r,
         prior_failures=len(p), prior_models=list(p), flags=list(f), unavailable_models=list(w))
    for (c, b, r) in ((0, 0, 0), (1, 1, 1), (2, 2, 0))
    for u in (0, 1, 2, 3)
    for p in ([], [LUNA], [LUNA, HAIKU])
    for f in ([], ["unknown_root_cause"], ["bridge_down"], ["unknown_root_cause", "bridge_down"])
    for w in ([], [LUNA], [ID("xai_frontier")])      # [P2-sol-F9] withhold the LOW/MEDIUM worker, then the HIGH worker
]


def _cfg(penalty):
    cfg = copy.deepcopy(CFG)
    cfg["router"]["confidence"]["penalties"]["any_fallback"] = penalty
    return cfg


def _inputs():
    for task_class, dims, flags, runtime, scarce, (prior, failures) in itertools.product(
            TASK_CLASSES, DIMENSIONS, FLAG_SETS, RUNTIMES, SCARCITY_DOMAIN, HISTORY_DOMAIN):
        c, u, b, r = dims
        yield dict(task_class=task_class, complexity=c, uncertainty=u, blast_radius=b,
                   reversibility=r, flags=list(flags), runtime=runtime,
                   unavailable_models=list(scarce), prior_failures=failures, prior_models=list(prior))
    for g in GRID:
        for runtime in RUNTIMES:
            yield dict(task_class="DEBUGGING", runtime=runtime, **g)


def _in_class_c(inp, a, b):
    """u2 + a fallback origin + no other confidence signals, risk not CRITICAL.

    `fallbacks_applied` is taken from EITHER penalty. Promoting the review
    can drop the seat that produced the fallback (a withheld worker_balanced
    is needed at MEDIUM and not at HIGH), so the 0.10 side may look like it
    had no fallback while the 0.06 side still does. That ghost is class C.
    """
    return (inp["uncertainty"] == 2 and inp["prior_failures"] == 0
            and "unknown_root_cause" not in inp["flags"]
            and (bool(a["fallbacks_applied"]) or bool(b["fallbacks_applied"]))
            and a["risk_band"] != "CRITICAL")


STABLE_OUTSIDE_C = ("selected_role", "selected_model", "selected_effort", "worker_seat",
                    "fallbacks_applied", "terminal")
REVIEW_KEYS = ("band", "effort", "reviewers", "required_checks")
EFFORT_ORDER = ["MINIMAL", "LOW", "MEDIUM", "HIGH", "VERY_HIGH", "MAX"]
CRITICAL_ONLY_CAUSES = {"critical_review_band", "effort_below_floor", "no_adjudicator"}


def test_migration_0_10_to_0_06_changes_exactly_class_c():
    old_cfg, new_cfg = _cfg(OLD), _cfg(NEW)
    seen_c = seen_withheld_fallback = 0
    for inp in _inputs():
        try:
            task = Task(**inp)
        except Exception:
            continue                       # invalid combinations are not routes
        a, b = route(task, old_cfg), route(task, new_cfg)
        assert a["risk_band"] == b["risk_band"], inp
        assert a["host_seat_advisory"]["policy_ask"] == b["host_seat_advisory"]["policy_ask"], inp
        in_c = _in_class_c(inp, a, b)
        seen_c += in_c
        both_fb = bool(a["fallbacks_applied"]) and bool(b["fallbacks_applied"])
        neither_fb = not a["fallbacks_applied"] and not b["fallbacks_applied"]
        delta = round(b["routing_confidence"] - a["routing_confidence"], 2)
        if both_fb:
            assert delta == 0.04, (inp, delta)
        elif neither_fb:
            assert delta == 0.0, (inp, delta)
        elif b["fallbacks_applied"] and not a["fallbacks_applied"]:
            assert delta == -0.06, (inp, delta)   # ghost: only the new side still records the origin
        else:
            assert delta == 0.10, (inp, delta)    # old side paid the penalty, new side does not
        if inp.get("unavailable_models") and "bridge_down" not in inp["flags"] and (
                a["fallbacks_applied"] or b["fallbacks_applied"]):
            seen_withheld_fallback += 1                       # [P2-sol-F9] the withheld-model origin really fired
        review_differs = any(a["review"][k] != b["review"][k] for k in REVIEW_KEYS) \
            or a["band_overrides_applied"] != b["band_overrides_applied"]
        assert review_differs == in_c, (inp, a["review"]["band"], b["review"]["band"])
        if not in_c:
            for key in STABLE_OUTSIDE_C:
                assert a[key] == b[key], (inp, key, a[key], b[key])
            assert a["human_control_causes"] == b["human_control_causes"], inp
            assert a["requires_human_confirmation"] == b["requires_human_confirmation"], inp
            continue
        # The worker is chosen from the RISK band and the EXECUTION band, never
        # from the review band — but since 1.13.0 the execution cell is adopted
        # only if the settled review contract does not get worse, and the
        # confidence penalty moves exactly that contract. So inside class C the
        # two sides can seat different workers: the side whose review the
        # penalty promoted yields to the risk-band worker, the other adopts the
        # execution cell. That is the guard working, and it only ever moves the
        # seat UP relative to the yielding side (design 2026-09-03 DD-2 S6).
        # A HIGH review that cannot independently staff two reviewers can also
        # terminalise the 0.10 side and seat a worker only on 0.06.
        if a["terminal"] is None and b["terminal"] is None:
            if a["selected_role"] != b["selected_role"]:
                yielder, adopter = (a, b) if _yielded(a) else (b, a)
                assert _yielded(yielder) and _raised(adopter), (inp, a["notes"], b["notes"])
                # The yielding side is the one whose review the penalty promoted.
                assert yielder["review"]["band"] != adopter["review"]["band"], inp
                assert TIER_OF[adopter["selected_model"]] > TIER_OF[yielder["selected_model"]], inp
            else:
                for key in ("selected_role", "selected_model", "worker_seat"):
                    assert a[key] == b[key], (inp, key, a[key], b[key])
        if a["selected_effort"] not in (None, b["selected_effort"]) and b["selected_effort"] is not None:
            assert EFFORT_ORDER.index(b["selected_effort"]) < EFFORT_ORDER.index(a["selected_effort"]), (
                inp, a["selected_effort"], b["selected_effort"])
        causes_differ = a["human_control_causes"] != b["human_control_causes"]
        conf_differs = a["requires_human_confirmation"] != b["requires_human_confirmation"]
        core_high = a["risk_band"] == "HIGH"
        if core_high and causes_differ:
            added = set(b["human_control_causes"]) - set(a["human_control_causes"])
            dropped = set(a["human_control_causes"]) - set(b["human_control_causes"])
            assert not added, (inp, added)
            assert dropped <= CRITICAL_ONLY_CAUSES, (inp, dropped)
        if conf_differs:
            assert a["requires_human_confirmation"] is True
            assert b["requires_human_confirmation"] is False
        if b["terminal"] is None:
            assert b["review"]["band"] == a["risk_band"]      # promotion undone: back to the risk band
            table = new_cfg["review"][b["review"]["band"]]     # (v): the new block IS the band's table
            assert b["review"]["effort"] == table["effort"], inp
            assert b["review"]["independence_required"] == table["independent"], inp
            assert b["review"]["required_checks"] == table.get("required_checks", []), inp
            # A deconflicted seat is not the table ([P2-sol-F2][P2-grok-F1]), and neither
            # is a compensated one. `fallback_compensations.principal_architect_to_senior`
            # is `raise_effort_to_MAX_and_add_second_review`, so when the judge falls back
            # the plan gains a reviewer the band's table never listed. That path became
            # reachable when the 2026-09-03 sweep expansion added the (3, 2, 0, 0)
            # dimension; 1.12.1 emits the same roster for those inputs, so it is the
            # table's shape, not the execution axis's. Keyed on the compensation itself,
            # not on `route_path == "disagreement"`: the disagreement route seats a JUDGE,
            # and excluding all of it skipped 117 class-C rows to cover 24 [impl-R1-opus-F2].
            if not b["review"]["self_review_avoided"] and not b["review"]["compensating_reviewers"]:
                if "reviewers" in table:
                    assert b["review"]["reviewers"] == table["reviewers"], inp
                else:                                          # MEDIUM seats one candidate
                    assert len(b["review"]["reviewers"]) == 1 and b["review"]["reviewers"][0] in table["candidates"], inp
    assert seen_c >= 20, seen_c                               # the class is actually exercised
    assert seen_withheld_fallback >= 3, seen_withheld_fallback  # every band corner saw a withheld-model fallback
