"""The Claude architect seat's generation bump (design 2026-11-10, the next
Fable generation).

The overlay published this id after the P0-P4 gates and `model_sync.py
promote` moved it into the registry. These tests pin what that leaves behind:
the new row as probed (every effort token, the documented prices, the
substitution caveat), the old id as history input that no route seats, and the
way the tier is recorded - held with two replay scores, never as a measured
tier, because the method cannot decide a row at the top tier.

Run:  python3 -m pytest skills/model-router/tests/test_claude_architect_bump.py -q
"""

import copy
import json
import re
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

import model_sync  # noqa: E402
from route_task import Task, load_config, route  # noqa: E402

CFG = load_config()
KEY = "claude_architect"
CHAIN = json.loads((SKILL / "tests" / "fixtures" / "id-succession.json").read_text())["chains"][KEY]
NEW, OLD = CHAIN[-1], CHAIN[-2]
ROW = CFG["models"][KEY]
HISTORY = CFG["models"][f"{KEY}@{OLD}"]
ENTRIES = CFG["verification_ledger"]["entries"]
LEDGER = {e["item"]: e for e in ENTRIES}
REVIEW_ROW = f"{KEY} review replay (2026-11-10)"
IMPL_ROW = f"{KEY} implementation replay (2026-11-10)"


def _text(item: str) -> str:
    return " ".join(LEDGER[item]["evidence"].split())


def _generation(model_id: str) -> str:
    """`5.2` from the id, through the row's own lineage template."""
    prefix = ROW["lineage"]["template"].split("{gen}")[0]
    assert model_id.startswith(prefix), model_id
    return model_id[len(prefix):].replace("-", ".")


def test_the_row_is_the_probed_generation_with_no_effort_exception():
    """Every token the claude family map emits was accepted, so the row
    carries no effort_map or ceiling; context_window stays unset as on its
    predecessor (the 1.12.1 snapshot pins that leaf)."""
    assert ROW["id"] == NEW and ROW["lineage"]["line"] == "claude/fable"
    for leaf in ("effort_map", "effort_ceiling", "context_window"):
        assert leaf not in ROW, leaf
    # the tokens the row vouches for are the ones listed as probed and served, not any token
    # the prose happens to mention (review i1 sonnet F6)
    probed = _text(f"{NEW} reasoning-effort values")
    listed = re.search(r"SUCCEEDED with pong and served by the requested id: ([a-z, ]+) \(attempts", probed)
    assert listed, probed
    assert set(listed.group(1).replace(",", " ").split()) == set(CFG["effort_map"]["claude"].values())


def test_the_prices_are_the_documented_rates_with_no_long_context_tier():
    price = ROW["price_per_mtok"]
    assert (price["input"], price["output"], price["cached_input"], price["cache_write"]) == \
        (8.0, 40.0, 0.2, 10.0)
    assert "long_context" not in price
    doc = LEDGER[f"{NEW} pricing and context window"]
    assert doc["status"] == "documented" and doc["source"] == price["source"]
    assert doc["probed_on"] == price["verified_on"]


def test_the_old_id_is_a_history_row_with_its_own_prices_and_tier():
    assert HISTORY["id"] == OLD and HISTORY["history_of"] == KEY
    assert HISTORY["dispatchable"] is False and "lineage" not in HISTORY
    assert HISTORY["capability_tier"] == ROW["capability_tier"] == 3
    assert (HISTORY["price_per_mtok"]["input"], HISTORY["price_per_mtok"]["output"]) == (10.0, 50.0)


def _emitted(r):
    rv = r.get("review") or {}
    return {r.get("selected_model"), rv.get("judge_model"), *(rv.get("reviewer_models") or ())}


def test_the_old_id_is_never_seated_and_the_new_one_is():
    """Wherever the architect seat is reached - as worker, reviewer, judge or a
    fallback, including with the new id itself out - the old id never comes
    back; and the sweep does reach the seat (it is not vacuous)."""
    astra = CFG["models"]["openai_frontier"]["id"]
    opus = CFG["models"]["claude_senior"]["id"]
    scenarios = ({}, {"unavailable_models": [astra]}, {"unavailable_models": [opus]},
                 {"unavailable_models": [NEW]}, {"_family_quota": {"openai": "exhausted"}},
                 {"_local_policy": {"allowed_families": ["claude"]}},
                 {"flags": ["review_disagreement"]})
    seated = 0
    for runtime in sorted(CFG["runtimes"]):
        for cls in CFG["worker_selection"]:
            for dims in ((1, 1, 1, 0), (2, 2, 2, 1), (3, 3, 3, 3)):
                for extra in scenarios:
                    out = route(Task(task_class=cls, complexity=dims[0], uncertainty=dims[1],
                                     blast_radius=dims[2], reversibility=dims[3],
                                     runtime=runtime, **copy.deepcopy(extra)), CFG)
                    names = _emitted(out)
                    assert OLD not in names, (runtime, cls, dims, extra)
                    seated += NEW in names
    assert seated > 0


@pytest.mark.parametrize("model_id", [OLD, NEW])
@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_a_failure_of_either_generation_exhausts_the_ladder(model_id, runtime):
    """The history id keeps tier 3, so a control loop that crosses the release
    holding a failure of it gets the answer it got before: nothing is strictly
    stronger, a person decides (`prior_failure_exit` 1 in the fixture)."""
    out = route(Task(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime=runtime, prior_failures=1,
                     prior_models=[model_id]), CFG)
    assert out["terminal"] == "HUMAN_REQUIRED" and out["selected_model"] is None


def test_the_substitution_caveat_is_carried_and_documented_for_this_generation():
    """The refusal classifier stays on this generation; the caveat on the row
    and a documented ledger row for the generation go together."""
    assert ROW["served_model_caveats"] == ["security_sensitive"]
    item = (f"{ROW['lineage']['catalog_name']} {_generation(NEW)} provider-side model "
            f"substitution (refusal classifier)")
    assert LEDGER[item]["status"] == "documented" and LEDGER[item]["source"].startswith("https://")
    assert "not an execution probe" in _text(item)
    out = route(Task(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=2,
                     reversibility=1, runtime="claude_code", flags=["security_sensitive"]), CFG)
    assert out["selected_model"] == NEW
    assert any(f"{KEY} is declared_only" in n for n in out["notes"])


def test_the_maker_seat_row_exists_for_the_new_id():
    """promote always writes one: verified by an attended probe, or
    `maker_not_reprobed` saying what stands in for it."""
    row = LEDGER[f"{NEW} maker seat"]
    assert row["status"] in ("verified", "maker_not_reprobed")
    if row["status"] == "maker_not_reprobed":
        text = _text(f"{NEW} maker seat")
        assert "No attended probe-maker summary" in text and "acceptEdits" in text


def test_the_quality_disclosure_supersedes_what_promote_would_name():
    """promote's shape: the disclosure points at the rows whose item names the
    superseded id; the two replay rows are cited in its evidence."""
    item = f"{KEY} quality evidence after the {NEW} id bump"
    others = [e for e in ENTRIES
              if not re.search(r"(?<![A-Za-z0-9._-])" + re.escape(NEW) + r"(?![A-Za-z0-9_-])", e["item"])]
    want = model_sync._superseded_items(KEY, [OLD], others)
    assert LEDGER[item]["status"] == "quality_inherited_not_remeasured"
    assert LEDGER[item]["supersedes"] == want
    text = _text(item)
    for cited in (REVIEW_ROW, IMPL_ROW):
        assert cited in LEDGER and cited in text, cited


REPO = SKILL.parent.parent
CLAIM = re.compile(r"\b(?:measured|confirmed|supported|evidence for)\b", re.I)
NEGATION = re.compile(r"\b(?:not|no|never|neither|nor|cannot|nothing)\b|않|아닙|아니|없", re.I)


def _sentences(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s*", " ".join(text.split())) if x]


def _changelog_entry(name: str) -> str:
    text = (REPO / name).read_text()
    version = json.loads((REPO / "package.json").read_text())["version"]
    start = text.index(f"## [{version}]")
    return text[start:text.index("\n## [", start + 1)]


def _profile_sections() -> str:
    text = (SKILL / "references" / "model-profiles.md").read_text()
    a = text.index("### The Claude architect seat")
    b = text.index("## The architect seat's replays on 2026-11-10")
    return text[a:text.index("\n## ", a)] + text[b:]


def test_the_tier_is_recorded_as_an_assignment_never_as_measured():
    """The method cannot decide a row at the top tier: there is no tier above
    it for the gate, and no other top-tier row has a score. Both replay rows
    must say so, carry the advisory reading under that name, and name the
    disclosures the scores depend on (the predecessor as a judge and as the
    author of most of the answer key; the subject as the orchestrator)."""
    top = max(m["capability_tier"] for m in CFG["models"].values())
    assert ROW["capability_tier"] == top
    peers = [k for k, m in CFG["models"].items()
             if m["capability_tier"] == top and "history_of" not in m
             and m.get("dispatchable", True) is not False and k != KEY]
    # "no scored tier-3 anchor": every other live top-tier row is named in the review row and
    # has no replay or tier-measurement row of its own (review i1 sonnet F6: not a fixed key)
    assert peers
    review = _text(REVIEW_ROW)
    for peer in peers:
        assert CFG["models"][peer]["id"] in review, peer
        assert not [i for i in LEDGER if i.startswith(f"{peer} ")
                    and ("replay" in i or "tier measurement" in i)], peer
    for item in (REVIEW_ROW, IMPL_ROW):
        text = _text(item)
        assert LEDGER[item]["status"] == "verified"
        assert "verdict is withheld by construction" in text, item
        assert "Advisory reading" in text and "policy assignment" in text, item
        assert "orchestrat" in text, item
    assert OLD in review and "judge" in review and "19 of the 29" in review


def test_nothing_published_claims_the_tier_is_measured_or_supported():
    """Plan A1.9: the scores must not be said to measure, confirm or support
    tier 3 (review i1: a sentence may use such a word about tier 3 only inside a
    negation). Checked in every place the figures are published."""
    texts = {REVIEW_ROW: _text(REVIEW_ROW), IMPL_ROW: _text(IMPL_ROW),
             "quality row": _text(f"{KEY} quality evidence after the {NEW} id bump"),
             "profile": _profile_sections(), "CHANGELOG.md": _changelog_entry("CHANGELOG.md"),
             "CHANGELOG.ko.md": _changelog_entry("CHANGELOG.ko.md")}
    for where, text in texts.items():
        for sentence in _sentences(text):
            if re.search(r"tier[- ]3", sentence, re.I) and CLAIM.search(sentence) \
                    or re.search(r"tier 3", sentence) and re.search(r"측정|확인|뒷받침", sentence):
                assert NEGATION.search(sentence), (where, sentence)


def test_the_published_figures_agree_everywhere():
    """One measurement, five places (review i1, missing tests): the two ledger
    rows, the model profile and both changelogs carry the same primary and
    as-graded figures, and the changelogs carry the same numbers and the same
    disclosures in both languages."""
    def pct(x: str) -> str:
        return f"{round(float(x) * 100)}%"
    review, impl = _text(REVIEW_ROW), _text(IMPL_ROW)
    r_primary = re.search(rf"Recall: {re.escape(NEW)} (0\.\d+)", review).group(1)
    r_graded = re.search(r"As graded, before the memory correction below, (0\.\d+)", review).group(1)
    p_primary = re.search(rf"Pass rate: {re.escape(NEW)} (0\.\d+)", impl).group(1)
    p_graded = re.search(r"As graded, before the memory correction below, (0\.\d+)", impl).group(1)
    figures = [pct(r_primary), pct(r_graded), pct(p_primary), pct(p_graded)]
    assert figures == ["56%", "59%", "69%", "75%"], figures
    # the profile states them twice (the seat's section and the replay table), each time as
    # "primary (as graded)" beside the bar the reading was taken against
    bars = [f"{float(re.search(r'bar (0\.\d+)', x).group(1)) * 100:.1f}%" for x in (review, impl)]
    profile = " ".join(_profile_sections().split())
    seat, table = profile.split("## The architect seat's replays on 2026-11-10")
    assert f"{figures[0]} review recall against a {bars[0]} bar ({figures[1]} as graded)" in seat
    assert f"{figures[2]} of implementation runs against {bars[1]} ({figures[3]} as graded)" in seat
    rows = {r.split("|")[1].strip(): [c.strip() for c in r.split("|")[2:-1]]
            for r in _profile_sections().splitlines() if r.startswith("| ") and "as graded" in r}
    assert rows["Review recall"][0] == f"{figures[0]} ({figures[1]} as graded)"
    assert rows["Implementation pass rate"][0] == f"{figures[2]} ({figures[3]} as graded)"
    assert [rows["Review recall"][3], rows["Implementation pass rate"][3]] == bars
    assert table.count(figures[1]) == 1 and table.count(figures[3]) == 1
    en, ko = _changelog_entry("CHANGELOG.md"), _changelog_entry("CHANGELOG.ko.md")
    for f in figures[:3]:                         # the changelogs give the fourth as 12 of 16
        assert f in en and f in ko, f
    assert "12 of 16" in en and "16회 중 12회" in ko and "11 of 16" in en and "16회 중 11회" in ko
    # every percentage, price and route count, as a multiset: one language spells some counts
    # out ("sixteen"), so plain integers are compared through the pairs above instead
    numbers = lambda t: sorted(re.findall(r"\$\d+(?:\.\d+)?|\d+%|\d{1,3}(?:,\d{3})+", t))  # noqa: E731
    assert numbers(en) == numbers(ko), (numbers(en), numbers(ko))
    for a, b in (("shared memory", "공유 메모리"), ("answer key", "정답지"), ("judges", "심판"),
                 ("model being measured", "측정 대상 모델"), ("never audited", "감사된 적이 없")):
        assert a in en and b in ko, (a, b)
