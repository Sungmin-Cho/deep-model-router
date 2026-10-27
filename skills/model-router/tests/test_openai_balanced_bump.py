"""The OpenAI balanced seat's generation bump (design 2026-10-27, the next
Terra generation).

The overlay never published this id: model_sync's P0 defers an OpenAI id the
installed CLI's bundled catalog lacks, and codex-cli 0.164.0 lists it in the
account catalog only. The bump was written into the registry by hand in
promote's shape, after the same P1-P4 gates passed. These tests pin what that
leaves behind: the new row as probed, the old id as history input that no
route seats, the deferral it replaces dropped, and the account-catalog
disclosure on the ledger (review d1 fable, missing tests).

Run:  python3 -m pytest skills/model-router/tests/test_openai_balanced_bump.py -q
"""

import copy
import datetime as dt
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
KEY = "openai_worker_balanced"
CHAIN = json.loads((SKILL / "tests" / "fixtures" / "id-succession.json").read_text())["chains"][KEY]
NEW, OLD = CHAIN[-1], CHAIN[-2]
ROW = CFG["models"][KEY]
HISTORY = CFG["models"][f"{KEY}@{OLD}"]
LEDGER = {e["item"]: e for e in CFG["verification_ledger"]["entries"]}
SLUGS = json.loads((SKILL / "tests" / "fixtures" / "catalogs" / "codex" /
                    "debug-models-slugs-0.164.0.json").read_text())


def _text(item: str) -> str:
    return " ".join(LEDGER[item]["evidence"].split())


def test_the_row_is_the_probed_generation_with_no_effort_exception():
    """Every token the openai family map emits was accepted (none included),
    so the row carries no effort_map or ceiling; context_window stays unset as
    on its predecessor (the 1.12.1 snapshot pins that leaf)."""
    assert ROW["id"] == NEW and ROW["lineage"]["line"] == "openai/terra"
    for leaf in ("effort_map", "effort_ceiling", "context_window"):
        assert leaf not in ROW, leaf
    probed = _text(f"{NEW} reasoning-effort values")
    for token in CFG["effort_map"]["openai"].values():
        assert re.search(rf"\b{token}\b", probed), token


def test_the_prices_are_the_documented_rates_with_the_long_context_tier():
    price = ROW["price_per_mtok"]
    assert (price["input"], price["output"], price["cached_input"], price["cache_write"]) == \
        (1.0, 5.0, 0.05, 1.25)
    lc = price["long_context"]
    assert (lc["above_input_tokens"], lc["boundary"]) == (272000, "exclusive")
    assert (lc["input"], lc["output"], lc["cached_input"], lc["cache_write"]) == \
        (2.0, 7.5, 0.1, 2.5)
    doc = LEDGER[f"{NEW} pricing and context window"]
    assert doc["status"] == "documented" and doc["source"] == price["source"]
    assert NEW in price["source"]


def test_the_old_id_is_a_history_row_with_its_own_prices():
    assert HISTORY["id"] == OLD and HISTORY["history_of"] == KEY
    assert HISTORY["dispatchable"] is False and "lineage" not in HISTORY
    assert HISTORY["price_per_mtok"]["output"] == 12.0 and OLD in HISTORY["price_per_mtok"]["source"]


def _emitted(r):
    rv = r.get("review") or {}
    return {r.get("selected_model"), rv.get("judge_model"), *(rv.get("reviewer_models") or ())}


def test_the_old_id_is_never_seated():
    """Wherever the balanced OpenAI seat is reached — including with the new id
    itself out — the old id never comes back."""
    xai = CFG["models"]["xai_frontier"]["id"]
    son = CFG["models"]["claude_worker_balanced"]["id"]
    scenarios = ({}, {"unavailable_models": [xai]}, {"unavailable_models": [xai, son]},
                 {"unavailable_models": [xai, NEW]}, {"_family_quota": {"claude": "exhausted"}},
                 {"_local_policy": {"allowed_families": ["openai"]}})
    for runtime in sorted(CFG["runtimes"]):
        for cls in CFG["worker_selection"]:
            for dims in ((1, 1, 1, 0), (2, 2, 2, 1), (3, 3, 3, 3)):
                for extra in scenarios:
                    out = route(Task(task_class=cls, complexity=dims[0], uncertainty=dims[1],
                                     blast_radius=dims[2], reversibility=dims[3],
                                     runtime=runtime, **copy.deepcopy(extra)), CFG)
                    assert OLD not in _emitted(out), (runtime, cls, dims, extra)


@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_a_failure_of_the_old_id_still_routes_a_retry(runtime):
    out = route(Task(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime=runtime, prior_failures=1, prior_models=[OLD]), CFG)
    assert out["terminal"] is None and out["selected_model"] not in (OLD,)


def test_the_cli_metadata_deferral_is_dropped_once_the_id_is_live(tmp_path):
    """The deferral model_sync kept for this id (P0 cli_metadata, retry on a
    CLI version change) has nothing left to retry after the bump."""
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    now = dt.datetime(2026, 10, 27, 12, tzinfo=dt.timezone.utc)
    model_sync.update_work_state(root, lambda st: st["negatives"].__setitem__(KEY, {
        "id": NEW, "family": "openai", "outcome": "deferred", "reason": "cli_metadata",
        "retry_after": {"kind": "cli_version_change", "cli_version": "0.164.0"},
        "summary_sha256": None, "at": now.isoformat()}))

    class View:
        config = CFG
    model_sync._prune_obsolete_negatives(root, View)
    with model_sync.StateRoot.open(root) as r:
        assert KEY not in model_sync.read_work_state(r)["negatives"]


def test_p0_defers_this_id_on_the_bundled_catalog_and_passes_on_the_account_one():
    """What the hand-written bump stood in for: on 0.164.0's bundled catalog the
    automatic path defers; given the account catalog, P0 passes."""
    assert NEW not in SLUGS["bundled_slugs"] and NEW in SLUGS["account_slugs"]
    ctx = model_sync.ProbeContext(now=dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc),
                                  cli_version="0.164.0")
    efforts = ["low", "medium", "high", "xhigh", "max", "ultra"]
    kw = dict(family="openai", candidate_id=NEW, row=ROW, base=CFG, catalog_efforts=efforts, ctx=ctx)
    assert model_sync.gate_p0(cli_metadata_ids=SLUGS["bundled_slugs"], **kw)["reason"] == "cli_metadata"
    assert model_sync.gate_p0(cli_metadata_ids=SLUGS["account_slugs"], **kw)["outcome"] == "pass"


def test_a_live_openai_id_missing_from_the_bundled_catalog_discloses_it():
    """The P0 deviation cannot be dropped from the ledger silently: every live
    OpenAI id the captured bundled catalog lacks names both catalogs in its
    model id row."""
    live = [m["id"] for m in CFG["models"].values()
            if m.get("family") == "openai" and "history_of" not in m
            and m.get("dispatchable", True) is not False]
    missing = [mid for mid in live if mid not in SLUGS["bundled_slugs"]]
    assert missing == [NEW], missing
    for mid in missing:
        text = _text(f"{mid} model id")
        assert "bundled catalog" in text and "account catalog" in text, mid
        assert LEDGER[f"{mid} model id"]["cli_version"] == SLUGS["cli"].split()[-1]


def test_the_maker_seat_row_exists_for_the_new_id():
    """promote's shape always writes one (review d2 fable F5): verified by an
    attended probe, or `maker_not_reprobed` saying what stands in for it."""
    row = LEDGER[f"{NEW} maker seat"]
    assert row["status"] in ("verified", "maker_not_reprobed")
    if row["status"] == "maker_not_reprobed":
        text = _text(f"{NEW} maker seat")
        assert "No attended probe-maker summary" in text and "workspace-write" in text


def test_the_quality_disclosure_supersedes_what_promote_would_name():
    """promote's shape (review d3 fable F3): the disclosure points at the rows
    whose item names the superseded id, not at shared rows that stay current
    for other seats (those are cited in its evidence instead)."""
    item = f"{KEY} quality evidence after the {NEW} id bump"
    others = [e for e in CFG["verification_ledger"]["entries"] if e["item"] != item]
    want = model_sync._superseded_items(KEY, [OLD], others)
    assert LEDGER[item]["supersedes"] == want
    text = _text(item)
    for shared in ("Reviewer seat tiers against live anchors (real-review replay, 2026-10-14)",
                   "Implementation replay of the writing rows (2026-10-15)"):
        assert shared in LEDGER and " ".join(shared.split()) in text, shared
