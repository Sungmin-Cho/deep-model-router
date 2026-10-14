"""model_sync catalog readers for the gemini and meta lines (design 2026-10-14
DD-61a/DD-61b, issue #61).

agy publishes its catalogue only through `agy models`, a network call, so the
tick stays offline: it decides from local facts whether the gemini row can be
fetched at all, and `run` fetches the list once and asks again. muse keeps an
account catalogue on disk, but its stream carries no token usage, so a newer
muse id is reported and never published. The fixtures under
fixtures/catalogs/agy and fixtures/catalogs/muse are real captures (agy 1.3.5,
Muse Code 1.4.7). Nothing here runs a real CLI or a model.

Run:  python3 -m pytest skills/model-router/tests/test_catalog_readers.py -q
"""

import copy
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))

import dispatch_agent  # noqa: E402
import model_sync  # noqa: E402
from route_task import load_config  # noqa: E402
from test_gemini_seat import _home as agy_home  # noqa: E402
from test_model_sync import (NOW, calls_of, fake_bin, fake_home, new_root,  # noqa: E402
                             read_gen, sync_env, work_state)

CFG = load_config()
GEMINI_KEY, META_KEY = "gemini_worker_balanced", "meta_worker_balanced"
GEMINI, META = CFG["models"][GEMINI_KEY]["id"], CFG["models"][META_KEY]["id"]
CATALOGS = SKILL / "tests" / "fixtures" / "catalogs"
AGY_LIST = (CATALOGS / "agy" / "agy-models-1.3.5.stdout.txt").read_text()
MUSE_DOC = json.loads((CATALOGS / "muse" / "6d657461__p746268.json").read_text())
NEWER = "gemini-3.9-flash"


def newer_list(*extra_lines):
    lines = [f"{NEWER}-high\tGemini 3.9 Flash (High)", f"{NEWER}-medium\tGemini 3.9 Flash (Medium)",
             f"{NEWER}-low\tGemini 3.9 Flash (Low)", *extra_lines]
    return AGY_LIST + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The agy list
# ---------------------------------------------------------------------------

def test_the_captured_agy_list_is_read_into_base_ids_and_efforts():
    cat = model_sync.parse_agy_models_text(AGY_LIST)
    assert cat.status == "ok" and cat.family == "gemini"
    assert cat.models[GEMINI]["efforts"] == ["high", "medium", "low"]
    # other vendors are listed (and only the template keeps them out)
    assert cat.models["claude-sonnet-4-6"]["efforts"] == []
    assert cat.models["gpt-oss-120b"]["efforts"] == ["medium"]
    assert len(cat.listed_ids) == 6


def test_a_newer_flash_generation_is_the_candidate_and_nothing_else_is():
    text = newer_list("gemini-3.9-pro-high\tGemini 3.9 Pro (High)",
                      "gemini-4-flash-lite-high\tGemini 4 Flash Lite (High)",
                      "claude-opus-9-high\tClaude Opus 9")
    cat = model_sync.parse_agy_models_text(text)
    found = model_sync.find_candidates({GEMINI_KEY: CFG["models"][GEMINI_KEY]}, {"gemini": cat})
    assert found[GEMINI_KEY]["status"] == "candidate"
    assert found[GEMINI_KEY]["id"] == NEWER
    assert sorted(found[GEMINI_KEY]["efforts"]) == ["high", "low", "medium"]


def test_the_current_list_proposes_nothing():
    cat = model_sync.parse_agy_models_text(AGY_LIST)
    found = model_sync.find_candidates({GEMINI_KEY: CFG["models"][GEMINI_KEY]}, {"gemini": cat})
    assert found[GEMINI_KEY]["status"] == "none"


@pytest.mark.parametrize("text, reason", [
    (None, "no catalog output"),
    ("", "empty"),
    ("\n\n", "empty"),
    (AGY_LIST + "Fetching available models...\n", "shape"),
    (AGY_LIST + "gemini-9-flash-high Gemini 9 Flash\n", "shape"),
])
def test_an_agy_list_it_does_not_understand_is_discovery_unavailable(text, reason):
    cat = model_sync.parse_agy_models_text(text)
    assert cat.status == "discovery_unavailable" and cat.reason == reason


# ---------------------------------------------------------------------------
# The tick decides from offline facts only
# ---------------------------------------------------------------------------

FAKE_AGY = r'''#!{python}
import json, os, re, sys
from pathlib import Path
d = Path({fakedir!r})
args = sys.argv[1:]
with (d / "agy.calls").open("a") as f:
    f.write(json.dumps({{"args": args}}) + "\n")
if args == ["--version"]:
    sys.stdout.write("1.3.5\n"); sys.exit(0)
if args == ["models"]:
    sys.stderr.write("Fetching available models...\n")
    mode = (d / "agy.list").read_text() if (d / "agy.list").exists() else None
    if mode is None:
        sys.exit(1)
    sys.stdout.write(mode); sys.exit(0)
log = args[args.index("--log-file") + 1]
model = args[args.index("--model") + 1]
effort = args[args.index("--effort") + 1]
prompt = args[args.index("-p") + 1]
with open(log, "a") as f:
    f.write('I1014 11:00:00.000000       1 model_resolver.go:155] model alias "%s" resolved to "%s-%s"\n'
            % (model, model, effort))
    f.write('I1014 11:00:01.000000       1 cli_setting_manager.go:211] '
            'ApplyProjectPermissionGrants: stored 0 allow, 5 deny grants from project "dmr-reviewer-v1"\n')
answer = "pong" if "pong" in prompt else "?"
sys.stdout.write(json.dumps({{"conversation_id": "c", "status": "SUCCESS", "response": answer,
    "duration_seconds": 1.0, "num_turns": 1,
    "usage": {{"input_tokens": 6000, "output_tokens": 1, "thinking_tokens": 0,
              "cache_read_tokens": 0, "total_tokens": 6001}}}}))
'''


def with_agy(tmp_path, *, listing=None, host=True):
    bindir, fakedir = fake_bin(tmp_path)
    exe = bindir / "agy"
    exe.write_text(FAKE_AGY.format(python=sys.executable, fakedir=str(fakedir)))
    exe.chmod(0o755)
    if listing is not None:
        (fakedir / "agy.list").write_text(listing)
    home = fake_home(tmp_path)
    if host:
        agy_home(tmp_path)          # the reviewer agent, project and settings under tmp/home
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    return bindir, fakedir, home, root, env


def agy_calls(fakedir):
    path = fakedir / "agy.calls"
    return [json.loads(l)["args"] for l in path.read_text().splitlines()] if path.exists() else []


def test_without_agy_on_path_the_row_is_unavailable_and_nothing_is_fetched(tmp_path):
    bindir, fakedir = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    cat = model_sync.gemini_placeholder({"PATH": str(bindir)}, home)
    assert (cat.status, cat.reason) == ("discovery_unavailable", "no_cli")


def test_a_host_without_the_reviewer_seat_files_is_never_fetched(tmp_path):
    _, fakedir, home, root, env = with_agy(tmp_path, listing=newer_list(), host=False)
    cat = model_sync.gemini_placeholder(env, home)
    assert (cat.status, cat.reason) == ("discovery_unavailable", "seat_setup")
    rep = model_sync.run(env=env, home=home, now=NOW, keys=[GEMINI_KEY])
    assert rep["skipped"][GEMINI_KEY] == "discovery_unavailable"
    assert ["models"] not in agy_calls(fakedir)


def test_the_tick_plans_a_fetch_and_never_runs_agy_models(tmp_path):
    _, fakedir, home, root, env = with_agy(tmp_path, listing=newer_list())
    spawned = []
    got = model_sync.tick(env=env, home=home, now=NOW, spawn=spawned.append)
    assert got["status"] == "spawned" and GEMINI_KEY in spawned[-1]
    assert all(args == ["--version"] for args in agy_calls(fakedir)), agy_calls(fakedir)
    assert model_sync.status(env=env, home=home, now=NOW)["candidates"][GEMINI_KEY] == "catalog_fetch"


def test_cli_versions_reads_agy_and_muse_without_letting_muse_update(tmp_path):
    bindir, fakedir = fake_bin(tmp_path)
    (bindir / "agy").write_text(FAKE_AGY.format(python=sys.executable, fakedir=str(fakedir)))
    (bindir / "agy").chmod(0o755)
    muse = bindir / "muse"
    muse.write_text(f"#!{sys.executable}\nimport os, sys\n"
                    f"open({str(fakedir / 'muse.env')!r}, 'w').write(os.environ.get('MUSE_NO_AUTO_UPDATE', ''))\n"
                    "print('Muse Code 1.4.7 (1.4.7-R6101.1)')\n")
    muse.chmod(0o755)
    got = model_sync.cli_versions({"PATH": str(bindir)})
    assert got["agy"] == "1.3.5" and got["muse"] == "1.4.7"
    assert (fakedir / "muse.env").read_text() == "1"
    assert model_sync.FAMILY_CLI["gemini"] == "agy" and model_sync.FAMILY_CLI["meta"] == "muse"


# ---------------------------------------------------------------------------
# run: one fetch, then the key is asked again; every outcome stamps the interval
# ---------------------------------------------------------------------------

def test_a_failed_fetch_is_a_deferral_with_no_id_and_stamps_the_interval(tmp_path):
    _, fakedir, home, root, env = with_agy(tmp_path, listing=None)
    rep = model_sync.run(env=env, home=home, now=NOW, keys=[GEMINI_KEY])
    assert rep["failed"] == [GEMINI_KEY] and rep["catalog_fetch"]["status"] == "discovery_unavailable"
    st = work_state(root)
    neg = st["negatives"][GEMINI_KEY]
    assert neg["id"] is None and neg["reason"] == "catalog_fetch"
    assert neg["retry_after"]["kind"] == "time"
    assert st["last_probe"][GEMINI_KEY] == NOW.isoformat()
    assert [a for a in agy_calls(fakedir) if a != ["--version"]] == [["models"]]
    # and the next plan holds it back until the deferral expires
    view = model_sync.committed_view(root, CFG)
    cats = model_sync.load_catalogs(home, {}, env=env)
    _, skipped = model_sync.plan(view=view, catalogs=cats, versions={}, work=st, now=NOW,
                                 keys=[GEMINI_KEY], interval=False)
    assert skipped[GEMINI_KEY] == "deferred:catalog_fetch"


def test_a_list_with_nothing_newer_records_none_and_probes_nothing(tmp_path):
    _, fakedir, home, root, env = with_agy(tmp_path, listing=AGY_LIST)
    rep = model_sync.run(env=env, home=home, now=NOW, keys=[GEMINI_KEY])
    assert rep["skipped"][GEMINI_KEY] == "none" and rep["inferences"] == 0
    assert [a for a in agy_calls(fakedir) if a != ["--version"]] == [["models"]]
    st = work_state(root)
    assert st["last_probe"][GEMINI_KEY] == NOW.isoformat()
    assert st["recent"][0]["outcome"] == "none"
    # the interval now holds the next unattended plan back
    view = model_sync.committed_view(root, CFG)
    cats = model_sync.load_catalogs(home, {}, env=env)
    _, skipped = model_sync.plan(view=view, catalogs=cats, versions={}, work=st, now=NOW)
    assert skipped[GEMINI_KEY] == "interval"


def test_a_fetched_id_under_a_live_deferral_is_held_without_inference(tmp_path):
    _, fakedir, home, root, env = with_agy(tmp_path, listing=newer_list())

    def defer(st):
        st["negatives"][GEMINI_KEY] = {
            "id": NEWER, "family": "gemini", "outcome": "failed", "reason": "smoke",
            "retry_after": {"kind": "time", "at": (NOW + dt.timedelta(hours=6)).isoformat()},
            "summary_sha256": None, "at": NOW.isoformat()}
    model_sync.update_work_state(root, defer)
    rep = model_sync.run(env=env, home=home, now=NOW, keys=[GEMINI_KEY])
    assert rep["skipped"][GEMINI_KEY] == "held" and rep["inferences"] == 0
    st = work_state(root)
    assert st["negatives"][GEMINI_KEY]["id"] == NEWER      # the deferral survives
    assert st["recent"][0]["outcome"] == "held"
    assert all(a in (["--version"], ["models"]) for a in agy_calls(fakedir))


def test_a_newer_generation_is_probed_through_the_reviewer_profile_and_published(tmp_path):
    _, fakedir, home, root, env = with_agy(tmp_path, listing=newer_list())
    rep = model_sync.run(env=env, home=home, now=NOW, keys=[GEMINI_KEY])
    assert rep["passed"] == [GEMINI_KEY], rep
    probes = [a for a in agy_calls(fakedir) if "-p" in a]
    assert probes and rep["inferences"] == len(probes) <= model_sync.MAX_INFERENCES["gemini"]
    for args in probes:
        assert args[args.index("--agent") + 1] == dispatch_agent.AGY_REVIEWER_AGENT
        assert args[args.index("--project") + 1] == dispatch_agent.AGY_REVIEWER_PROJECT
        assert "--mode" not in args and "--add-dir" not in args
    shape, _, gen = read_gen(root)
    assert shape == "ok" and gen["entries"][GEMINI_KEY]["id"] == NEWER
    assert GEMINI in gen["history"]
    summary = json.loads((root / "committed" / "summaries" /
                          f"{gen['entries'][GEMINI_KEY]['probe_summary_sha256']}.json").read_text())
    assert summary["served_basis"] == "resolved"
    assert summary["served_models"] == [f"{NEWER}-low"]
    assert summary["p3_basis"] == "paired"


def test_promote_turns_the_gemini_entry_into_a_history_row_and_resolution_wording(tmp_path):
    from test_model_sync import REL_CONFIG, REL_SUCCESSION, repo_copy
    import yaml
    _, fakedir, home, root, env = with_agy(tmp_path, listing=newer_list())
    rep = model_sync.run(env=env, home=home, now=NOW, keys=[GEMINI_KEY])
    assert rep["passed"] == [GEMINI_KEY], rep
    repo = repo_copy(tmp_path)
    out = model_sync.promote(repo=repo, key=GEMINI_KEY, state_path=root, price=(
        "input=0.75,output=3.75,cached_input=0.075,source=https://example.invalid/p,"
        "verified_on=2026-10-14"))
    assert out["history_rows"] == [f"{GEMINI_KEY}@{GEMINI}"]
    cfg = yaml.safe_load((repo / REL_CONFIG).read_text())
    assert cfg["models"][GEMINI_KEY]["id"] == NEWER
    assert cfg["models"][f"{GEMINI_KEY}@{GEMINI}"]["dispatchable"] is False
    rows = {r["item"]: r for r in cfg["verification_ledger"]["entries"]}
    text = rows[f"{NEWER} model id"]["evidence"]
    assert "resolved the requested id" in text and "served forms" not in text
    assert json.loads((repo / REL_SUCCESSION).read_text())["chains"][GEMINI_KEY] == [GEMINI, NEWER]


# ---------------------------------------------------------------------------
# The gates for an agy candidate
# ---------------------------------------------------------------------------

def _receipt(resolved, usage=None):
    return {"output_envelope": "agy-print-json-v1", "model_id": NEWER,
            "result": {"state": "SUCCEEDED", "termination_confirmed": True,
                       "envelope": {"usage": usage}, "agy_project_grants": {
                           "resolved_model": resolved}}}


@pytest.mark.parametrize("resolved, outcome", [
    (NEWER, "pass"), (f"{NEWER}-low", "pass"), (f"{NEWER}-high", "pass"),
    (f"{NEWER}-lite", "failed"), (f"{NEWER}-xhigh", "failed"), (GEMINI, "failed"),
    (None, "deferred")])
def test_p2_admits_exactly_the_declared_served_forms(resolved, outcome):
    lin = CFG["models"][GEMINI_KEY]["lineage"]
    ctx = model_sync.ProbeContext(now=NOW)
    got = model_sync.gate_p2(_receipt(resolved), lin, ctx)
    assert got["outcome"] == outcome
    if outcome == "pass":
        assert got["basis"] == "resolved"


def test_p3_counts_agy_cache_reads_into_the_boot_input():
    r = _receipt(NEWER, usage={"input_tokens": 6000, "cache_read_tokens": 1500})
    assert model_sync.boot_input_tokens(r) == 7500


def test_the_gemini_probe_argv_is_the_reviewer_recipe_with_the_prompt_in_argv(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text(model_sync.P1_PROMPT)
    argv = model_sync.probe_argv(family="gemini", model_id=NEWER, effort_native="low",
                                 attempt_id="a-1", receipt_dir=tmp_path / "r",
                                 child_cwd=tmp_path / "cwd", prompt_file=prompt,
                                 deadline_seconds=60, agy_log=tmp_path / "a-1.agy.log",
                                 receipt_guard=False)
    sup, child = argv[:argv.index("--")], argv[argv.index("--") + 1:]
    assert "--prompt-file" not in sup
    for flag, value in (("--seat-profile", "agy-reviewer-v1"), ("--output-schema", "none"),
                        ("--output-envelope", "agy-print-json-v1"),
                        ("--transport-id", "model_sync.to_gemini"), ("--seat", "probe"),
                        ("--runtime", "model_sync"), ("--agy-log", str(tmp_path / "a-1.agy.log"))):
        assert sup[sup.index(flag) + 1] == value, flag
    assert dispatch_agent._agy_argv_problem(child, tmp_path / "a-1.agy.log", NEWER) is None
    assert child[child.index("-p") + 1] == model_sync.P1_PROMPT


# ---------------------------------------------------------------------------
# The supervisor admits a schema-less reviewer-profile run for that probe only
# ---------------------------------------------------------------------------

def _preflight(tmp_path, *, runtime, seat, transport, schema="none"):
    home = agy_home(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    log = tmp_path / f"{runtime}-{seat}.agy.log"
    child = ["agy", "--agent", "dmr-reviewer-v1", "--model", "gemini-3.8-flash", "--effort", "low",
             "--project", "dmr-reviewer-v1", "--disable-slash-commands", "--output-format", "json",
             "--print-timeout", "0s", "--log-file", str(log), "-p", "Reply with exactly: pong"]
    env = {**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "dispatch_agent.py"), "run",
         "--attempt-id", f"x-{runtime}-{seat}", "--receipt-dir", str(tmp_path / "r"),
         "--deadline-seconds", "30", "--seat", seat, "--runtime", runtime,
         "--model-id", "gemini-3.8-flash", "--effort-native", "low",
         "--transport-id", transport, "--output-schema", schema,
         "--output-envelope", "agy-print-json-v1", "--seat-profile", "agy-reviewer-v1",
         "--agy-log", str(log), "--child-cwd", str(ws), "--", *child],
        capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.parametrize("runtime, seat, transport", [
    ("claude_code", "reviewer-1", "claude_code.to_gemini"),
    ("model_sync", "reviewer-1", "model_sync.to_gemini"),
    ("model_sync", "probe", "claude_code.to_gemini"),
    ("claude_code", "probe", "model_sync.to_gemini"),
])
def test_a_schema_less_reviewer_run_is_refused_outside_the_model_sync_probe(
        tmp_path, runtime, seat, transport):
    proc = _preflight(tmp_path, runtime=runtime, seat=seat, transport=transport)
    assert proc.returncode == 2 and "--output-schema review" in proc.stderr, proc.stderr


def test_the_model_sync_probe_passes_the_schema_preflight(tmp_path):
    proc = _preflight(tmp_path, runtime="model_sync", seat="probe",
                      transport="model_sync.to_gemini")
    # the spawn itself fails (no agy on this PATH), but never on the schema rule
    assert "--output-schema review" not in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------
# muse: discovered, never published
# ---------------------------------------------------------------------------

def muse_dir(tmp_path, doc=MUSE_DOC, name="6d657461__p746268.json"):
    d = tmp_path / "home" / ".local" / "share" / "muse" / "model-catalog"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(doc))
    return d


def test_the_captured_muse_catalog_keeps_visible_rows_only(tmp_path):
    cat = model_sync.read_muse_catalog(muse_dir(tmp_path))
    assert cat.status == "ok"
    assert sorted(cat.models) == ["muse-spark-1.2", "muse-spark-1.3"]
    assert cat.models["muse-spark-1.3"]["efforts"] == ["minimal", "low", "medium", "high",
                                                        "xhigh", "max"]
    assert cat.excluded["muse-spark-1.3-contributor"] == "hidden"
    found = model_sync.find_candidates({META_KEY: CFG["models"][META_KEY]}, {"meta": cat})
    assert found[META_KEY]["status"] == "none"


def newer_muse(visibility="visible"):
    doc = copy.deepcopy(MUSE_DOC)
    row = copy.deepcopy(doc["rows"][0])
    row.update(model_id="muse-spark-1.4", display_label="muse-spark-1.4", visibility=visibility)
    doc["rows"].insert(0, row)
    contributor = copy.deepcopy(row)
    contributor.update(model_id="muse-spark-1.5-contributor", visibility="visible")
    doc["rows"].append(contributor)
    return doc


def test_a_newer_visible_spark_is_a_candidate_and_contributor_rows_never_are(tmp_path):
    cat = model_sync.read_muse_catalog(muse_dir(tmp_path, newer_muse()))
    found = model_sync.find_candidates({META_KEY: CFG["models"][META_KEY]}, {"meta": cat})
    assert found[META_KEY]["status"] == "candidate" and found[META_KEY]["id"] == "muse-spark-1.4"
    hidden = model_sync.read_muse_catalog(muse_dir(tmp_path / "h", newer_muse("hidden")))
    assert model_sync.find_candidates({META_KEY: CFG["models"][META_KEY]},
                                      {"meta": hidden})[META_KEY]["status"] == "none"


@pytest.mark.parametrize("mutate, status, reason", [
    (lambda d: d.update(schema_version=3), "discovery_unavailable", "shape"),
    (lambda d: d["rows"][0].pop("visibility"), "discovery_unavailable", "missing_discriminator"),
    (lambda d: d["rows"][0].update(reasoning_effort_variants=[{"x": 1}]),
     "discovery_unavailable", "shape"),
])
def test_a_muse_catalog_it_does_not_understand_is_discovery_unavailable(tmp_path, mutate,
                                                                         status, reason):
    doc = copy.deepcopy(MUSE_DOC)
    mutate(doc)
    cat = model_sync.read_muse_catalog(muse_dir(tmp_path, doc))
    assert (cat.status, cat.reason) == (status, reason)


def test_two_meta_profiles_or_none_at_all_are_not_guessed_between(tmp_path):
    d = muse_dir(tmp_path)
    shutil.copy(d / "6d657461__p746268.json", d / "6d657461__p0000.json")
    assert model_sync.read_muse_catalog(d).reason == "ambiguous"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert model_sync.read_muse_catalog(empty).status == "absent"
    found = model_sync.find_candidates({META_KEY: CFG["models"][META_KEY]},
                                       {"meta": model_sync.read_muse_catalog(empty)})
    assert found[META_KEY]["status"] == "discovery_unavailable"


def test_a_muse_candidate_stops_at_p0_without_any_inference(tmp_path):
    bindir, fakedir = fake_bin(tmp_path)
    muse = bindir / "muse"
    muse.write_text(f"#!{sys.executable}\nimport sys\n"
                    f"open({str(fakedir / 'muse.calls')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                    "print('Muse Code 1.4.7 (1.4.7-R6101.1)') if sys.argv[1:] == ['--version'] "
                    "else sys.exit(97)\n")
    muse.chmod(0o755)
    home = fake_home(tmp_path)
    muse_dir(tmp_path, newer_muse())
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    rep = model_sync.run(env=env, home=home, now=NOW, keys=[META_KEY])
    assert rep["deferred"] == [META_KEY] and rep["inferences"] == 0, rep
    calls = (fakedir / "muse.calls").read_text().split("\n") if (fakedir / "muse.calls").exists() else []
    assert all(c in ("--version", "") for c in calls), calls
    neg = work_state(root)["negatives"][META_KEY]
    assert neg["reason"] == "overhead_unmeasurable" and neg["id"] == "muse-spark-1.4"
    assert neg["retry_after"]["kind"] == "catalog_change"
    assert read_gen(root)[0] != "ok" or META_KEY not in (read_gen(root)[2] or {}).get("entries", {})


def test_p0_names_why_a_muse_candidate_is_never_probed():
    row = CFG["models"][META_KEY]
    got = model_sync.gate_p0(family="meta", candidate_id="muse-spark-1.4", row=row, base=CFG,
                             catalog_efforts=["low", "medium", "high"], cli_metadata_ids=None,
                             ctx=model_sync.ProbeContext(now=NOW))
    assert (got["outcome"], got["reason"]) == ("deferred", "overhead_unmeasurable")
    assert model_sync.MAX_INFERENCES["meta"] == 0
