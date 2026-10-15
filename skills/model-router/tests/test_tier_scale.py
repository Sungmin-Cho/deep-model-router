"""The tier scale (issue #63, model-router 1.22.0).

A tier is held by its live rows, so a measurement is read against them — never
against a history row or a seat the measurement is deciding — and a review
replay decides a read-only seat's tier but never moves a writing row's on its
own. These tests re-derive the recorded verdicts and bars from the recorded
numbers with that method, so the ledger, the registry and the method cannot
drift apart silently.

Run:  python3 -m pytest skills/model-router/tests/test_tier_scale.py -q
"""

import re
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import load_config  # noqa: E402

CFG = load_config()
MODELS = CFG["models"]
LEDGER = {e["item"]: e for e in CFG["verification_ledger"]["entries"]}
EVALUATION_MD = (SKILL / "references" / "evaluation.md").read_text()
PROFILES_MD = (SKILL / "references" / "model-profiles.md").read_text()

SEATS = ("meta_worker_balanced", "gemini_worker_balanced")
LIVE_ROW = "Reviewer seat tiers against live anchors (real-review replay, 2026-10-14)"
COUNTERFACTUALS = "Tier counterfactuals after the 2026-10-14 replays"
# Margins of the method (references/evaluation.md, "How a tier is measured").
SEPARATION, CONFIRM_MARGIN = 0.10, 0.05
VERDICT_TIER = {"confirmed": 1, "lowered": 0}


def _text(item: str) -> str:
    return " ".join(LEDGER[item]["evidence"].split())


def _recalls(text: str) -> dict:
    """`<registry id> <recall>` pairs as the row records them (six places)."""
    ids = {m["id"] for m in MODELS.values()}
    out = {}
    for mid, value in re.findall(r"([a-z0-9][a-z0-9.\-]*) (0\.\d{6})\b", text):
        if mid in ids:
            out.setdefault(mid, float(value))
    return out


def _live_rows(tier: int) -> list[str]:
    """Live rows of a tier that may anchor: not history, not a decided seat."""
    return [m["id"] for key, m in MODELS.items()
            if m.get("dispatchable", True) is not False and "history_of" not in m
            and key not in SEATS and m["capability_tier"] == tier]


def _derive(text: str) -> dict:
    measured = _recalls(text)
    tier1 = [measured[i] for i in _live_rows(1) if i in measured]
    tier2 = [measured[i] for i in _live_rows(2) if i in measured]
    a1, t2 = min(tier1), sum(tier2) / len(tier2)
    valid = t2 - a1 >= SEPARATION
    verdicts = {}
    for key in SEATS:
        r = measured[MODELS[key]["id"]]
        verdicts[key] = ("inconclusive" if not valid else
                         "confirmed" if r >= a1 - CONFIRM_MARGIN else "lowered")
    return {"measured": measured, "tier1": tier1, "a1": a1, "t2": t2, "valid": valid,
            "verdicts": verdicts}


def test_the_method_is_written_down():
    section = EVALUATION_MD.split("## How a tier is measured", 1)
    assert len(section) == 2, "evaluation.md has no tier-measurement section"
    body = " ".join(section[1].split())
    for phrase in ("Anchors are live rows", "never used as its anchor",
                   "Neither may a row whose tier the measurement is deciding",
                   "the anchor is the lowest one measured", "*valid*", "*confirmed*",
                   "*lowered*", "*inconclusive*", "Thresholds compare unrounded values",
                   "an implementation replay point the same way",
                   "A review replay alone is recorded as a measurement and moves nothing"):
        assert phrase in body, phrase


def test_the_live_anchor_row_supersedes_both_seat_measurements():
    row = LEDGER[LIVE_ROW]
    assert row["status"] == "verified"
    old = [f"{key} tier measurement (real-review replay, 2026-10-14)" for key in SEATS]
    assert sorted(row["supersedes"]) == sorted(old)
    for item in old:
        assert item in LEDGER, item


def test_the_recorded_verdicts_and_bars_follow_from_the_recorded_numbers():
    """Re-derive: anchor = the lowest measured live tier-1 row other than the
    seats being decided; the tier-2 mean over the measured live tier-2 rows.
    Each seat's stated verdict, its registry tier and the stated bar,
    separation and tier-2 mean must agree with the derivation."""
    text = _text(LIVE_ROW)
    d = _derive(text)
    history = [m["id"] for m in MODELS.values() if "history_of" in m and m["id"] in d["measured"]]
    assert history, "the old history anchor should be reported beside the result"
    assert len(d["tier1"]) >= 2, d["tier1"]
    stated = dict(re.findall(r"Verdict ([a-z0-9.\-]+): (confirmed|lowered|inconclusive)\.", text))
    for key in SEATS:
        mid = MODELS[key]["id"]
        assert stated.get(mid) == d["verdicts"][key], (mid, stated.get(mid), d["verdicts"][key])
        if d["verdicts"][key] in VERDICT_TIER:
            assert MODELS[key]["capability_tier"] == VERDICT_TIER[d["verdicts"][key]], key
    # Six places in and out (review r2 sol F2): a stated figure that moves by
    # more than the inputs' own rounding (2e-6) fails — far below any margin
    # the verdict uses, where four places let a 1e-4 drift through.
    bar = float(re.search(r"Bar for tier 1: (0\.\d{6})", text).group(1))
    mean, sep = map(float, re.search(r"Tier-2 mean (0\.\d{6}), separation (0\.\d{6})",
                                     text).groups())
    assert abs(bar - (d["a1"] - CONFIRM_MARGIN)) < 2e-6
    assert abs(mean - d["t2"]) < 2e-6 and abs(sep - (d["t2"] - d["a1"])) < 2e-6
    assert d["valid"] is (sep >= SEPARATION)


def test_every_live_tier_1_row_is_measured_or_named_unmeasured():
    """Adding a measured row can only lower the bar, so a row left out
    silently would hide what the bar rests on."""
    text = _text(LIVE_ROW)
    measured = _recalls(text)
    for mid in _live_rows(1):
        assert mid in measured or re.search(re.escape(mid) + r"[^.]*unmeasured", text), mid


@pytest.mark.parametrize("key", SEATS)
def test_the_profiles_state_the_seat_tier_the_registry_holds(key):
    heading = {"meta_worker_balanced": "### The meta reviewer seat",
               "gemini_worker_balanced": "### The gemini reviewer seat"}[key]
    section = PROFILES_MD.split(heading, 1)[1].split("\n### ", 1)[0]
    tier = MODELS[key]["capability_tier"]
    assert f"tier-{tier} " in section and f"tier is {tier} since" in section
    assert "measured since" not in section and "provisional again" not in section


def test_a_writing_row_keeps_its_tier_on_a_review_replay_alone():
    """claude_worker_fast recalled level with the tier-1 Claude row and the
    tier-2 mean; it writes, so the review replay is recorded and the tier
    stays."""
    assert MODELS["claude_worker_fast"]["capability_tier"] == 0
    rows = [e for e in LEDGER.values()
            if e.get("status") == "quality_inherited_not_remeasured"
            and "claude_worker_fast" in e["item"]
            and MODELS["claude_worker_fast"]["id"] in e["item"]]
    assert len(rows) == 1
    evidence = " ".join(rows[0]["evidence"].split())
    assert "implementation replay" in evidence and COUNTERFACTUALS in evidence
    assert COUNTERFACTUALS in LEDGER and LEDGER[COUNTERFACTUALS]["status"] == "verified"


# ---------------------------------------------------------------------------
# What a tier-0 seat in a tier-1 role's fallback list must not do (1.22.0)
# ---------------------------------------------------------------------------

from route_task import Policy, Task, route  # noqa: E402

POLICY = Policy.of(CFG)
ID = lambda key: MODELS[key]["id"]                               # noqa: E731
XAI = ID("xai_frontier")
SCARCITY = [
    dict(unavailable_models=[XAI]),
    dict(unavailable_models=[XAI, ID("claude_worker_balanced")]),
    dict(unavailable_models=[XAI, ID("claude_worker_balanced"), ID("meta_worker_balanced")]),
    dict(unavailable_models=[XAI], _family_quota={"claude": "exhausted"}),
    dict(unavailable_models=[XAI, ID("openai_worker_fast")], _family_quota={"claude": "exhausted"}),
    dict(unavailable_models=[XAI, ID("openai_worker_fast")]),
    dict(_family_quota={"claude": "exhausted"}),
]


def _tier_of(out):
    return POLICY.tier_of[out["selected_model"]]


@pytest.mark.parametrize("runtime", ["claude_code", "codex", "grok"])
def test_an_at_least_promotion_never_seats_a_weaker_worker(runtime):
    """Review r2 (sol F1): `investigation promotion` moves the worker to the
    balanced ROLE, which a tier-0 seat can now resolve — a promotion that
    seated a tier-0 model where the table chose a tier-1 one. On such a
    route the role is floored at its binding's tier, so the promoted worker
    is tier 1 or better and never a tier-0 seat. (A promotion that trades a
    tier-2 model a scarce fast role resolved to for a tier-1 balanced one is
    older than this release and is tracked separately.)"""
    import itertools
    for extra, c, u, b, r in itertools.product(SCARCITY, range(4), range(2), range(3), range(2)):
        kw = dict(task_class="INVESTIGATION", complexity=c, uncertainty=u, blast_radius=b,
                  reversibility=r, runtime=runtime, **extra)
        plain = route(Task(**kw), CFG)
        promoted = route(Task(**kw, flags=["unknown_root_cause"]), CFG)
        if plain.get("terminal") or promoted.get("terminal"):
            continue
        assert _tier_of(promoted) >= 1, (kw, plain["selected_model"],
                                         promoted["selected_model"])
        for key in SEATS:
            assert promoted["selected_model"] != ID(key), (kw, key)
            # Where the worker sits on another role the seats stay reviewer
            # supply; a band that asks more of them records the shortfall.
            if ID(key) in promoted["review"]["reviewer_models"] and promoted["review"]["band"] != "LOW":
                assert any(e["model"] == ID(key) and e["capability_tier"] == 0
                           for e in promoted["review"]["review_depth_reduced"]), (kw, key)


def test_the_floored_role_takes_the_next_model_that_meets_it():
    """sol's case: Codex, the fast and xai seats out, Claude exhausted. The
    balanced role skips gemini for the OpenAI balanced model (tier 1) rather
    than jumping to a senior seat, and says why through its fallback record."""
    kw = dict(task_class="INVESTIGATION", complexity=1, uncertainty=0, blast_radius=0,
              reversibility=0, runtime="codex", unavailable_models=[XAI, ID("openai_worker_fast")],
              _family_quota={"claude": "exhausted"})
    out = route(Task(**kw, flags=["unknown_root_cause"]), CFG)
    assert out["selected_model"] == ID("openai_worker_balanced")
    assert out["selected_role"] == "worker_balanced"
    assert "investigation promotion: unknown root cause" in out["notes"]
    assert f"worker_balanced: {XAI} unavailable -> {ID('openai_worker_balanced')}" \
        in out["fallbacks_applied"]
    # Without the flag nothing is floored: the reading lead may be the seat.
    plain = route(Task(**dict(kw, complexity=2, uncertainty=1)), CFG)
    assert plain["selected_model"] in (ID("gemini_worker_balanced"),
                                       ID("openai_worker_balanced"))


@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_a_low_claude_quota_swaps_a_read_lead_past_the_tier_0_seats(runtime):
    """Review r2 (fable F1): the `low` swap goes to the first SAME-TIER model of
    another family; the seats are no longer that tier."""
    out = route(Task(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime=runtime, unavailable_models=[XAI],
                     _family_quota={"claude": "low"}), CFG)
    assert out["selected_model"] == ID("openai_worker_balanced")
    assert any(n.startswith("family_quota: claude low") for n in out["notes"])
    for key in SEATS:
        assert ID(key) not in [out["selected_model"], *out["review"]["reviewer_models"]]


@pytest.mark.parametrize("key,runtime", [("meta_worker_balanced", "claude_code"),
                                         ("gemini_worker_balanced", "codex")])
def test_a_retry_after_a_seat_fails_climbs_above_tier_0(key, runtime):
    """Review r2 (fable F3): a failed reading lead on a tier-0 seat is retried
    on a model above tier 0, and the route says so."""
    kw = dict(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
              reversibility=1, runtime=runtime, unavailable_models=[XAI],
              _family_quota={"claude": "exhausted"})
    assert route(Task(**kw), CFG)["selected_model"] == ID(key)
    out = route(Task(**kw, prior_failures=1, prior_models=[ID(key)]), CFG)
    assert _tier_of(out) >= 1 and out["selected_model"] != ID(key)
    assert any("capability tier 0" in n for n in out["notes"])


@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_a_retry_of_a_floored_investigation_keeps_the_seats_for_review(runtime):
    """The r2 replay: flooring the balanced role on a retry too (whose ladder
    already asks for more than what failed) took the seats out of review
    seating, and with the fast, xai and Claude seats out the retry found no
    independent reviewers. The floor is for the first attempt only."""
    kw = dict(task_class="INVESTIGATION", complexity=0, uncertainty=0, blast_radius=1,
              reversibility=2, runtime=runtime, flags=["unknown_root_cause"],
              unavailable_models=[XAI, ID("openai_worker_fast")],
              _family_quota={"claude": "exhausted"})
    fresh = route(Task(**kw), CFG)
    assert fresh["selected_model"] == ID("openai_worker_balanced")
    retry = route(Task(**kw, prior_failures=1, prior_models=[fresh["selected_model"]]), CFG)
    assert retry.get("terminal") is None
    assert _tier_of(retry) >= 2


def _with_seat_tiers(tier: int) -> dict:
    """The registry with the two seats at `tier` — 1 is what 1.21.0 shipped."""
    import copy
    cfg = copy.deepcopy(CFG)
    for key in SEATS:
        cfg["models"][key]["capability_tier"] = tier
    return cfg


DEEP_SCARCITY = SCARCITY + [
    dict(unavailable_models=[XAI, ID("openai_worker_fast"), ID("openai_frontier")],
         _family_quota={"claude": "exhausted"}),
    dict(unavailable_models=[XAI, ID("openai_frontier")], _family_quota={"claude": "exhausted"}),
]


@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_the_promotion_floor_makes_no_route_terminal(runtime):
    """Review r3 (sol F1): with the frontier OpenAI seat out too, the floored
    lead took the reviewers' last distinct model and the route went
    INDEPENDENCE_UNAVAILABLE where 1.21.0 dispatched it. Wherever the seats at
    their 1.21.0 tier route an investigation, the floored registry does too."""
    import itertools
    old = _with_seat_tiers(1)
    for extra, c, u, b, r in itertools.product(DEEP_SCARCITY, range(3), range(2), range(3), range(3)):
        kw = dict(task_class="INVESTIGATION", complexity=c, uncertainty=u, blast_radius=b,
                  reversibility=r, runtime=runtime, flags=["unknown_root_cause"], **extra)
        before = route(Task(**kw), old)
        after = route(Task(**kw), CFG)
        if before.get("terminal") is None:
            assert after.get("terminal") is None, (kw, after.get("terminal"))


def test_a_floor_that_would_end_the_route_yields_and_says_so():
    kw = dict(task_class="INVESTIGATION", complexity=0, uncertainty=0, blast_radius=1,
              reversibility=2, runtime="codex", flags=["unknown_root_cause"],
              unavailable_models=[XAI, ID("openai_worker_fast"), ID("openai_frontier")],
              _family_quota={"claude": "exhausted"})
    out = route(Task(**kw), CFG)
    assert out.get("terminal") is None
    assert len(set(out["review"]["reviewer_models"]) | {out["selected_model"]}) == \
        1 + len(out["review"]["reviewer_models"])
    assert ("investigation promotion: worker_balanced not floored at capability tier 1 "
            "on this route — floored, it was INDEPENDENCE_UNAVAILABLE") in out["notes"]


def test_recovered_operational_history_keeps_the_floor():
    """A recovered transport failure is not a capability failure: the route
    is still a first attempt for the ladder, so the floor applies."""
    import route_task as rt
    req = dict(route_schema_version=1, task_class="INVESTIGATION", complexity=1, uncertainty=0,
               blast_radius=0, reversibility=0, runtime="codex", flags=["unknown_root_cause"],
               availability_snapshot={"unavailable_models": [XAI, ID("openai_worker_fast")],
                                      "family_quota": {"claude": "exhausted"}},
               attempt_outcomes=[dict(attempt_id="attempt-1", model_id=ID("openai_worker_balanced"),
                                      kind="transport_failure", evidence_sha256="a" * 64,
                                      recovery_sha256="b" * 64)])
    out = rt.route(rt.task_from_request_v1(req), CFG)
    assert out.get("terminal") is None
    assert _tier_of(out) >= 1 and out["selected_model"] not in [ID(k) for k in SEATS]
