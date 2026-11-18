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
