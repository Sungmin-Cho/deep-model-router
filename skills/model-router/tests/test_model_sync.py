"""model_sync (design 2026-09-25 DD-A4, DD-A5): catalog adapters, candidates,
probe gates, contained probe argv and the probe harness.

Every catalog here is a trimmed real capture under `tests/fixtures/catalogs/`
(account identity, org ids and tokens removed); every codex output is a real
capture under `tests/fixtures/codex/`. Nothing here reaches the network or a
model: the CLIs a test needs are fake executables first on PATH.

Literal vendor ids live in the JSON fixtures only (`expected-candidates.json`),
never in this file — the registry-id guard in test_docs.py scans test code.
"""
import copy
import json
import os
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

import model_sync  # noqa: E402
from route_task import load_config  # noqa: E402

CATALOGS = SKILL / "tests" / "fixtures" / "catalogs"
EXPECTED = json.loads((CATALOGS / "expected-candidates.json").read_text())
BASE = load_config()


def rows(overrides=None) -> dict:
    """Registry rows as the captures saw them: today's lineage and family,
    with the id each key held when the catalogs were captured."""
    ids = {**EXPECTED["from_ids"], **(overrides or {})}
    out = {}
    for key, mid in ids.items():
        row = copy.deepcopy(BASE["models"][key])
        row["id"] = mid
        out[key] = row
    return out


def codex_cache(tmp_path=None, mutate=None) -> Path:
    src = CATALOGS / "codex" / "models_cache.json"
    if mutate is None:
        return src
    doc = json.loads(src.read_text())
    mutate(doc)
    path = tmp_path / "models_cache.json"
    path.write_text(json.dumps(doc))
    return path


def grok_cache(tmp_path=None, mutate=None) -> Path:
    src = CATALOGS / "grok" / "models_cache.json"
    if mutate is None:
        return src
    doc = json.loads(src.read_text())
    mutate(doc)
    path = tmp_path / "grok_models_cache.json"
    path.write_text(json.dumps(doc))
    return path


def claude_dir(tmp_path=None, mutate=None) -> Path:
    src = CATALOGS / "claude"
    if mutate is None:
        return src
    doc = json.loads((src / "catalog-cc.json").read_text())
    mutate(doc)
    out = tmp_path / "model-catalog"
    out.mkdir()
    (out / "fixture-cc.json").write_text(json.dumps(doc))
    return out


GROK_TEXT = (CATALOGS / "grok" / "grok-models.txt").read_text()
CLAUDE_CLI = model_sync.parse_cli_version(
    (CATALOGS / "claude" / "version.txt").read_text())


def catalogs(tmp_path=None, *, codex=None, grok=None, claude=None,
             claude_cli=CLAUDE_CLI, grok_text=GROK_TEXT):
    return {
        "openai": model_sync.read_codex_catalog(codex or codex_cache()),
        "xai": model_sync.read_grok_catalog(grok or grok_cache(), grok_text),
        "claude": model_sync.read_claude_catalog(claude or claude_dir(), claude_cli),
    }


def _codex_entry(doc, mid):
    return next(m for m in doc["models"] if m["slug"] == mid)


def _claude_entry(doc, mid):
    return next(m for m in doc["catalog"]["config"]["models"] if m["id"] == mid)


# ---------------------------------------------------------------------------
# A8 — catalog adapters and candidates (DD-A4)
# ---------------------------------------------------------------------------

def test_candidates_match_the_design_table():
    """opus -> 5-5, sol -> 6-sol, luna -> 6-luna, nothing else (design §1.1)."""
    found = model_sync.find_candidates(rows(), catalogs())
    got = {k: c["id"] for k, c in found.items() if c["status"] == "candidate"}
    assert got == EXPECTED["candidates"]
    for key, cand in found.items():
        if key not in EXPECTED["candidates"]:
            assert cand["status"] == "none", (key, cand)
        else:
            assert cand["from_id"] == EXPECTED["from_ids"][key]
            assert cand["line"] == BASE["models"][key]["lineage"]["line"]
            assert cand["efforts"], key


def test_candidate_is_the_highest_newer_generation():
    """From an older sol the candidate is still the newest generation listed."""
    older = rows({"openai_reasoning": "gpt-5-sol"})
    found = model_sync.find_candidates(older, catalogs())
    assert found["openai_reasoning"]["id"] == EXPECTED["candidates"]["openai_reasoning"]


def test_grok_build_fast_variant_is_never_a_candidate():
    found = model_sync.find_candidates(
        rows({"xai_frontier": EXPECTED["xai_older_from_id"]}), catalogs())
    assert found["xai_frontier"]["status"] == "candidate"
    assert found["xai_frontier"]["id"] == EXPECTED["from_ids"]["xai_frontier"]
    cat = catalogs()["xai"]
    assert EXPECTED["xai_excluded_variant"] in cat.listed_ids
    assert all(c.get("id") != EXPECTED["xai_excluded_variant"]
               for c in found.values())


def test_grok_candidate_must_be_listed_by_grok_models():
    text = GROK_TEXT.replace(EXPECTED["from_ids"]["xai_frontier"] + " ", "absent ")
    found = model_sync.find_candidates(
        rows({"xai_frontier": EXPECTED["xai_older_from_id"]}),
        catalogs(grok_text=text))
    assert found["xai_frontier"]["status"] == "none"


def test_grok_models_text_parses_the_list_and_the_default():
    listed, default = model_sync.parse_grok_models_text(GROK_TEXT)
    assert default == EXPECTED["from_ids"]["xai_frontier"]
    assert EXPECTED["xai_excluded_variant"] in listed
    assert model_sync.parse_grok_models_text("nothing here") == ([], None)


@pytest.mark.parametrize("mutate", [
    lambda d: _codex_entry(d, EXPECTED["candidates"]["openai_reasoning"]).update(visibility="hide"),
    lambda d: _codex_entry(d, EXPECTED["candidates"]["openai_reasoning"]).update(supported_in_api=False),
])
def test_codex_hidden_or_api_unsupported_entries_are_excluded(tmp_path, mutate):
    cats = catalogs(codex=codex_cache(tmp_path, mutate))
    found = model_sync.find_candidates(rows(), cats)
    assert found["openai_reasoning"]["status"] == "none"
    assert found["openai_worker_fast"]["status"] == "candidate"
    assert EXPECTED["candidates"]["openai_reasoning"] in cats["openai"].excluded


def test_grok_hidden_entry_is_excluded(tmp_path):
    target = EXPECTED["from_ids"]["xai_frontier"]
    cats = catalogs(grok=grok_cache(
        tmp_path, lambda d: d["models"][target]["info"].update(hidden=True)))
    found = model_sync.find_candidates(
        rows({"xai_frontier": EXPECTED["xai_older_from_id"]}), cats)
    assert found["xai_frontier"]["status"] == "none"


def test_claude_overflow_section_is_excluded(tmp_path):
    target = EXPECTED["candidates"]["claude_senior"]
    cats = catalogs(claude=claude_dir(
        tmp_path, lambda d: _claude_entry(d, target).update(section="overflow")))
    assert model_sync.find_candidates(rows(), cats)["claude_senior"]["status"] == "none"


def test_claude_short_name_must_match_catalog_name(tmp_path):
    target = EXPECTED["candidates"]["claude_senior"]
    cats = catalogs(claude=claude_dir(
        tmp_path, lambda d: _claude_entry(d, target).update(short_name="Other")))
    assert model_sync.find_candidates(rows(), cats)["claude_senior"]["status"] == "none"


def test_claude_min_cli_above_installed_is_excluded():
    target = EXPECTED["candidates"]["claude_senior"]
    doc = json.loads((CATALOGS / "claude" / "catalog-cc.json").read_text())
    need = _claude_entry(doc, target)["min_claude_code_version"]
    parts = [int(x) for x in need.split(".")]
    below = ".".join(map(str, parts[:-1] + [parts[-1] - 1]))
    cats = catalogs(claude_cli=below)
    assert model_sync.find_candidates(rows(), cats)["claude_senior"]["status"] == "none"
    assert cats["claude"].excluded[target] == "min_cli"
    # Unknown installed version with a declared minimum also fails closed.
    cats = catalogs(claude_cli=None)
    assert model_sync.find_candidates(rows(), cats)["claude_senior"]["status"] == "none"
    # At the minimum it qualifies.
    cats = catalogs(claude_cli=need)
    assert model_sync.find_candidates(rows(), cats)["claude_senior"]["status"] == "candidate"


@pytest.mark.parametrize("family,mutate", [
    ("openai", lambda d: _codex_entry(d, EXPECTED["candidates"]["openai_reasoning"]).pop("visibility")),
    ("openai", lambda d: _codex_entry(d, EXPECTED["from_ids"]["openai_frontier"]).pop("supported_in_api")),
    ("xai", lambda d: d["models"][EXPECTED["xai_excluded_variant"]]["info"].pop("hidden")),
    ("claude", lambda d: _claude_entry(d, EXPECTED["from_ids"]["claude_worker_fast"]).pop("section")),
    ("claude", lambda d: _claude_entry(d, EXPECTED["candidates"]["claude_senior"]).pop("short_name")),
])
def test_a_missing_discriminator_key_makes_the_family_unavailable(tmp_path, family, mutate):
    """Fail closed: an entry without its discriminator is never a candidate,
    and neither is anything else in that family's catalog."""
    kwargs = {"openai": {"codex": codex_cache}, "xai": {"grok": grok_cache},
              "claude": {"claude": claude_dir}}[family]
    (arg, builder), = kwargs.items()
    cats = catalogs(**{arg: builder(tmp_path, mutate)})
    assert cats[family].status == "discovery_unavailable"
    found = model_sync.find_candidates(rows(), cats)
    for key, cand in found.items():
        if BASE["models"][key]["family"] == family:
            assert cand["status"] == "discovery_unavailable", key
    other = {"openai": "claude_senior", "xai": "openai_reasoning",
             "claude": "openai_reasoning"}[family]
    assert found[other]["status"] == "candidate"


@pytest.mark.parametrize("content", [b"{not json", b"[]", b'{"models": 3}',
                                     b'{"models": [], "models": []}'])
def test_a_malformed_cache_makes_the_family_unavailable(tmp_path, content):
    path = tmp_path / "models_cache.json"
    path.write_bytes(content)
    assert model_sync.read_codex_catalog(path).status == "discovery_unavailable"
    assert model_sync.read_grok_catalog(path, GROK_TEXT).status == "discovery_unavailable"
    d = tmp_path / "cat"
    d.mkdir()
    (d / "x-cc.json").write_bytes(content)
    assert model_sync.read_claude_catalog(d, CLAUDE_CLI).status == "discovery_unavailable"


def test_a_missing_cache_file_is_discovery_unavailable(tmp_path):
    assert model_sync.read_codex_catalog(tmp_path / "none.json").status == "discovery_unavailable"
    assert model_sync.read_grok_catalog(tmp_path / "none.json", None).status == "discovery_unavailable"


def test_a_symlinked_cache_is_refused(tmp_path):
    link = tmp_path / "models_cache.json"
    link.symlink_to(codex_cache())
    assert model_sync.read_codex_catalog(link).status == "discovery_unavailable"


def test_catalog_provenance_is_recorded_without_identity():
    cats = catalogs()
    src = json.loads((CATALOGS / "codex" / "models_cache.json").read_text())
    assert cats["openai"].source["fetched_at"] == src["fetched_at"]
    assert cats["openai"].source["etag"] == src["etag"]
    assert cats["openai"].source["client_version"] == src["client_version"]
    assert "identity" not in cats["openai"].source
    assert all(len(c.sha256) == 64 for c in cats.values())


def test_codex_retirement_is_a_notice_about_the_current_id():
    """`upgrade.retirement_at` is the CURRENT id's retirement, not a successor
    announcement: it is reported, and it proposes nothing."""
    doc = json.loads((CATALOGS / "codex" / "models_cache.json").read_text())
    retiring = next(m for m in doc["models"] if (m.get("upgrade") or {}).get("retirement_at"))
    demo = copy.deepcopy(BASE["models"]["openai_frontier"])
    demo["id"] = retiring["slug"]
    notices = model_sync.retirement_notices({"demo_key": demo}, catalogs())
    assert notices == [{"key": "demo_key", "id": retiring["slug"],
                        "retirement_at": retiring["upgrade"]["retirement_at"],
                        "upgrade_to": retiring["upgrade"]["model"]}]
    assert model_sync.retirement_notices(rows(), catalogs()) == []


def test_absent_claude_catalog_plans_an_alias_probe_instead(tmp_path):
    """Only when the catalog is missing: one contained alias probe per claude
    lineage row that names a catalog_name; nothing is proposed from it here."""
    empty = tmp_path / "empty"
    empty.mkdir()
    cats = catalogs(claude=empty)
    assert cats["claude"].status == "absent"
    found = model_sync.find_candidates(rows(), cats)
    claude_keys = sorted(k for k, r in rows().items() if r["family"] == "claude")
    assert all(found[k]["status"] == "alias_probe" for k in claude_keys)
    plan = model_sync.alias_probe_plan(rows(), cats["claude"])
    assert sorted(p["key"] for p in plan) == claude_keys
    for p in plan:
        assert p["alias"] == BASE["models"][p["key"]]["lineage"]["catalog_name"].lower()
    # A present catalog plans no alias probe.
    assert model_sync.alias_probe_plan(rows(), catalogs()["claude"]) == []


def test_candidates_follow_the_overlay_current_id():
    """The current id is the one after the overlay: a row already on the
    newest generation has nothing to propose."""
    moved = rows({k: v for k, v in EXPECTED["candidates"].items()})
    found = model_sync.find_candidates(moved, catalogs())
    assert all(c["status"] == "none" for c in found.values())


@pytest.mark.parametrize("fixture,family", [("codex", "openai"), ("claude", "claude"),
                                            ("grok", "xai")])
def test_cli_version_banners_parse(fixture, family):
    banner = (CATALOGS / fixture / "version.txt").read_text()
    version = model_sync.parse_cli_version(banner)
    assert version is not None and version in banner
    assert all(part.isdigit() for part in version.split("."))
    assert model_sync.parse_cli_version("no version here") is None


def test_cli_versions_are_read_from_the_cli_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for cli, fixture in (("codex", "codex"), ("claude", "claude"), ("grok", "grok")):
        exe = bindir / cli
        banner = (CATALOGS / fixture / "version.txt").read_text()
        exe.write_text(f"#!{sys.executable}\nimport sys\n"
                       f"assert sys.argv[1:] == ['--version'], sys.argv\n"
                       f"sys.stdout.write({banner!r})\n")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    got = model_sync.cli_versions()
    for cli, fixture in (("codex", "codex"), ("claude", "claude"), ("grok", "grok")):
        assert got[cli] == model_sync.parse_cli_version(
            (CATALOGS / fixture / "version.txt").read_text())
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert model_sync.cli_versions() == {"codex": None, "claude": None, "grok": None}


# ---------------------------------------------------------------------------
# A9 — probe gates (DD-A5). Pure functions over receipt-shaped evidence.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402
import subprocess  # noqa: E402
import urllib.parse  # noqa: E402

import model_state  # noqa: E402
import secure_io  # noqa: E402
from policy_digest import canonical_policy_sha256  # noqa: E402

CODEX = SKILL / "tests" / "fixtures" / "codex"
NOW = _dt.datetime(2026, 9, 25, 12, 0, tzinfo=_dt.timezone.utc)
CTX = model_sync.ProbeContext(now=NOW, cli_version="0.157.0", catalog_sha256="c" * 64)
CANDIDATE_FAST = EXPECTED["candidates"]["openai_worker_fast"]


def receipt(state="SUCCEEDED", *, confirmed=True, fmt=None, envelope=None,
            model_id="demo-model", session_summary=None):
    return {"attempt_id": "a1", "model_id": model_id, "output_envelope": fmt,
            "session_evidence": ({"summary": session_summary}
                                 if session_summary is not None else None),
            "result": {"state": state, "termination_confirmed": confirmed,
                       "envelope": envelope}}


@pytest.mark.parametrize("stdout", ["pong", "pong\n", "  pong  \n", "\npong"])
def test_gate_p1_passes_exactly_pong(stdout):
    assert model_sync.gate_p1(receipt(), stdout, CTX)["outcome"] == "pass"


@pytest.mark.parametrize("rec,stdout", [
    (receipt(), "pong."),
    (receipt(), "Pong"),
    (receipt(), "pong\nmore"),
    (receipt(), ""),
    (receipt(), None),
    (receipt(state="INVALID_OUTPUT"), "pong"),
    (receipt(state="FAILED"), "pong"),
    (receipt(state="TIMED_OUT"), "pong"),
    (receipt(confirmed=None), "pong"),
    (receipt(confirmed=False), "pong"),
])
def test_gate_p1_fails_closed_on_any_mismatch(rec, stdout):
    """A SUCCEEDED receipt is not enough: output and confirmed termination
    must both hold, and any one miss is `failed: smoke` retried in 6 hours."""
    got = model_sync.gate_p1(rec, stdout, CTX)
    assert got["outcome"] == "failed" and got["reason"] == "smoke"
    assert got["retry_after"] == {"kind": "time",
                                  "at": (NOW + _dt.timedelta(hours=6)).isoformat()}


def _efforts(*tokens):
    return list(tokens)


def test_gate_p0_passes_with_full_vocabulary_and_cli_metadata():
    got = model_sync.gate_p0(
        family="openai", candidate_id=CANDIDATE_FAST,
        row=BASE["models"]["openai_worker_fast"], base=BASE,
        catalog_efforts=_efforts("low", "medium", "high", "xhigh", "max"),
        cli_metadata_ids=[CANDIDATE_FAST], ctx=CTX)
    assert got["outcome"] == "pass" and got["effort_ceiling"] is None


@pytest.mark.parametrize("efforts,ceiling", [
    (("low", "medium", "high", "xhigh"), "VERY_HIGH"),
    (("low", "medium", "high"), "HIGH"),
])
def test_gate_p0_plans_an_effort_override(efforts, ceiling):
    got = model_sync.gate_p0(
        family="openai", candidate_id=CANDIDATE_FAST,
        row=BASE["models"]["openai_worker_fast"], base=BASE,
        catalog_efforts=list(efforts), cli_metadata_ids=[CANDIDATE_FAST], ctx=CTX)
    assert got["outcome"] == "pass" and got["effort_ceiling"] == ceiling


def test_gate_p0_respects_an_existing_row_ceiling():
    """xai maps MAX to xhigh and caps at VERY_HIGH: `max` is never needed."""
    row = BASE["models"]["xai_frontier"]
    got = model_sync.gate_p0(
        family="xai", candidate_id="demo", row=row, base=BASE,
        catalog_efforts=_efforts("xhigh", "high", "medium", "low"),
        cli_metadata_ids=None, ctx=CTX)
    assert got["outcome"] == "pass"
    assert got["effort_ceiling"] == row["effort_ceiling"]


def test_gate_p0_fails_when_a_routine_token_is_missing():
    got = model_sync.gate_p0(
        family="openai", candidate_id=CANDIDATE_FAST,
        row=BASE["models"]["openai_worker_fast"], base=BASE,
        catalog_efforts=_efforts("low", "medium"), cli_metadata_ids=[CANDIDATE_FAST],
        ctx=CTX)
    assert got["outcome"] == "failed" and got["reason"] == "effort_vocabulary"


@pytest.mark.parametrize("ids", [None, []])
def test_gate_p0_defers_openai_without_cli_metadata(ids):
    """`codex debug models` without the slug, or the command failing, is
    `deferred: cli_metadata` until the CLI version changes."""
    got = model_sync.gate_p0(
        family="openai", candidate_id=CANDIDATE_FAST,
        row=BASE["models"]["openai_worker_fast"], base=BASE,
        catalog_efforts=_efforts("low", "medium", "high", "xhigh", "max"),
        cli_metadata_ids=ids, ctx=CTX)
    assert (got["outcome"], got["reason"]) == ("deferred", "cli_metadata")
    assert got["retry_after"] == {"kind": "cli_version_change", "cli_version": "0.157.0"}


def test_bundled_cli_metadata_fixture_lists_the_candidates():
    doc = json.loads((CATALOGS / "codex" / "debug-models-bundled.json").read_text())
    ids = model_sync.parse_codex_debug_models(json.dumps(doc))
    assert CANDIDATE_FAST in ids
    assert model_sync.parse_codex_debug_models("not json") is None


def _codex_text_envelope(header_model, warning=False):
    return {"parse_ok": True, "stop_reason": None, "session_id": None,
            "served_models": None, "error_type": None, "usage": None,
            "header_model": header_model, "header_model_basis": "header-reported",
            "metadata_warning": warning, "footer_tokens_uncached": 1,
            "cli_version": "0.157.0"}


def test_gate_p2_codex_header_equal_to_request_is_id_accepted():
    rec = receipt(fmt="codex-exec-text-v1", model_id=CANDIDATE_FAST,
                  envelope=_codex_text_envelope(CANDIDATE_FAST))
    got = model_sync.gate_p2(rec, BASE["models"]["openai_worker_fast"]["lineage"], CTX)
    assert got["outcome"] == "pass" and got["basis"] == "id_accepted"
    assert got["observed"] == [CANDIDATE_FAST]


@pytest.mark.parametrize("env,outcome,reason,retry", [
    (_codex_text_envelope(None), "deferred", "served_unproven", "cli_version_change"),
    (_codex_text_envelope("other-model"), "failed", "served_mismatch", "catalog_change"),
    (_codex_text_envelope(CANDIDATE_FAST, warning=True), "deferred", "cli_metadata",
     "cli_version_change"),
])
def test_gate_p2_codex_failures(env, outcome, reason, retry):
    rec = receipt(fmt="codex-exec-text-v1", model_id=CANDIDATE_FAST, envelope=env)
    got = model_sync.gate_p2(rec, BASE["models"]["openai_worker_fast"]["lineage"], CTX)
    assert (got["outcome"], got["reason"], got["retry_after"]["kind"]) == (outcome, reason, retry)


def test_gate_p2_codex_json_mode_cannot_prove_serving():
    rec = receipt(fmt="codex-exec-json-v1", model_id=CANDIDATE_FAST,
                  envelope={"parse_ok": True, "served_models": None})
    got = model_sync.gate_p2(rec, BASE["models"]["openai_worker_fast"]["lineage"], CTX)
    assert (got["outcome"], got["reason"]) == ("deferred", "served_unproven")


def test_gate_p2_grok_accepts_a_served_form():
    lin = BASE["models"]["xai_frontier"]["lineage"]
    served = [f.replace("{id}", "demo-9") for f in lin["served_forms"]][-1]
    rec = receipt(fmt="grok-headless-json-v1", model_id="demo-9",
                  envelope={"served_models": [served]},
                  session_summary={"current_model_id": served})
    got = model_sync.gate_p2(rec, lin, CTX)
    assert got["outcome"] == "pass" and got["basis"] == "served"
    rec = receipt(fmt="grok-headless-json-v1", model_id="demo-9",
                  envelope={"served_models": ["demo-8"]},
                  session_summary={"current_model_id": "demo-8"})
    assert model_sync.gate_p2(rec, lin, CTX)["reason"] == "served_mismatch"


def test_gate_p2_claude_served_models_must_be_the_id():
    lin = BASE["models"]["claude_senior"]["lineage"]
    ok = receipt(fmt="claude-print-json-v1", model_id="demo-9",
                 envelope={"served_models": ["demo-9"]})
    assert model_sync.gate_p2(ok, lin, CTX)["outcome"] == "pass"
    bad = receipt(fmt="claude-print-json-v1", model_id="demo-9",
                  envelope={"served_models": ["demo-9", "demo-small"]})
    assert model_sync.gate_p2(bad, lin, CTX)["reason"] == "served_mismatch"
    none = receipt(fmt="claude-print-json-v1", model_id="demo-9",
                   envelope={"served_models": None})
    assert model_sync.gate_p2(none, lin, CTX)["reason"] == "served_unproven"


def test_boot_input_tokens_is_the_total_including_cache():
    events = [json.loads(l) for l in (CODEX / "codex-0.157.0-json.jsonl").read_text().splitlines()]
    usage = [e for e in events if e["type"] == "turn.completed"][-1]["usage"]
    rec = receipt(fmt="codex-exec-json-v1", envelope={"usage": usage})
    assert model_sync.boot_input_tokens(rec) == usage["input_tokens"]
    claude = receipt(fmt="claude-print-json-v1", envelope={"usage": {
        "input_tokens": 2, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7}})
    assert model_sync.boot_input_tokens(claude) == 14
    assert model_sync.boot_input_tokens(receipt(fmt="codex-exec-text-v1",
                                                envelope={"usage": None})) is None


@pytest.mark.parametrize("pair,prior,cap,outcome,basis", [
    ({"current": 20000, "candidate": 29000}, None, 60000, "pass", "paired"),
    ({"current": 20000, "candidate": 31000}, None, 60000, "deferred", "paired"),
    ({"current": 50000, "candidate": 61000}, None, 60000, "deferred", "paired"),
    ({"current": None, "candidate": 29000, "current_unsupported": True}, 20000, 60000,
     "pass", "prior_baseline"),
    ({"current": None, "candidate": 31000, "current_unsupported": True}, 20000, 60000,
     "deferred", "prior_baseline"),
    ({"current": None, "candidate": 59000, "current_unsupported": True}, None, 60000,
     "pass", "absolute_only"),
    ({"current": None, "candidate": 61000, "current_unsupported": True}, None, 60000,
     "deferred", "absolute_only"),
])
def test_gate_p3_pairs_and_falls_back(pair, prior, cap, outcome, basis):
    got = model_sync.gate_p3(pair, prior, cap, CTX)
    assert (got["outcome"], got["p3_basis"]) == (outcome, basis)
    if outcome == "deferred":
        assert got["reason"] == "overhead"


def test_gate_p3_cap_applies_to_the_cached_total():
    """The real capture: 26,068 input of which 11,008 cached. A cap between the
    uncached and the total must fail — cache lowers the bill, not the count."""
    events = [json.loads(l) for l in (CODEX / "codex-0.157.0-json.jsonl").read_text().splitlines()]
    usage = [e for e in events if e["type"] == "turn.completed"][-1]["usage"]
    total, uncached = usage["input_tokens"], usage["input_tokens"] - usage["cached_input_tokens"]
    cap = (total + uncached) // 2
    rec = receipt(fmt="codex-exec-json-v1", envelope={"usage": usage})
    pair = {"current": model_sync.boot_input_tokens(rec),
            "candidate": model_sync.boot_input_tokens(rec)}
    assert model_sync.gate_p3(pair, None, cap, CTX)["outcome"] == "deferred"


def test_gate_p3_without_a_candidate_measurement_fails():
    got = model_sync.gate_p3({"current": 1, "candidate": None}, None, 60000, CTX)
    assert got["outcome"] == "failed" and got["reason"] == "smoke"


def test_gate_p4_top_token_and_one_step_down():
    assert model_sync.gate_p4([{"effort": "MAX", "native": "max", "accepted": True}],
                              CTX) == {"gate": "P4", "outcome": "pass",
                                       "reason": None, "effort_ceiling": None,
                                       "retry_after": None}
    got = model_sync.gate_p4([{"effort": "MAX", "native": "max", "accepted": False},
                              {"effort": "VERY_HIGH", "native": "xhigh", "accepted": True}], CTX)
    assert got["outcome"] == "pass" and got["effort_ceiling"] == "VERY_HIGH"
    got = model_sync.gate_p4([{"effort": "MAX", "native": "max", "accepted": False},
                              {"effort": "VERY_HIGH", "native": "xhigh", "accepted": False}], CTX)
    assert got["outcome"] == "failed" and got["reason"] == "top_token"


def test_retry_after_per_reason():
    later = int((NOW + _dt.timedelta(hours=30)).timestamp())
    assert model_sync.retry_after_for("quota", CTX._replace(quota_resets_at=later)) == \
        {"kind": "time", "at": _dt.datetime.fromtimestamp(later, _dt.timezone.utc).isoformat()}
    assert model_sync.retry_after_for("quota", CTX) == \
        {"kind": "time", "at": (NOW + _dt.timedelta(hours=24)).isoformat()}
    assert model_sync.retry_after_for("smoke", CTX)["at"] == \
        (NOW + _dt.timedelta(hours=6)).isoformat()
    assert model_sync.retry_after_for("served_unproven", CTX) == \
        {"kind": "cli_version_change", "cli_version": "0.157.0"}
    assert model_sync.retry_after_for("served_mismatch", CTX) == \
        {"kind": "catalog_change", "catalog_sha256": "c" * 64}


# ---------------------------------------------------------------------------
# A9 — contained probe argv (DD-A0). Read-only reviewer recipes only.
# ---------------------------------------------------------------------------

def _argv(family, **kw):
    params = dict(family=family, model_id="demo-9", effort_native="low",
                  attempt_id="p-demo", receipt_dir=Path("/r"),
                  child_cwd=Path("/c"), prompt_file=Path("/p/prompt.txt"),
                  deadline_seconds=120, grok_home=Path("/g"),
                  session_id="11111111-2222-3333-4444-555555555555",
                  receipt_guard=True)
    params.update(kw)
    return model_sync.probe_argv(**params)


def _split(argv):
    at = argv.index("--")
    return argv[:at], argv[at + 1:]


def test_claude_probe_argv_is_the_contained_reviewer_recipe():
    sup, child = _split(_argv("claude"))
    assert sup[:3] == [sys.executable, str(model_sync.DISPATCH), "run"]
    assert child[:2] == ["claude", "-p"]
    assert "--strict-mcp-config" in child
    assert child[child.index("--permission-mode") + 1] == "plan"
    assert child[child.index("--output-format") + 1] == "json"
    assert sup[sup.index("--output-envelope") + 1] == "claude-print-json-v1"
    assert sup[sup.index("--receipt-guard") + 1] == "darwin-sandbox-v1"
    assert sup[sup.index("--child-cwd") + 1] == "/c"
    assert sup[sup.index("--prompt-file") + 1] == "/p/prompt.txt"


@pytest.mark.parametrize("mode,envelope", [("text", "codex-exec-text-v1"),
                                           ("json", "codex-exec-json-v1")])
def test_codex_probe_argv_is_read_only_without_the_guard(mode, envelope):
    sup, child = _split(_argv("openai", codex_mode=mode))
    assert child[:2] == ["codex", "exec"]
    assert child[child.index("-s") + 1] == "read-only"
    assert ("--json" in child) == (mode == "json")
    assert child[-1] == "-"
    assert sup[sup.index("--output-envelope") + 1] == envelope
    assert "--receipt-guard" not in sup
    assert "--allow-nested-sandbox" not in sup


def test_grok_probe_argv_carries_the_whole_evidence_set():
    uuid = "11111111-2222-3333-4444-555555555555"
    sup, child = _split(_argv("xai", child_cwd=Path("/c d")))
    assert child[0] == "grok"
    assert child[child.index("-s") + 1] == uuid
    assert child[child.index("--sandbox") + 1] == "read-only"
    assert child[child.index("--permission-mode") + 1] == "plan"
    assert sup[sup.index("--output-envelope") + 1] == "grok-headless-json-v1"
    assert sup[sup.index("--session-id") + 1] == uuid
    assert sup[sup.index("--session-evidence") + 1] == (
        "grok-session-v1:/g/sessions/" + urllib.parse.quote("/c d", safe="") + "/" + uuid)
    assert sup[sup.index("--expect-sandbox-profile") + 1] == "read-only"
    assert sup[sup.index("--receipt-guard") + 1] == "darwin-sandbox-v1"
    assert sup[sup.index("--transport-id") + 1].endswith(".to_xai")


WRITE_MARKERS = ("workspace-write", "danger-full-access", "acceptEdits",
                 "bypassPermissions", "Write(", "Edit(", "search_replace",
                 "dmr-maker-v1", "--seat-profile", "--require-artifact")


@pytest.mark.parametrize("family,kw", [("claude", {}), ("openai", {"codex_mode": "text"}),
                                       ("openai", {"codex_mode": "json"}), ("xai", {})])
def test_no_probe_argv_is_a_write_recipe(family, kw):
    argv = _argv(family, **kw)
    assert not [a for a in argv if any(m in a for m in WRITE_MARKERS)]


def test_model_sync_has_no_code_path_that_builds_a_write_recipe():
    """The automatic probe never seats a writer (DD-A0): no write-recipe token
    appears anywhere in the module, and the builder takes no seat/mode knob."""
    import inspect
    src = (SKILL / "scripts" / "model_sync.py").read_text()
    assert not [m for m in WRITE_MARKERS if m in src]
    params = set(inspect.signature(model_sync.probe_argv).parameters)
    assert not params & {"seat", "permission_mode", "sandbox", "write", "maker"}


def test_child_cwd_is_fresh_empty_and_private(tmp_path):
    a = model_sync.new_child_cwd(tmp_path)
    b = model_sync.new_child_cwd(tmp_path)
    assert a != b and list(a.iterdir()) == [] and (a.stat().st_mode & 0o777) == 0o700


def _trap(bindir: Path, name: str, marker: Path) -> None:
    exe = bindir / name
    exe.write_text(f"#!{sys.executable}\nopen({str(marker)!r}, 'a').write('ran\\n')\n")
    exe.chmod(0o755)


def test_grok_argv_without_session_evidence_is_refused_by_dispatch(tmp_path):
    """Not inferred: the real dispatch_agent refuses before spawn, and the
    fake grok first on PATH is never reached."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "grok-ran"
    _trap(bindir, "grok", marker)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Reply with exactly: pong\n")
    argv = _argv("xai", receipt_dir=tmp_path / "receipts",
                 child_cwd=model_sync.new_child_cwd(tmp_path), prompt_file=prompt)
    at = argv.index("--session-evidence")
    del argv[at:at + 2]
    env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert "--session-evidence" in proc.stderr
    assert not marker.exists()
    assert not (tmp_path / "receipts" / "p-demo.json").exists()


# ---------------------------------------------------------------------------
# A9 — the harness: dispatch_agent subprocesses, one id per process, a
# content-addressed summary under work/probes/summaries/.
# ---------------------------------------------------------------------------

FAKE_CODEX = r'''#!{python}
import json, os, sys
from pathlib import Path
fx = Path({fixtures!r})
log = Path(os.environ["FAKE_CODEX_LOG"])
args = sys.argv[1:]
if args == ["--version"]:
    sys.stdout.write((fx / "catalogs" / "codex" / "version.txt").read_text()); sys.exit(0)
if args[:2] == ["debug", "models"]:
    assert "--bundled" in args, args
    sys.stdout.write((fx / "catalogs" / "codex" / "debug-models-bundled.json").read_text()); sys.exit(0)
assert args[0] == "exec", args
assert os.listdir(".") == [], "probe cwd is not empty"
model = args[args.index("-m") + 1]
effort = [a for a in args if a.startswith("model_reasoning_effort=")][0].split("=", 1)[1]
with log.open("a") as f:
    f.write(json.dumps({{"model": model, "effort": effort, "json": "--json" in args,
                        "pid": os.getpid()}}) + "\n")
sys.stdin.read()
if "--json" in args:
    sys.stdout.write((fx / "codex" / "codex-0.157.0-json.jsonl").read_text())
else:
    sys.stdout.write((fx / "codex" / "codex-0.157.0-plain.stdout").read_text())
    sys.stderr.write((fx / "codex" / "codex-0.157.0-plain.stderr").read_text())
'''


def _fake_codex(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "codex"
    exe.write_text(FAKE_CODEX.format(python=sys.executable,
                                     fixtures=str(SKILL / "tests" / "fixtures")))
    exe.chmod(0o755)
    for trap in ("claude", "grok"):
        _trap(bindir, trap, tmp_path / f"{trap}-ran")
    log = tmp_path / "codex.log"
    env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
           "FAKE_CODEX_LOG": str(log)}
    return env, log


def _state(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    return root


def test_harness_probes_a_codex_candidate_end_to_end(tmp_path):
    env, log = _fake_codex(tmp_path)
    key = "openai_worker_fast"
    cats = catalogs()
    cand = model_sync.find_candidates(rows(), cats)[key]
    assert cand["id"] == _banner_model_of_fixture()
    base = copy.deepcopy(BASE)
    base["models"][key]["id"] = cand["from_id"]
    state = _state(tmp_path)
    result = model_sync.probe_candidate(
        key=key, candidate=cand, base=base, state_root=state, env=env,
        catalog=cats["openai"], current_id=cand["from_id"], superseded=[],
        scratch=tmp_path / "scratch", ctx=CTX)
    assert result["summary"]["outcome"] == "pass", result["summary"]
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    # P1 (plain), P3 pair (json, current then candidate), P4 (plain, top token).
    assert [(c["model"], c["json"]) for c in calls] == [
        (cand["id"], False), (cand["from_id"], True), (cand["id"], True),
        (cand["id"], False)]
    assert len({c["pid"] for c in calls}) == 4
    assert calls[0]["effort"] == "low"
    assert calls[-1]["effort"] == BASE["effort_map"]["openai"]["MAX"]
    assert result["inferences"] == 4
    assert not (tmp_path / "claude-ran").exists() and not (tmp_path / "grok-ran").exists()

    summary = result["summary"]
    sha = result["summary_sha256"]
    with secure_io.StateRoot.open(state) as root:
        stored, got = root.read_json_with_sha(f"work/probes/summaries/{sha}.json")
    assert got == sha and stored == summary
    for k in model_state.SUMMARY_MATCH_KEYS:
        assert k in summary, k
    assert summary["base_row_sha256"] == model_state.base_row_sha256(base, key)
    assert summary["recipe_sha256"] == model_state.recipe_sha256(base, "openai")
    assert summary["guard"] == "omitted: nested Seatbelt"
    assert summary["served_basis"] == "id_accepted"
    assert summary["p3_basis"] == "paired"
    assert summary["input_tokens"]["candidate"] == summary["input_tokens"]["current"] > 0
    assert summary["catalog"]["sha256"] == cats["openai"].sha256
    assert summary["cli_version"] == "0.157.0"
    assert summary["base_policy_sha256"] == canonical_policy_sha256(base)
    assert len(summary["probes"]) == 4
    assert all(len(p["receipt_sha256"]) == 64 and p["argv"] for p in summary["probes"])

    # The summary is exactly what model_state admits for an entry built from it.
    entry = {k: summary[k] for k in ("line", "from_id", "id", "superseded",
                                     "effort_map", "effort_ceiling")}
    entry["probe_summary_sha256"] = sha
    gen = {"overlay_schema_version": 1, "entries": {key: entry}, "history": {},
           "blocked_ids": [], "parent_generation_sha256": None,
           "base_policy_sha256": canonical_policy_sha256(base)}
    cfg, prov = model_state.effective_config(
        base, gen, apply_entries=True, summary=lambda s: summary if s == sha else None)
    assert prov.applied == [key] and cfg["models"][key]["id"] == cand["id"]


def _banner_model_of_fixture():
    import re
    return re.search(r"^model: (\S+)$", (CODEX / "codex-0.157.0-plain.stderr").read_text(),
                     re.M).group(1)


def test_harness_stops_at_a_served_mismatch(tmp_path):
    """The fixture banner names one model; probing another id through it is a
    served mismatch — recorded, retried only when the catalog changes, and no
    further inference is spent."""
    env, log = _fake_codex(tmp_path)
    key = "openai_reasoning"
    cats = catalogs()
    cand = model_sync.find_candidates(rows(), cats)[key]
    assert cand["id"] != _banner_model_of_fixture()
    base = copy.deepcopy(BASE)
    base["models"][key]["id"] = cand["from_id"]
    result = model_sync.probe_candidate(
        key=key, candidate=cand, base=base, state_root=_state(tmp_path), env=env,
        catalog=cats["openai"], current_id=cand["from_id"], superseded=[],
        scratch=tmp_path / "scratch", ctx=CTX)
    s = result["summary"]
    assert (s["outcome"], s["reason"]) == ("failed", "served_mismatch")
    assert s["retry_after"] == {"kind": "catalog_change", "catalog_sha256": cats["openai"].sha256}
    assert result["inferences"] == 1 and len(log.read_text().splitlines()) == 1


def test_harness_defers_without_cli_metadata_and_spends_nothing(tmp_path):
    env, log = _fake_codex(tmp_path)
    key = "openai_worker_fast"
    cats = catalogs()
    cand = model_sync.find_candidates(rows(), cats)[key]
    result = model_sync.probe_candidate(
        key=key, candidate=cand, base=BASE, state_root=_state(tmp_path), env=env,
        catalog=cats["openai"], current_id=cand["from_id"], superseded=[],
        scratch=tmp_path / "scratch", ctx=CTX, cli_metadata_ids=[])
    assert (result["summary"]["outcome"], result["summary"]["reason"]) == \
        ("deferred", "cli_metadata")
    assert result["inferences"] == 0 and not log.exists()
