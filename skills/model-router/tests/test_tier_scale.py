"""The tier scale (issue #63, model-router 1.22.0).

A tier is held by its live rows, so a measurement is read against them — never
against a history row or a seat the measurement is deciding — and a review
replay decides a read-only seat's tier but never moves a writing row's on its
own. These tests re-derive the recorded verdicts and bars from the recorded
numbers with that method, so the ledger, the registry and the method cannot
drift apart silently.

Run:  python3 -m pytest skills/model-router/tests/test_tier_scale.py -q
"""

import json
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


# A ledger row records the ids it measured and is never edited afterwards, so
# a key whose id moved after the row (1.24.1: openai_worker_balanced to the
# next Terra generation) must be read under the id the row names, not the live
# one (review d1 fable F4). The succession chain says which ids a key has held.
CHAINS = json.loads((SKILL / "tests" / "fixtures" / "id-succession.json").read_text())["chains"]


def _names(mid: str, text: str) -> bool:
    # a sentence-final "." is not an id character (review d3 fable F6); ".5" would be
    return re.search(r"(?<![A-Za-z0-9._-])" + re.escape(mid) + r"(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9])",
                     text) is not None


def _id(key: str, text: str) -> str:
    """The id `key` held when the row was written: the newest generation of
    its succession chain the row names, else the live id."""
    named = [mid for mid in CHAINS.get(key, []) if _names(mid, text)]
    return named[-1] if named else MODELS[key]["id"]


def _recalls(text: str) -> dict:
    """`<registry id> <recall>` pairs as the row records them (six places)."""
    ids = {m["id"] for m in MODELS.values()}
    out = {}
    for mid, value in re.findall(r"([a-z0-9][a-z0-9.\-]*) (0\.\d{6})\b", text):
        if mid in ids:
            out.setdefault(mid, float(value))
    return out


# A verdict reads the registry as it stood before its measurement
# (references/evaluation.md): the 2026-10-14 replays ran while the Claude
# balanced model was tier 1; 1.23.0 moved it to 2 (issue #65).
TIERS_BEFORE_1_23 = {"claude_worker_balanced": 1}


# Rows the registry gained after the 2026-10-14 replays; they were not there
# to anchor them (1.24.0: the gemini seat moved to gemini-4-argon; 1.26.0: to
# gemini-4-hydrogen; 1.27.0: to gemini-4-neon).
ADDED_AFTER_2026_10_14 = {"gemini_frontier", "gemini_hydrogen", "gemini_neon"}


def _live_rows(tier: int, as_of: dict | None = None, text: str = "") -> list[str]:
    """Live rows of a tier that may anchor: not history, not a decided seat,
    and present when the measurement ran — each under the id it held then."""
    as_of = as_of or {}
    return [_id(key, text) for key, m in MODELS.items()
            if m.get("dispatchable", True) is not False and "history_of" not in m
            and key not in SEATS and key not in ADDED_AFTER_2026_10_14
            and as_of.get(key, m["capability_tier"]) == tier]


def _derive(text: str) -> dict:
    measured = _recalls(text)
    tier1 = [measured[i] for i in _live_rows(1, TIERS_BEFORE_1_23, text) if i in measured]
    tier2 = [measured[i] for i in _live_rows(2, TIERS_BEFORE_1_23, text) if i in measured]
    a1, t2 = min(tier1), sum(tier2) / len(tier2)
    valid = t2 - a1 >= SEPARATION
    verdicts = {}
    for key in SEATS:
        r = measured[_id(key, text)]
        verdicts[key] = ("inconclusive" if not valid else
                         "confirmed" if r >= a1 - CONFIRM_MARGIN else "lowered")
    return {"measured": measured, "tier1": tier1, "a1": a1, "t2": t2, "valid": valid,
            "verdicts": verdicts}


def test_the_method_is_written_down():
    section = EVALUATION_MD.split("## How a tier is measured", 1)
    assert len(section) == 2, "evaluation.md has no tier-measurement section"
    body = " ".join(section[1].split())
    for phrase in ("Anchors are live rows", "never used as its anchor",
                   "Neither may the row whose tier a verdict is deciding",
                   "each verdict's anchors leave out that verdict's own row only", "*raised*",
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
        mid = _id(key, text)
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
    for mid in _live_rows(1, TIERS_BEFORE_1_23, text):
        assert mid in measured or re.search(re.escape(mid) + r"[^.]*unmeasured", text), mid


@pytest.mark.parametrize("key", ["meta_worker_balanced", "gemini_neon"])
def test_the_profiles_state_the_seat_tier_the_registry_holds(key):
    """The current seats (1.27.0: the gemini section describes gemini-4-neon, (a)
    the key moved; its tier, 2, is the registry's)."""
    heading = {"meta_worker_balanced": "### The meta reviewer seat",
               "gemini_neon": "### The gemini reviewer seat"}[key]
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


GEMINI_KEY = "gemini_neon"      # the gemini seat's live key since 1.27.0 (1.26.x: gemini_hydrogen)


def _gemini_seat_at_tier_0() -> dict:
    """The 1.22.0-1.23 shape: a tier-0 gemini seat right behind the Claude model
    on Codex (and behind meta on Claude Code). 1.24.0 seats a tier-1 model
    there (gemini-4-argon, measured; from 1.26.0 gemini-4-hydrogen, measured)
    and 1.27.0 a tier-2 one (gemini-4-neon, measured, T15), so the floor
    mechanisms below are kept under test on this copy, and the shipped registry
    is checked beside it."""
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["models"][GEMINI_KEY]["capability_tier"] = 0
    return cfg


def _gemini_seat_at_tier_1() -> dict:
    """The 1.24.0-1.26.x shape: a tier-1 gemini seat in the same place (argon,
    then hydrogen). Where a test below pinned what the tier-1 seat did, that is
    kept under test on this copy, and the shipped tier-2 seat is checked beside
    it (ledger row "1.27.0 gemini seat placement counterfactuals")."""
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["models"][GEMINI_KEY]["capability_tier"] = 1
    return cfg


GEMINI_TIER0 = _gemini_seat_at_tier_0()
GEMINI_TIER1 = _gemini_seat_at_tier_1()
GEMINI = ID(GEMINI_KEY)


def _tier0_seats(cfg: dict) -> list[str]:
    """Review i1 (sol F1, fable F4): the behavioural checks below are about the
    dispatchable tier-0 seats a tier-1 role's fallback list holds — in the
    shipped registry the meta seat; in the tier-0 shape the gemini seat too.
    (`SEATS` stays the 2026-10-14 measurement's subjects, for the ledger.)"""
    listed = {k for per_role in cfg["fallbacks"].values() for k in per_role["worker_balanced"]}
    return sorted(k for k in listed if cfg["models"][k].get("dispatchable", True) is not False
                  and cfg["models"][k]["capability_tier"] == 0)


REGISTRIES = {"shipped": CFG, "gemini_tier0": GEMINI_TIER0}


@pytest.mark.parametrize("which", sorted(REGISTRIES))
@pytest.mark.parametrize("runtime", ["claude_code", "codex", "grok"])
def test_an_at_least_promotion_never_seats_a_weaker_worker(runtime, which):
    """Review r2 (sol F1): `investigation promotion` moves the worker to the
    balanced ROLE, which a tier-0 seat can now resolve — a promotion that
    seated a tier-0 model where the table chose a tier-1 one. On such a
    route the role is floored at its binding's tier, so the promoted worker
    is tier 1 or better and never a tier-0 seat. (A promotion that traded a
    tier-2 model a scarce fast role resolved to for a tier-1 balanced one is
    older than 1.22.0; issue #67 closed it in 1.23.0 — see below.)"""
    import itertools
    cfg = REGISTRIES[which]
    seats = _tier0_seats(cfg)
    assert seats, which
    for extra, c, u, b, r in itertools.product(SCARCITY, range(4), range(2), range(3), range(2)):
        kw = dict(task_class="INVESTIGATION", complexity=c, uncertainty=u, blast_radius=b,
                  reversibility=r, runtime=runtime, **extra)
        plain = route(Task(**kw), cfg)
        promoted = route(Task(**kw, flags=["unknown_root_cause"]), cfg)
        if plain.get("terminal") or promoted.get("terminal"):
            continue
        # the tier the registry under test holds (the tier-0 shape differs from POLICY)
        assert cfg["models"][POLICY.id_to_key[promoted["selected_model"]]]["capability_tier"] >= 1, \
            (kw, plain["selected_model"], promoted["selected_model"])
        for key in seats:
            assert promoted["selected_model"] != ID(key), (kw, key)
            # Where the worker sits on another role the seats stay reviewer
            # supply; a band that asks more of them records the shortfall.
            if ID(key) in promoted["review"]["reviewer_models"] and promoted["review"]["band"] != "LOW":
                assert any(e["model"] == ID(key) and e["capability_tier"] == 0
                           for e in promoted["review"]["review_depth_reduced"]), (kw, key)


def test_the_floored_role_takes_the_next_model_that_meets_it():
    """sol's case: Codex, the fast and xai seats out, Claude exhausted. With a
    tier-0 seat there, the balanced role skips it for the OpenAI balanced
    model (tier 1) rather than jumping to a senior seat, and says why through
    its fallback record. The shipped tier-1 seat meets the floor itself."""
    kw = dict(task_class="INVESTIGATION", complexity=1, uncertainty=0, blast_radius=0,
              reversibility=0, runtime="codex", unavailable_models=[XAI, ID("openai_worker_fast")],
              _family_quota={"claude": "exhausted"})
    out = route(Task(**kw, flags=["unknown_root_cause"]), GEMINI_TIER0)
    assert out["selected_model"] == ID("openai_worker_balanced")
    assert out["selected_role"] == "worker_balanced"
    assert "investigation promotion: unknown root cause" in out["notes"]
    assert f"worker_balanced: {XAI} unavailable -> {ID('openai_worker_balanced')}" \
        in out["fallbacks_applied"]
    # Without the flag nothing is floored: the reading lead may be the seat.
    plain = route(Task(**dict(kw, complexity=2, uncertainty=1)), GEMINI_TIER0)
    assert plain["selected_model"] in (GEMINI, ID("openai_worker_balanced"))
    # 1.24.0: the seat is tier 1, the floor; it is the promoted lead. (b) 1.27.0:
    # the shipped seat is tier 2 (T15), above the floor, and still the promoted
    # lead; the tier-1 case is kept on GEMINI_TIER1.
    t1 = route(Task(**kw, flags=["unknown_root_cause"]), GEMINI_TIER1)
    assert t1["selected_model"] == GEMINI and t1["selected_capability_tier"] == 1
    shipped = route(Task(**kw, flags=["unknown_root_cause"]), CFG)
    assert shipped["selected_model"] == GEMINI and _tier_of(shipped) == 2
    assert f"worker_balanced: {XAI} unavailable -> {GEMINI}" in shipped["fallbacks_applied"]


@pytest.mark.parametrize("which", sorted(REGISTRIES))
@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_a_low_claude_quota_swaps_a_read_lead_past_the_tier_0_seats(runtime, which):
    """Review r2 (fable F1): the `low` swap goes to the first SAME-TIER model of
    another family; the seats are no longer that tier. Since 1.23.0 the Claude
    balanced model it swaps away from is tier 2, so the swap lands on the
    OpenAI tier-2 model (issue #65).

    (b) 1.27.0: the shipped gemini seat is tier 2 too (T15) and sits ahead of
    the OpenAI tier-2 model in the list, so on the shipped registry the swap
    lands on it — a read worker, and the seat reads (ledger row "1.27.0 gemini
    seat placement counterfactuals"). With the seat at tier 1 (the 1.24.0-1.26.x
    shape) or tier 0 it is still the OpenAI tier-2 model."""
    cfg = REGISTRIES[which]
    kw = dict(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
              reversibility=1, runtime=runtime, unavailable_models=[XAI],
              _family_quota={"claude": "low"})
    out = route(Task(**kw), cfg)
    expected = {"shipped": GEMINI, "gemini_tier0": ID("openai_reasoning")}[which]
    assert out["selected_model"] == expected
    policy = Policy.of(cfg)
    assert policy.tier_of[out["selected_model"]] == policy.tier_of[ID("claude_worker_balanced")]
    assert any(n.startswith("family_quota: claude low") for n in out["notes"])
    for key in _tier0_seats(cfg):
        assert ID(key) not in [out["selected_model"], *out["review"]["reviewer_models"]]
    assert route(Task(**kw), GEMINI_TIER1)["selected_model"] == ID("openai_reasoning")


@pytest.mark.parametrize("key,runtime,withheld,shape", [
    ("meta_worker_balanced", "claude_code", [], "shipped"),
    ("gemini_neon", "codex", [], "tier0"),
    ("gemini_neon", "claude_code", ["meta_worker_balanced"], "tier0"),
    ("gemini_neon", "codex", [], "shipped"),
    ("gemini_neon", "claude_code", ["meta_worker_balanced"], "shipped"),
    ("gemini_neon", "codex", [], "tier1"),
    ("gemini_neon", "claude_code", ["meta_worker_balanced"], "tier1"),
], ids=["meta", "gemini-tier0-codex", "gemini-tier0-claude_code", "gemini-codex",
        "gemini-claude_code", "gemini-tier1-codex", "gemini-tier1-claude_code"])
def test_a_retry_after_a_seat_fails_climbs_above_its_tier(key, runtime, withheld, shape):
    """Review r2 (fable F3): a failed reading lead on a tier-0 seat is retried
    on a model above tier 0, and the route says so (r5: the gemini seat on
    Claude Code, with the meta seat withheld, too). 1.24.0: the shipped gemini
    seat is tier 1, so its retry climbs above tier 1; the tier-0 shape is kept
    under test on a copy.

    (b) 1.27.0: the shipped seat is tier 2 (T15), so its retry climbs to a
    tier-3 model (references/model-profiles.md, "The gemini reviewer seat": "a
    retry after this seat fails now climbs to a tier-3 model"); the tier-1 shape
    (1.24.0-1.26.x) is kept under test on GEMINI_TIER1 beside the tier-0 one."""
    cfg = {"tier0": GEMINI_TIER0, "tier1": GEMINI_TIER1, "shipped": CFG}[shape]
    kw = dict(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
              reversibility=1, runtime=runtime,
              unavailable_models=[XAI, *(ID(k) for k in withheld)],
              _family_quota={"claude": "exhausted"})
    assert route(Task(**kw), cfg)["selected_model"] == ID(key)
    out = route(Task(**kw, prior_failures=1, prior_models=[ID(key)]), cfg)
    failed_tier = cfg["models"][key]["capability_tier"]
    assert POLICY.tier_of[out["selected_model"]] > failed_tier and out["selected_model"] != ID(key)
    assert any(f"capability tier {failed_tier}" in n for n in out["notes"])
    if key == GEMINI_KEY and shape == "shipped":
        assert failed_tier == 2 and POLICY.tier_of[out["selected_model"]] == 3


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
    fresh = route(Task(**kw), GEMINI_TIER0)
    assert fresh["selected_model"] == ID("openai_worker_balanced")
    retry = route(Task(**kw, prior_failures=1, prior_models=[fresh["selected_model"]]), GEMINI_TIER0)
    assert retry.get("terminal") is None
    assert _tier_of(retry) >= 2
    # 1.24.0: the tier-1 seat meets the floor and leads; its retry stays dispatchable.
    # (1.27.0: the tier-2 seat too; its retry climbs to the tier-3 OpenAI frontier seat.)
    fresh = route(Task(**kw), CFG)
    assert fresh["selected_model"] == GEMINI
    retry = route(Task(**kw, prior_failures=1, prior_models=[GEMINI]), CFG)
    assert retry.get("terminal") is None and _tier_of(retry) >= 2


def _with_seat_tiers(tier: int) -> dict:
    """The registry with the two live reader seats (meta and gemini) at `tier` —
    1 is what 1.21.0 shipped. (Review i1: the gemini seat's live key — since
    1.27.0 gemini_neon; the retired rows are never seated, so setting their tier
    would change nothing.)"""
    import copy
    cfg = copy.deepcopy(CFG)
    for key in ("meta_worker_balanced", GEMINI_KEY):
        cfg["models"][key]["capability_tier"] = tier
    return cfg


DEEP_SCARCITY = SCARCITY + [
    dict(unavailable_models=[XAI, ID("openai_worker_fast"), ID("openai_frontier")],
         _family_quota={"claude": "exhausted"}),
    dict(unavailable_models=[XAI, ID("openai_frontier")], _family_quota={"claude": "exhausted"}),
]


@pytest.mark.parametrize("which", sorted(REGISTRIES))
@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_the_promotion_floor_makes_no_route_terminal(runtime, which):
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
        after = route(Task(**kw), REGISTRIES[which])
        if before.get("terminal") is None:
            assert after.get("terminal") is None, (kw, after.get("terminal"))


def test_a_floor_that_would_end_the_route_yields_and_says_so():
    """On the tier-0 shape (1.24.0 seats a tier-1 model there, which meets the
    floor, so the shipped registry never reaches this branch on the grid; the
    tier-2 seat since 1.27.0 meets it too)."""
    kw = dict(task_class="INVESTIGATION", complexity=0, uncertainty=0, blast_radius=1,
              reversibility=2, runtime="codex", flags=["unknown_root_cause"],
              unavailable_models=[XAI, ID("openai_worker_fast"), ID("openai_frontier")],
              _family_quota={"claude": "exhausted"})
    out = route(Task(**kw), GEMINI_TIER0)
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
    assert _tier_of(out) >= 1 and out["selected_model"] not in [ID(k) for k in _tier0_seats(CFG)]


def test_a_floor_no_usable_model_meets_is_waived_and_noted():
    """Review r4: the other waiver. With every tier-1-or-better model withheld,
    the promoted role can only hold a tier-0 seat; the route says so."""
    out = route(Task(task_class="INVESTIGATION", complexity=0, uncertainty=0, blast_radius=0,
                     reversibility=0, runtime="claude_code", flags=["unknown_root_cause"],
                     unavailable_models=[XAI, ID("openai_worker_balanced"), ID("openai_reasoning"),
                                         ID("openai_frontier")],
                     _local_policy={"allowed_families": ["meta", "openai"]}), CFG)
    assert out.get("terminal") is None
    assert out["selected_model"] == ID("meta_worker_balanced")
    assert out["selected_capability_tier"] == 0
    assert ("investigation promotion: capability floor 1 for worker_balanced waived — no "
            "usable model in the role meets it") in out["notes"]


# (b) 1.27.0: retries of a first attempt the tier-2 gemini seat led, which end
# HUMAN_REQUIRED on the shipped registry where the seats at their 1.21.0 tier
# dispatched them: a retry climbs strictly above the tier that failed, and with
# the frontier OpenAI seat withheld and the Claude family exhausted no usable
# model is above tier 2. Counted on this grid, per host.
_TIER2_RETRY_ENDS = {"claude_code": 46, "codex": 60}


@pytest.mark.parametrize("which", sorted(REGISTRIES) + ["gemini_tier1"])
@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_a_retry_is_never_floored_and_never_ends_where_1_21_0_did_not(runtime, which):
    """Reviews r3/r4: a retry is left to its ladder. No retry of a first
    attempt carries a floor note, and no retry becomes terminal where the
    seats at their 1.21.0 tier would have dispatched it.

    (b) 1.27.0: the second half holds exactly on the tier-0 and tier-1 shapes
    (GEMINI_TIER1 is the 1.24.0-1.26.x registry's), and on the shipped registry
    everywhere but the disclosed retries after the tier-2 gemini seat fails:
    "a retry after this seat fails now climbs to a tier-3 model", and where
    none is usable the retry ends HUMAN_REQUIRED (references/model-profiles.md,
    "The gemini reviewer seat"; ledger row "1.27.0 gemini seat placement
    counterfactuals", which counts 34 such retries on its own grid)."""
    import itertools
    old = _with_seat_tiers(1)
    cfg_under_test = GEMINI_TIER1 if which == "gemini_tier1" else REGISTRIES[which]
    ended = 0
    for extra, c, u, b, r in itertools.product(DEEP_SCARCITY, range(3), range(2), range(3), range(3)):
        kw = dict(task_class="INVESTIGATION", complexity=c, uncertainty=u, blast_radius=b,
                  reversibility=r, runtime=runtime, flags=["unknown_root_cause"], **extra)
        pairs, leads = [], []
        for cfg in (old, cfg_under_test):
            fresh = route(Task(**kw), cfg)
            leads.append(fresh.get("selected_model"))
            if fresh.get("terminal") or not fresh.get("selected_model"):
                pairs.append(None)
                continue
            pairs.append(route(Task(**kw, prior_failures=1,
                                    prior_models=[fresh["selected_model"]]), cfg))
        before, after = pairs
        if after is not None:
            assert not any("not floored" in n or "floor 1 for worker_balanced waived" in n
                           for n in after["notes"]), kw
        if before is not None and before.get("terminal") is None and after is not None:
            if after.get("terminal") is not None and which == "shipped":
                assert after["terminal"] == "HUMAN_REQUIRED", kw
                assert leads[1] == GEMINI and POLICY.tier_of[GEMINI] == 2, kw
                assert ID("openai_frontier") in extra["unavailable_models"], kw
                assert extra["_family_quota"] == {"claude": "exhausted"}, kw
                assert "retry ladder exhausted: no usable model is stronger than capability tier 2" \
                    in after["notes"], kw
                ended += 1
                continue
            assert after.get("terminal") is None, (kw, after.get("terminal"))
    assert ended == (_TIER2_RETRY_ENDS[runtime] if which == "shipped" else 0), ended


# ---------------------------------------------------------------------------
# Issue #67: the promotion never trades a stronger table worker away (1.23.0)
# ---------------------------------------------------------------------------

LUNA = ID("openai_worker_fast")


def test_the_promotion_keeps_a_strictly_stronger_table_worker():
    """Issue #67's case: Claude Code, the xai and fast OpenAI seats out, Claude
    exhausted. The fast role's ladder reaches gpt-6.1-sol; the balanced role's
    floored list holds a tier-1 model. An unknown root cause is evidence of
    difficulty, which must never weaken a route, so the flag leaves the worker
    where the table put it.

    (b) 1.27.0: that tier-1 model was the gemini seat; the shipped seat is tier 2
    (T15), level with the fast role's Sol seat, so on the shipped registry the
    promotion moves the role as it does at any equal tier (the next test) and
    seats the gemini seat at tier 2 — no weaker. The strictly-stronger case is
    kept on GEMINI_TIER1, the 1.24.0-1.26.x registry (ledger row "1.27.0 gemini
    seat placement counterfactuals")."""
    kw = dict(task_class="INVESTIGATION", complexity=3, uncertainty=0, blast_radius=0,
              reversibility=0, runtime="claude_code", unavailable_models=[XAI, LUNA],
              _family_quota={"claude": "exhausted"})
    plain = route(Task(**kw), GEMINI_TIER1)
    flagged = route(Task(**kw, flags=["unknown_root_cause"]), GEMINI_TIER1)
    assert plain["selected_model"] == flagged["selected_model"] == ID("openai_reasoning")
    assert flagged["selected_role"] == "worker_fast"
    assert ("investigation promotion: worker_fast kept at capability tier 2 — worker_balanced "
            "resolves to capability tier 1") in flagged["notes"]
    assert "investigation promotion: unknown root cause" not in flagged["notes"]
    # the shipped tier-2 seat
    plain = route(Task(**kw), CFG)
    flagged = route(Task(**kw, flags=["unknown_root_cause"]), CFG)
    assert plain["selected_model"] == ID("openai_reasoning") and plain["selected_role"] == "worker_fast"
    assert (flagged["selected_model"], flagged["selected_role"]) == (GEMINI, "worker_balanced")
    assert _tier_of(flagged) == _tier_of(plain) == 2
    assert "investigation promotion: unknown root cause" in flagged["notes"]
    assert not any(n.startswith("investigation promotion: worker_fast kept") for n in flagged["notes"])


def test_at_an_equal_tier_the_promotion_still_moves_the_role():
    """Codex with the fast OpenAI seat out: the fast role falls back to the
    OpenAI balanced model and the balanced role holds its own binding — both
    tier 1. The role moves as it always did. Keeping the fast role at an equal
    tier too was replayed and rejected: it strengthened nothing, swapped tier-1
    models and added a review through the fast seat's fallback penalty."""
    out = route(Task(task_class="INVESTIGATION", complexity=1, uncertainty=0, blast_radius=0,
                     reversibility=0, runtime="codex", unavailable_models=[LUNA],
                     flags=["unknown_root_cause"]), CFG)
    assert out["selected_role"] == "worker_balanced"
    assert out["selected_model"] == XAI
    assert "investigation promotion: unknown root cause" in out["notes"]
    assert out["review"]["band"] == "LOW"


# (b) 1.27.0: on the shipped registry the balanced role's floored list reaches the
# tier-2 gemini seat wherever the fast role's ladder reaches the Sol seat on this
# grid, so the promotion never has a strictly stronger fast worker to keep there;
# the 32 Claude Code cells the tier-1 seat kept move at an equal tier instead.
_PROMOTION_COUNTS = {("claude_code", "gemini_tier1"): (128, 32),
                     ("claude_code", "shipped"): (160, 0)}


@pytest.mark.parametrize("which", ["shipped", "gemini_tier1"])
@pytest.mark.parametrize("runtime", ["claude_code", "codex", "grok"])
def test_the_promotion_never_resolves_weaker_than_the_table_worker(runtime, which):
    """Grid, first attempts: the risk chain's worker after the promotion is
    never weaker than the table cell's own worker, and is strictly stronger
    only where the promotion moved it. (A whole route can still seat a weaker
    worker with the flag than without it: the flag's confidence penalty can
    make the execution cell yield to keep the review unchanged — the
    execution guard, not the promotion.)

    (b) 1.27.0: the kept-a-stronger-worker branch is reached on Claude Code only
    with a tier-1 seat in the gemini place, so the grid also runs on
    GEMINI_TIER1 (the 1.24.0-1.26.x registry), where it must be reached; on the
    shipped registry the property holds and that branch is not reached
    (counts pinned in _PROMOTION_COUNTS; ledger row "1.27.0 gemini seat
    placement counterfactuals")."""
    import itertools
    import route_task as rt
    cfg = GEMINI_TIER1 if which == "gemini_tier1" else CFG
    policy = Policy.of(cfg)
    moved = kept = 0
    for extra, c, u, b, r in itertools.product(DEEP_SCARCITY + [{}], range(4), range(4),
                                               range(4), range(4)):
        task = Task(task_class="INVESTIGATION", complexity=c, uncertainty=u, blast_radius=b,
                    reversibility=r, runtime=runtime, flags=["unknown_root_cause"], **extra)
        resolver = rt.Resolver(task, policy)
        band = rt.apply_overrides(task, rt.band_from_score(rt.score(task, cfg), policy), policy)[0]
        table = rt._resolve_cell(cfg["worker_selection"]["INVESTIGATION"][band], task, [])
        role, notes = rt._legacy_pre(task, band, policy, resolver)
        before, after = resolver.peek(table, write=True), resolver.peek(role, write=True)
        if before is None:
            continue
        assert after is not None and policy.tier_of[after] >= policy.tier_of[before], \
            (extra, (c, u, b, r), table, before, role, after)
        if role != table:
            moved += 1
        elif any(n.startswith("investigation promotion: worker_fast kept") for n in notes):
            kept += 1
    assert moved
    if which == "gemini_tier1":
        assert kept or runtime != "claude_code"
    if (runtime, which) in _PROMOTION_COUNTS:
        assert (moved, kept) == _PROMOTION_COUNTS[(runtime, which)], (moved, kept)


def test_the_promotion_keeps_the_table_worker_when_the_balanced_role_has_nothing():
    """Review d1 (fable, missing test): the balanced role's list can hold
    nothing usable while the fast role's own fallback still resolves (Claude
    Code, the Claude family only, every Claude model but the fast one
    withheld). The promotion then keeps the fast role and says why."""
    import route_task as rt
    task = Task(task_class="INVESTIGATION", complexity=0, uncertainty=0, blast_radius=0,
                reversibility=0, runtime="claude_code", flags=["unknown_root_cause"],
                unavailable_models=[ID("claude_worker_balanced"), ID("claude_senior"),
                                    ID("claude_architect")],
                _local_policy={"allowed_families": ["claude"]})
    resolver = rt.Resolver(task, POLICY)
    role, notes = rt._legacy_pre(task, "LOW", POLICY, resolver)
    assert role == "worker_fast" and resolver.peek(role, write=True) == ID("claude_worker_fast")
    assert resolver.peek("worker_balanced", write=True) is None
    assert ("investigation promotion: worker_fast kept at capability tier 0 — worker_balanced "
            "resolves to nothing usable") in notes


def test_a_retry_reads_the_unfloored_balanced_role_and_keeps_the_stronger_worker():
    """Review d1 (fable, missing test): a retry carries no floor, so the
    balanced role resolves to a tier-0 seat; the fast role's gpt-6.1-sol stays
    and the ladder keeps it above the failed tier-1 model."""
    out = route(Task(task_class="INVESTIGATION", complexity=3, uncertainty=0, blast_radius=0,
                     reversibility=0, runtime="claude_code", flags=["unknown_root_cause"],
                     unavailable_models=[XAI, LUNA], _family_quota={"claude": "exhausted"},
                     prior_failures=1, prior_models=[ID("openai_worker_balanced")]), CFG)
    assert (out["terminal"], out["selected_model"], out["selected_role"]) == \
        (None, ID("openai_reasoning"), "worker_fast")
    assert ("investigation promotion: worker_fast kept at capability tier 2 — worker_balanced "
            "resolves to capability tier 0") in out["notes"]
    assert "retry keeps worker_fast: already above the failed capability tier 1" in out["notes"]


# ---------------------------------------------------------------------------
# Issue #65: the implementation replay and the writing rows' tiers (1.23.0)
# ---------------------------------------------------------------------------

IMPL_ROW = "Implementation replay of the writing rows (2026-10-15)"
WRITING = ("claude_worker_balanced", "claude_worker_fast", "openai_worker_balanced",
           "openai_worker_fast")
ANCHORS_2 = ("claude_senior", "openai_reasoning")


def _row_verdicts(scores: dict, text: str) -> dict:
    """evaluation.md's rule, per writing row, against the tiers the registry
    held before the measurement; each verdict leaves out only its own row."""
    tier = {key: TIERS_BEFORE_1_23.get(key, MODELS[key]["capability_tier"])
            for key in WRITING + ANCHORS_2}
    out = {}
    for key in WRITING:
        k = tier[key]
        own = [scores[_id(o, text)] for o in tier if o != key and tier[o] == k]
        up = [scores[_id(o, text)] for o in tier if o != key and tier[o] == k + 1]
        gate = sum(up) / len(up) - min(own) >= SEPARATION
        x = scores[_id(key, text)]
        raise_ok = gate and x >= min(up) - CONFIRM_MARGIN
        lower_ok = gate and k >= 1 and x < min(own) - CONFIRM_MARGIN
        out[key] = ("hold" if raise_ok == lower_ok else "raise" if raise_ok else "lower")
    return out


def test_the_implementation_replay_verdicts_follow_from_its_numbers():
    text = _text(IMPL_ROW)
    seg = text.split("Pass rates (preregistered grading):", 1)[1].split("The preregistered rule", 1)[0]
    scores = _recalls(seg)
    assert set(scores) == {_id(k, text) for k in WRITING + ANCHORS_2}, scores
    verdicts = _row_verdicts(scores, text)
    stated = dict(re.findall(r"Implementation verdict ([a-z0-9.\-]+): (raise|lower|hold)\.", text))
    assert stated == {_id(k, text): v for k, v in verdicts.items()}
    assert verdicts == {"claude_worker_balanced": "raise", "claude_worker_fast": "hold",
                        "openai_worker_balanced": "hold", "openai_worker_fast": "hold"}
    assert LEDGER[IMPL_ROW]["status"] == "verified"
    superseded = LEDGER[IMPL_ROW]["supersedes"]
    assert len(superseded) == 1 and superseded[0].startswith(
        "claude_worker_balanced tier measurement after the ") and superseded[0] in LEDGER


def test_a_writing_row_moves_only_where_both_replays_say_so():
    """The review replay read by the same rule (the 2026-10-14 numbers), and
    the implementation replay: a tier moved exactly where both raise."""
    review = _row_verdicts(_recalls(_text(LIVE_ROW)), _text(LIVE_ROW))
    assert review == {"claude_worker_balanced": "raise", "claude_worker_fast": "raise",
                      "openai_worker_balanced": "hold", "openai_worker_fast": "hold"}
    seg = _text(IMPL_ROW).split("Pass rates (preregistered grading):", 1)[1]
    impl = _row_verdicts(_recalls(seg.split("The preregistered rule", 1)[0]), _text(IMPL_ROW))
    for key in WRITING:
        before = TIERS_BEFORE_1_23.get(key, MODELS[key]["capability_tier"])
        step = {"raise": 1, "lower": -1}.get(review[key], 0) if review[key] == impl[key] else 0
        assert MODELS[key]["capability_tier"] == before + step, (key, review[key], impl[key])


def test_a_retry_at_the_confidence_threshold_dispatches_the_balanced_seat():
    """Review i1 (fable F2, sol), pinned in 1.23.0 as ESCALATE_ROUTING at 0.54
    and REPAIRED in 1.23.1 (issue #70). A retry after the xai seat fails sits
    exactly at 0.60 (u3 0.20, unknown root cause 0.10, one failure 0.05). With
    the Claude balanced model at tier 2 the ladder's weakest role above tier 1
    is the balanced role, which reaches that model through its fallback list.
    1.23.0 billed that as a scarcity fallback (0.06) on top of the failure the
    retry already pays for; nothing was missing, the xai seat had FAILED, so
    1.23.1 compares the role against the first seat that did not merely fail
    (`Resolver._retry_baseline`) and the retry dispatches Sonnet at 0.60. The
    1.23.0 population (454 in the release replay) is the population measured,
    not a count promised back; the retries that stay terminal are the ones whose
    supply is really short (test_retry_eligibility.py)."""
    kw = dict(task_class="DEBUGGING", complexity=0, uncertainty=3, blast_radius=0,
              reversibility=2, runtime="claude_code", flags=["unknown_root_cause"])
    fresh = route(Task(**kw), CFG)
    assert fresh["selected_model"] == XAI
    retry = route(Task(**kw, prior_failures=1, prior_models=[XAI]), CFG)
    assert retry["terminal"] is None
    assert retry["selected_model"] == ID("claude_worker_balanced")
    assert retry["fallbacks_applied"] == []
    assert retry["routing_confidence"] == pytest.approx(0.60)
    old = copy_cfg_with_tier("claude_worker_balanced", 1)
    before = route(Task(**kw, prior_failures=1, prior_models=[XAI]), old)
    assert (before["terminal"], before["selected_model"]) == (None, ID("claude_senior"))
    assert before["routing_confidence"] == pytest.approx(0.60)


def test_a_review_with_two_families_out_now_asks_to_be_rerouted():
    """Review i2 (fable F1): the first-attempt half of the 718 — 264 REVIEW
    tasks with an unknown root cause, the xai seat withheld and OpenAI
    exhausted. 1.22.0 paired the senior and architect seats at 0.65; the
    seat search now lands on a fallback seat (0.06) and the route asks to be
    re-routed.

    KEPT in 1.23.1 on purpose (issue #70). Unlike the retry above nothing here
    failed: two families are really withheld, so the fallback is a genuine
    scarcity observation and the 0.59 is the router saying the supply is thin.
    There is no history to exempt, and no blanket exemption for assignment or
    fallback was added."""
    kw = dict(task_class="REVIEW", complexity=0, uncertainty=3, blast_radius=1,
              reversibility=3, runtime="claude_code", flags=["unknown_root_cause"],
              unavailable_models=[XAI], _family_quota={"openai": "exhausted"})
    out = route(Task(**kw), CFG)
    assert out["terminal"] == "ESCALATE_ROUTING"
    assert out["routing_confidence"] == pytest.approx(0.59)
    before = route(Task(**kw), copy_cfg_with_tier("claude_worker_balanced", 1))
    assert before["terminal"] is None and before["routing_confidence"] == pytest.approx(0.65)
    assert [s["model_id"] for s in before["dispatch_seats"]] == [ID("claude_senior"),
                                                                 ID("claude_architect")]


def copy_cfg_with_tier(key: str, tier: int) -> dict:
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["models"][key]["capability_tier"] = tier
    return cfg


# ---------------------------------------------------------------------------
# The gemini seat's own measurement (1.24.0, T5)
# ---------------------------------------------------------------------------

ARGON_ROW = "gemini_frontier tier measurement (real-review replay, 2026-10-27)"


def test_the_gemini_seat_measurement_rederives_its_tier():
    """Re-derive T5's verdict from the numbers the row records: anchors are the
    measured live rows of the registry before the move (the seat itself left
    out), the gate separates tier 1 from tier 2, and the seat takes the highest
    tier whose bar it clears. The registry must hold that tier."""
    row = LEDGER[ARGON_ROW]
    assert row["status"] == "verified"
    assert row["supersedes"] == ["gemini_frontier tier evidence (external, not measured)"]
    text = _text(ARGON_ROW)
    rec = _recalls(text)
    seat = MODELS["gemini_frontier"]["id"]
    live = {key: m for key, m in MODELS.items()
            if m.get("dispatchable", True) is not False and "history_of" not in m
            and key != "gemini_frontier"}
    tier = lambda t: [rec[_id(k, text)] for k, m in live.items()           # noqa: E731
                      if m["capability_tier"] == t and _id(k, text) in rec]
    t2, t1 = tier(2), tier(1)
    assert len(t2) == 3 and len(t1) == 1, (t2, t1)
    a2, a1, mean2 = min(t2), min(t1), sum(t2) / len(t2)
    gate = mean2 - a1
    assert gate >= SEPARATION
    stated = {k: float(v) for k, v in re.findall(
        r"(Tier-2 mean|separation|Bar for tier 2:|Bar for tier 1:) (0\.\d{6})", text)}
    assert abs(stated["Tier-2 mean"] - mean2) < 2e-6 and abs(stated["separation"] - gate) < 2e-6
    assert abs(stated["Bar for tier 2:"] - (a2 - CONFIRM_MARGIN)) < 2e-6
    assert abs(stated["Bar for tier 1:"] - (a1 - CONFIRM_MARGIN)) < 2e-6
    r = rec[seat]
    derived = 2 if r >= a2 - CONFIRM_MARGIN else 1 if r >= a1 - CONFIRM_MARGIN else 0
    assert re.search(rf"Verdict {re.escape(seat)}: tier {derived}\.", text)
    assert MODELS["gemini_frontier"]["capability_tier"] == derived
    # the seat it replaced is reported beside it, not used as an anchor
    assert MODELS["gemini_worker_balanced"]["id"] in rec


def test_the_gemini_seat_cannot_write_without_an_agreeing_implementation_replay():
    """Argon's replay never ran and was stopped with the row (1.26.0); the live seat's
    own replay is recorded, and write authority stays withheld unless both replays
    name the same tier and the user then grants it (review i1, fable F7: bound to the
    live seat). In 1.26.0 they disagreed (hydrogen), so the direction could not write.

    (a) 1.27.0: the live seat is gemini_neon; hydrogen is retired with its two
    disagreeing replays kept as the record. Neon's review replay (T15) reads tier
    2; its implementation replay row, "gemini_neon implementation replay
    (2026-11-23)", is not in the ledger until that replay is scored. While no
    verified implementation-replay row for the live seat exists, or while one
    names a different tier than its review replay, or while the seat is not the
    family's only dispatchable row, neither direction may write."""
    assert LEDGER["gemini_frontier implementation replay (not run, 2026-10-27)"]["status"] == "documented"
    assert LEDGER["gemini_frontier implementation replay (stopped, 2026-11-15)"]["status"] == "documented"
    # hydrogen (1.26.x): its replays disagreed, it never wrote, and it is retired
    assert _stated_tier(HYDROGEN_ROW) != _stated_tier(HYDROGEN_IMPL_ROW)
    assert MODELS["gemini_hydrogen"]["dispatchable"] is False
    # the live seat
    assert MODELS[GEMINI_KEY].get("dispatchable", True) is True
    # the rule: write authority on a gemini direction needs the two replays to agree
    # and this to be the family's only dispatchable row (review i2, fable)
    if not _gemini_write_proposable(LEDGER, MODELS):
        for host in ("claude_code", "codex"):
            assert CFG["transports"][host]["to_gemini"]["write_verified"] is False, host
    # and the state: nothing has been granted (a grant changes this together with
    # the ledger row that records it)
    for host in ("claude_code", "codex"):
        assert CFG["transports"][host]["to_gemini"]["write_verified"] is False, host


def _row_tier(entry: dict) -> int:
    found = re.findall(r"Verdict: tier (\d)\.", " ".join(entry["evidence"].split()))
    assert len(found) == 1, (entry["item"], found)
    return int(found[0])


def _gemini_write_proposable(ledger: dict, models: dict) -> bool:
    """Write authority may be proposed for the live gemini seat only when every
    verified implementation-replay row for it names the tier its review replay
    names (and at least one exists), and it is the family's only dispatchable row."""
    review = _row_tier(ledger[NEON_ROW])
    impl = {_row_tier(e) for k, e in ledger.items()
            if k.startswith(f"{GEMINI_KEY} implementation replay") and e.get("status") == "verified"}
    rows = [k for k, m in models.items()
            if m["family"] == "gemini" and m.get("dispatchable", True) is not False]
    return impl == {review} and rows == [GEMINI_KEY]


def test_the_write_guard_reads_a_missing_or_disagreeing_implementation_replay_as_no():
    """The predicate of the guard above, on fabricated ledgers, so its branches are
    exercised before the live seat's implementation replay lands: no row, a row naming
    another tier, an unverified row, two rows that disagree, a second dispatchable
    gemini row — each withholds write authority; only an agreeing verified row on the
    family's only dispatchable row leaves it proposable (still not granted).

    Review i1 (sol F8): this is a release prerequisite, not a runtime check. The
    router never reads these ledger rows to decide a write grant (`Policy` validates
    the maker recipe and its verification entries, not the replays); what keeps a
    grant from shipping without agreeing replays is the test above, which fails on
    the shipped config whenever `write_verified` is true and this predicate is not."""
    import copy
    review = _row_tier(LEDGER[NEON_ROW])
    other = 1 if review != 1 else 2
    name = f"{GEMINI_KEY} implementation replay (2026-11-23)"

    def ledger(*rows):
        out = {k: v for k, v in LEDGER.items()
               if not k.startswith(f"{GEMINI_KEY} implementation replay")}
        for i, (tier, status) in enumerate(rows):
            item = name if i == 0 else f"{GEMINI_KEY} implementation replay (re-run {i})"
            out[item] = {"item": item, "status": status,
                         "evidence": f"Pass rate: x. Verdict: tier {tier}. Limits: y."}
        return out
    two_live = copy.deepcopy(MODELS)
    two_live["gemini_hydrogen"]["dispatchable"] = True
    assert not _gemini_write_proposable(ledger(), MODELS)
    assert not _gemini_write_proposable(ledger((other, "verified")), MODELS)
    assert not _gemini_write_proposable(ledger((review, "documented")), MODELS)
    assert not _gemini_write_proposable(ledger((review, "verified"), (other, "verified")), MODELS)
    assert not _gemini_write_proposable(ledger((review, "verified")), two_live)
    assert _gemini_write_proposable(ledger((review, "verified")), MODELS)


HYDROGEN_ROW = "gemini_hydrogen tier measurement (real-review replay, 2026-11-15)"
HYDROGEN_IMPL_ROW = "gemini_hydrogen implementation replay (2026-11-15)"
NEON_ROW = "gemini_neon tier measurement (real-review replay, 2026-11-23)"


def _stated_tier(item: str) -> int:
    found = re.findall(r"Verdict: tier (\d)\.", " ".join(_text(item).split()))
    assert len(found) == 1, (item, found)
    return int(found[0])


def test_the_hydrogen_implementation_replay_rederives_its_verdict_and_moves_nothing():
    """1.26.0, T12: re-derive the implementation verdict from the pass rates the row
    records, against the live writing rows of the registry before the measurement
    (rows with no implementation score are not anchors; the tier-3 architect is
    reported beside). A seat's tier moves only where both replays agree, so with
    the review replay at tier 1 and this one at tier 2 the row stays at tier 1."""
    row = LEDGER[HYDROGEN_IMPL_ROW]
    assert row["status"] == "verified"
    text = _text(HYDROGEN_IMPL_ROW)
    scores = _recalls(text)
    seat = MODELS["gemini_hydrogen"]["id"]
    live = _live_others("gemini_hydrogen", **BEFORE_1_26)
    # each row under the id it held when measured (review i2, fable: an anchor's
    # later id bump must not break this)
    tier = lambda t: [scores[_id(k, text)] for k, m in live.items()     # noqa: E731
                      if m["capability_tier"] == t and _id(k, text) in scores]
    t2, t1, t0 = tier(2), tier(1), tier(0)
    assert len(t2) == 3 and len(t1) == 1 and len(t0) == 2, (t2, t1, t0)
    assert _id("claude_architect", text) not in scores        # beside, not an anchor
    assert _id("gemini_frontier", text) not in scores         # argon never wrote
    a2, a1, mean2 = min(t2), min(t1), sum(t2) / len(t2)
    stated = {k: float(v) for k, v in re.findall(
        r"(Tier-2 mean|separation|Bar for tier 2:|Bar for tier 1:) (0\.\d{6})", text)}
    assert abs(stated["Tier-2 mean"] - mean2) < 2e-6
    assert abs(stated["separation"] - (mean2 - a1)) < 2e-6 and mean2 - a1 >= SEPARATION
    assert abs(stated["Bar for tier 2:"] - (a2 - CONFIRM_MARGIN)) < 2e-6
    assert abs(stated["Bar for tier 1:"] - (a1 - CONFIRM_MARGIN)) < 2e-6
    x = scores[seat]
    derived = 2 if x >= a2 - CONFIRM_MARGIN else 1 if x >= a1 - CONFIRM_MARGIN else 0
    assert _stated_tier(HYDROGEN_IMPL_ROW) == derived
    # the figures agree: the runs named, the per-task list, the profile table
    flat = " ".join(text.split())
    runs = re.search(rf"Pass rate: {re.escape(seat)} 0\.\d{{6}} \((\w+) of sixteen runs: "
                     r"both on ([^,]+(?:, [^,]+)*) and (T\d\d), none on (T\d\d) or (T\d\d)", flat)
    assert runs, flat
    words = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14}
    both = runs.group(2).split(", ") + [runs.group(3)]
    assert words[runs.group(1)] == 2 * len(both) == round(x * 16)
    assert len(both) + 2 == 8 and not {runs.group(4), runs.group(5)} & set(both)
    profile = (SKILL / "references" / "model-profiles.md").read_text()
    section = profile.split("## Tiers measured on 2026-11-15", 1)[1]
    assert f"**{round(x * 100)}% ({round(x * 16)} of 16)**" in section
    review = _stated_tier(HYDROGEN_ROW)
    if derived != review:
        assert "replays disagree" in " ".join(text.split())
    assert MODELS["gemini_hydrogen"]["capability_tier"] == review


def test_the_hydrogen_seat_measurement_rederives_its_tier():
    """1.26.0, T11: re-derive the verdict from the numbers the row records, with the
    registry as it stood before the measurement (argon live at tier 1, the subject
    left out): two tier-1 anchors now, the lowest sets the bar."""
    row = LEDGER[HYDROGEN_ROW]
    assert row["status"] == "verified"
    assert row["supersedes"] == ["gemini_hydrogen tier evidence (external, not measured)"]
    text = _text(HYDROGEN_ROW)
    rec = _recalls(text)
    seat = MODELS["gemini_hydrogen"]["id"]
    live = _live_others("gemini_hydrogen", **BEFORE_1_26)
    tier = lambda t: [rec[_id(k, text)] for k, m in live.items()           # noqa: E731
                      if m["capability_tier"] == t and _id(k, text) in rec]
    t2, t1 = tier(2), tier(1)
    assert len(t2) == 3 and len(t1) == 2, (t2, t1)
    a2, a1, mean2 = min(t2), min(t1), sum(t2) / len(t2)
    gate = mean2 - a1
    assert gate >= SEPARATION
    stated = {k: float(v) for k, v in re.findall(
        r"(Tier-2 mean|separation|Bar for tier 2:|Bar for tier 1:) (0\.\d{6})", text)}
    assert abs(stated["Tier-2 mean"] - mean2) < 2e-6 and abs(stated["separation"] - gate) < 2e-6
    assert abs(stated["Bar for tier 2:"] - (a2 - CONFIRM_MARGIN)) < 2e-6
    assert abs(stated["Bar for tier 1:"] - (a1 - CONFIRM_MARGIN)) < 2e-6
    r = rec[seat]
    derived = 2 if r >= a2 - CONFIRM_MARGIN else 1 if r >= a1 - CONFIRM_MARGIN else 0
    assert "Verdict: tier %d." % derived in " ".join(text.split())
    assert MODELS["gemini_hydrogen"]["capability_tier"] == derived
    assert MODELS["gemini_frontier"]["id"] in rec      # the retired seat was an anchor then


def test_the_neon_seat_measurement_rederives_its_tier():
    """1.27.0, T15: re-derive the verdict from the numbers the row records, with the
    registry as it stood before the measurement (1.26.1: hydrogen live at tier 1, the
    subject left out): three tier-2 anchors set the tier-2 mean and bar, two tier-1
    anchors the tier-1 bar; tier-0 rows and the tier-3 policy rows are reported
    beside and decide nothing. The registry must hold the derived tier."""
    row = LEDGER[NEON_ROW]
    assert row["status"] == "verified"
    assert row["supersedes"] == ["gemini_neon tier evidence (external, not measured)"]
    assert LEDGER["gemini_neon tier evidence (external, not measured)"]["status"] == "documented"
    text = _text(NEON_ROW)
    rec = _recalls(text)
    seat = MODELS[GEMINI_KEY]["id"]
    live = _live_others(GEMINI_KEY, **BEFORE_1_27)
    assert "gemini_hydrogen" in live and GEMINI_KEY not in live
    # each row under the id it held when measured
    tier = lambda t: [rec[_id(k, text)] for k, m in live.items()           # noqa: E731
                      if m["capability_tier"] == t and _id(k, text) in rec]
    t2, t1, t3 = tier(2), tier(1), tier(3)
    assert len(t2) == 3 and len(t1) == 2 and len(t3) == 2, (t2, t1, t3)
    assert MODELS["gemini_hydrogen"]["id"] in rec       # the retired seat was an anchor then
    assert MODELS["xai_frontier"]["id"] not in rec      # named unmeasured
    assert re.search(re.escape(MODELS["xai_frontier"]["id"]) + r"[^.;)]*unmeasured", text)
    a2, a1, mean2 = min(t2), min(t1), sum(t2) / len(t2)
    gate = mean2 - a1
    assert gate >= SEPARATION
    stated = {k: float(v) for k, v in re.findall(
        r"(Tier-2 mean|separation|Bar for tier 2:|Bar for tier 1:) (0\.\d{6})", text)}
    assert abs(stated["Tier-2 mean"] - mean2) < 2e-6 and abs(stated["separation"] - gate) < 2e-6
    assert abs(stated["Bar for tier 2:"] - (a2 - CONFIRM_MARGIN)) < 2e-6
    assert abs(stated["Bar for tier 1:"] - (a1 - CONFIRM_MARGIN)) < 2e-6
    r = rec[seat]
    # the recall is the mean of the package recalls the row lists
    packages = [float(v) for v in re.search(
        re.escape(seat) + r" 0\.\d{6} \(packages (0\.\d{6}), (0\.\d{6}), (0\.\d{6})\)",
        text).groups()]
    assert abs(sum(packages) / 3 - r) < 2e-6
    derived = 2 if r >= a2 - CONFIRM_MARGIN else 1 if r >= a1 - CONFIRM_MARGIN else 0
    assert _stated_tier(NEON_ROW) == derived == 2
    assert MODELS[GEMINI_KEY]["capability_tier"] == derived
    # tier 3 is reported beside, not decided: no live tier-3 row has a measured tier,
    # and the figure the row compares against is the lower policy row's bar
    beside = float(re.search(r"the recall is above (0\.\d{6}), within 0\.05 of the lower "
                             r"tier-3 policy row's", text).group(1))
    assert abs(beside - (min(t3) - CONFIRM_MARGIN)) < 2e-6 and r >= beside
    assert "tier 3 is not decidable on this suite" in text
    # the profile's table states the same figures
    section = PROFILES_MD.split("## Tiers measured on 2026-11-23", 1)[1]
    assert f"**{derived}** | **{round(r * 100)}%**" in section
    assert (f"The tier-2 bar is {(a2 - CONFIRM_MARGIN) * 100:.1f}% and the tier-1 bar "
            f"{(a1 - CONFIRM_MARGIN) * 100:.1f}%") in " ".join(section.split())


# ---------------------------------------------------------------------------
# Reading a ledger row under the ids it measured (1.24.1, review d1 fable F4)
# ---------------------------------------------------------------------------

def test_a_row_is_read_under_the_id_it_names_not_the_live_one():
    """The 2026-10-14/15 rows measured the balanced OpenAI seat under the id
    the registry held then; the live row has moved on since. A row naming
    neither generation reads the live id, and a key with no chain is live."""
    key = "openai_worker_balanced"
    was, now = CHAINS[key][-2], CHAINS[key][-1]
    assert MODELS[key]["id"] == now != was
    for item in (LIVE_ROW, IMPL_ROW, ARGON_ROW):
        assert _id(key, _text(item)) == was, item
    assert _id(key, "a row that names no generation") == now
    assert _id(key, f"{was} 0.1 then {now} 0.2") == now      # the newest named wins
    assert _id("claude_architect", "nothing") == MODELS["claude_architect"]["id"]
    # a longer id that merely contains the old one is not a mention of it
    assert _id(key, f"{was}-preview 0.1") == now
    assert _id(key, f"a row that measured {was}.") == was      # sentence-final period
    assert _id(key, f"{was}.5 0.1") == now                      # a longer dotted id


# ---------------------------------------------------------------------------
# The balanced OpenAI seat's new generation (1.24.1, T7 review replay, T8
# implementation replay)
# ---------------------------------------------------------------------------

TERRA_REVIEW_ROW = "openai_worker_balanced tier measurement (real-review replay, 2026-10-27)"
TERRA_IMPL_ROW = "openai_worker_balanced implementation replay (2026-10-27)"
TERRA_KEY = "openai_worker_balanced"


# The registry as it stood before a measurement (references/evaluation.md): 1.26.0 retired
# the argon row (live from 2026-10-27, so an anchor for T7 and T11) and added the hydrogen
# row (live from 1.26.0, after every measurement before T11, and T11's own subject).
# (a) 1.27.0 retired the hydrogen row (live in 1.26.x, so an anchor for T15) and added the
# neon row (live from 1.27.0, after every measurement before T15, and T15's own subject),
# so the registry before 1.26 leaves neon out too.
BEFORE_1_26 = dict(plus=("gemini_frontier",), minus=("gemini_hydrogen", "gemini_neon"))
BEFORE_1_27 = dict(plus=("gemini_hydrogen",), minus=("gemini_neon",))


def _live_others(key: str, plus=(), minus=()) -> dict:
    return {k: m for k, m in MODELS.items()
            if ((m.get("dispatchable", True) is not False and "history_of" not in m) or k in plus)
            and k != key and k not in minus}


def test_the_balanced_openai_review_replay_rederives_its_verdict():
    """T7, read for a writing row at tier 1 with the per-row rule: anchors are
    the measured live rows of the registry before the measurement, the decided
    row left out (its predecessor reported beside, never an anchor)."""
    row = LEDGER[TERRA_REVIEW_ROW]
    assert row["status"] == "verified"
    text = _text(TERRA_REVIEW_ROW)
    rec = _recalls(text)
    seat, was = MODELS[TERRA_KEY]["id"], CHAINS[TERRA_KEY][-2]
    live = _live_others(TERRA_KEY, **BEFORE_1_26)
    tier = lambda t: [rec[_id(k, text)] for k, m in live.items()           # noqa: E731
                      if m["capability_tier"] == t and _id(k, text) in rec]
    t2, t1 = tier(2), tier(1)
    assert len(t2) == 3 and len(t1) == 1, (t2, t1)
    a2, a1, mean2 = min(t2), min(t1), sum(t2) / len(t2)
    stated = {k: float(v) for k, v in re.findall(
        r"(Tier-2 mean|separation|Bar for raise:|Bar for lower:) (0\.\d{6})", text)}
    assert abs(stated["Tier-2 mean"] - mean2) < 2e-6
    assert abs(stated["separation"] - (mean2 - a1)) < 2e-6 and mean2 - a1 >= SEPARATION
    assert abs(stated["Bar for raise:"] - (a2 - CONFIRM_MARGIN)) < 2e-6
    assert abs(stated["Bar for lower:"] - (a1 - CONFIRM_MARGIN)) < 2e-6
    r = rec[seat]
    derived = ("raise" if r >= a2 - CONFIRM_MARGIN else
               "lower" if r < a1 - CONFIRM_MARGIN else "hold")
    assert re.search(rf"Review verdict {re.escape(seat)}: {derived}\.", text)
    assert was in rec and was not in [_id(k, text) for k in live]       # beside, not an anchor


def test_the_balanced_openai_implementation_replay_rederives_its_advisory_and_the_tier():
    """T8: no other live tier-1 row has an implementation score, so the
    method's verdict is withheld; the preregistered advisory uses the
    predecessor as the tier-1 reference. A tier moves only when the review
    verdict and the implementation reading agree on raise or lower."""
    row = LEDGER[TERRA_IMPL_ROW]
    assert row["status"] == "verified"
    text = _text(TERRA_IMPL_ROW)
    assert "withheld by construction" in text
    scores = _recalls(text)
    seat, was = MODELS[TERRA_KEY]["id"], CHAINS[TERRA_KEY][-2]
    live = _live_others(TERRA_KEY, **BEFORE_1_26)
    # each anchor under the id the row names (1.26.1 moved openai_reasoning to the next Sol
    # generation; the row's tier-2 Sol anchor is the id it measured)
    up = [scores[_id(k, text)] for k, m in live.items()
          if m["capability_tier"] == 2 and _id(k, text) in scores]
    own = [scores[_id(k, text)] for k, m in live.items()
           if m["capability_tier"] == 1 and _id(k, text) in scores]
    assert len(up) == 3 and own == [], (up, own)
    ref, x = scores[was], scores[seat]
    gate = sum(up) / len(up) - ref
    stated = {k: float(v) for k, v in re.findall(
        r"(gate|advisory raise bar|advisory lower bar) (0\.\d{6})", text)}
    assert abs(stated["gate"] - gate) < 2e-6 and gate >= SEPARATION
    assert abs(stated["advisory raise bar"] - (min(up) - CONFIRM_MARGIN)) < 2e-6
    assert abs(stated["advisory lower bar"] - (ref - CONFIRM_MARGIN)) < 2e-6
    advisory = ("raise" if x >= min(up) - CONFIRM_MARGIN else
                "lower" if x < ref - CONFIRM_MARGIN else "hold")
    assert re.search(rf"Advisory verdict {re.escape(seat)}: {advisory}\.", text)
    review = re.search(rf"Review verdict {re.escape(seat)}: (raise|lower|hold)\.",
                       _text(TERRA_REVIEW_ROW)).group(1)
    step = {"raise": 1, "lower": -1}.get(review, 0) if review == advisory else 0
    assert MODELS[TERRA_KEY]["capability_tier"] == 1 + step


def test_the_balanced_openai_review_row_names_the_gate_blocked_sensitivities():
    """Review i1 (fable F2): a sensitivity whose gate fails is not evidence of
    hold, and the row must say which ones those are."""
    text = _text(TERRA_REVIEW_ROW)
    assert "Every sensitivity" not in text
    sol = _id("openai_reasoning", text)          # the Sol generation the row measured against
    assert "gate fails" in text and f"{sol} alone" in text and "package C alone" in text


@pytest.mark.parametrize("runtime,ahead", [("codex", ["xai_frontier"]),
                                           ("claude_code", ["xai_frontier", "meta_worker_balanced"])])
def test_a_retry_after_the_seat_fails_climbs_to_the_frontier_and_is_gated(runtime, ahead):
    """Review i1 (sol, fable: missing test). After the tier-2 seat fails with the
    Claude family exhausted, the retry climbs above tier 2, to the frontier OpenAI
    model; a MEDIUM review of a tier-3 implementer's work must be tier 3, and with
    the Claude family out the only reviewer left is the Sol seat, so the route is
    gated `review_below_band` (760 such retries in the replay; ledger row "1.27.0
    gemini seat placement counterfactuals"). With the seat at tier 1 the retry went
    to the Sol seat, which the frontier model reviewed, ungated."""
    kw = dict(task_class="INVESTIGATION", complexity=0, uncertainty=0, blast_radius=1,
              reversibility=2, runtime=runtime, unavailable_models=[ID(k) for k in ahead],
              _family_quota={"claude": "exhausted"})
    assert route(Task(**kw), CFG)["selected_model"] == GEMINI
    sol, astra = ID("openai_reasoning"), ID("openai_frontier")
    before = route(Task(**kw, prior_failures=1, prior_models=[GEMINI]), GEMINI_TIER1)
    after = route(Task(**kw, prior_failures=1, prior_models=[GEMINI]), CFG)
    assert (before["selected_model"], before["review"]["reviewer_models"]) == (sol, [astra])
    assert before["human_control_causes"] == [] and before["requires_human_confirmation"] is False
    assert (after["selected_model"], after["review"]["reviewer_models"]) == (astra, [sol])
    assert after["terminal"] is None and after["review"]["band"] == "MEDIUM"
    assert POLICY.tier_of[astra] == 3 and POLICY.tier_of[sol] == 2
    assert after["human_control_causes"] == ["review_below_band"]
    assert after["requires_human_confirmation"] is True
