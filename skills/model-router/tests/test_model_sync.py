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
        # grok runs offline only with --no-auto-update (it self-updates on start)
        want = ["--no-auto-update", "--version"] if cli == "grok" else ["--version"]
        exe.write_text(f"#!{sys.executable}\nimport sys\n"
                       f"assert sys.argv[1:] == {want!r}, sys.argv\n"
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

import lineage  # noqa: E402
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
    got = model_sync.gate_p4([{"effort": "MAX", "native": "max", "accepted": False,
                               "rejected": True},
                              {"effort": "VERY_HIGH", "native": "xhigh", "accepted": True,
                               "rejected": False}], CTX)
    assert got["outcome"] == "pass" and got["effort_ceiling"] == "VERY_HIGH"
    got = model_sync.gate_p4([{"effort": "MAX", "native": "max", "accepted": False,
                               "rejected": True},
                              {"effort": "VERY_HIGH", "native": "xhigh", "accepted": False,
                               "rejected": True}], CTX)
    assert got["outcome"] == "failed" and got["reason"] == "top_token"


def test_gate_p4_a_failure_without_a_rejection_is_transient_not_a_ceiling():
    """i1r1 opus F6: a timeout or a 5xx on the top token is no vendor verdict
    on the effort — lowering the ceiling from it would publish a policy clamp."""
    got = model_sync.gate_p4([{"effort": "MAX", "native": "max", "accepted": False,
                               "rejected": False}], CTX)
    assert (got["outcome"], got["reason"], got["effort_ceiling"]) == \
        ("failed", "transient", None)
    assert got["retry_after"] == {"kind": "time",
                                  "at": (NOW + _dt.timedelta(hours=6)).isoformat()}
    got = model_sync.gate_p4([{"effort": "MAX", "native": "max", "accepted": False,
                               "rejected": True},
                              {"effort": "VERY_HIGH", "native": "xhigh", "accepted": False,
                               "rejected": False}], CTX)
    assert (got["outcome"], got["reason"]) == ("failed", "transient")


class _ScriptedRun:
    """Stands in for `_Run`: each call pops (receipt, text, output)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def call(self, gate, model_id, effort_native, *, codex_mode="text"):
        self.calls.append((gate, model_id, effort_native, codex_mode))
        return self.answers.pop(0)


def _p4_row():
    key = next(k for k, r in BASE["models"].items()
               if r.get("family") == "claude" and "lineage" in r and "history_of" not in r
               and not r.get("effort_ceiling"))
    return BASE["models"][key]


@pytest.mark.parametrize("failure,output,expect", [
    ("TIMED_OUT", "", ("failed", "transient", None, 1)),
    ("FAILED", "error: upstream returned 503 Service Unavailable\n", ("failed", "transient", None, 1)),
    # the metadata warning mentions "not found" but is no error line
    ("FAILED", "warning: Model metadata for demo not found; effort not supported?\n",
     ("failed", "transient", None, 1)),
    ("FAILED", "error: invalid value 'max' for '--effort <EFFORT>'\n",
     ("pass", None, "VERY_HIGH", 2)),
    ("FAILED", '{"type":"result","is_error":true,"result":"effort level max is not supported for this model"}\n',
     ("pass", None, "VERY_HIGH", 2)),
])
def test_p4_lowers_the_ceiling_only_on_an_effort_rejection(failure, output, expect):
    row = _p4_row()
    top = model_sync.native_token(BASE, row, "MAX")
    ok = receipt()
    run = _ScriptedRun((receipt(failure), None, output), (ok, "pong", ""))
    record = {}
    got = model_sync._p4(run, BASE, row, "demo-id", None, CTX, record)
    assert (got["outcome"], got["reason"], got["effort_ceiling"], len(run.calls)) == expect
    assert run.calls[0][2] == top


@pytest.mark.parametrize("output,unsupported", [
    ("warning: Model metadata for demo-current not found, using defaults\n", False),
    ("the model said: not found anywhere\n", False),
    ('{"type":"error","message":"The requested model is not supported"}\n', True),
    ("ERROR: model demo-current not found\n", True),
])
def test_p3_reads_unsupported_markers_on_error_lines_only(output, unsupported):
    row = _p4_row()
    cand = receipt(fmt="claude-print-json-v1",
                   envelope={"usage": {"input_tokens": 10}})
    run = _ScriptedRun((receipt("FAILED"), None, output))
    record = {}
    got = model_sync._p3(run, "claude", "demo-cand", "demo-current", "low", cand,
                         None, 60_000, CTX, record)
    if unsupported:
        assert (got["outcome"], got["p3_basis"]) == ("pass", "absolute_only")
    else:
        assert (got["outcome"], got["reason"]) == ("failed", "smoke")


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


# ---------------------------------------------------------------------------
# A10 — publication, tick, disable, revert, unblock, repair, status, quota,
# attended probe-maker (design DD-A2, DD-A6, DD-A7).
#
# Every CLI a test reaches is a fake first on PATH (FAKE_CLI below), and the
# PATH handed to model_sync holds nothing else a model CLI could resolve from.
# ---------------------------------------------------------------------------

import shutil as _shutil  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

sys.path.insert(0, str(SKILL / "tests"))
from _overlay import (  # noqa: E402
    BASE_SHA, base_record, entry as ov_entry, env_for, generation as ov_generation,
    probe_record, publish as ov_publish, sha_of, successor, summary as ov_summary,
)
from route_task import Policy, Task, resolve_effective_policy, route  # noqa: E402

FIXTURES = SKILL / "tests" / "fixtures"
SYNC = SKILL / "scripts" / "model_sync.py"
ROUTE = SKILL / "scripts" / "route_task.py"
TASK = dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, blast_radius=1,
            reversibility=1)
ROLLOUT_EVENT = json.loads((CODEX / "codex-rollout-token_count.jsonl").read_text().splitlines()[0])

FAKE_CLI = r'''#!{python}
import json, os, re, sys, time
from pathlib import Path
name = {name!r}
fx = Path({fixtures!r})
d = Path({fakedir!r})
args = sys.argv[1:]
plain = args[1:] if name == "grok" and args[:1] == ["--no-auto-update"] else args
if plain == ["--version"]:
    v = d / (name + ".version")
    sys.stdout.write(v.read_text() if v.exists()
                     else (fx / "catalogs" / {vfix!r} / "version.txt").read_text())
    sys.exit(0)
if name == "grok" and plain == ["models"]:
    sys.stdout.write((fx / "catalogs" / "grok" / "grok-models.txt").read_text()); sys.exit(0)
if name == "codex" and args[:2] == ["debug", "models"]:
    sys.stdout.write((fx / "catalogs" / "codex" / "debug-models-bundled.json").read_text())
    sys.exit(0)
mode_file = d / (name + ".mode")
mode = mode_file.read_text().strip() if mode_file.exists() else "trap"
with (d / (name + ".calls")).open("a") as f:
    f.write(json.dumps({{"args": args, "cwd": os.getcwd(), "pid": os.getpid(),
                        "mode": mode}}) + "\n")
if mode == "trap":
    sys.exit(97)
prompt = sys.stdin.read()
if mode == "sleep":
    time.sleep(120)
    sys.exit(0)
if mode == "alias":
    # The contained reviewer recipe or nothing (i1r1 grok F2).
    if "--strict-mcp-config" not in args or "plan" not in args:
        sys.exit(98)
    model = args[args.index("--model") + 1]
    serve = d / "claude.serve"
    served = (json.loads(serve.read_text()) if serve.exists() else {{}}).get(model, model)
    answers = d / "claude.answers"
    answer = (json.loads(answers.read_text()) if answers.exists() else {{}}).get(model, "pong")
    sys.stdout.write(json.dumps({{"type": "result", "subtype": "success",
        "is_error": False, "stop_reason": "end_turn", "result": answer,
        "session_id": "s", "num_turns": 1,
        "usage": {{"input_tokens": 100, "output_tokens": 1}},
        "modelUsage": {{served: {{"inputTokens": 100}}}}}}))
    sys.exit(0)
if mode == "maker":
    token = re.search(r"MADE-[0-9a-f]{{16}}", prompt).group(0)
    tmp = Path("made.txt.tmp")
    tmp.write_text(token + "\n")
    os.replace(tmp, "made.txt")          # a NEW inode, the way claude writes
    if name == "claude":
        model = args[args.index("--model") + 1]
        sys.stdout.write(json.dumps({{"type": "result", "subtype": "success",
            "is_error": False, "stop_reason": "end_turn", "result": "done",
            "session_id": "s", "num_turns": 1,
            "usage": {{"input_tokens": 1, "output_tokens": 1}},
            "modelUsage": {{model: {{"inputTokens": 1}}}}}}))
    else:
        sys.stdout.write("done\n")
        sys.stderr.write((fx / "codex" / "codex-0.157.0-plain.stderr").read_text())
    sys.exit(0)
# mode == "codex": the recorded plain / JSON outputs
if "--json" in args:
    sys.stdout.write((fx / "codex" / "codex-0.157.0-json.jsonl").read_text())
else:
    sys.stdout.write((fx / "codex" / "codex-0.157.0-plain.stdout").read_text())
    sys.stderr.write((fx / "codex" / "codex-0.157.0-plain.stderr").read_text())
'''


def fake_bin(tmp_path, **modes):
    """codex/claude/grok fakes (default: trap) and a PATH holding nothing else
    a model CLI could resolve from. Returns (bindir, fakedir)."""
    bindir = tmp_path / "fakebin"
    fakedir = tmp_path / "fakes"
    bindir.mkdir(exist_ok=True)
    fakedir.mkdir(exist_ok=True)
    for name, vfix in (("codex", "codex"), ("claude", "claude"), ("grok", "grok")):
        exe = bindir / name
        exe.write_text(FAKE_CLI.format(python=sys.executable, name=name, vfix=vfix,
                                       fixtures=str(FIXTURES), fakedir=str(fakedir)))
        exe.chmod(0o755)
        if name in modes:
            (fakedir / f"{name}.mode").write_text(modes[name])
    return bindir, fakedir


def calls_of(fakedir, name):
    path = fakedir / f"{name}.calls"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def rollout(home, used, ts, resets):
    day = home / ".codex" / "sessions" / ts.strftime("%Y/%m/%d")
    day.mkdir(parents=True, exist_ok=True)
    ev = copy.deepcopy(ROLLOUT_EVENT)
    ev["timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ev["payload"]["rate_limits"]["primary"].update(used_percent=used,
                                                   resets_at=int(resets.timestamp()))
    path = day / f"rollout-{ts.strftime('%Y-%m-%dT%H-%M-%S')}-fixture.jsonl"
    path.write_text(json.dumps(ev) + "\n")
    return path


def fake_home(tmp_path, *, claude=True, grok=False, codex_mutate=None):
    """Catalogs where the CLIs keep them. Codex and claude by default (the
    captured claude catalog proposes nothing against the live registry), grok
    unavailable; `claude=False` leaves the claude catalog absent, which makes
    every claude lineage row an alias probe (DD-A4)."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    doc = json.loads((CATALOGS / "codex" / "models_cache.json").read_text())
    if codex_mutate:
        codex_mutate(doc)
    (home / ".codex" / "models_cache.json").write_text(json.dumps(doc))
    if claude:
        d = home / ".claude" / "cache" / "model-catalog"
        d.mkdir(parents=True, exist_ok=True)
        _shutil.copy(CATALOGS / "claude" / "catalog-cc.json", d / "catalog-cc.json")
    if grok:
        (home / ".grok").mkdir(exist_ok=True)
        _shutil.copy(CATALOGS / "grok" / "models_cache.json", home / ".grok" / "models_cache.json")
    return home


def sync_env(tmp_path, root, home, bindir, **extra):
    env = env_for(root)
    env.update(HOME=str(home), PATH=f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
               DEEP_MODEL_ROUTER_AUTOUPGRADE="1")
    env.update(extra)
    env.pop("CODEX_HOME", None)
    return env


def new_root(tmp_path, name="state"):
    root = tmp_path / name
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def worker_key() -> str:
    return Policy.of(BASE).id_to_key[route(Task(**TASK), BASE)["selected_model"]]


def passing(key, step=1, superseded=(), new_id=None, base=BASE):
    e = ov_entry(key, new_id or successor(key, step, base), superseded=list(superseded),
                 base=base)
    s = ov_summary(key, e, base)
    s.update(outcome="pass", reason=None, retry_after=None)
    return (key, s, sha_of(s))


def routed(root, home, **task):
    return route(Task(**{**TASK, **task}), env=env_for(root), home=home)


def read_gen(root):
    with model_state.read_state(root) as st:
        return st.shape, st.generation_sha256, st.generation


def work_state(root):
    with secure_io.StateRoot.open(root) as r:
        return r.read_json("work/state.json")


def cli_sync(env, *args, argv=None, **kw):
    return subprocess.run([*(argv or [sys.executable, str(SYNC)]), *args],
                          capture_output=True, text=True,
                          env=env, timeout=kw.pop("timeout", 120), **kw)


# --- the registry the codex capture was taken against ------------------------
#
# The codex catalog under `fixtures/catalogs/codex/` proposes a successor for
# every OpenAI key in `expected-candidates.json`. Once the registry promotes a
# key to exactly that successor, the capture proposes nothing for it, and a
# test about probing, deferring or publishing an OpenAI candidate would have
# nothing to probe. These tests therefore run against a copy of the registry
# with those keys rewound to the id the capture saw — the promoted row with the
# predecessor's id and price, and without the history row the promotion left
# for that id. Claude keys stay live: `fake_home` documents that the claude
# capture proposes nothing, and the quota tests rely on it.

CAPTURE_REWOUND = tuple(sorted(
    k for k in EXPECTED["candidates"] if BASE["models"][k]["family"] == "openai"))

_LAUNCHER = """\
import runpy, sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
import route_task
real = route_task.load_config
route_task.load_config = (
    lambda path=route_task.CONFIG_PATH:
    real(Path({cfg!r}) if path == route_task.CONFIG_PATH else path))
sys.argv = [{sync!r}, *sys.argv[1:]]
runpy.run_path({sync!r}, run_name="__main__")
"""


def captured_base() -> dict:
    base = copy.deepcopy(BASE)
    models = base["models"]
    for key in CAPTURE_REWOUND:
        was = EXPECTED["from_ids"][key]
        if models[key]["id"] == was:
            continue
        # Promoted to exactly the captured successor, or the capture is stale
        # and this rewind would hide it.
        assert models[key]["id"] == EXPECTED["candidates"][key], key
        hist = models.pop(f"{key}@{was}")
        assert hist["history_of"] == key and hist["id"] == was, key
        models[key]["id"] = was
        models[key]["price_per_mtok"] = hist["price_per_mtok"]
    return base


@pytest.fixture
def captured(tmp_path, monkeypatch):
    """The capture-time registry, in process (`load_config`, the cached
    default) and for a `model_sync.py` child (`captured.argv`)."""
    import types
    import route_task
    import yaml
    base = captured_base()
    cfg = tmp_path / "captured-model-routing.yaml"
    cfg.write_text(yaml.safe_dump(base, sort_keys=False))
    assert route_task.load_config(cfg) == base
    real = route_task.load_config
    monkeypatch.setattr(route_task, "load_config",
                        lambda path=route_task.CONFIG_PATH:
                        real(cfg if path == route_task.CONFIG_PATH else path))
    monkeypatch.setattr(route_task, "_DEFAULT_CFG", None)
    launcher = tmp_path / "model_sync_captured.py"
    launcher.write_text(_LAUNCHER.format(scripts=str(SKILL / "scripts"), cfg=str(cfg),
                                         sync=str(SYNC)))
    return types.SimpleNamespace(base=base, argv=[sys.executable, str(launcher)])


# --- publication order, crash points, bootstrap ----------------------------

@pytest.mark.parametrize("stage,moved", [("after_summaries", False),
                                         ("after_generation", False),
                                         ("before_pointer", False),
                                         ("after_pointer", True)])
def test_a_crash_between_publication_steps_leaves_old_or_new_never_a_mixture(
        tmp_path, monkeypatch, stage, moved):
    key = worker_key()
    root = new_root(tmp_path)
    h = tmp_path / "h"
    model_sync.publish_results(root, BASE, [passing(key)])
    shape, g1, _ = read_gen(root)
    before = routed(root, h)

    class Crash(Exception):
        pass

    def fault(name):
        if name == stage:
            raise Crash(name)
    monkeypatch.setattr(model_sync, "_fault", fault)
    k2, s2, sha2 = passing(key, 2, superseded=[successor(key)])
    with pytest.raises(Crash):
        model_sync.publish_results(root, BASE, [(k2, s2, sha2)])
    after = routed(root, h)
    shape, g, gen = read_gen(root)
    assert shape == "ok"
    if moved:
        assert g != g1 and after["selected_model"] == successor(key, 2)
    else:
        assert g == g1 and after == before
        # The generation written before the crash is unreachable, even by pin.
        stray = [n[:-5] for n in os.listdir(root / "committed" / "generations")
                 if n[:-5] != g1]
        for sha in stray:
            with model_state.read_state(root) as st:
                sgen = st.generation_at(sha)
                cfg, _ = model_state.effective_config(BASE, sgen, apply_entries=True,
                                                      summary=st.summary)
            t = Task(**TASK)
            t._policy_pin = canonical_policy_sha256(cfg)
            out = route(t, env=env_for(root), home=h)
            assert out["terminal"] == "MODEL_STATE_UNAVAILABLE"
            assert out["model_overlay"]["state_reason"] == "pin_generation_missing"


def test_publication_installs_the_summary_before_the_pointer(tmp_path, monkeypatch):
    key = worker_key()
    root = new_root(tmp_path)
    seen = []

    def fault(name):
        seen.append((name, sorted(os.listdir(root))))
    monkeypatch.setattr(model_sync, "_fault", fault)
    model_sync.publish_results(root, BASE, [passing(key)])      # bootstrap
    model_sync.publish_results(root, BASE, [passing(key, 2, superseded=[successor(key)])])
    names = [n for n, _ in seen]
    assert names[0] == "before_bootstrap_rename"
    assert names[1:] == ["after_summaries", "after_generation", "before_pointer",
                         "after_pointer"]
    # Nothing but committed/ and work/ is left behind by the bootstrap.
    assert sorted(os.listdir(root)) == ["committed", "work"]
    shape, g, gen = read_gen(root)
    with model_state.read_state(root) as st:
        assert st.summary(gen["entries"][key]["probe_summary_sha256"]) is not None


def test_first_publication_is_an_atomic_bootstrap(tmp_path, monkeypatch):
    key = worker_key()
    root = new_root(tmp_path)
    h = tmp_path / "h"

    def fault(name):
        if name == "before_bootstrap_rename":
            raise RuntimeError("crash")
    monkeypatch.setattr(model_sync, "_fault", fault)
    with pytest.raises(RuntimeError):
        model_sync.publish_results(root, BASE, [passing(key)])
    assert not (root / "committed").exists()
    assert any(n.startswith("committed.tmp-") for n in os.listdir(root))
    assert read_gen(root)[0] == "absent"
    assert routed(root, h)["model_overlay"] is None
    monkeypatch.setattr(model_sync, "_fault", lambda name: None)
    model_sync.publish_results(root, BASE, [passing(key)])
    assert routed(root, h)["selected_model"] == successor(key)


def test_two_writers_publish_once(tmp_path):
    key = worker_key()
    root = new_root(tmp_path)
    result = passing(key)
    out = []
    barrier = threading.Barrier(2)

    def writer():
        barrier.wait()
        out.append(model_sync.publish_results(root, BASE, [result]))
    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([o for o in out if o]) == 1
    assert len(os.listdir(root / "committed" / "generations")) == 1


def test_a_successor_accumulates_history_six_to_seven(tmp_path):
    key = worker_key()
    root = new_root(tmp_path)
    k1, s1, sha1 = passing(key)
    model_sync.publish_results(root, BASE, [(k1, s1, sha1)])
    _, g1, gen1 = read_gen(root)
    sup = model_sync.superseded_for(gen1, key, BASE)
    assert sup == [successor(key)]
    model_sync.publish_results(root, BASE, [passing(key, 2, superseded=sup)])
    _, g2, gen2 = read_gen(root)
    e = gen2["entries"][key]
    assert (e["from_id"], e["id"], e["superseded"]) == (ID_(key), successor(key, 2),
                                                        [successor(key)])
    assert gen2["history"][successor(key)] == probe_record(key, sha1)
    assert gen2["history"][ID_(key)] == base_record(key)
    assert gen2["parent_generation_sha256"] == g1
    out = routed(tmp_path / "state", tmp_path / "h", prior_failures=1,
                 prior_models=[successor(key)])
    assert out["selected_model"] not in (successor(key),)


def ID_(key):
    return BASE["models"][key]["id"]


def test_a_summary_measured_against_another_chain_is_not_published(tmp_path):
    """The probe recorded `superseded` from the generation it saw; a writer
    that moved the chain meanwhile makes it stale — nothing is published."""
    key = worker_key()
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [passing(key)])
    _, g1, _ = read_gen(root)
    assert model_sync.publish_results(root, BASE, [passing(key, 2)]) is None
    assert read_gen(root)[1] == g1


# --- revert / unblock / concurrency with routes ------------------------------

def test_revert_blocks_the_id_and_keeps_its_history(tmp_path):
    key = worker_key()
    root = new_root(tmp_path)
    k, s, sha = passing(key)
    model_sync.publish_results(root, BASE, [(k, s, sha)])
    _, g1, _ = read_gen(root)
    model_sync.revert(root, key, base=BASE)
    _, g2, gen = read_gen(root)
    assert key not in gen["entries"]
    assert gen["blocked_ids"] == [successor(key)]
    assert gen["history"][successor(key)] == probe_record(key, sha)
    assert gen["parent_generation_sha256"] == g1
    h = tmp_path / "h"
    assert routed(root, h)["selected_model"] == ID_(key)
    # the reverted id is valid history input everywhere
    env = env_for(root)
    rid = successor(key)
    for extra in (["--prior-failures", "1", "--prior-models", rid],
                  ["--host-model", rid, "--host-effort", "HIGH", "--runtime",
                   {"openai": "codex", "claude": "claude_code",
                    "xai": "grok"}[BASE["models"][key]["family"]]]):
        proc = subprocess.run([sys.executable, str(ROUTE), "--class", "IMPLEMENTATION",
                               "--complexity", "2", "--uncertainty", "1", "--blast-radius",
                               "1", "--reversibility", "1", "--format", "json", *extra],
                              capture_output=True, text=True, env=env)
        assert proc.returncode in (0, 1), (extra, proc.stderr)
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"route_schema_version": 1, **TASK, "attempt_outcomes": [
        {"attempt_id": "a-1", "model_id": rid, "kind": "capability_failure",
         "evidence_sha256": "a" * 64}]}))
    proc = subprocess.run([sys.executable, str(ROUTE), "--request-json", str(req),
                           "--format", "json"], capture_output=True, text=True, env=env)
    assert proc.returncode in (0, 1), proc.stderr


def test_unblock_reseats_a_reverted_overlay_id(tmp_path):
    key = worker_key()
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [passing(key)])
    model_sync.revert(root, key, base=BASE)
    model_sync.unblock(root, successor(key), base=BASE)
    _, _, gen = read_gen(root)
    assert gen["blocked_ids"] == []
    assert gen["entries"][key]["id"] == successor(key)
    assert successor(key) not in gen["history"]
    assert routed(root, tmp_path / "h")["selected_model"] == successor(key)


def test_unblock_reseats_a_revoked_base_id(tmp_path):
    """After promote the overlay id IS the base id: revert blocks the base
    id itself, unblock lets it sit again."""
    key = worker_key()
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [passing(key)])
    promoted = copy.deepcopy(BASE)
    promoted["models"][key]["id"] = successor(key)
    model_sync.revert(root, key, base=promoted)
    _, _, gen = read_gen(root)
    with model_state.read_state(root) as st:
        cfg, _ = model_state.effective_config(promoted, gen, apply_entries=True,
                                              summary=st.summary)
    out = route(Task(**TASK), cfg)
    assert successor(key) not in {out["selected_model"], *out["review"]["reviewer_models"]}
    model_sync.unblock(root, successor(key), base=promoted)
    _, _, gen = read_gen(root)
    with model_state.read_state(root) as st:
        cfg, _ = model_state.effective_config(promoted, gen, apply_entries=True,
                                              summary=st.summary)
    assert route(Task(**TASK), cfg)["selected_model"] == successor(key)


def test_revert_and_routes_run_concurrently_without_an_unpublished_view(tmp_path):
    """The REAL writers (revert/unblock) against unpinned and pinned routes:
    every answer is a published policy, or the pin's named revocation."""
    key = worker_key()
    root = new_root(tmp_path)
    h = tmp_path / "h"
    model_sync.publish_results(root, BASE, [passing(key)])
    p1 = routed(root, h)["policy_sha256"]
    model_sync.revert(root, key, base=BASE)
    p2 = routed(root, h)["policy_sha256"]
    model_sync.unblock(root, successor(key), base=BASE)
    assert routed(root, h)["policy_sha256"] == p1
    stop = threading.Event()
    errors = []

    def flip():
        try:
            for _ in range(12):
                if stop.is_set():
                    break
                model_sync.revert(root, key, base=BASE)
                model_sync.unblock(root, successor(key), base=BASE)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    t = threading.Thread(target=flip)
    t.start()
    try:
        for _ in range(40):
            assert routed(root, h)["policy_sha256"] in (p1, p2)
            pt = Task(**TASK)
            pt._policy_pin = p1
            out = route(pt, env=env_for(root), home=h)
            if out["terminal"] is None:
                assert out["policy_sha256"] == p1
            else:
                assert out["model_overlay"]["state_reason"] == "pin_revoked"
    finally:
        stop.set()
        t.join()
    assert not errors


def test_revert_without_an_entry_is_refused(tmp_path):
    root = new_root(tmp_path)
    with pytest.raises(model_sync.SyncError):
        model_sync.revert(root, worker_key(), base=BASE)
    with pytest.raises(model_sync.SyncError):
        model_sync.unblock(root, "no-such-id", base=BASE)


def test_a_same_generation_spelling_is_refused_and_the_old_id_stays_history(tmp_path):
    key = worker_key()
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [passing(key)])
    model_sync.revert(root, key, base=BASE)
    _, g, _ = read_gen(root)
    template = BASE["models"][key]["lineage"]["template"]
    variant = template.replace("{gen}", str(
        lineage.parse(template, successor(key)).parts[0]) + ".0")
    assert variant != successor(key)
    assert model_sync.publish_results(root, BASE, [passing(key, new_id=variant)]) is None
    assert read_gen(root)[1] == g
    out = routed(root, tmp_path / "h", prior_failures=1, prior_models=[successor(key)])
    assert out["selected_model"] != variant


# --- repair, fail closed -------------------------------------------------------

def test_repair_leaves_a_sound_pointer_alone(tmp_path):
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [passing(worker_key())])
    _, g, _ = read_gen(root)
    assert model_sync.repair(root)["status"] == "ok"
    assert read_gen(root)[1] == g


def test_repair_never_picks_a_generation_on_a_damaged_pointer(tmp_path):
    key = worker_key()
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [passing(key)])
    _, g1, _ = read_gen(root)
    model_sync.revert(root, key, base=BASE)
    _, g2, gen2 = read_gen(root)
    pointer = root / "committed" / "current.json"
    pointer.write_text('{"generation_sha256": "nope"}')
    os.chmod(pointer, 0o600)
    with pytest.raises(model_sync.SyncError) as exc:
        model_sync.repair(root)
    assert "--to" in str(exc.value)
    assert pointer.read_text() == '{"generation_sha256": "nope"}'
    proc = cli_sync(env_for(root), "repair")
    assert proc.returncode == 1 and g1 in proc.stdout + proc.stderr
    got = model_sync.repair(root, to=g1)
    assert got["generation_sha256"] == g1
    assert got["disappearing_revocations"] == [successor(key)]
    assert read_gen(root)[1] == g1
    got = model_sync.repair(root, to=g2, force=True)     # g1 is sound now: --force
    assert read_gen(root)[2]["blocked_ids"] == gen2["blocked_ids"] == [successor(key)]
    assert got["disappearing_revocations"] == []
    with pytest.raises(model_sync.SyncError):
        model_sync.repair(root, to="f" * 64, force=True)


def test_repair_to_replaces_a_sound_pointer_only_with_force(tmp_path):
    """i1r1 opus F8: `--to` on a sound pointer is a rollback the operator
    must confirm."""
    key = worker_key()
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [passing(key)])
    _, g1, _ = read_gen(root)
    model_sync.revert(root, key, base=BASE)
    _, g2, _ = read_gen(root)
    with pytest.raises(model_sync.SyncError, match="--force"):
        model_sync.repair(root, to=g1)
    assert read_gen(root)[1] == g2
    proc = cli_sync(env_for(root), "repair", "--to", g1)
    assert proc.returncode == 1 and "--force" in proc.stderr
    assert read_gen(root)[1] == g2
    proc = cli_sync(env_for(root), "repair", "--to", g1, "--force")
    assert proc.returncode == 0, proc.stderr
    assert read_gen(root)[1] == g1


def test_repair_flags_an_orphan_generation_and_needs_force_for_it(tmp_path):
    """i1r1 opus F8: a generation no pointer history reaches (left by a crash,
    or by a publication the recheck refused after `disable`) is listed as an
    orphan and never adopted without --force."""
    key = worker_key()
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [passing(key)])
    _, g1, gen1 = read_gen(root)
    model_sync.revert(root, key, base=BASE)
    _, g2, _ = read_gen(root)
    stray = copy.deepcopy(gen1)
    stray["parent_generation_sha256"] = g2
    stray["blocked_ids"] = []
    with secure_io.StateRoot.open(root) as r:
        orphan = model_state.write_generation(r, stray)      # no pointer ever named it
    pointer = root / "committed" / "current.json"
    pointer.write_text('{"generation_sha256": "nope"}')
    os.chmod(pointer, 0o600)
    with pytest.raises(model_sync.SyncError) as exc:
        model_sync.repair(root)
    listing = str(exc.value)
    orphan_line = next(l for l in listing.splitlines() if orphan in l)
    assert "orphan" in orphan_line
    assert "orphan" not in next(l for l in listing.splitlines() if g1 in l and "parent" in l
                                and l.strip().startswith(g1))
    with pytest.raises(model_sync.SyncError, match="orphan"):
        model_sync.repair(root, to=orphan)
    assert pointer.read_text() == '{"generation_sha256": "nope"}'
    got = model_sync.repair(root, to=g1)          # reachable as g2's parent: no --force
    assert got["generation_sha256"] == g1
    assert model_sync.repair(root, to=orphan, force=True)["generation_sha256"] == orphan


def test_deleting_the_pointer_fails_closed_and_deleting_work_state_does_not(tmp_path):
    key = worker_key()
    root = new_root(tmp_path)
    h = tmp_path / "h"
    model_sync.publish_results(root, BASE, [passing(key)])
    model_sync.enable(root)                        # work/state.json exists
    before = routed(root, h)
    (root / "work" / "state.json").unlink()
    assert routed(root, h) == before
    (root / "committed" / "current.json").unlink()
    out = routed(root, h)
    assert out["terminal"] == "MODEL_STATE_UNAVAILABLE"
    assert out["model_overlay"]["state_reason"] == "unreadable"


# --- quota ----------------------------------------------------------------------

def test_quota_takes_the_fullest_window(tmp_path):
    """opus uncertainty: where `primary` is the short window, weekly
    exhaustion lives in `secondary` — the fullest window decides, with its
    own reset time. A null or absent secondary leaves primary alone."""
    home = tmp_path / "home"
    ts = NOW - _dt.timedelta(minutes=5)
    path = rollout(home, 5.0, ts, NOW + _dt.timedelta(hours=3))
    ev = json.loads(path.read_text())
    weekly_reset = int((NOW + _dt.timedelta(days=4)).timestamp())
    ev["payload"]["rate_limits"]["secondary"] = {"used_percent": 97.0, "window_minutes": 10080,
                                                 "resets_at": weekly_reset}
    path.write_text(json.dumps(ev) + "\n")
    q = model_sync.read_quota(home / ".codex" / "sessions", NOW)
    assert (q["status"], q["used_percent"], q["resets_at"], q["window"]) == \
        ("ok", 97.0, weekly_reset, "secondary")
    assert model_sync.quota_defers(q)
    ev["payload"]["rate_limits"]["secondary"]["used_percent"] = 1.0
    path.write_text(json.dumps(ev) + "\n")
    q = model_sync.read_quota(home / ".codex" / "sessions", NOW)
    assert (q["used_percent"], q["window"]) == (5.0, "primary")
    ev["payload"]["rate_limits"]["secondary"] = {"used_percent": "x", "resets_at": 1}
    path.write_text(json.dumps(ev) + "\n")
    q = model_sync.read_quota(home / ".codex" / "sessions", NOW)
    assert (q["status"], q["reason"]) == ("unknown", "shape")


def test_quota_reads_the_last_token_count_event_and_never_runs_codex(tmp_path):
    bindir, fakedir = fake_bin(tmp_path)
    home = tmp_path / "home"
    ts = _dt.datetime(2026, 9, 25, 15, 47, 54, tzinfo=_dt.timezone.utc)
    day = home / ".codex" / "sessions" / "2026" / "09" / "25"
    day.mkdir(parents=True)
    _shutil.copy(CODEX / "codex-rollout-token_count.jsonl",
                 day / "rollout-2026-09-25T15-00-00-fixture.jsonl")
    q = model_sync.read_quota(home / ".codex" / "sessions", ts + _dt.timedelta(minutes=5))
    primary = ROLLOUT_EVENT["payload"]["rate_limits"]["primary"]
    assert q["status"] == "ok" and q["used_percent"] == primary["used_percent"]
    assert q["resets_at"] == primary["resets_at"]
    assert model_sync.quota_defers(q)
    stale = model_sync.read_quota(home / ".codex" / "sessions", ts + _dt.timedelta(hours=7))
    assert stale["status"] == "unknown" and model_sync.quota_defers(stale)
    assert model_sync.read_quota(tmp_path / "none", ts)["status"] == "unknown"
    rollout(home, 12.0, ts + _dt.timedelta(minutes=1), ts + _dt.timedelta(days=2))
    fresh = model_sync.read_quota(home / ".codex" / "sessions", ts + _dt.timedelta(minutes=5))
    assert fresh["used_percent"] == 12.0 and not model_sync.quota_defers(fresh)
    env = sync_env(tmp_path, new_root(tmp_path), home, bindir)
    proc = cli_sync(env, "quota")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["status"] in ("ok", "unknown")
    assert calls_of(fakedir, "codex") == []


def recorder(inferences=None, outcome="failed"):
    calls = []

    def probe(**kw):
        key, cand = kw["key"], kw["candidate"]
        calls.append(key)
        fam = kw["base"]["models"][key]["family"]
        n = model_sync.MAX_INFERENCES[fam] if inferences is None else inferences
        if outcome == "pass":
            e = ov_entry(key, cand["id"], superseded=kw["superseded"], base=kw["base"])
            s = ov_summary(key, e, kw["base"])
            s.update(outcome="pass", reason=None, retry_after=None)
        else:
            s = {"key": key, "id": cand["id"], "outcome": "failed", "reason": "smoke",
                 "retry_after": model_sync.retry_after_for("smoke", kw["ctx"])}
        with secure_io.StateRoot.open(kw["state_root"]) as r:
            sha = model_state.write_summary(r, s, prefix=model_sync.SUMMARY_PREFIX)
        return {"summary": s, "summary_sha256": sha, "inferences": n}
    probe.calls = calls
    return probe


def test_run_enforces_the_inference_budget_before_each_candidate(tmp_path, captured):
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path, claude=True)
    rollout(home, 10.0, NOW - _dt.timedelta(hours=1), NOW + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    probe = recorder()
    # Candidates against the registry the run loads (`captured`): a key
    # already promoted to its captured successor has nothing left to find.
    # The budget is set one short of probing them all, so the pre-candidate
    # check has to cut one — however many candidates remain.
    live = sorted(k for k, v in EXPECTED["candidates"].items()
                  if captured.base["models"][k]["id"] == EXPECTED["from_ids"][k])
    assert len(live) >= 2, live
    budget = min(model_sync.INFERENCE_BUDGET,
                 sum(model_sync.MAX_INFERENCES[BASE["models"][k]["family"]] for k in live) - 1)
    rep = model_sync.run(env=sync_env(tmp_path, root, home, bindir), home=home, now=NOW,
                         probe=probe, budget=budget)
    fams = [BASE["models"][k]["family"] for k in probe.calls]
    assert sum(model_sync.MAX_INFERENCES[f] for f in fams) <= budget
    assert sorted(probe.calls + rep["over_budget"]) == live
    assert rep["over_budget"], rep
    assert rep["inferences"] <= budget


def test_run_defers_openai_on_quota_and_retries_after_the_reset(tmp_path, captured):
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    resets = NOW + _dt.timedelta(hours=30)
    rollout(home, 94.0, NOW - _dt.timedelta(hours=1), resets)
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    probe = recorder()
    rep = model_sync.run(env=env, home=home, now=NOW, probe=probe)
    assert probe.calls == []
    openai = sorted(k for k in EXPECTED["candidates"] if BASE["models"][k]["family"] == "openai")
    neg = work_state(root)["negatives"]
    for key in openai:
        assert neg[key]["reason"] == "quota"
        assert neg[key]["retry_after"] == {
            "kind": "time", "at": _dt.datetime.fromtimestamp(
                int(resets.timestamp()), _dt.timezone.utc).isoformat()}
    assert sorted(rep["deferred"]) == openai
    # Catalogs and CLIs unchanged: the tick stores its hash, then idles…
    spawned = []
    assert model_sync.tick(env=env, home=home, now=NOW,
                           spawn=spawned.append)["status"] == "no_candidates"
    assert model_sync.tick(env=env, home=home, now=NOW,
                           spawn=spawned.append)["status"] == "unchanged"
    # …until the deferral expires.
    later = resets + _dt.timedelta(minutes=1)
    got = model_sync.tick(env=env, home=home, now=later, spawn=spawned.append)
    assert got["status"] == "spawned" and sorted(spawned[-1]) == openai
    # Unknown quota (a record older than 6 h) also defers — 6 h, not a guess.
    rollout(home, 5.0, later - _dt.timedelta(hours=7), later + _dt.timedelta(days=1))
    for p in (home / ".codex" / "sessions").rglob("*.jsonl"):
        if "94" in p.read_text():
            p.unlink()
    rep = model_sync.run(env=env, home=home, now=later, probe=probe)
    assert probe.calls == [] and sorted(rep["deferred"]) == openai
    assert work_state(root)["negatives"][openai[0]]["retry_after"]["at"] == \
        (later + _dt.timedelta(hours=6)).isoformat()
    # A fresh record under the threshold probes.
    rollout(home, 5.0, later, later + _dt.timedelta(days=1))
    after = later + _dt.timedelta(hours=6, minutes=1)
    rollout(home, 5.0, after - _dt.timedelta(minutes=1), after + _dt.timedelta(days=1))
    model_sync.run(env=env, home=home, now=after, probe=probe)
    assert sorted(probe.calls) == openai


def test_quota_deferral_releases_as_soon_as_a_fresh_reading_has_room(tmp_path, captured):
    """A limit can reset earlier than the recorded time (plan change, early
    reset). The recorded reset time must not keep a recovered family held:
    a fresh reading under the threshold releases the deferral on the next
    tick and run, before the recorded time."""
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    resets = NOW + _dt.timedelta(days=2)
    rollout(home, 99.0, NOW - _dt.timedelta(minutes=5), resets)
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    probe = recorder()
    model_sync.run(env=env, home=home, now=NOW, probe=probe)
    assert probe.calls == []
    openai = sorted(k for k in EXPECTED["candidates"] if BASE["models"][k]["family"] == "openai")
    spawned = []
    assert model_sync.tick(env=env, home=home, now=NOW,
                           spawn=spawned.append)["status"] == "no_candidates"
    # an hour later — long before `resets` — a fresh reading shows room
    soon = NOW + _dt.timedelta(hours=1)
    rollout(home, 0.0, soon - _dt.timedelta(minutes=1), soon + _dt.timedelta(days=7))
    got = model_sync.tick(env=env, home=home, now=soon, spawn=spawned.append)
    assert got["status"] == "spawned" and sorted(spawned[-1]) == openai
    model_sync.run(env=env, home=home, now=soon, probe=probe, keys=openai)
    assert sorted(probe.calls) == openai


def test_tick_recomputes_when_only_a_cli_version_changes(tmp_path):
    bindir, fakedir = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    rollout(home, 94.0, NOW, NOW + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    model_sync.run(env=env, home=home, now=NOW, probe=recorder())
    spawned = []
    model_sync.tick(env=env, home=home, now=NOW, spawn=spawned.append)
    assert model_sync.tick(env=env, home=home, now=NOW,
                           spawn=spawned.append)["status"] == "unchanged"
    (fakedir / "claude.version").write_text("9.9.9 (Claude Code)\n")
    assert model_sync.tick(env=env, home=home, now=NOW,
                           spawn=spawned.append)["status"] != "unchanged"
    assert spawned == []


def test_a_tick_whose_run_never_completed_is_due_again(tmp_path, captured):
    """i1r1 grok F3: the hash advances only when the run it started completes.
    A detached run that died before recording anything leaves the same
    catalogs due, not `unchanged`."""
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    rollout(home, 5.0, NOW, NOW + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    model_sync.enable(root)
    spawned = []
    first = model_sync.tick(env=env, home=home, now=NOW, spawn=spawned.append)
    assert first["status"] == "spawned"
    assert work_state(root)["tick_hash"] is None
    # the spawned run died: nothing recorded, so the next tick spawns again
    again = model_sync.tick(env=env, home=home, now=NOW, spawn=spawned.append)
    assert again["status"] == "spawned" and again["tick_hash"] == first["tick_hash"]
    rep = model_sync.run(env=env, home=home, now=NOW, probe=recorder(inferences=1),
                         tick_hash=first["tick_hash"])
    assert rep["status"] == "ok" and not rep["over_budget"], rep
    assert work_state(root)["tick_hash"] == first["tick_hash"]
    assert model_sync.tick(env=env, home=home, now=NOW,
                           spawn=spawned.append)["status"] == "unchanged"
    assert len(spawned) == 2


def test_a_run_cut_short_by_the_budget_does_not_complete_the_tick(tmp_path, captured):
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    rollout(home, 5.0, NOW, NOW + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    model_sync.enable(root)
    got = model_sync.tick(env=env, home=home, now=NOW, spawn=lambda keys: None)
    rep = model_sync.run(env=env, home=home, now=NOW, probe=recorder(), budget=1,
                         tick_hash=got["tick_hash"])
    assert rep["over_budget"], rep
    assert work_state(root)["tick_hash"] is None


@pytest.mark.parametrize("when", ["between_probes", "at_the_recheck"])
def test_a_pass_that_was_never_published_stays_due(tmp_path, monkeypatch, when, captured):
    """i1r2 opus F2: a pass recorded by `_record` but never published (the run
    is disabled, or dies, before the pointer moves) must not start the
    24-hour interval, and the tick it started must not complete — after
    `enable`, the next tick spawns a run for that key again."""
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    rollout(home, 5.0, NOW, NOW + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    model_sync.enable(root)
    spawned = []
    got = model_sync.tick(env=env, home=home, now=NOW, spawn=spawned.append)
    assert got["status"] == "spawned" and len(got["keys"]) >= 2, got
    passer = recorder(inferences=1, outcome="pass")
    if when == "between_probes":
        def probe(**kw):
            if passer.calls:
                model_sync.disable(root)
            return passer(**kw)
    else:
        probe = passer

        def fault(name):
            # inside the publication lock: `disable` would wait on it, so the
            # env switch (read by the same recheck) stands in for it
            if name in ("after_generation", "before_bootstrap_rename"):
                env["DEEP_MODEL_ROUTER_AUTOUPGRADE"] = "0"
        monkeypatch.setattr(model_sync, "_fault", fault)
    rep = model_sync.run(env=env, home=home, now=NOW, probe=probe,
                         tick_hash=got["tick_hash"])
    first = passer.calls[0]
    assert any(r["key"] == first and r["outcome"] == "pass"
               for r in work_state(root)["recent"]), rep
    assert read_gen(root)[0] == "absent", rep
    assert first not in work_state(root)["last_probe"]
    assert work_state(root)["tick_hash"] is None
    monkeypatch.setattr(model_sync, "_fault", lambda name: None)
    env.pop("DEEP_MODEL_ROUTER_AUTOUPGRADE", None)
    model_sync.enable(root)
    later = NOW + _dt.timedelta(hours=1)
    again = model_sync.tick(env=env, home=home, now=later, spawn=spawned.append)
    assert again["status"] == "spawned" and first in again["keys"], again
    # Published, the pass starts the interval like any probe.
    rep = model_sync.run(env=env, home=home, now=later, probe=recorder(
        inferences=1, outcome="pass"), keys=[first])
    assert rep["passed"] == [first] and rep["generation_sha256"], rep
    assert work_state(root)["last_probe"][first] == later.isoformat()
    eligible, skipped = model_sync.plan(
        view=model_sync.committed_view(root, captured.base),
        catalogs=model_sync.load_catalogs(home, model_sync.cli_versions(env)),
        versions=model_sync.cli_versions(env), work=work_state(root),
        now=later + _dt.timedelta(hours=1), keys=[first])
    assert first not in [c["key"] for c in eligible]


def test_a_tick_while_a_run_holds_the_lock_is_busy_and_stores_nothing(tmp_path, captured):
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    rollout(home, 5.0, NOW, NOW + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    model_sync.enable(root)
    spawned = []
    with secure_io.StateRoot.open(root) as r, r.lock(timeout=0, relpath=model_sync.RUN_LOCK):
        got = model_sync.tick(env=env, home=home, now=NOW, spawn=spawned.append)
    assert got["status"] == "busy" and spawned == []
    assert work_state(root)["tick_hash"] is None
    assert model_sync.tick(env=env, home=home, now=NOW,
                           spawn=spawned.append)["status"] == "spawned"


def _alias_setup(tmp_path, *, serve_step=1, answers=None):
    """No claude catalog, a fake claude in `alias` mode that serves the row's
    alias as the id `serve_step` generations above the live one."""
    key = next(k for k, r in sorted(BASE["models"].items())
               if r.get("family") == "claude" and (r.get("lineage") or {}).get("catalog_name")
               and "history_of" not in r and r.get("dispatchable", True))
    alias = BASE["models"][key]["lineage"]["catalog_name"].lower()
    served = successor(key, serve_step) if serve_step else BASE["models"][key]["id"]
    bindir, fakedir = fake_bin(tmp_path, claude="alias")
    (fakedir / "claude.serve").write_text(json.dumps({alias: served}))
    if answers:
        (fakedir / "claude.answers").write_text(json.dumps(answers(alias, served)))
    home = fake_home(tmp_path, claude=False)
    rollout(home, 5.0, NOW, NOW + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    return key, alias, served, fakedir, home, root, sync_env(tmp_path, root, home, bindir)


def test_an_absent_claude_catalog_runs_the_alias_probe_and_publishes_after_the_gates(tmp_path):
    """i1r1 opus F7 / grok F1-F2: with no `*-cc.json`, tick plans the alias
    probe and run EXECUTES it — through dispatch_agent, on the contained
    read-only reviewer argv — and publishes the served successor only after
    P1..P4 pass on the concrete id."""
    key, alias, served, fakedir, home, root, env = _alias_setup(tmp_path)
    spawned = []
    got = model_sync.tick(env=env, home=home, now=NOW, spawn=spawned.append)
    assert got["status"] == "spawned" and key in spawned[-1]
    rep = model_sync.run(env=env, home=home, now=NOW, keys=[key])
    calls = calls_of(fakedir, "claude")
    assert calls, "the alias probe never ran"
    assert calls[0]["args"][calls[0]["args"].index("--model") + 1] == alias
    assert all("--strict-mcp-config" in c["args"] and "plan" in c["args"] for c in calls)
    assert {c["args"][c["args"].index("--model") + 1] for c in calls[1:]} >= {served}
    assert rep["aliases"] == {key: "candidate"} and rep["passed"] == [key], rep
    assert rep["inferences"] == len(calls) <= model_sync.MAX_INFERENCES["claude"] + 1
    shape, _, gen = read_gen(root)
    assert shape == "ok" and gen["entries"][key]["id"] == served


@pytest.mark.parametrize("case", ["alias_not_pong", "no_successor", "candidate_not_pong"])
def test_an_alias_probe_publishes_nothing_unless_every_gate_passes(tmp_path, case):
    answers = {"alias_not_pong": lambda a, s: {a: "nope"},
               "candidate_not_pong": lambda a, s: {s: "nope"}}.get(case)
    key, alias, served, fakedir, home, root, env = _alias_setup(
        tmp_path, serve_step=0 if case == "no_successor" else 1, answers=answers)
    rep = model_sync.run(env=env, home=home, now=NOW, keys=[key])
    calls = calls_of(fakedir, "claude")
    assert all("--strict-mcp-config" in c["args"] for c in calls)
    assert read_gen(root)[0] == "absent", rep
    assert rep["passed"] == [] and rep["generation_sha256"] is None
    if case == "candidate_not_pong":
        assert rep["aliases"] == {key: "candidate"} and len(calls) == 2
        assert work_state(root)["negatives"][key]["id"] == served
    else:
        assert len(calls) == 1
        assert rep["aliases"] == {key: "failed" if case == "alias_not_pong" else "none"}
    if case == "alias_not_pong":
        neg = work_state(root)["negatives"][key]
        assert neg["alias"] == alias and neg["id"] is None and neg["reason"] == "smoke"
        # the deferral holds the alias back until it expires
        assert model_sync.plan(view=model_sync.committed_view(root, BASE),
                               catalogs=model_sync.load_catalogs(home, {"claude": None}),
                               versions={}, work=work_state(root), now=NOW,
                               keys=[key], interval=False)[1][key] == "deferred:smoke"


def test_tick_is_a_no_op_when_auto_upgrade_is_off(tmp_path):
    bindir, fakedir = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    root = tmp_path / "state-never"
    env = sync_env(tmp_path, root, home, bindir, DEEP_MODEL_ROUTER_AUTOUPGRADE="0")
    spawned = []
    assert model_sync.tick(env=env, home=home, now=NOW,
                           spawn=spawned.append)["status"] == "disabled"
    assert not root.exists() and spawned == [] and calls_of(fakedir, "codex") == []
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    model_sync.disable(root)
    assert model_sync.tick(env=env, home=home, now=NOW,
                           spawn=spawned.append)["status"] == "disabled"
    assert model_sync.run(env=env, home=home, now=NOW, probe=recorder())["status"] == "disabled"
    model_sync.enable(root)
    assert work_state(root)["auto_upgrade"] == "enabled"


def test_a_lineage_is_probed_at_most_once_a_day(tmp_path, captured):
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    rollout(home, 5.0, NOW, NOW + _dt.timedelta(days=2))
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    probe = recorder(inferences=1)
    model_sync.run(env=env, home=home, now=NOW, probe=probe)
    first = list(probe.calls)
    assert first
    # The smoke retry (6 h) has expired, the lineage interval (24 h) has not.
    later = NOW + _dt.timedelta(hours=7)
    rollout(home, 5.0, later, later + _dt.timedelta(days=2))
    rep = model_sync.run(env=env, home=home, now=later, probe=probe)
    assert probe.calls == first and sorted(rep["interval"]) == sorted(first)
    # An explicit --key is a manual request: the interval does not apply.
    model_sync.run(env=env, home=home, now=later, probe=probe, keys=[first[0]])
    assert probe.calls == first + [first[0]]


def test_status_reports_state_deferrals_notices_and_summaries(tmp_path, captured):
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    rollout(home, 94.0, NOW, NOW + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    model_sync.publish_results(root, captured.base,
                               [passing(worker_key(), base=captured.base)])
    model_sync.run(env=env, home=home, now=NOW, probe=recorder())
    st = model_sync.status(env=env, home=home, now=NOW)
    _, g, _ = read_gen(root)
    assert st["committed"]["shape"] == "ok" and st["committed"]["generation_sha256"] == g
    assert st["committed"]["entries"][worker_key()]["id"] == \
        successor(worker_key(), base=captured.base)
    assert st["auto_upgrade"] == "enabled"
    assert "retirement_notices" in st
    deferred = st["deferred"]
    assert deferred and all(d["retry_after"]["kind"] == "time" for d in deferred.values())
    model_sync.disable(root)
    assert model_sync.status(env=env, home=home, now=NOW)["auto_upgrade"] == "disabled"
    proc = cli_sync(env, "status", argv=captured.argv)
    assert proc.returncode == 0 and json.loads(proc.stdout)["auto_upgrade"] == "disabled"


# --- end to end through the CLI, fake codex -----------------------------------

def test_run_publishes_a_passing_probe_and_copies_its_summary(tmp_path, captured):
    key = "openai_worker_fast"
    bindir, fakedir = fake_bin(tmp_path, codex="codex")
    home = fake_home(tmp_path)
    now = _dt.datetime.now(_dt.timezone.utc)
    rollout(home, 5.0, now, now + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    proc = cli_sync(env, "run", "--key", key, argv=captured.argv, timeout=600)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    shape, g, gen = read_gen(root)
    assert shape == "ok" and gen["entries"][key]["id"] == _banner_model_of_fixture()
    sha = gen["entries"][key]["probe_summary_sha256"]
    assert (root / "committed" / "summaries" / f"{sha}.json").read_bytes() == \
        (root / "work" / "probes" / "summaries" / f"{sha}.json").read_bytes()
    eff = resolve_effective_policy(env, home)
    assert key in eff.provenance.applied
    assert calls_of(fakedir, "claude") == [] and calls_of(fakedir, "grok") == []
    assert work_state(root)["in_flight"] == []


def _wait(pred, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_disable_during_a_run_cancels_the_attempt_and_publishes_nothing(tmp_path, captured):
    key = "openai_worker_fast"
    bindir, fakedir = fake_bin(tmp_path, codex="sleep")
    home = fake_home(tmp_path)
    now = _dt.datetime.now(_dt.timezone.utc)
    rollout(home, 5.0, now, now + _dt.timedelta(days=1))
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    proc = subprocess.Popen([*captured.argv, "run", "--key", key], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        def running():
            try:
                flight = work_state(root)["in_flight"]
            except (OSError, ValueError):
                return None
            if not flight:
                return None
            a = flight[0]
            path = Path(a["receipt_dir"]) / f"{a['attempt_id']}.json"
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                return None
            return a if rec["result"]["state"] == "RUNNING" else None
        attempt = _wait(running)
        got = cli_sync(env, "disable", argv=captured.argv)
        assert got.returncode == 0, got.stderr
        assert attempt["attempt_id"] in got.stdout
        out, err = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not (root / "committed").exists()
    rec = json.loads((Path(attempt["receipt_dir"]) / f"{attempt['attempt_id']}.json").read_text())
    assert rec["result"]["state"] != "SUCCEEDED"
    st = work_state(root)
    assert st["auto_upgrade"] == "disabled" and st["in_flight"] == []
    assert st["negatives"] == {}


def test_disable_after_the_recheck_waits_for_an_atomic_publication(tmp_path, monkeypatch):
    key = worker_key()
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [passing(key)])
    _, g1, _ = read_gen(root)
    done = threading.Event()
    t = None

    def fault(name):
        nonlocal t
        if name == "before_pointer":
            t = threading.Thread(target=lambda: (model_sync.disable(root), done.set()))
            t.start()
            time.sleep(0.3)
            assert not done.is_set(), "disable must wait for the publication lock"
    monkeypatch.setattr(model_sync, "_fault", fault)
    env = {"DEEP_MODEL_ROUTER_AUTOUPGRADE": "1"}
    model_sync.publish_results(root, BASE, [passing(key, 2, superseded=[successor(key)])],
                               recheck=model_sync.auto_upgrade_recheck(env))
    t.join()
    shape, g2, gen = read_gen(root)
    assert shape == "ok" and g2 != g1 and gen["entries"][key]["id"] == successor(key, 2)
    assert work_state(root)["auto_upgrade"] == "disabled"
    # The recheck refuses once disabled: the pointer does not move.
    monkeypatch.setattr(model_sync, "_fault", lambda name: None)
    assert model_sync.publish_results(
        root, BASE, [passing(key, 3, superseded=[successor(key), successor(key, 2)])],
        recheck=model_sync.auto_upgrade_recheck(env)) is None
    assert read_gen(root)[1] == g2


def test_disable_never_signals_a_raw_pid():
    src = SYNC.read_text()
    assert "os.kill" not in src and "killpg" not in src and "import signal" not in src
    assert '"cancel"' in src


# --- attended probe-maker ----------------------------------------------------------

import probe_maker  # noqa: E402

GROK_KEY = next(k for k in EXPECTED["from_ids"] if BASE["models"][k]["family"] == "xai")


def maker_env(tmp_path, **modes):
    bindir, fakedir = fake_bin(tmp_path, **modes)
    home = fake_home(tmp_path)
    (home / ".grok").mkdir(exist_ok=True)
    (home / ".grok" / "auth.json").write_text("{}")
    root = new_root(tmp_path)
    return sync_env(tmp_path, root, home, bindir), home, root, fakedir


def test_probe_maker_refuses_without_a_tty(tmp_path):
    env, home, root, fakedir = maker_env(tmp_path)
    proc = subprocess.run([sys.executable, str(SYNC), "probe-maker", "openai_worker_fast"],
                          env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 2 and "TTY" in proc.stderr
    assert not (root / "work").exists()
    assert calls_of(fakedir, "codex") == []


def test_probe_maker_runs_nothing_without_a_typed_y(tmp_path):
    import pty
    env, home, root, fakedir = maker_env(tmp_path, codex="maker")
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, str(SYNC), "probe-maker", "openai_worker_fast"],
                            env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    buf = b""
    end = time.monotonic() + 60
    while b"Type y" not in buf and time.monotonic() < end:
        try:
            buf += os.read(master, 4096)
        except OSError:
            break
    assert b"work/probes" in buf, buf
    os.write(master, b"n\n")
    assert proc.wait(timeout=60) == 1
    os.close(master)
    assert calls_of(fakedir, "codex") == []


def _maker(tmp_path, key, env, home, root, **kw):
    return probe_maker.probe_maker(key=key, env=env, home=home, now=NOW, state_path=root,
                                   confirm=lambda plan: True, receipt_guard=False, **kw)


def test_grok_maker_argv_carries_every_required_supervisor_flag(tmp_path):
    import dispatch_agent
    argv = probe_maker.maker_argv(
        cfg=BASE, family="xai", model_id="demo-9", effort_native="low", attempt_id="m-1",
        receipt_dir=Path("/r"), child_cwd=Path("/c"), prompt_file=Path("/p"),
        deadline_seconds=60, grok_home=Path("/gh"), auth_seed=Path("/a.json"),
        session_id="11111111-2222-3333-4444-555555555555", receipt_guard=True)
    sup, child = _split(argv)
    for flag, _ in dispatch_agent.MAKER_SEAT_REQUIRED:
        assert flag in sup, flag
    assert sup[sup.index("--seat-profile") + 1] == dispatch_agent.MAKER_SEAT_PROFILE
    assert sup[sup.index("--expect-sandbox-enforced") + 1] == dispatch_agent.MAKER_SANDBOX_PROFILE
    assert not [a for a in argv if "<" in a and ">" in a], "an unsubstituted placeholder"
    assert any("/gh/sessions/sandbox-events.jsonl" in a for a in child)
    recipe = BASE["transports"]["claude_code"]["to_xai"]["mechanism_maker"]
    assert child[child.index("--sandbox") + 1] == recipe.split("--sandbox ")[1].split()[0]


@pytest.mark.parametrize("flag", [f for f, _ in __import__("dispatch_agent").MAKER_SEAT_REQUIRED])
def test_grok_maker_missing_any_required_flag_is_refused_and_leaves_no_summary(
        tmp_path, monkeypatch, flag):
    env, home, root, fakedir = maker_env(tmp_path, grok="maker")
    real = probe_maker.maker_argv

    def dropped(**kw):
        argv = real(**kw)
        at = argv.index(flag)
        takes_value = flag != "--require-single-linked-cwd"
        del argv[at:at + (2 if takes_value else 1)]
        return argv
    monkeypatch.setattr(probe_maker, "maker_argv", dropped)
    got = _maker(tmp_path, GROK_KEY, env, home, root)
    assert got["status"] == "refused" and got["exit_code"] == 2
    assert calls_of(fakedir, "grok") == []
    assert not (root / "work" / "probes" / "makers").exists()


def test_claude_maker_is_certified_by_content_hash_not_require_artifact(tmp_path):
    env, home, root, fakedir = maker_env(tmp_path, claude="maker")
    key = "claude_senior"
    got = _maker(tmp_path, key, env, home, root)
    s = got["summary"]
    assert got["status"] == "pass", got
    assert s["outcome"] == "pass" and s["id"] == ID_(key)
    assert not [a for a in s["argv"] if a.startswith("--require-artifact")]
    assert s["artifact"]["sha256"] == s["artifact"]["expected_sha256"]
    child = s["argv"][s["argv"].index("--") + 1:]
    ledger = next(e for e in BASE["verification_ledger"]["entries"]
                  if "to_claude.write_verified" in e["item"])
    mode = ledger["argv"].split("--permission-mode ")[1].split()[0]
    assert child[child.index("--permission-mode") + 1] == mode
    stored = (root / "work" / "probes" / "makers" / f"{got['summary_sha256']}.json")
    assert json.loads(stored.read_text()) == s
    assert work_state(root)["makers"][key]["summary_sha256"] == got["summary_sha256"]


def test_openai_maker_is_the_ledger_write_recipe_without_a_guard(tmp_path):
    env, home, root, fakedir = maker_env(tmp_path, codex="maker")
    key = "openai_worker_fast"
    got = probe_maker.probe_maker(key=key, env=env, home=home, now=NOW, state_path=root,
                                  confirm=lambda plan: True)
    assert got["status"] == "pass", got
    argv = got["summary"]["argv"]
    child = argv[argv.index("--") + 1:]
    ledger = next(e for e in BASE["verification_ledger"]["entries"]
                  if "to_openai.write_verified" in e["item"])
    sandbox = ledger["argv"].split(" -s ")[1].split()[0]
    assert child[child.index("-s") + 1] == sandbox
    assert "--receipt-guard" not in argv
    assert calls_of(fakedir, "codex")[-1]["args"] == child[1:]
    if sys.platform == "darwin":
        at = argv.index("--")
        guarded = argv[:at] + ["--receipt-guard", "darwin-sandbox-v1"] + argv[at:]
        guarded[guarded.index("--attempt-id") + 1] = "m-guarded"
        proc = subprocess.run(guarded, capture_output=True, text=True, env=env, timeout=60)
        assert proc.returncode == 2 and "--receipt-guard" in proc.stderr


def test_only_the_attended_command_imports_the_maker_module():
    """The automatic path (tick/run) cannot reach a write recipe: the one
    import of probe_maker sits in the `probe-maker` branch of main()."""
    assert _maker_import_owners(SYNC.read_text()) == (["main"], [])


def _maker_import_owners(source: str):
    """(functions importing probe_maker, module-level imports of it). Both
    spellings count: `import probe_maker` and `from probe_maker import …`
    (i1r1 grok: the ImportFrom form used to slip past this guard)."""
    import ast
    tree = ast.parse(source)

    def imports_maker(node) -> bool:
        if isinstance(node, ast.Import):
            return any(a.name.split(".")[0] == "probe_maker" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            return (node.module or "").split(".")[0] == "probe_maker" or (
                node.module is None and any(a.name == "probe_maker" for a in node.names))
        return False
    owners = [fn.name for fn in ast.walk(tree)
              if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
              for node in ast.walk(fn) if imports_maker(node)]
    top = [n for n in tree.body if imports_maker(n)]
    return owners, top


@pytest.mark.parametrize("planted", [
    "    from probe_maker import maker_argv\n",
    "    import probe_maker\n",
    "    from probe_maker import *\n",
])
def test_the_maker_import_guard_sees_every_import_spelling(planted):
    """Negative verification of the guard above: planting either spelling
    inside `run` is caught."""
    source = SYNC.read_text()
    anchor = "    if autoupgrade_env_off(env):\n        return {\"status\": \"disabled\", \"reason\": \"DEEP_MODEL_ROUTER_AUTOUPGRADE=0\"}\n    home, now = _home(env, home), _utcnow(now)\n    state_path = model_state.state_root_path(env, home)\n    with StateRoot.open(state_path, create=True) as root:\n        lock"
    assert source.count(anchor) == 1
    planted_src = source.replace(anchor, planted.replace("    ", "    ", 1) + anchor)
    owners, top = _maker_import_owners(planted_src)
    assert "run" in owners, owners


# ---------------------------------------------------------------------------
# A12 — promote an overlay entry into the repository (design DD-A7). Always
# on a temporary copy of the repo's config and succession fixture.
# ---------------------------------------------------------------------------

import difflib  # noqa: E402

import yaml  # noqa: E402

REL_CONFIG = Path("skills/model-router/config/model-routing.yaml")
REL_SUCCESSION = Path("skills/model-router/tests/fixtures/id-succession.json")
PRICE = ("input=4.00,output=20.00,cached_input=0.20,"
         "source=https://example.invalid/pricing,verified_on=2026-09-25")


def repo_copy(tmp_path) -> Path:
    repo = tmp_path / "repo"
    for rel in (REL_CONFIG, REL_SUCCESSION):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy(SKILL.parent.parent / rel, repo / rel)
    return repo


def probed(key, **entry_kw):
    """A passing probe summary the way the harness records one."""
    new = successor(key)
    e = ov_entry(key, new, **entry_kw)
    s = ov_summary(key, e)
    child = ["cli", "-m", new]
    s.update(outcome="pass", reason=None, retry_after=None,
             family=BASE["models"][key]["family"], date="2026-09-25", cli_version="9.9.9",
             served_basis="id_accepted" if BASE["models"][key]["family"] == "openai" else "served",
             served_models=[new], input_tokens={"current": 100, "candidate": 110},
             p3_basis="paired",
             effort_results=[{"effort": "MAX", "native": "max", "accepted": True}],
             probes=[{"gate": "P1", "attempt_id": "p-1", "model_id": new,
                      "effort_native": "low", "argv": ["sup", "--", *child],
                      "receipt_sha256": "a" * 64, "state": "SUCCEEDED"},
                     {"gate": "P4", "attempt_id": "p-4", "model_id": new,
                      "effort_native": "max", "argv": ["sup", "--", *child],
                      "receipt_sha256": "b" * 64, "state": "SUCCEEDED"}])
    return (key, s, sha_of(s))


def promote(repo, root, key, price=PRICE):
    return model_sync.promote(repo=repo, key=key, price=price, state_path=root)


def _block(lines, key):
    """Line range of `key`'s row block in the original config (test's own
    reading: header to the next two-space-indented line)."""
    head = next(i for i, l in enumerate(lines) if l.rstrip() in (f"  {key}:", f'  "{key}":'))
    end = next(i for i in range(head + 1, len(lines)) if re.match(r"^  \S", lines[i]))
    return head, end


import re  # noqa: E402


def _whole(needle, text):
    return re.search(r"(^|[^A-Za-z0-9._-])" + re.escape(needle) + r"([^A-Za-z0-9._-]|$)",
                     text) is not None


@pytest.mark.parametrize("key", ["claude_senior", "openai_reasoning"])
def test_promote_rewrites_the_row_and_appends_history_ledger_and_chain(tmp_path, key):
    repo = repo_copy(tmp_path)
    root = new_root(tmp_path)
    model_sync.publish_results(root, BASE, [probed(key)])
    before = (repo / REL_CONFIG).read_text()
    succ_before = json.loads((repo / REL_SUCCESSION).read_text())
    rep = promote(repo, root, key)
    after = (repo / REL_CONFIG).read_text()
    cfg = yaml.safe_load(after)
    Policy.of(cfg)                                              # the Policy checks pass
    new, old = successor(key), ID_(key)
    row = cfg["models"][key]
    assert row["id"] == new
    assert row["price_per_mtok"] == {"input": 4.0, "output": 20.0, "cached_input": 0.2,
                                     "source": "https://example.invalid/pricing",
                                     "verified_on": "2026-09-25"}
    hist = cfg["models"][f"{key}@{old}"]
    rec = base_record(key)
    assert hist["id"] == old and hist["history_of"] == key and hist["dispatchable"] is False
    assert (hist["family"], hist["capability_tier"]) == (rec["family"], rec["capability_tier"])
    assert hist.get("effort_ceiling") == rec["effort_ceiling"]
    assert hist.get("effort_map", {}) == rec["effort_map"]
    # Bytes outside the row block (and its appended rows) are untouched.
    a, b = before.splitlines(), after.splitlines()
    head, end = _block(a, key)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            assert head <= i1 and i2 <= end, (tag, a[i1:i2])
    # No comment is lost. Counted, not filtered by membership: once the repo
    # holds a promoted history row, the appended row's comment lines already
    # occur in `a`, so a membership filter would count them twice.
    from collections import Counter
    ca = Counter(l for l in a if l.lstrip().startswith("#"))
    cb = Counter(l for l in b if l.lstrip().startswith("#"))
    assert not (ca - cb), ca - cb
    # Ledger rows.
    items = {e["item"]: e for e in cfg["verification_ledger"]["entries"]}
    assert items[f"{new} model id"]["status"] == "verified"
    efforts = items[f"{new} reasoning-effort values"]
    assert efforts["status"] == "verified" and "low" in efforts["evidence"] \
        and "max" in efforts["evidence"]
    price_row = items[f"{new} pricing and context window"]
    assert price_row["status"] == "documented"
    assert price_row["source"] == "https://example.invalid/pricing"
    quality = items[f"{key} quality evidence after the {new} id bump"]
    assert quality["status"] == "quality_inherited_not_remeasured"
    # A list of rows that exist and name the superseded id — the shape the
    # docs guard (test_docs) follows, not a bare id it would iterate by char.
    sup = quality["supersedes"]
    assert isinstance(sup, list) and sup
    assert all(item in items and _whole(old, item) for item in sup), sup
    assert items[f"{new} maker seat"]["status"] == "maker_not_reprobed"
    if BASE["models"][key]["family"] == "openai":
        assert "id accepted" in items[f"{new} model id"]["evidence"]
    # Succession chain appended; prior_failure_exit left to a person.
    succ = json.loads((repo / REL_SUCCESSION).read_text())
    assert succ["chains"][key][-2:] == [old, new]
    assert succ["prior_failure_exit"] == succ_before["prior_failure_exit"]
    assert f"{key}@{old}" in rep["prior_failure_exit_hint"]
    assert rep["checklist"]
    # The state entry is now a promoted no-op.
    _, _, gen = read_gen(root)
    with model_state.read_state(root) as st:
        _, prov = model_state.effective_config(cfg, gen, apply_entries=True, summary=st.summary)
    assert {"key": key, "reason": "promoted"} in prov.noop


def test_promote_without_a_price_stops(tmp_path):
    repo = repo_copy(tmp_path)
    root = new_root(tmp_path)
    key = "claude_senior"
    model_sync.publish_results(root, BASE, [probed(key)])
    before = (repo / REL_CONFIG).read_bytes()
    for bad in (None, "", "input=1,output=2,cached_input=0.1,verified_on=2026-09-25",
                "input=1,output=2,cached_input=0.1,source=http://x,verified_on=2026-09-25",
                "input=x,output=2,cached_input=0.1,source=https://x,verified_on=2026-09-25",
                "input=1,output=2,source=https://x,verified_on=2026-09-25",
                "input=1,output=2,cached_input=0.1,source=https://x,verified_on=today"):
        with pytest.raises(model_sync.SyncError):
            promote(repo, root, key, price=bad)
    assert (repo / REL_CONFIG).read_bytes() == before
    env = env_for(root)
    proc = cli_sync(env, "promote", "--repo", str(repo), "--key", key)
    assert proc.returncode == 2 and "--price" in proc.stderr


def test_promote_refuses_an_effort_change_in_this_release(tmp_path):
    repo = repo_copy(tmp_path)
    root = new_root(tmp_path)
    key = "claude_senior"
    assert BASE["models"][key].get("effort_ceiling") is None
    model_sync.publish_results(root, BASE, [probed(key, ceiling="HIGH")])
    before = (repo / REL_CONFIG).read_bytes()
    with pytest.raises(model_sync.SyncError, match="effort"):
        promote(repo, root, key)
    assert (repo / REL_CONFIG).read_bytes() == before


def test_promote_refuses_a_key_without_an_applied_entry(tmp_path):
    repo = repo_copy(tmp_path)
    root = new_root(tmp_path)
    with pytest.raises(model_sync.SyncError):
        promote(repo, root, "claude_senior")


def test_promote_writes_a_verified_maker_row_from_an_attended_summary(tmp_path):
    repo = repo_copy(tmp_path)
    root = new_root(tmp_path)
    key = "claude_senior"
    model_sync.publish_results(root, BASE, [probed(key)])
    maker = {"kind": "maker", "attended": True, "key": key, "family": "claude",
             "id": successor(key), "transport_id": "codex.to_claude", "recipe_key": "mechanism",
             "argv": ["sup", "--", "claude", "-p"], "attempt_id": "m-1",
             "receipt_sha256": "c" * 64, "state": "SUCCEEDED", "outcome": "pass",
             "artifact": {"sha256": "d" * 64, "expected_sha256": "d" * 64},
             "cli_version": "9.9.9", "date": "2026-09-25"}
    with secure_io.StateRoot.open(root) as r:
        sha = model_state.write_addressed(r, model_sync.MAKERS_PREFIX, maker)
    model_sync.update_work_state(root, lambda st: st["makers"].__setitem__(
        key, {"id": successor(key), "summary_sha256": sha, "outcome": "pass"}))
    promote(repo, root, key)
    cfg = yaml.safe_load((repo / REL_CONFIG).read_text())
    row = next(e for e in cfg["verification_ledger"]["entries"]
               if e["item"] == f"{successor(key)} maker seat")
    assert row["status"] == "verified" and row["attempt_id"] == "m-1"
    assert "codex.to_claude" in row["evidence"]


def test_the_succession_fixture_round_trips_through_its_writer():
    text = (SKILL / "tests" / "fixtures" / "id-succession.json").read_text()
    assert model_sync.dump_succession(json.loads(text)) == text
