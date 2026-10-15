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
    """`<registry id> <recall>` pairs as the row records them (four places)."""
    ids = {m["id"] for m in MODELS.values()}
    out = {}
    for mid, value in re.findall(r"([a-z0-9][a-z0-9.\-]*) (0\.\d{4})\b", text):
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
    bar = float(re.search(r"Bar for tier 1: (0\.\d{4})", text).group(1))
    mean, sep = map(float, re.search(r"Tier-2 mean (0\.\d{4}), separation (0\.\d{4})",
                                     text).groups())
    assert abs(bar - (d["a1"] - CONFIRM_MARGIN)) < 1e-4
    assert abs(mean - d["t2"]) < 1e-4 and abs(sep - (d["t2"] - d["a1"])) < 1e-4
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
