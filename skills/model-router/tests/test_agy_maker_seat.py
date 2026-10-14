"""The agy maker seat profile `agy-maker-v1` (design 2026-10-14 DD-59, issue #59).

agy's write tools have no path containment (measured on agy 1.3.5: an absolute
path, `..`, a symlink, a pre-existing hard link and HOME were all written), so
the seat is contained by four layers: the maker agent's tool list, a deny-rule
project, a kernel write fence the receipt guard carries, and the workspace
audit. Everything is checked before spawn; the CLI log the run leaves is drift
detection only, because the maker could rewrite it.

The fake `agy` below is a Python script run under the REAL Seatbelt guard on
Darwin, so the fence tests are kernel tests, not mocks. Nothing here runs the
real CLI or a model.

Run:  python3 -m pytest skills/model-router/tests/test_agy_maker_seat.py -q
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "tests"))

import dispatch_agent  # noqa: E402
import receipt_guard  # noqa: E402
from route_task import load_config  # noqa: E402

SCRIPT = SKILL / "scripts" / "dispatch_agent.py"
GEMINI = load_config()["models"]["gemini_worker_balanced"]["id"]
DARWIN = sys.platform == "darwin" and os.path.isfile(receipt_guard.SANDBOX_EXEC)
MAKER = dispatch_agent.AGY_MAKER_PROFILE
LOADED = dispatch_agent._agy_grants_loaded(MAKER)


def real_tmp(prefix):
    return Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))


@pytest.fixture
def box():
    """A canonical scratch tree outside pytest's tmp_path (whose ancestors may
    not be canonical everywhere): home with both seat files, a workspace, an
    outside directory and a log directory."""
    root = real_tmp("dmr-maker-test-")
    home = root / "home"
    for spec in dispatch_agent.AGY_PROFILES.values():
        agents = home / ".gemini" / "config" / "agents"
        projects = home / ".gemini" / "config" / "projects"
        agents.mkdir(parents=True, exist_ok=True)
        projects.mkdir(parents=True, exist_ok=True)
        (agents / f"{spec['agent']}.md").write_text(spec["agent_text"])
        (projects / f"{spec['project']}.json").write_text(json.dumps(spec["project_doc"]))
    cli = home / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True, exist_ok=True)
    (cli / "settings.json").write_text(json.dumps({"telemetryEnabled": False}))
    for d in ("ws", "outside", "logs", "receipts", "bin"):
        (root / d).mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)


FAKE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
log = args[args.index("--log-file") + 1]
model = args[args.index("--model") + 1]
plan = json.loads(os.environ.get("FAKE_PLAN", "{{}}"))
results = {{}}
for name, path in plan.get("writes", {{}}).items():
    try:
        with open(path, "w") as f:
            f.write("x")
        results[name] = "written"
    except OSError as e:
        results[name] = "denied:" + str(e.errno)
for name, (src, dst) in plan.get("renames", {{}}).items():
    try:
        os.rename(src, dst)
        results[name] = "renamed"
    except OSError as e:
        results[name] = "denied:" + str(e.errno)
for name, (link, target) in plan.get("via_new_symlink", {{}}).items():
    try:
        os.symlink(target, link)
        with open(link, "w") as f:
            f.write("x")
        results[name] = "written"
    except OSError as e:
        results[name] = "denied:" + str(e.errno)
results["tmpdir"] = os.environ.get("TMPDIR")
with open(log, "a") as f:
    f.write('I1014 11:00:00.000000       1 model_resolver.go:155] model alias "%s" resolved to "%s%s"\n'
            % (model, model, plan.get("resolved_suffix", "-low")))
    for line in plan.get("grants", [{loaded!r}]):
        f.write("I1014 11:00:01.000000       1 cli_setting_manager.go:211] "
                "ApplyProjectPermissionGrants: " + line + "\n")
sys.stdout.write(json.dumps({{"conversation_id": "c", "status": "SUCCESS",
    "response": json.dumps(results), "num_turns": 1,
    "usage": {{"input_tokens": 1, "output_tokens": 1}}}}))
'''


def fake(box):
    exe = box / "bin" / "agy"
    exe.write_text(FAKE.format(python=sys.executable, loaded=LOADED))
    exe.chmod(0o755)
    return exe


def child(exe, log, **over):
    argv = [str(exe), "--agent", dispatch_agent.AGY_MAKER_AGENT, "--model", GEMINI,
            "--effort", "low", "--project", dispatch_agent.AGY_MAKER_PROJECT,
            "--mode", dispatch_agent.AGY_MAKER_MODE, "--disable-slash-commands",
            "--output-format", "json", "--print-timeout", "0s", "--log-file", str(log),
            "-p", "Create made.txt."]
    for flag, value in over.items():
        flag = "--" + flag.replace("_", "-")
        if value is None:
            i = argv.index(flag)
            del argv[i:i + 2]
        else:
            argv[argv.index(flag) + 1] = value
    return argv


def run(box, *, attempt="m1", cwd=None, log=None, guard=True, single=True, plan=None,
        argv=None, extra=(), home=None):
    cwd = cwd or box / "ws"
    log = log or box / "logs" / f"{attempt}.agy.log"
    sup = [sys.executable, str(SCRIPT), "run", "--attempt-id", attempt,
           "--receipt-dir", str(box / "receipts"), "--deadline-seconds", "60",
           "--seat", "worker", "--runtime", "claude_code", "--model-id", GEMINI,
           "--effort-native", "low", "--transport-id", "claude_code.to_gemini",
           "--output-schema", "none", "--output-envelope", "agy-print-json-v1",
           "--seat-profile", MAKER, "--agy-log", str(log), "--child-cwd", str(cwd),
           *(["--require-single-linked-cwd"] if single else []),
           *(["--receipt-guard", "darwin-sandbox-v1"] if guard else []), *extra]
    env = {**os.environ, "HOME": str(home or box / "home"),
           "FAKE_PLAN": json.dumps(plan or {})}
    proc = subprocess.run([*sup, "--", *(argv or child(fake(box), log))],
                          capture_output=True, text=True, env=env, timeout=120)
    receipt = box / "receipts" / f"{attempt}.json"
    return proc, (json.loads(receipt.read_text()) if receipt.is_file() else None)


# ---------------------------------------------------------------------------
# The host files and the profile table
# ---------------------------------------------------------------------------

def test_the_maker_agent_lists_exactly_the_three_file_tools():
    text = dispatch_agent.AGY_MAKER_AGENT_TEXT
    tools = [l.strip()[2:] for l in text.split("---")[1].splitlines() if l.startswith("  - ")]
    assert tools == ["view_file", "write_to_file", "replace_file_content"]
    for absent in ("run_command", "search_web", "read_url_content", "invoke_subagent"):
        assert absent not in text


def test_the_maker_project_denies_commands_mcp_and_urls_and_nothing_else():
    assert set(dispatch_agent.AGY_MAKER_DENY) == {"command(*)", "mcp(*)", "read_url(*)",
                                                   "execute_url(*)"}
    deny = dispatch_agent.AGY_MAKER_PROJECT_DOC["permissionGrants"]["permissionGrants"]
    assert set(deny) == {"deny"}
    assert LOADED == 'stored 0 allow, 4 deny grants from project "dmr-maker-v1"'


def test_the_reviewer_profile_still_refuses_a_mode_and_the_maker_requires_accept_edits(box):
    log = box / "logs" / "x.agy.log"
    rev = child("agy", log, agent=dispatch_agent.AGY_REVIEWER_AGENT,
                project=dispatch_agent.AGY_REVIEWER_PROJECT)
    assert "--mode" in rev and dispatch_agent._agy_argv_problem(rev, log, GEMINI) is not None
    mk = child("agy", log)
    assert dispatch_agent._agy_argv_problem(mk, log, GEMINI, MAKER) is None
    for bad in (child("agy", log, mode="auto"), child("agy", log, mode=None),
                child("agy", log, agent=dispatch_agent.AGY_REVIEWER_AGENT)):
        assert dispatch_agent._agy_argv_problem(bad, log, GEMINI, MAKER) is not None


# ---------------------------------------------------------------------------
# Refused before spawn
# ---------------------------------------------------------------------------

def _refused(proc, receipt, needle):
    assert proc.returncode == 2 and receipt is None, (proc.returncode, proc.stderr)
    assert needle in proc.stderr, proc.stderr


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_no_receipt_guard_no_fence_no_seat(box):
    proc, receipt = run(box, guard=False)
    _refused(proc, receipt, "--receipt-guard darwin-sandbox-v1")


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_no_single_link_audit_no_seat(box):
    proc, receipt = run(box, single=False)
    _refused(proc, receipt, "--require-single-linked-cwd")


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_a_non_canonical_workspace_is_refused_not_normalized(box):
    link = box / "ws-link"
    link.symlink_to(box / "ws")
    proc, receipt = run(box, cwd=link)
    _refused(proc, receipt, "not its own canonical path")


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_a_workspace_inside_the_cli_home_is_refused(box):
    cwd = box / "home" / ".gemini" / "config"
    proc, receipt = run(box, cwd=cwd)
    _refused(proc, receipt, "overlaps the CLI's configuration home")


@pytest.mark.parametrize("where", ["in_workspace", "in_cli_home"])
@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_a_log_inside_the_workspace_or_the_cli_home_is_refused(box, where):
    log = (box / "ws" / "a.agy.log" if where == "in_workspace"
           else box / "home" / ".gemini" / "a.agy.log")
    proc, receipt = run(box, log=log)
    _refused(proc, receipt, "--agy-log")


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_missing_maker_host_files_refuse_the_seat(box):
    (box / "home" / ".gemini" / "config" / "agents" / "dmr-maker-v1.md").unlink()
    proc, receipt = run(box)
    _refused(proc, receipt, "dmr-maker-v1")


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_a_symlink_in_the_workspace_refuses_the_seat(box):
    """The single-link audit refuses a symlink too: a write through it would be
    checked by the fence against its target, but the tree must not name a file
    outside itself at all."""
    (box / "ws" / "link").symlink_to(box / "outside" / "via-link.txt")
    proc, receipt = run(box)
    _refused(proc, receipt, "symlink")


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_a_hard_link_in_the_workspace_refuses_the_seat(box):
    target = box / "outside" / "victim.txt"
    target.write_text("v")
    os.link(target, box / "ws" / "hl.txt")
    proc, receipt = run(box)
    _refused(proc, receipt, "hard link")
    assert target.read_text() == "v"


# ---------------------------------------------------------------------------
# The fence, rendered and recomputed
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_the_fence_sits_after_allow_default_and_before_the_receipt_rules(box):
    out = (box / "receipts" / "a.stdout").open("w")
    err = (box / "receipts" / "a.stderr").open("w")
    fence = {"cwd": str(box / "ws"), "tmpdir": str(box / "outside"),
             "agy_log": str(box / "logs" / "a.log"),
             "home_gemini": str(box / "home" / ".gemini")}
    prefix, meta = receipt_guard._recipe(box / "receipts", out.fileno(), err.fileno(), fence)
    policy = prefix[prefix.index("-p") + 1]
    assert policy.startswith("(version 1)\n(allow default)\n" + receipt_guard.FENCE_POLICY)
    assert policy.index("FENCE_HOME_GEMINI") < policy.index('(param "ROOT")') \
        < policy.index('(literal (param "STDOUT"))')
    assert meta["write_fence"] == fence
    plain, meta0 = receipt_guard._recipe(box / "receipts", out.fileno(), err.fileno())
    assert "FENCE" not in plain[plain.index("-p") + 1] and "write_fence" not in meta0
    out.close()
    err.close()


@pytest.mark.parametrize("bad", [
    {"cwd": "relative/ws"},
    {"cwd": "/tmp"},                       # /tmp is a symlink to /private/tmp
    {"extra": "/x"},
])
def test_a_fence_with_a_bad_path_is_refused(box, bad):
    fence = {"cwd": str(box / "ws"), "tmpdir": str(box / "outside"),
             "agy_log": str(box / "logs" / "a.log"),
             "home_gemini": str(box / "home" / ".gemini")}
    fence.update(bad)
    with pytest.raises(receipt_guard.GuardError):
        receipt_guard.check_fence(fence)


@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_the_receipt_verifies_from_its_recorded_fence_after_cleanup(box):
    proc, receipt = run(box)
    assert receipt["result"]["state"] == "SUCCEEDED", (proc.stderr, receipt["result"])
    meta = receipt["receipt_guard"]
    assert meta["write_fence"]["cwd"] == receipt["child_cwd"]
    assert meta["write_fence"]["agy_log"] == receipt["agy_log"]
    assert not os.path.exists(meta["write_fence"]["tmpdir"])     # removed at exit
    shutil.rmtree(box / "ws")
    assert receipt_guard.verified_metadata(meta, box / "receipts", "m1")
    forged = json.loads(json.dumps(meta))
    forged["write_fence"]["cwd"] = str(box / "outside")
    assert not receipt_guard.verified_metadata(forged, box / "receipts", "m1")


# ---------------------------------------------------------------------------
# Through the real Seatbelt guard
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_the_fence_lets_the_workspace_in_and_keeps_everything_else_out(box):
    (box / "ws" / "sub").mkdir()
    home_agent = box / "home" / ".gemini" / "config" / "agents" / "dmr-reviewer-v1.md"
    before = home_agent.read_bytes()
    writes = {
        "inside": str(box / "ws" / "made.txt"),
        "inside_sub": str(box / "ws" / "sub" / "made.txt"),
        "gitignore": str(box / "ws" / ".gitignore"),
        "outside": str(box / "outside" / "abs.txt"),
        "dotdot": str(box / "ws" / ".." / "outside" / "rel.txt"),
        "reviewer_agent": str(home_agent),
        "git_hook": str(box / "ws" / ".git" / "hooks" / "pre-commit"),
        "nested_git": str(box / "ws" / "sub" / ".git" / "config"),
        # a root `.agents` would be refused before spawn by the workspace
        # audit; the fence keeps the model from creating one, and from writing
        # a nested one the audit does not look at
        "agents_root": str(box / "ws" / ".agents"),
        "agents_nested": str(box / "ws" / "sub" / ".agents" / "hooks.json"),
    }
    for name in ("git_hook", "nested_git", "agents_nested"):
        Path(writes[name]).parent.mkdir(parents=True)
    proc, receipt = run(box, plan={"writes": writes})
    assert receipt["result"]["state"] == "SUCCEEDED", (proc.stderr, receipt["result"])
    got = json.loads(json.loads((box / "receipts" / "m1.stdout").read_text())["response"])
    assert got["inside"] == got["inside_sub"] == got["gitignore"] == "written"
    for name in ("outside", "dotdot", "reviewer_agent", "git_hook", "nested_git",
                 "agents_root", "agents_nested"):
        assert got[name].startswith("denied:"), (name, got[name])
    assert not (box / "outside" / "abs.txt").exists()
    assert home_agent.read_bytes() == before
    # the child's TMPDIR is the attempt's own, inside the fence
    assert got["tmpdir"] == receipt["receipt_guard"]["write_fence"]["tmpdir"]


@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_the_maker_grants_line_is_the_four_rule_one(box):
    proc, receipt = run(box)
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["result"]["agy_project_grants"]["lines"] == [LOADED]
    proc, receipt = run(box, attempt="m2", plan={"grants": [dispatch_agent.AGY_GRANTS_LOADED]})
    assert receipt["result"]["state"] != "SUCCEEDED"
    assert any(r.startswith("agy_project_grants:") for r in receipt["result"]["invalid_reasons"])


# ---------------------------------------------------------------------------
# The attended maker probe (probe_maker, design DD-59d)
# ---------------------------------------------------------------------------

import copy  # noqa: E402

import model_sync  # noqa: E402
import probe_maker  # noqa: E402
import route_task  # noqa: E402

GEMINI_KEY = "gemini_worker_balanced"

MAKER_FAKE = r'''#!{python}
import json, re, sys
args = sys.argv[1:]
log = args[args.index("--log-file") + 1]
model = args[args.index("--model") + 1]
token = re.search(r"MADE-[0-9a-f]{{16}}", args[args.index("-p") + 1]).group(0)
with open("made.txt", "w") as f:          # in place, the way agy writes
    f.write(token + "\n")
with open(log, "a") as f:
    f.write('I1014 11:00:00.000000       1 model_resolver.go:155] model alias "%s" resolved to "%s-low"\n'
            % (model, model))
    f.write("I1014 11:00:01.000000       1 cli_setting_manager.go:211] "
            "ApplyProjectPermissionGrants: " + {loaded!r} + "\n")
sys.stdout.write(json.dumps({{"conversation_id": "c", "status": "SUCCESS", "response": "done",
    "num_turns": 1, "usage": {{"input_tokens": 1, "output_tokens": 1}}}}))
'''


def writable_cfg():
    cfg = copy.deepcopy(load_config())
    cfg["transports"]["claude_code"]["to_gemini"]["write_verified"] = True
    return cfg


def test_the_gemini_maker_argv_is_the_whole_profile_with_the_prompt_in_argv(box):
    prompt = box / "p.txt"
    prompt.write_text("Create made.txt: MADE-0123456789abcdef")
    log = box / "logs" / "m.agy.log"
    argv = probe_maker.maker_argv(cfg=writable_cfg(), family="gemini", model_id=GEMINI,
                                  effort_native="low", attempt_id="m-1",
                                  receipt_dir=box / "receipts", child_cwd=box / "ws",
                                  prompt_file=prompt, deadline_seconds=60, agy_log=log,
                                  receipt_guard=False)
    sup, kid = argv[:argv.index("--")], argv[argv.index("--") + 1:]
    assert sup[sup.index("--seat-profile") + 1] == MAKER
    assert sup[sup.index("--agy-log") + 1] == str(log)
    assert "--require-single-linked-cwd" in sup and "--prompt-file" not in sup
    assert sup[sup.index("--receipt-guard") + 1] == "darwin-sandbox-v1"   # never optional
    assert not [a for a in argv if "<" in a and ">" in a], "an unsubstituted placeholder"
    assert dispatch_agent._agy_argv_problem(kid, log, GEMINI, MAKER) is None
    assert kid[kid.index("-p") + 1] == prompt.read_text()


def test_without_write_authority_probe_maker_refuses_the_gemini_key(box):
    """While the direction's write_verified is false (design DD-59c), there is
    no write-verified direction to re-verify an overlay id through."""
    with pytest.raises(model_sync.SyncError, match="no write_verified transport"):
        probe_maker.maker_transport(load_config(), "gemini")


@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_the_attended_gemini_maker_probe_certifies_the_written_file(box, monkeypatch):
    exe = box / "bin" / "agy"
    exe.write_text(MAKER_FAKE.format(python=sys.executable, loaded=LOADED))
    exe.chmod(0o755)
    monkeypatch.setattr(route_task, "load_config", lambda *a, **k: writable_cfg())
    root = box / "state"
    root.mkdir(mode=0o700)
    env = {**os.environ, "HOME": str(box / "home"),
           "PATH": f"{box / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin",
           "DEEP_MODEL_ROUTER_STATE_DIR": str(root)}
    got = probe_maker.probe_maker(key=GEMINI_KEY, env=env, home=box / "home", now=None,
                                  state_path=root, confirm=lambda plan: True)
    assert got["status"] == "pass", got
    s = got["summary"]
    assert s["transport_id"] == "claude_code.to_gemini" and s["recipe_key"] == "mechanism_maker"
    assert s["artifact"]["sha256"] == s["artifact"]["expected_sha256"]
    assert s["receipt_guard"] is True


@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_the_cli_home_stays_unwritable_even_inside_an_overlapping_workspace(box):
    """Rule 2 is defence in depth: the preflight already refuses a workspace
    that overlaps `~/.gemini`, so only the rendered profile can show the
    deny outranking the workspace allowance (r1 sol F3)."""
    gem = box / "home" / ".gemini"
    out = (box / "receipts" / "o.stdout").open("w")
    err = (box / "receipts" / "o.stderr").open("w")
    fence = {"cwd": str(gem / "config"), "tmpdir": str(box / "outside"),
             "agy_log": str(box / "logs" / "o.log"), "home_gemini": str(gem)}
    prefix, _ = receipt_guard._recipe(box / "receipts", out.fileno(), err.fileno(), fence)
    target = gem / "config" / "agents" / "x.md"
    proc = subprocess.run([*prefix, "/bin/sh", "-c", f"echo x > '{target}'"],
                          capture_output=True, text=True)
    assert proc.returncode != 0 and not target.exists()
    ok = subprocess.run([*prefix, "/bin/sh", "-c", f"echo x > '{box / 'outside' / 'ok'}'"],
                        capture_output=True, text=True)
    assert ok.returncode == 0
    out.close()
    err.close()


# ---------------------------------------------------------------------------
# The registry: the recipe, its ledger row, and withheld write authority
# ---------------------------------------------------------------------------

def _maker_recipe_argv(log="/abs/m.agy.log", prompt="make it"):
    recipe = load_config()["transports"]["claude_code"]["to_gemini"]["mechanism_maker"]
    subs = {"<id>": GEMINI, "<native-effort>": "low", "<agy-log>": log, "<prompt>": prompt}
    return [subs.get(t, t) for t in recipe.replace('"<prompt>"', "<prompt>").split()]


def test_the_shipped_maker_recipe_is_the_profile_argv():
    argv = _maker_recipe_argv()
    assert dispatch_agent._agy_argv_problem(argv, Path("/abs/m.agy.log"), GEMINI, MAKER) is None
    assert dispatch_agent._agy_argv_problem(argv, Path("/abs/m.agy.log"), GEMINI) is not None


def test_the_maker_row_binds_the_recipe_and_write_authority_stays_withheld():
    from test_docs import _assert_direction_row_binds, _whole_token
    cfg = load_config()
    entry = cfg["transports"]["claude_code"]["to_gemini"]
    needle = "transports.claude_code.to_gemini.mechanism_maker"
    rows = [e for e in cfg["verification_ledger"]["entries"]
            if _whole_token(needle, str(e.get("item", "")))]
    assert len(rows) == 1 and rows[0]["status"] == "verified"
    _assert_direction_row_binds(entry["mechanism_maker"], rows[0], rows[0]["item"])
    # U-5: probed and recorded, but not authorized to seat write work
    assert entry["write_verified"] is False
    assert route_task.Policy.of(cfg).write_capable("claude_code", "gemini") is False


@pytest.mark.skipif(DARWIN, reason="checks the refusal on hosts without the guard")
def test_off_darwin_the_maker_seat_is_refused_before_spawn(box):
    proc, receipt = run(box)
    assert proc.returncode == 2 and receipt is None


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
@pytest.mark.parametrize("where", ["log_in_other_git", "store_in_agents", "store_in_cli_home"])
def test_auxiliary_outputs_may_not_sit_in_protected_directories(box, where):
    """i1 sol F1: the log is inside the fence and the store's stdout/stderr are
    allowed by the guard's last rule, so neither may land where a later tool
    loads or runs what is written there."""
    other = box / "other" / ".git" / "hooks"
    other.mkdir(parents=True)
    kw = {"log_in_other_git": dict(log=other / "pre-commit"),
          "store_in_agents": dict(extra=()),
          "store_in_cli_home": dict(extra=())}[where]
    if where == "log_in_other_git":
        proc, receipt = run(box, **kw)
        _refused(proc, receipt, "'.git'")
        return
    store = (box / "ws2" / ".agents" / "r" if where == "store_in_agents"
             else box / "home" / ".gemini" / "r")
    store.parent.mkdir(parents=True, exist_ok=True)
    log = box / "logs" / f"{where}.agy.log"
    sup = [sys.executable, str(SCRIPT), "run", "--attempt-id", "s1", "--receipt-dir", str(store),
           "--deadline-seconds", "30", "--seat", "worker", "--runtime", "claude_code",
           "--model-id", GEMINI, "--effort-native", "low", "--transport-id",
           "claude_code.to_gemini", "--output-schema", "none", "--output-envelope",
           "agy-print-json-v1", "--seat-profile", MAKER, "--agy-log", str(log), "--child-cwd",
           str(box / "ws"), "--require-single-linked-cwd", "--receipt-guard", "darwin-sandbox-v1"]
    proc = subprocess.run([*sup, "--", *child(fake(box), log)], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(box / "home")}, timeout=60)
    assert proc.returncode == 2 and "--receipt-dir" in proc.stderr, proc.stderr
    assert not (store / "s1.json").exists()


@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_case_variants_symlinks_and_renames_made_during_the_run_stay_fenced(box):
    """i1 fable F2 and the missing tests: a `.GIT`/`.AGENTS` the child creates
    is git's and agy's own name on a case-insensitive volume; a symlink the
    child creates and writes through, or a rename out of the workspace, leaves
    the fence the same way a direct write would."""
    (box / "ws" / "sub").mkdir()
    (box / "ws" / "keep.txt").write_text("k")
    writes = {"upper_git": str(box / "ws" / ".GIT"),
              "nested_upper_agents": str(box / "ws" / "sub" / ".AGENTS")}
    plan = {"writes": writes,
            "renames": {"rename_out": [str(box / "ws" / "keep.txt"),
                                       str(box / "outside" / "moved.txt")]},
            "via_new_symlink": {"new_link": [str(box / "ws" / "lnk"),
                                             str(box / "outside" / "via-new-link.txt")]}}
    proc, receipt = run(box, plan=plan)
    assert receipt["result"]["state"] == "SUCCEEDED", (proc.stderr, receipt["result"])
    got = json.loads(json.loads((box / "receipts" / "m1.stdout").read_text())["response"])
    for name in ("upper_git", "nested_upper_agents", "rename_out", "new_link"):
        assert got[name].startswith("denied:"), (name, got[name])
    assert (box / "ws" / "keep.txt").read_text() == "k"
    assert not (box / "outside" / "moved.txt").exists()
    assert not (box / "outside" / "via-new-link.txt").exists()


@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_a_maker_receipt_is_bound_to_its_own_fence(box):
    """i1 fable F1: the fence the guard recorded must name this receipt's
    workspace and log and rebuild from its recorded values."""
    proc, receipt = run(box)
    assert dispatch_agent.maker_fence_problem(receipt, box / "receipts") is None
    for field, value in (("cwd", str(box / "outside")), ("agy_log", str(box / "logs" / "x"))):
        forged = json.loads(json.dumps(receipt))
        forged["receipt_guard"]["write_fence"][field] = value
        assert dispatch_agent.maker_fence_problem(forged, box / "receipts") is not None, field
    unguarded = json.loads(json.dumps(receipt))
    unguarded["receipt_guard"] = None
    assert dispatch_agent.maker_fence_problem(unguarded, box / "receipts") is not None


@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_a_maker_resolution_outside_the_effort_spellings_fails(box):
    proc, receipt = run(box, plan={"resolved_suffix": "-lite"})
    assert receipt["result"]["state"] != "SUCCEEDED"
    assert any(r.startswith("agy_resolved_model:")
               for r in receipt["result"]["invalid_reasons"]), receipt["result"]


def test_the_reviewer_profile_still_refuses_a_mode_through_the_supervisor(box):
    """The refactor kept the reviewer allow-list: `--mode` is refused before
    spawn, not just by the unit check."""
    log = box / "logs" / "r.agy.log"
    argv = child(fake(box), log, agent=dispatch_agent.AGY_REVIEWER_AGENT,
                 project=dispatch_agent.AGY_REVIEWER_PROJECT)
    sup = [sys.executable, str(SCRIPT), "run", "--attempt-id", "r1", "--receipt-dir",
           str(box / "receipts"), "--deadline-seconds", "30", "--seat", "reviewer-1",
           "--runtime", "claude_code", "--model-id", GEMINI, "--effort-native", "low",
           "--transport-id", "claude_code.to_gemini", "--output-schema", "review",
           "--output-envelope", "agy-print-json-v1", "--seat-profile", "agy-reviewer-v1",
           "--agy-log", str(log), "--child-cwd", str(box / "ws")]
    proc = subprocess.run([*sup, "--", *argv], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(box / "home")}, timeout=60)
    assert proc.returncode == 2 and "'--mode' is outside the agy-reviewer-v1 allow-list" \
        in proc.stderr, proc.stderr


@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_the_attended_gemini_maker_probe_fails_without_a_bound_fence(box, monkeypatch):
    exe = box / "bin" / "agy"
    exe.write_text(MAKER_FAKE.format(python=sys.executable, loaded=LOADED))
    exe.chmod(0o755)
    monkeypatch.setattr(route_task, "load_config", lambda *a, **k: writable_cfg())
    monkeypatch.setattr(dispatch_agent, "maker_fence_problem",
                        lambda receipt, receipt_dir: "forged for the test")
    root = box / "state"
    root.mkdir(mode=0o700)
    env = {**os.environ, "HOME": str(box / "home"),
           "PATH": f"{box / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    got = probe_maker.probe_maker(key=GEMINI_KEY, env=env, home=box / "home", now=None,
                                  state_path=root, confirm=lambda plan: True)
    assert got["status"] == "failed" and got["summary"]["reason"] == "fence"


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_a_store_inside_a_relocated_cli_home_is_refused(box):
    """The overlap check, not the name check: with `~/.gemini` a symlink to a
    directory of another name, a receipt store inside that directory names no
    `.gemini` component, and only the canonical overlap refuses it."""
    real = box / "geminiconf"
    (box / "home" / ".gemini").rename(real)
    (box / "home" / ".gemini").symlink_to(real)
    store = real / "r"
    log = box / "logs" / "reloc.agy.log"
    sup = [sys.executable, str(SCRIPT), "run", "--attempt-id", "s2", "--receipt-dir", str(store),
           "--deadline-seconds", "30", "--seat", "worker", "--runtime", "claude_code",
           "--model-id", GEMINI, "--effort-native", "low", "--transport-id",
           "claude_code.to_gemini", "--output-schema", "none", "--output-envelope",
           "agy-print-json-v1", "--seat-profile", MAKER, "--agy-log", str(log), "--child-cwd",
           str(box / "ws"), "--require-single-linked-cwd", "--receipt-guard", "darwin-sandbox-v1"]
    proc = subprocess.run([*sup, "--", *child(fake(box), log)], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(box / "home")}, timeout=60)
    assert proc.returncode == 2 and "overlaps the CLI's configuration home" in proc.stderr, \
        proc.stderr


@pytest.mark.skipif(not DARWIN, reason="Darwin receipt guard")
def test_a_fence_forged_with_a_consistent_profile_still_fails_the_binding(box):
    """A forger who rebuilds the profile hash for a fence naming another
    workspace passes `verified_metadata`; the binding to the receipt's own
    child_cwd and agy_log is what still refuses it."""
    proc, receipt = run(box)
    forged = json.loads(json.dumps(receipt))
    fence = dict(forged["receipt_guard"]["write_fence"], cwd=str(box / "outside"))
    fds = [os.open(box / "receipts" / f"m1.{x}", os.O_RDONLY) for x in ("stdout", "stderr")]
    try:
        _, meta = receipt_guard._recipe(box / "receipts", *fds, fence)
    finally:
        for fd in fds:
            os.close(fd)
    meta["phase"] = "launched"
    forged["receipt_guard"] = meta
    assert receipt_guard.verified_metadata(meta, box / "receipts", "m1")
    assert "another workspace" in dispatch_agent.maker_fence_problem(forged, box / "receipts")


@pytest.mark.skipif(not DARWIN, reason="the maker profile is Darwin-only")
def test_case_aliases_of_the_cli_home_are_refused_before_spawn(box):
    """i2 sol F1 / fable F3: on a case-insensitive volume a differently
    capitalized path names the same directory, and a nonexistent store is
    judged by the directories it would be created under — by identity, not
    by spelling."""
    if not os.path.exists(str(box / "HOME")):          # a case-sensitive volume
        pytest.skip("case-sensitive volume")
    real = box / "geminiconf"
    (box / "home" / ".gemini").rename(real)
    (box / "home" / ".gemini").symlink_to(real)
    alias_store = box / "GEMINICONF" / "r"
    log = box / "logs" / "alias.agy.log"
    sup = [sys.executable, str(SCRIPT), "run", "--attempt-id", "s3", "--receipt-dir",
           str(alias_store), "--deadline-seconds", "30", "--seat", "worker", "--runtime",
           "claude_code", "--model-id", GEMINI, "--effort-native", "low", "--transport-id",
           "claude_code.to_gemini", "--output-schema", "none", "--output-envelope",
           "agy-print-json-v1", "--seat-profile", MAKER, "--agy-log", str(log), "--child-cwd",
           str(box / "ws"), "--require-single-linked-cwd", "--receipt-guard", "darwin-sandbox-v1"]
    env = {**os.environ, "HOME": str(box / "home")}
    proc = subprocess.run([*sup, "--", *child(fake(box), log)], capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode == 2 and "overlaps the CLI's configuration home" in proc.stderr, \
        proc.stderr
    assert not (real / "r").exists()
    (real / "ws").mkdir()
    proc, receipt = run(box, cwd=box / "GEMINICONF" / "ws", attempt="s4")
    _refused(proc, receipt, "overlaps the CLI's configuration home")


def test_the_new_ledger_rows_cite_only_their_final_attempts():
    """i2 fable F1: a row that says it cites the `*-fin` set (the released
    supervisor) names no earlier-build attempt in its evidence, except where
    it says why."""
    import re as _re
    rows = {e["item"]: e for e in load_config()["verification_ledger"]["entries"]}
    for item in ("Codex -> Gemini transport (transports.codex.to_gemini.mechanism_reviewer)",
                 "Gemini maker seat recipe (transports.claude_code.to_gemini.mechanism_maker)"):
        text = " ".join(rows[item]["evidence"].split())
        listed = {p["attempt_id"] for p in rows[item]["probes"]}
        assert all(a.endswith("-fin") for a in listed), listed
        cited = set(_re.findall(r"\b(?:cx[a-z]+|m[a-i])-(?:1|rel|fin)\b", text))
        stray = {a for a in cited if not a.endswith("-fin")} - {"mb-1"}  # mb-1 is named on purpose
        assert not stray, (item, stray)
