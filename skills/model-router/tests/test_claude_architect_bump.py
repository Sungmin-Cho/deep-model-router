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
# What a claim for tier 3 looks like, by stem (the scores "confirm" it, it "is supported", they
# are "evidence for" it), and what denies one. English puts the denial before the verb, Korean
# after it; either way it has to sit in the claim's own clause (review i2: a denial anywhere in
# the sentence, and participles only, let "these scores confirm tier 3" through).
ABOUT_TIER = re.compile(r"tier[- ]?(?:3(?!\d)|three\b)|top[- ]tier", re.I)
CLAIM_EN = re.compile(r"\b(?:measur\w*|confirm\w*|support\w*|validat\w*|verif\w*|establish\w*"
                      r"|prov(?:e[sdn]?|ing)|show(?:s|ed|n|ing)?|demonstrat\w*|justif\w*"
                      r"|corroborat\w*|ratif\w*|back(?:s|ed|ing)?(?! to\b)|b(?:ear|ears|orne) out"
                      r"|evidence (?:for|of))\b", re.I)
DENIAL_EN = re.compile(r"\b(?:not|no|never|neither|nor|cannot|nothing)\b|n't\b", re.I)
CLAUSE_EN = re.compile(r"[;:()]|\s[-–—]\s|,\s+and\b|\b(?:but|although|though|while|whereas|yet)\b", re.I)
CLAIM_KO = re.compile(r"측정|확인|뒷받침|입증|검증|증명")
DENIAL_KO = re.compile(r"않|아닙|아니|없|못")
CLAUSE_KO = re.compile(r"[;:()]|지만|으나|는데|(?<=[고며])[,\s]")


def _blocks(text: str):
    """Paragraphs, with a heading or a table row alone and a list item starting its own: a
    heading or a table cell reading "not below tier 2" must not deny a claim made after it."""
    block: list[str] = []
    for line in text.splitlines():
        alone = line.lstrip().startswith(("#", "|"))
        if alone or not line.strip() or re.match(r"\s*[-*] ", line):
            if block:
                yield " ".join(block)
            block = []
            if alone:
                yield line
                continue
        if line.strip():
            block.append(line)
    if block:
        yield " ".join(block)


def _sentences(text: str) -> list[str]:
    out = []
    for block in _blocks(text):
        plain = re.sub(r"[*`]+", "", " ".join(block.split()))    # a Markdown mark hides a full stop
        out += [x for x in re.split(r"(?<=[.!?])[\"')\]]*\s+", plain) if x]
    return out


def _tier_claims(text: str) -> list[str]:
    """The clauses that use a claim word, in a sentence naming tier 3, without a denial in the
    same clause. A tripwire for the common forms, not a proof: reviews i2 and i3 each found
    forms an earlier version let through, and a pattern cannot close that. What it misses:
    a claim word outside the list; a claim whose subject is in the sentence before; a claim
    after a denial in the same clause ("is not merely assigned, it is confirmed" - a clause
    ends at ; : ( ) a dash, ", and" or a contrast word, not at a bare comma, which would cut
    "do not measure, confirm or support it"). It also errs the other way: every claim word in
    such a sentence needs a denial, whatever it is about, because a claim made through "it"
    would otherwise pass. The reviewers of an edit remain the check."""
    found = []
    for sentence in _sentences(text):
        if not ABOUT_TIER.search(sentence):
            continue
        for clause in CLAUSE_EN.split(sentence):
            found += [clause.strip() for m in CLAIM_EN.finditer(clause)
                      if not DENIAL_EN.search(clause[:m.start()])]
        for clause in CLAUSE_KO.split(sentence):
            found += [clause.strip() for m in CLAIM_KO.finditer(clause)
                      if not DENIAL_KO.search(clause[m.end():])]
    return found


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


def _published() -> dict[str, str]:
    """Every place the replay figures are published."""
    return {REVIEW_ROW: _text(REVIEW_ROW), IMPL_ROW: _text(IMPL_ROW),
            "quality row": _text(f"{KEY} quality evidence after the {NEW} id bump"),
            "profile": _profile_sections(), "CHANGELOG.md": _changelog_entry("CHANGELOG.md"),
            "CHANGELOG.ko.md": _changelog_entry("CHANGELOG.ko.md")}


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
        peer_id = CFG["models"][peer]["id"]
        assert peer_id in review, peer
        assert not [i for i in LEDGER if i.startswith((f"{peer} ", f"{peer_id} "))
                    and ("replay" in i or "tier measurement" in i)], peer
    for item in (REVIEW_ROW, IMPL_ROW):
        text = _text(item)
        assert LEDGER[item]["status"] == "verified"
        assert "verdict is withheld by construction" in text, item
        assert "Advisory reading" in text and "policy assignment" in text, item
        assert "orchestrat" in text, item
    assert OLD in review and "judge" in review and "19 of the 29" in review


@pytest.mark.parametrize("sentence", [
    "These scores confirm tier 3.",
    "The replays support the tier-3 assignment.",
    "The readings validate tier 3.",
    "Tier 3 is supported, but the predecessor was never measured.",
    "Tier 3 is measured, not assumed.",
    "**Tier 3 is confirmed.** No other route changes.",
    "Nothing else moves. The two replays are evidence for tier 3.",
    "| Review recall | 56% | not below tier 2 |\n\nBoth rows support tier 3.",
    "These scores are evidence of tier 3.",
    "The scores show tier 3 holds.",
    "Tier 3 is justified by these replays.",
    "These replays back tier 3.",
    "Tier three is confirmed.",
    "The top tier is borne out by both replays.",
    "These scores do not change anything, and they confirm tier 3.",
    "Without hesitation, the replays confirm tier 3.",
    "tier 3이 확인되었고 전임 모델은 측정되지 않았습니다.",
    "## Not a verdict\nThe replays establish tier 3.",
    "이 결과로 tier 3이 확인되었습니다.",
    "tier 3이 확인되었고, 전임 모델은 측정되지 않았습니다.",
    "두 재현이 tier 3을 뒷받침합니다. 다른 변경은 없습니다.",
])
def test_the_claim_guard_catches_an_affirmed_tier(sentence):
    """The guard's own control (review i2): each of these is a claim the plans forbid, in a
    form an earlier version of the guard let through."""
    assert _tier_claims(sentence), sentence


@pytest.mark.parametrize("sentence", [
    "Tier 3 was never measured for this seat.",
    "Tier 3 stays as assigned: these scores do not measure, confirm or support it.",
    'It is not a measured tier, and "not below tier 2" is no evidence for tier 3.',
    "Tier 3 holds by assignment.",
    "이 점수는 tier 3을 측정하지도, 확인하지도, 뒷받침하지도 않습니다.",
    "tier 3은 측정된 tier가 아닙니다.",
])
def test_the_claim_guard_passes_a_denied_one(sentence):
    assert not _tier_claims(sentence), _tier_claims(sentence)


def test_nothing_published_claims_the_tier_is_measured_or_supported():
    """Plan A1.9: the scores must not be said to measure, confirm or support
    tier 3. Checked in every place the figures are published."""
    for where, text in _published().items():
        assert not _tier_claims(text), (where, _tier_claims(text))


def test_the_timing_statements_two_reviews_found_false_stay_out():
    """What was fixed when is recorded in the plans, which are a local archive, so no test can
    check it. Reviews i1 and i2 found blanket statements of it false: every rule written before
    any dispatch; only the advisory reading fixed before dispatch; the anchors' sessions never
    audited (true of the review replay's tier-2 rows only). This keeps those blanket forms out
    (a scoped, true sentence such as "T3's anchors ... were never audited" stays allowed) and
    pins the account each text gives instead."""
    false_forms = [r"rules (?:were )?(?:written|fixed|set) before any dispatch",
                   r"only the advisory reading", r"\bthe anchors' (?:own )?sessions were never audited",
                   r"보조 질문 하나만", r"앞선 모델들의 세션은[^.]*감사된 적이 없"]
    for where, text in _published().items():
        flat = " ".join(text.split())
        for form in false_forms:
            assert not re.search(form, flat, re.I), (where, form)
    review, impl = _text(REVIEW_ROW), _text(IMPL_ROW)
    assert "The plan fixed, before the reviews were dispatched" in review
    assert "A3 after the judges had run and before any grade, review or hand-read verdict was read" in review
    assert "fixed before any attempt was dispatched" in impl and "while attempts ran" in impl
    for text in (review, impl):
        assert "two readers from different families, neither the subject model" in text
    profile = " ".join(_profile_sections().split())
    assert "before any grade was read" in profile and "before any grade or hand-read verdict was read" in profile


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
                 ("model being measured", "측정 대상 모델"), ("audited less strictly", "약하게 감사"),
                 ("seven of the eight", "8건 중 7건"), ("fixed before any run", "실행 전에 정해 둔")):
        assert a in en and b in ko, (a, b)
