"""The SessionStart tick hook (design 2026-09-25 DD-A0 "훅", DD-A6 trigger).

A hooks.json `command` is a STRING the host shell interprets, and Codex
trust-approves it by hash — so the exact string is pinned here, and it is
executed through /bin/sh the way a host runs it: from a plugin root with a
space in it, and with both root variables empty. Trap executables stand in
for every model CLI (and for python3 where it must not run).
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
PLUGIN = SKILL.parent.parent
FIXTURES = SKILL / "tests" / "fixtures"

HOOK_COMMAND = ('r="${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}"; [ -n "$r" ] && python3 '
                '"$r/skills/model-router/scripts/model_sync.py" tick --detach; exit 0')


def _hooks():
    return json.loads((PLUGIN / "hooks" / "hooks.json").read_text())


def test_hooks_json_is_one_session_start_hook_with_the_pinned_command():
    doc = _hooks()
    assert set(doc["hooks"]) == {"SessionStart"}
    (group,) = doc["hooks"]["SessionStart"]
    assert group["matcher"] == "startup"
    (hook,) = group["hooks"]
    assert hook == {"type": "command", "command": HOOK_COMMAND, "async": True, "timeout": 10}


def _trap(bindir: Path, name: str, marker: Path) -> None:
    exe = bindir / name
    exe.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexit 1\n")
    exe.chmod(0o755)


def _plugin_copy(root: Path) -> None:
    shutil.copytree(PLUGIN / ".claude-plugin", root / ".claude-plugin")
    shutil.copytree(SKILL, root / "skills" / "model-router",
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))


def _env(tmp_path, bindir, **roots):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "catalogs" / "codex" / "models_cache.json",
                home / ".codex" / "models_cache.json")
    env = {"HOME": str(home), "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
           "DEEP_MODEL_ROUTER_AUTOUPGRADE": "0",
           "DEEP_MODEL_ROUTER_STATE_DIR": str(tmp_path / "state"),
           "CLAUDE_PLUGIN_ROOT": "", "PLUGIN_ROOT": ""}
    env.update(roots)
    return env


def _bin(tmp_path, *, real_python: bool) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for cli in ("codex", "claude", "grok"):
        _trap(bindir, cli, tmp_path / f"{cli}-ran")
    if real_python:
        (bindir / "python3").symlink_to(sys.executable)
    else:
        _trap(bindir, "python3", tmp_path / "python3-ran")
    return bindir


def _run(env):
    start = time.monotonic()
    proc = subprocess.run(["/bin/sh", "-c", HOOK_COMMAND], env=env, capture_output=True,
                          text=True, timeout=10)
    return proc, time.monotonic() - start


def test_the_hook_runs_offline_from_a_plugin_root_with_a_space(tmp_path):
    root = tmp_path / "plug in"
    _plugin_copy(root)
    bindir = _bin(tmp_path, real_python=True)
    for roots in ({"CLAUDE_PLUGIN_ROOT": str(root)}, {"PLUGIN_ROOT": str(root)}):
        proc, elapsed = _run(_env(tmp_path, bindir, **roots))
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr == "" and proc.stdout == ""
        assert elapsed < 2.0, elapsed
    for cli in ("codex", "claude", "grok"):
        assert not (tmp_path / f"{cli}-ran").exists(), cli
    assert not (tmp_path / "state").exists()


def test_the_hook_runs_the_script_it_names(tmp_path):
    """Not a vacuous exit 0: with a root whose script is missing, python3
    really runs (and fails) — the `exit 0` still keeps the session unblocked."""
    root = tmp_path / "plug in"
    root.mkdir()
    bindir = _bin(tmp_path, real_python=True)
    proc, _ = _run(_env(tmp_path, bindir, CLAUDE_PLUGIN_ROOT=str(root)))
    assert proc.returncode == 0
    assert "model_sync.py" in proc.stderr


def test_the_hook_runs_nothing_when_both_roots_are_empty(tmp_path):
    bindir = _bin(tmp_path, real_python=False)
    proc, _ = _run(_env(tmp_path, bindir))
    assert proc.returncode == 0
    assert not (tmp_path / "python3-ran").exists()
    env = _env(tmp_path, bindir)
    del env["CLAUDE_PLUGIN_ROOT"], env["PLUGIN_ROOT"]
    proc, _ = _run(env)
    assert proc.returncode == 0 and not (tmp_path / "python3-ran").exists()


_OFFLINE_CLI = """#!/bin/sh
echo "{name} $*" >> '{log}'
case "$*" in
  "--version"|"--no-auto-update --version") cat '{version}'; exit 0 ;;
esac
echo "{name} $*" >> '{inference}'
exit 97
"""

_STUB_RUN = """import json, os, sys
# Step aside for the interpreter's own sitecustomize (Homebrew's adds
# site-packages there), then stub `model_sync.py run`.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
del sys.modules["sitecustomize"]
try:
    import sitecustomize  # noqa: F401
except ImportError:
    pass
if len(sys.argv) > 1 and sys.argv[0].endswith("model_sync.py") and sys.argv[1] == "run":
    with open({marker!r}, "a") as f:
        f.write(json.dumps({{"pid": os.getpid(), "argv": sys.argv[1:],
                            "session_leader": os.getsid(0) == os.getpid()}}) + "\\n")
    os._exit(0)
"""


def test_the_default_on_hook_ticks_offline_and_detaches_exactly_one_run(tmp_path):
    """i1r1 opus F10: the path users actually run — AUTOUPGRADE unset. Fake
    CLIs answer only `--version` (anything else is an inference and is
    recorded as one), a captured codex catalog makes a candidate due, and a
    stub stands in for `run` itself (sitecustomize, so the real
    `_spawn_detached` is what starts it). The hook exits 0 within its
    timeout, runs no inference, and leaves exactly one detached child."""
    root = tmp_path / "plug in"
    _plugin_copy(root)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log, inference = tmp_path / "cli.log", tmp_path / "inference.log"
    for cli in ("codex", "claude", "grok"):
        exe = bindir / cli
        exe.write_text(_OFFLINE_CLI.format(
            name=cli, log=log, inference=inference,
            version=FIXTURES / "catalogs" / cli / "version.txt"))
        exe.chmod(0o755)
    (bindir / "python3").symlink_to(sys.executable)
    stub = tmp_path / "stub"
    stub.mkdir()
    marker = tmp_path / "run-started.jsonl"
    (stub / "sitecustomize.py").write_text(_STUB_RUN.format(marker=str(marker)))
    env = _env(tmp_path, bindir, CLAUDE_PLUGIN_ROOT=str(root))
    del env["DEEP_MODEL_ROUTER_AUTOUPGRADE"]
    env["PYTHONPATH"] = str(stub)
    proc, elapsed = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 10.0, elapsed
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)                       # a second child would have landed by now
    runs = [json.loads(l) for l in marker.read_text().splitlines()] if marker.exists() else []
    assert len(runs) == 1, runs
    assert runs[0]["argv"][0] == "run" and runs[0]["session_leader"] is True
    assert runs[0]["argv"][1:2] == ["--tick-hash"]
    assert not inference.exists(), inference.read_text()
    called = sorted(set(log.read_text().splitlines()))
    assert called == ["claude --version", "codex --version", "grok --no-auto-update --version"]
    state = tmp_path / "state"
    assert (state / "work").is_dir()
    assert not (state / "committed").exists()
