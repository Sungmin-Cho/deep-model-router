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
