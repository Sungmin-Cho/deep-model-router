"""The OpenAI reasoning and frontier seats' generation bump (design 2026-11-18,
the next Sol and Astra generations).

Both ids went through the automatic path: codex-cli 0.169.1's bundled catalog
carries them, model_sync's P0-P4 gates passed, the overlay published them and
`model_sync.py promote` wrote them into the registry. These tests pin what
promote does not write and what the bump must leave behind: the documented
long-context rates, the per-model effort exception as re-probed on the new
generation, the old ids as history input that no route seats, and ledger rows
that say what was probed and what was only read.

Run:  python3 -m pytest skills/model-router/tests/test_openai_sol_astra_bump.py -q
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
FIXTURE = json.loads((SKILL / "tests" / "fixtures" / "id-succession.json").read_text())
CHAINS = FIXTURE["chains"]
KEYS = ("openai_reasoning", "openai_frontier")
LEDGER = {e["item"]: e for e in CFG["verification_ledger"]["entries"]}
# (input, output, cached_input, cache_write) per MTok as the model pages state them
RATES = {"openai_reasoning": (2.0, 10.0, 0.1, 2.5), "openai_frontier": (8.0, 40.0, 0.4, 10.0)}
OLD_OUTPUT = {"openai_reasoning": 10.0, "openai_frontier": 50.0}
TIER = {"openai_reasoning": 2, "openai_frontier": 3}
CLI = "0.169.1"


def _ids(key: str) -> tuple[str, str]:
    return CHAINS[key][-1], CHAINS[key][-2]


def _text(item: str) -> str:
    return " ".join(LEDGER[item]["evidence"].split())


@pytest.mark.parametrize("key", KEYS)
def test_the_row_is_the_probed_generation_with_its_effort_exception(key):
    """Five of the six tokens the openai family map emits were accepted and
    `none` was rejected on the new generation too, so the row keeps the
    per-model map that lifts MINIMAL to `low`; no ceiling. The tier is the one
    the row had: a generation bump moves no tier by itself."""
    new, _old = _ids(key)
    row = CFG["models"][key]
    assert row["id"] == new and row["capability_tier"] == TIER[key]
    assert row["effort_map"] == {"MINIMAL": "low"} and "effort_ceiling" not in row
    assert CFG["effort_map"]["openai"]["MINIMAL"] == "none"          # what the exception is for
    probed = _text(f"{new} reasoning-effort values")
    assert LEDGER[f"{new} reasoning-effort values"]["status"] == "verified"
    for token in ("low", "medium", "high", "xhigh", "max"):
        assert re.search(rf"\b{token}\b", probed), token
    assert re.search(r"none was rejected", probed) and "unsupported_value" in probed


@pytest.mark.parametrize("key", KEYS)
def test_the_per_model_exception_reaches_the_emitted_token(key):
    """Wherever the model is seated, MINIMAL never leaves the router as the
    token this generation rejects."""
    row = CFG["models"][key]
    merged = {**CFG["effort_map"]["openai"], **row["effort_map"]}
    assert "none" not in merged.values()
    assert merged["MINIMAL"] == "low" and merged["MAX"] == CFG["effort_map"]["openai"]["MAX"]


@pytest.mark.parametrize("key", KEYS)
def test_the_prices_are_the_documented_rates_with_the_long_context_tier(key):
    """promote writes the four short-context axes it is given; the long-context
    tier is added by hand: the model page states the rule (input and cache
    rates double, output 1.5x, above 272K input tokens, whole request) and the
    pricing page prints the same figures; the ledger says which is which."""
    new, _old = _ids(key)
    price = CFG["models"][key]["price_per_mtok"]
    rates = (price["input"], price["output"], price["cached_input"], price["cache_write"])
    assert rates == RATES[key]
    assert price["verified_on"] == "2026-11-18" and new in price["source"]
    lc = price["long_context"]
    assert (lc["above_input_tokens"], lc["boundary"]) == (272000, "exclusive")
    assert (lc["input"], lc["cached_input"], lc["cache_write"]) == \
        (2 * rates[0], 2 * rates[2], 2 * rates[3])
    assert lc["output"] == 1.5 * rates[1]
    doc = LEDGER[f"{new} pricing and context window"]
    assert doc["status"] == "documented" and doc["source"] == price["source"]
    text = _text(f"{new} pricing and context window")
    assert "272K" in text and "pricing page prints" in text and "by hand" in text


@pytest.mark.parametrize("key", KEYS)
def test_the_old_id_is_a_history_row_with_its_own_prices(key):
    _new, old = _ids(key)
    hist = CFG["models"][f"{key}@{old}"]
    assert hist["id"] == old and hist["history_of"] == key
    assert hist["dispatchable"] is False and "lineage" not in hist
    assert hist["capability_tier"] == TIER[key]
    assert hist["price_per_mtok"]["output"] == OLD_OUTPUT[key] and old in hist["price_per_mtok"]["source"]
    assert FIXTURE["prior_failure_exit"][f"{key}@{old}"] in (0, 1)


def _emitted(r):
    rv = r.get("review") or {}
    return {r.get("selected_model"), rv.get("judge_model"), *(rv.get("reviewer_models") or ())}


@pytest.mark.parametrize("key", KEYS)
def test_the_old_id_is_never_seated(key):
    """Wherever the seat is reached — including with the new id itself out —
    the old id never comes back, as a worker, a reviewer or a judge."""
    new, old = _ids(key)
    fable = CFG["models"]["claude_architect"]["id"]
    opus = CFG["models"]["claude_senior"]["id"]
    scenarios = ({}, {"unavailable_models": [fable]}, {"unavailable_models": [fable, opus]},
                 {"unavailable_models": [new]}, {"_family_quota": {"claude": "exhausted"}},
                 {"_local_policy": {"allowed_families": ["openai"]}})
    seen_new = False
    for runtime in sorted(CFG["runtimes"]):
        for cls in CFG["worker_selection"]:
            for dims in ((1, 1, 1, 0), (2, 2, 2, 1), (3, 3, 3, 3)):
                for flags in ([], ["review_disagreement"]):
                    for extra in scenarios:
                        out = route(Task(task_class=cls, complexity=dims[0], uncertainty=dims[1],
                                         blast_radius=dims[2], reversibility=dims[3], runtime=runtime,
                                         flags=list(flags), **copy.deepcopy(extra)), CFG)
                        assert old not in _emitted(out), (runtime, cls, dims, flags, extra)
                        seen_new = seen_new or new in _emitted(out)
    assert seen_new        # the sweep does reach the seat it is about


@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_a_failure_of_the_old_reasoning_id_still_routes_a_retry(runtime):
    """A control loop that crosses the release holding a failure of the old
    Sol id keeps working: the history id keeps its tier, so the retry climbs
    above it."""
    new, old = _ids("openai_reasoning")
    out = route(Task(task_class="IMPLEMENTATION", complexity=3, uncertainty=2, blast_radius=2,
                     reversibility=1, runtime=runtime, prior_failures=1, prior_models=[old]), CFG)
    assert out["terminal"] is None and out["selected_model"] not in (old, new)
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    assert tier[out["selected_model"]] > CFG["models"][f"openai_reasoning@{old}"]["capability_tier"]


def test_a_failure_of_the_old_frontier_id_ends_where_a_failure_of_the_new_one_does():
    """Nothing outranks the top tier, so a failed frontier attempt has no
    stronger retry: the history id reaches the same terminal the live id does."""
    new, old = _ids("openai_frontier")
    kw = dict(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=3, reversibility=3,
              runtime="codex", prior_failures=1)
    was = route(Task(**kw, prior_models=[old]), CFG)
    now = route(Task(**kw, prior_models=[new]), CFG)
    assert was["terminal"] == now["terminal"]
    assert was.get("selected_model") == now.get("selected_model")


@pytest.mark.parametrize("key", KEYS)
def test_the_model_id_row_is_the_automatic_paths_probe(key):
    """Unlike the balanced seat's hand-written bump, these ids passed P0 on the
    installed CLI's own bundled catalog; the row is promote's, under the CLI
    version that probed it."""
    new, old = _ids(key)
    row = LEDGER[f"{new} model id"]
    assert row["status"] == "verified" and row["cli_version"] == CLI and row["probed_on"] == "2026-11-18"
    text = _text(f"{new} model id")
    assert f"{old} -> {new}" in text and "P1" in text and "P3" in text
    # P0 passed on the bundled catalog of the CLI that probed it: the capture of that version
    # lists the id, and the row says which catalog P0 read (review d1)
    cap = json.loads((SKILL / "tests" / "fixtures" / "catalogs" / "codex" /
                      f"debug-models-slugs-{CLI}.json").read_text())
    assert cap["cli"].split()[-1] == CLI and new in cap["bundled_slugs"] and new in cap["account_slugs"]
    assert "P0" in text and "bundled catalog" in text


@pytest.mark.parametrize("key", KEYS)
def test_the_maker_seat_row_exists_for_the_new_id(key):
    """promote's shape always writes one: verified by an attended probe, or
    `maker_not_reprobed` saying so."""
    new, _old = _ids(key)
    row = LEDGER[f"{new} maker seat"]
    assert row["status"] in ("verified", "maker_not_reprobed")
    text = _text(f"{new} maker seat")
    if row["status"] == "maker_not_reprobed":
        assert "No attended probe-maker summary" in text
    else:
        # the confirmation the probe asks for was typed by a script, with the user's permission
        assert "probe-maker" in text and row["probed_on"] == "2026-11-18"
        assert "answered by an expect script" in text and "not typed by a person" in text


@pytest.mark.parametrize("key", KEYS)
def test_the_quality_disclosure_supersedes_what_promote_would_name(key):
    """The disclosure points at the rows whose item names the superseded id,
    as promote computes them from the ledger before the bump."""
    new, old = _ids(key)
    item = f"{key} quality evidence after the {new} id bump"
    others = [e for e in CFG["verification_ledger"]["entries"]
              if not re.search(r"(?<![A-Za-z0-9._-])" + re.escape(new) + r"(?![A-Za-z0-9_-])", e["item"])
              and "(2026-11-18)" not in e["item"]]
    want = model_sync._superseded_items(key, [old], others)
    assert LEDGER[item]["supersedes"] == want


# --------------------------------------------------------------------------- the two replays

from test_claude_architect_bump import _tier_claims  # noqa: E402  (the tier-3 claim tripwire)

REPO = SKILL.parent.parent
PUBLISHED_IN = "1.26.1"
MARGIN, SEPARATION = 0.05, 0.10
ROWS = {(key, kind): f"{key} {kind} replay (2026-11-18)"
        for key in KEYS for kind in ("review", "implementation")}


def _scores(text: str) -> dict:
    """`<registry id> <score>` pairs as a row records them (six places), first mention each."""
    ids = {m["id"] for m in CFG["models"].values()}
    out = {}
    for mid, value in re.findall(r"([a-z0-9][a-z0-9.\-]*) (0\.\d{6})\b", text):
        if mid in ids:
            out.setdefault(mid, float(value))
    return out


def _id(key: str) -> str:
    return CFG["models"][key]["id"]


def _stated(text: str, label: str) -> float:
    return float(re.search(re.escape(label) + r" (0\.\d{6})", text).group(1))


def test_every_replay_row_is_verified_and_dated_after_the_rows_it_reads():
    for item in ROWS.values():
        assert LEDGER[item]["status"] == "verified" and LEDGER[item]["probed_on"] == "2026-11-18", item
        assert re.search(r"summary\.json sha256 [0-9a-f]{64}\)", _text(item)), item


def test_the_reasoning_review_row_rederives_its_bars_and_its_undecided_verdict():
    """The per-row rule for a tier-2 row: the other tier-2 rows set the lower bar, the one scored
    tier-3 row the raise bar, and the suite must separate them by 0.10. The primary value holds;
    the as-graded one alone clears the raise bar, which the plan leaves undecided."""
    new, old = _ids("openai_reasoning")
    text = _text(ROWS[("openai_reasoning", "review")])
    s = _scores(text)
    t3 = s[_id("claude_architect")]
    others = [s[_id("claude_senior")], s[_id("claude_worker_balanced")]]
    a2 = min(others)
    assert abs(_stated(text, "separation") - (t3 - a2)) < 2e-6 and t3 - a2 >= SEPARATION
    assert abs(_stated(text, "Bar for raise:") - (t3 - MARGIN)) < 2e-6
    assert abs(_stated(text, "Bar for lower:") - (a2 - MARGIN)) < 2e-6
    r = s[new]
    assert a2 - MARGIN <= r < t3 - MARGIN                       # primary: hold
    as_graded = float(re.search(r"As graded, before the memory correction below, (0\.\d{6})", text).group(1))
    assert as_graded >= t3 - MARGIN > r                          # as graded alone: over the raise bar
    t3_as_graded = float(re.search(r"against that row as graded \((0\.\d{6}), bar (0\.\d{6})\)", text).group(1))
    assert as_graded < t3_as_graded - MARGIN                     # like for like it holds too
    assert re.search(rf"Review verdict {re.escape(new)}: undecided \(straddle\)", text)
    was = float(re.search(rf"previous id {re.escape(old)} scored (0\.\d{{6}})", text).group(1))
    assert was < a2 - MARGIN and "set the bar" in text           # the predecessor, beside, never an anchor
    assert "policy assignment" in text


def test_the_reasoning_implementation_row_holds_by_the_gate_it_states():
    """On the implementation suite the one scored tier-3 row and the best other tier-2 row are
    level: the separation is under 0.10, so no score could raise or lower the row, and the row
    says the verdict was hold before any dispatch."""
    new, old = _ids("openai_reasoning")
    text = _text(ROWS[("openai_reasoning", "implementation")])
    s = _scores(text)
    t3 = s[_id("claude_architect")]
    a2 = min(s[_id("claude_senior")], s[_id("claude_worker_balanced")])
    assert abs(_stated(text, "separation") - (t3 - a2)) < 2e-6 and t3 - a2 < SEPARATION
    assert "not valid" in text and "inconclusive" in text and "before any dispatch" in text
    assert re.search(rf"Implementation verdict {re.escape(new)}: hold\.", text)
    was = float(re.search(rf"previous id {re.escape(old)} scored (0\.\d{{6}})", text).group(1))
    assert s[new] == was == 0.625                                 # ten of sixteen, as the predecessor
    # the fixed sensitivity: the tier-3 row as graded opens the gate, and both values still hold
    ag = float(re.search(r"tier-3 row as graded \((0\.\d{6}), separation (0\.\d{6})\)", text).group(1))
    assert ag - a2 >= SEPARATION
    as_graded = float(re.search(r"As graded, before the memory correction below, (0\.\d{6})", text).group(1))
    assert a2 - MARGIN <= s[new] < ag - MARGIN and a2 - MARGIN <= as_graded < ag - MARGIN


@pytest.mark.parametrize("kind", ["review", "implementation"])
def test_the_frontier_rows_rederive_the_advisory_and_claim_no_tier(kind):
    """A row at the top tier has no tier above for the gate: the verdict is withheld by
    construction and the advisory asks only whether the model is below the weakest tier-2 row."""
    new, old = _ids("openai_frontier")
    text = _text(ROWS[("openai_frontier", kind)])
    assert "verdict is withheld by construction" in text and "policy assignment" in text
    assert "Advisory reading" in text and "no evidence for" in text
    s = _scores(text)
    tier2 = [s[_id(k)] for k in ("claude_senior", "claude_worker_balanced")] + \
        [s[CHAINS["openai_reasoning"][-2]]]                      # the Sol id the 1.26.0 registry held
    tier1 = [s[_id("openai_worker_balanced")], s[_id("gemini_hydrogen")]]
    gate = sum(tier2) / len(tier2) - min(tier1)
    assert abs(_stated(text, "mean") - sum(tier2) / len(tier2)) < 2e-6
    assert abs(float(re.search(r"gate:? (0\.\d{6}), met", text).group(1)) - gate) < 2e-6
    assert gate >= SEPARATION
    bar = min(tier2) - MARGIN
    assert abs(_stated(text, "bar") - bar) < 2e-6
    assert s[new] >= bar and re.search(rf"{re.escape(new)} {s[new]:.6f} \([^)]*\): not below tier 2", text)
    assert old not in s                                           # the predecessor was never a subject
    assert not _tier_claims(text), _tier_claims(text)


def test_neither_tier_moved_and_both_quality_rows_say_what_was_scored():
    assert CFG["models"]["openai_reasoning"]["capability_tier"] == 2
    assert CFG["models"]["openai_frontier"]["capability_tier"] == 3
    for key in KEYS:
        new, _old = _ids(key)
        text = _text(f"{key} quality evidence after the {new} id bump")
        for kind in ("review", "implementation"):
            assert ROWS[(key, kind)] in text, (key, kind)
    frontier = _text(f"openai_frontier quality evidence after the {_ids('openai_frontier')[0]} id bump")
    assert "not a measured tier" in frontier and not _tier_claims(frontier)


def _changelog_entry(name: str) -> str:
    text = (REPO / name).read_text()
    start = text.index(f"## [{PUBLISHED_IN}]")
    return text[start:text.index("\n## [", start + 1)]


def _profile_sections() -> str:
    text = (SKILL / "references" / "model-profiles.md").read_text()
    out = []
    for head in ("### The OpenAI frontier seat", "### The OpenAI reasoning seat"):
        a = text.index(head)
        out.append(text[a:text.index("\n### ", a + 1)])
    b = text.index("## The OpenAI reasoning and frontier seats' replays on 2026-11-18")
    end = text.find("\n## ", b + 1)
    out.append(text[b:end if end != -1 else len(text)])
    return "\n".join(out)


def test_nothing_published_claims_the_frontier_tier_is_measured_or_supported():
    """The frontier seat's scores must not be said to measure, confirm or support tier 3, in
    any place the figures are published."""
    for where, text in {"profile": _profile_sections(), "CHANGELOG.md": _changelog_entry("CHANGELOG.md"),
                        "CHANGELOG.ko.md": _changelog_entry("CHANGELOG.ko.md")}.items():
        assert not _tier_claims(text), (where, _tier_claims(text))


def test_the_published_percentages_are_the_ledgers_scores():
    """The profile and both changelogs print whole percentages. Each printed figure is read from
    the sentence or table cell that says whose it is, on which replay and in which reading, and
    compared with the ledger's six-place score rounded half up (review i1: a bag of percentages
    lets two figures swap places)."""
    sol, astra = _ids("openai_reasoning")[0], _ids("openai_frontier")[0]
    rs, ra = _text(ROWS[("openai_reasoning", "review")]), _text(ROWS[("openai_frontier", "review")])
    is_, ia = (_text(ROWS[("openai_reasoning", "implementation")]),
               _text(ROWS[("openai_frontier", "implementation")]))
    as_graded = lambda t: float(re.search(r"As graded, before the memory correction below, (0\.\d{6})", t).group(1))  # noqa: E731
    pct = lambda v: int(100 * v + 0.5)                              # noqa: E731  (half up)
    want = {"sol review": pct(_scores(rs)[sol]), "sol review as graded": pct(as_graded(rs)),
            "astra review": pct(_scores(ra)[astra]), "astra review as graded": pct(as_graded(ra)),
            "sol fixes": pct(_scores(is_)[sol]), "sol fixes as graded": pct(as_graded(is_)),
            "astra fixes": pct(_scores(ia)[astra])}
    assert want == {"sol review": 48, "sol review as graded": 52, "astra review": 56,
                    "astra review as graded": 59, "sol fixes": 63, "sol fixes as graded": 69,
                    "astra fixes": 63}

    def read(text, patterns):
        out = {}
        for rx, names in patterns:
            m = re.search(rx, text)
            assert m, rx
            out.update(zip(names, (int(g) for g in m.groups())))
        return out

    en = read(_changelog_entry("CHANGELOG.md"), [
        (r"GPT-6\.2 Sol recalls (\d+)% \(GPT-6\.1 Sol", ["sol review"]),
        (r"and GPT-6\.2 Astra (\d+)% \(Claude Fable", ["astra review"]),
        (r"both pass (\d+)% of their runs", ["sol fixes"]),
        (r"as graded they are (\d+)% and (\d+)% on reviews and (\d+)% for GPT-6\.2 Sol on fixes",
         ["sol review as graded", "astra review as graded", "sol fixes as graded"])])
    ko = read(_changelog_entry("CHANGELOG.ko.md"), [
        (r"GPT-6\.2 Sol의 recall은 (\d+)%", ["sol review"]),
        (r"GPT-6\.2 Astra는 (\d+)%\(Claude Fable", ["astra review"]),
        (r"두 모델 모두 실행의 (\d+)%를 통과", ["sol fixes"]),
        (r"채점 그대로는 리뷰 (\d+)%·(\d+)%, 수정은 GPT-6\.2 Sol (\d+)%",
         ["sol review as graded", "astra review as graded", "sol fixes as graded"])])
    profile = read(_profile_sections(), [
        (r"\| Review recall \| (\d+)% \((\d+)% as graded\) \| (\d+)% \((\d+)% as graded\) \|",
         ["sol review", "sol review as graded", "astra review", "astra review as graded"]),
        (r"\| Implementation pass rate \| (\d+)% \((\d+)% as graded\) \| (\d+)% \|",
         ["sol fixes", "sol fixes as graded", "astra fixes"])])
    for where, got in (("CHANGELOG.md", en), ("CHANGELOG.ko.md", ko), ("profile", profile)):
        for name, value in got.items():
            assert value == want[name], (where, name, value, want[name])
    # both changelogs say "both" for the fix replay: the frontier model's figure is the same one
    assert want["astra fixes"] == want["sol fixes"]
    # the seat sections' prose prints the same figures (review i2: the table cells alone would
    # let a wrong figure in the prose through): each seat's own figures appear in its section
    text = (SKILL / "references" / "model-profiles.md").read_text()

    def section(head):
        a = text.index(head)
        return {int(v) for v in re.findall(r"(?<![\d.])(\d{2})%", text[a:text.index("\n### ", a + 1)])}
    assert {want["sol review"], want["sol review as graded"], want["sol fixes"],
            want["sol fixes as graded"]} <= section("### The OpenAI reasoning seat")
    assert {want["astra review"], want["astra review as graded"],
            want["astra fixes"]} <= section("### The OpenAI frontier seat")
