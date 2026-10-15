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


# A verdict reads the registry as it stood before its measurement
# (references/evaluation.md): the 2026-10-14 replays ran while the Claude
# balanced model was tier 1; 1.23.0 moved it to 2 (issue #65).
TIERS_BEFORE_1_23 = {"claude_worker_balanced": 1}


def _live_rows(tier: int, as_of: dict | None = None) -> list[str]:
    """Live rows of a tier that may anchor: not history, not a decided seat."""
    as_of = as_of or {}
    return [m["id"] for key, m in MODELS.items()
            if m.get("dispatchable", True) is not False and "history_of" not in m
            and key not in SEATS and as_of.get(key, m["capability_tier"]) == tier]


def _derive(text: str) -> dict:
    measured = _recalls(text)
    tier1 = [measured[i] for i in _live_rows(1, TIERS_BEFORE_1_23) if i in measured]
    tier2 = [measured[i] for i in _live_rows(2, TIERS_BEFORE_1_23) if i in measured]
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
    for mid in _live_rows(1, TIERS_BEFORE_1_23):
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
    is tier 1 or better and never a tier-0 seat. (A promotion that traded a
    tier-2 model a scarce fast role resolved to for a tier-1 balanced one is
    older than 1.22.0; issue #67 closed it in 1.23.0 — see below.)"""
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
    another family; the seats are no longer that tier. Since 1.23.0 the Claude
    balanced model it swaps away from is tier 2, so the swap lands on the
    OpenAI tier-2 model (issue #65)."""
    out = route(Task(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime=runtime, unavailable_models=[XAI],
                     _family_quota={"claude": "low"}), CFG)
    assert out["selected_model"] == ID("openai_reasoning")
    assert POLICY.tier_of[out["selected_model"]] == POLICY.tier_of[ID("claude_worker_balanced")]
    assert any(n.startswith("family_quota: claude low") for n in out["notes"])
    for key in SEATS:
        assert ID(key) not in [out["selected_model"], *out["review"]["reviewer_models"]]


@pytest.mark.parametrize("key,runtime,withheld", [
    ("meta_worker_balanced", "claude_code", []),
    ("gemini_worker_balanced", "codex", []),
    ("gemini_worker_balanced", "claude_code", ["meta_worker_balanced"]),
])
def test_a_retry_after_a_seat_fails_climbs_above_tier_0(key, runtime, withheld):
    """Review r2 (fable F3): a failed reading lead on a tier-0 seat is retried
    on a model above tier 0, and the route says so (r5: the gemini seat on
    Claude Code, with the meta seat withheld, too)."""
    kw = dict(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
              reversibility=1, runtime=runtime,
              unavailable_models=[XAI, *(ID(k) for k in withheld)],
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


@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_a_retry_is_never_floored_and_never_ends_where_1_21_0_did_not(runtime):
    """Reviews r3/r4: a retry is left to its ladder. No retry of a first
    attempt carries a floor note, and no retry becomes terminal where the
    seats at their 1.21.0 tier would have dispatched it."""
    import itertools
    old = _with_seat_tiers(1)
    for extra, c, u, b, r in itertools.product(DEEP_SCARCITY, range(3), range(2), range(3), range(3)):
        kw = dict(task_class="INVESTIGATION", complexity=c, uncertainty=u, blast_radius=b,
                  reversibility=r, runtime=runtime, flags=["unknown_root_cause"], **extra)
        pairs = []
        for cfg in (old, CFG):
            fresh = route(Task(**kw), cfg)
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
            assert after.get("terminal") is None, (kw, after.get("terminal"))


# ---------------------------------------------------------------------------
# Issue #67: the promotion never trades a stronger table worker away (1.23.0)
# ---------------------------------------------------------------------------

LUNA = ID("openai_worker_fast")


def test_the_promotion_keeps_a_strictly_stronger_table_worker():
    """Issue #67's case: Claude Code, the xai and fast OpenAI seats out, Claude
    exhausted. The fast role's ladder reaches gpt-6.1-sol; the balanced role's
    floored list holds a tier-1 model. An unknown root cause is evidence of
    difficulty, which must never weaken a route, so the flag leaves the worker
    where the table put it."""
    kw = dict(task_class="INVESTIGATION", complexity=3, uncertainty=0, blast_radius=0,
              reversibility=0, runtime="claude_code", unavailable_models=[XAI, LUNA],
              _family_quota={"claude": "exhausted"})
    plain = route(Task(**kw), CFG)
    flagged = route(Task(**kw, flags=["unknown_root_cause"]), CFG)
    assert plain["selected_model"] == flagged["selected_model"] == ID("openai_reasoning")
    assert flagged["selected_role"] == "worker_fast"
    assert ("investigation promotion: worker_fast kept at capability tier 2 — worker_balanced "
            "resolves to capability tier 1") in flagged["notes"]
    assert "investigation promotion: unknown root cause" not in flagged["notes"]


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


@pytest.mark.parametrize("runtime", ["claude_code", "codex", "grok"])
def test_the_promotion_never_resolves_weaker_than_the_table_worker(runtime):
    """Grid, first attempts: the risk chain's worker after the promotion is
    never weaker than the table cell's own worker, and is strictly stronger
    only where the promotion moved it. (A whole route can still seat a weaker
    worker with the flag than without it: the flag's confidence penalty can
    make the execution cell yield to keep the review unchanged — the
    execution guard, not the promotion.)"""
    import itertools
    import route_task as rt
    moved = kept = 0
    for extra, c, u, b, r in itertools.product(DEEP_SCARCITY + [{}], range(4), range(4),
                                               range(4), range(4)):
        task = Task(task_class="INVESTIGATION", complexity=c, uncertainty=u, blast_radius=b,
                    reversibility=r, runtime=runtime, flags=["unknown_root_cause"], **extra)
        resolver = rt.Resolver(task, POLICY)
        band = rt.apply_overrides(task, rt.band_from_score(rt.score(task, CFG), POLICY), POLICY)[0]
        table = rt._resolve_cell(CFG["worker_selection"]["INVESTIGATION"][band], task, [])
        role, notes = rt._legacy_pre(task, band, POLICY, resolver)
        before, after = resolver.peek(table, write=True), resolver.peek(role, write=True)
        if before is None:
            continue
        assert after is not None and POLICY.tier_of[after] >= POLICY.tier_of[before], \
            (extra, (c, u, b, r), table, before, role, after)
        if role != table:
            moved += 1
        elif any(n.startswith("investigation promotion: worker_fast kept") for n in notes):
            kept += 1
    assert moved and (kept or runtime != "claude_code")


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


def _row_verdicts(scores: dict) -> dict:
    """evaluation.md's rule, per writing row, against the tiers the registry
    held before the measurement; each verdict leaves out only its own row."""
    tier = {key: TIERS_BEFORE_1_23.get(key, MODELS[key]["capability_tier"])
            for key in WRITING + ANCHORS_2}
    out = {}
    for key in WRITING:
        k = tier[key]
        own = [scores[MODELS[o]["id"]] for o in tier if o != key and tier[o] == k]
        up = [scores[MODELS[o]["id"]] for o in tier if o != key and tier[o] == k + 1]
        gate = sum(up) / len(up) - min(own) >= SEPARATION
        x = scores[MODELS[key]["id"]]
        raise_ok = gate and x >= min(up) - CONFIRM_MARGIN
        lower_ok = gate and k >= 1 and x < min(own) - CONFIRM_MARGIN
        out[key] = ("hold" if raise_ok == lower_ok else "raise" if raise_ok else "lower")
    return out


def test_the_implementation_replay_verdicts_follow_from_its_numbers():
    text = _text(IMPL_ROW)
    seg = text.split("Pass rates (preregistered grading):", 1)[1].split("The preregistered rule", 1)[0]
    scores = _recalls(seg)
    assert set(scores) == {MODELS[k]["id"] for k in WRITING + ANCHORS_2}, scores
    verdicts = _row_verdicts(scores)
    stated = dict(re.findall(r"Implementation verdict ([a-z0-9.\-]+): (raise|lower|hold)\.", text))
    assert stated == {MODELS[k]["id"]: v for k, v in verdicts.items()}
    assert verdicts == {"claude_worker_balanced": "raise", "claude_worker_fast": "hold",
                        "openai_worker_balanced": "hold", "openai_worker_fast": "hold"}
    assert LEDGER[IMPL_ROW]["status"] == "verified"
    superseded = LEDGER[IMPL_ROW]["supersedes"]
    assert len(superseded) == 1 and superseded[0].startswith(
        "claude_worker_balanced tier measurement after the ") and superseded[0] in LEDGER


def test_a_writing_row_moves_only_where_both_replays_say_so():
    """The review replay read by the same rule (the 2026-10-14 numbers), and
    the implementation replay: a tier moved exactly where both raise."""
    review = _row_verdicts(_recalls(_text(LIVE_ROW)))
    assert review == {"claude_worker_balanced": "raise", "claude_worker_fast": "raise",
                      "openai_worker_balanced": "hold", "openai_worker_fast": "hold"}
    seg = _text(IMPL_ROW).split("Pass rates (preregistered grading):", 1)[1]
    impl = _row_verdicts(_recalls(seg.split("The preregistered rule", 1)[0]))
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
