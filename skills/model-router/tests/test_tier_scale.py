"""The tier scale (issue #63, model-router 1.21.1).

A tier is held by its live rows, so a measurement is read against them, not
against a history row; a review replay decides a read-only seat's tier and
never moves a writing row's on its own. These tests re-derive the recorded
verdicts from the recorded numbers with that method, so the ledger, the
registry and the method cannot drift apart silently.

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


def _text(item: str) -> str:
    return " ".join(LEDGER[item]["evidence"].split())


def _recalls(text: str) -> dict:
    """`<registry id> <recall>` pairs as the row records them."""
    ids = {m["id"] for m in MODELS.values()}
    out = {}
    for mid, value in re.findall(r"([a-z0-9][a-z0-9.\-]*) (0\.\d+)", text):
        if mid in ids:
            out.setdefault(mid, float(value))
    return out


def _live(tier: int, measured: dict) -> list[float]:
    """Recall of the measured live rows of a tier. History rows and the seats
    being decided are never anchors."""
    return [measured[m["id"]] for key, m in MODELS.items()
            if m.get("dispatchable", True) is not False and "history_of" not in m
            and key not in SEATS and m["capability_tier"] == tier and m["id"] in measured]


def test_the_method_is_written_down():
    section = EVALUATION_MD.split("## How a tier is measured", 1)
    assert len(section) == 2, "evaluation.md has no tier-measurement section"
    body = " ".join(section[1].split())
    for phrase in ("Anchors are live rows", "never used as its anchor",
                   "the anchor is the lowest one measured", "*Confirmed*", "*Lowered*",
                   "*inconclusive*", "an implementation replay point the same way",
                   "A review replay alone is recorded as a measurement and moves nothing"):
        assert phrase in body, phrase


def test_the_live_anchor_row_supersedes_both_seat_measurements():
    row = LEDGER[LIVE_ROW]
    assert row["status"] == "verified"
    old = [f"{key} tier measurement (real-review replay, 2026-10-14)" for key in SEATS]
    assert sorted(row["supersedes"]) == sorted(old)
    for item in old:
        assert item in LEDGER, item


def test_the_recorded_verdicts_follow_from_the_recorded_numbers():
    """Re-derive the verdict: A_1 = the lowest measured live tier-1 row, T_2 =
    the mean of the measured live tier-2 rows. A history row in the same
    evidence (the old anchor) must not change the answer."""
    text = _text(LIVE_ROW)
    measured = _recalls(text)
    history = [m["id"] for m in MODELS.values() if "history_of" in m and m["id"] in measured]
    assert history, "the old history anchor should be reported beside the result"
    tier1, tier2 = _live(1, measured), _live(2, measured)
    assert len(tier1) >= 2 and len(tier2) >= 2, (tier1, tier2)
    a1, t2 = min(tier1), sum(tier2) / len(tier2)
    assert t2 - a1 >= SEPARATION, "the suite must separate the tier-1 anchor from tier 2"
    for key in SEATS:
        r = measured[MODELS[key]["id"]]
        verdict = ("confirmed" if r >= a1 - CONFIRM_MARGIN - 1e-9 else
                   "lowered" if a1 - r >= SEPARATION else "inconclusive")
        expected_tier = 0 if verdict == "lowered" else 1
        assert MODELS[key]["capability_tier"] == expected_tier, (key, verdict)
        assert verdict in text, (key, verdict)


@pytest.mark.parametrize("key", SEATS)
def test_the_profiles_call_the_seat_tiers_provisional(key):
    """A tier that is not confirmed must not read as a current measurement."""
    heading = {"meta_worker_balanced": "### The meta reviewer seat",
               "gemini_worker_balanced": "### The gemini reviewer seat"}[key]
    section = PROFILES_MD.split(heading, 1)[1].split("\n### ", 1)[0]
    assert "provisional again" in section
    assert "measured since" not in section


def test_a_writing_row_keeps_its_tier_on_a_review_replay_alone():
    """claude_worker_fast recalled level with the tier-1 and tier-2 Claude
    rows; it writes, so the review replay is recorded and the tier stays."""
    assert MODELS["claude_worker_fast"]["capability_tier"] == 0
    rows = [e for e in LEDGER.values()
            if e.get("status") == "quality_inherited_not_remeasured"
            and "claude_worker_fast" in e["item"]
            and MODELS["claude_worker_fast"]["id"] in e["item"]]
    assert len(rows) == 1
    evidence = " ".join(rows[0]["evidence"].split())
    assert "implementation replay" in evidence and COUNTERFACTUALS in evidence
    assert COUNTERFACTUALS in LEDGER and LEDGER[COUNTERFACTUALS]["status"] == "verified"
