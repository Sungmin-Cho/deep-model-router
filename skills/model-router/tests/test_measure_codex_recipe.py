"""scripts/tools/measure_codex_recipe.py (plan B8): the quota gate comes first
and starts no codex process when the reading defers; a measured pair is
compared on boot input and review format. Codex is a fake here — no test
reaches the network."""
import datetime as dt
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "scripts" / "tools" / "measure_codex_recipe.py"

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_CODEX_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
sys.stdin.read()
tokens = int(os.environ["FAKE_EPHEMERAL_TOKENS"]) if "--ephemeral" in sys.argv else 26000
text = os.environ.get("FAKE_TEXT", "=== REVIEW ===\nverdict: PASS\nThe sign is fixed.")
for ev in ({"type": "thread.started", "thread_id": "t1"},
           {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
           {"type": "turn.completed", "usage": {"input_tokens": tokens, "output_tokens": 20}}):
    print(json.dumps(ev))
'''


def _setup(tmp_path, used=None, age_minutes=5):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    if used is not None:
        ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=age_minutes)
        day = sessions / ts.strftime("%Y/%m/%d")
        day.mkdir(parents=True)
        ev = {"timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "type": "event_msg",
              "payload": {"type": "token_count", "rate_limits": {"primary": {
                  "used_percent": used, "window_minutes": 300,
                  "resets_at": int((ts + dt.timedelta(hours=3)).timestamp())}}}}
        (day / f"rollout-{ts.strftime('%Y-%m-%dT%H-%M-%S')}-x.jsonl").write_text(json.dumps(ev) + "\n")
    codex = tmp_path / "codex"
    codex.write_text(FAKE_CODEX)
    codex.chmod(codex.stat().st_mode | stat.S_IEXEC)
    return sessions, codex


def _run(tmp_path, sessions, codex, **env):
    log = tmp_path / "codex.log"
    full_env = {**os.environ, "FAKE_CODEX_LOG": str(log), "FAKE_EPHEMERAL_TOKENS": "20000", **env}
    proc = subprocess.run([sys.executable, str(TOOL), "--out-dir", str(tmp_path / "out"),
                           "--sessions-dir", str(sessions), "--codex", str(codex)],
                          capture_output=True, text=True, env=full_env, timeout=120)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return proc, calls


@pytest.mark.parametrize("used,age,why", [(None, 5, "no_record"), (3.0, 60 * 30, "stale"),
                                          (95.0, 5, "used")])
def test_a_deferring_quota_starts_no_codex_process(tmp_path, used, age, why):
    sessions, codex = _setup(tmp_path, used, age)
    proc, calls = _run(tmp_path, sessions, codex)
    assert proc.returncode == 0, proc.stderr
    assert calls == [], "the gate let a codex process start"
    record = json.loads((tmp_path / "out" / "b8-deferred.json").read_text())
    assert record["measured"] is False and record["reason"] == "quota"
    assert "no codex process was started" in proc.stdout
    if why == "used":
        assert record["quota"]["status"] == "ok" and record["quota"]["used_percent"] >= 90
    else:
        assert record["quota"]["status"] == "unknown" and record["quota"]["reason"] == why


def test_a_measured_pair_compares_boot_input_and_format(tmp_path):
    sessions, codex = _setup(tmp_path, 10.0)
    proc, calls = _run(tmp_path, sessions, codex)
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 2
    current, candidate = calls
    assert "--ephemeral" not in current and "--ignore-user-config" not in current
    assert candidate[:-1] == current[:-1] + ["--ignore-user-config", "--ephemeral"]
    assert current[current.index("-s") + 1] == "read-only" and current[-1] == "-"
    summary = json.loads((tmp_path / "out" / "b8-summary.json").read_text())
    assert summary["runs"]["current"]["input_tokens"] == 26000
    assert summary["runs"]["candidate"]["input_tokens"] == 20000
    assert summary["adopt"] is True                         # 20000 / 26000 = 0.77


def test_a_small_drop_or_a_broken_format_is_not_adopted(tmp_path):
    sessions, codex = _setup(tmp_path, 10.0)
    proc, _ = _run(tmp_path, sessions, codex, FAKE_EPHEMERAL_TOKENS="24000")
    assert json.loads((tmp_path / "out" / "b8-summary.json").read_text())["adopt"] is False
    proc, _ = _run(tmp_path, sessions, codex, FAKE_TEXT="looks fine")
    assert json.loads((tmp_path / "out" / "b8-summary.json").read_text())["adopt"] is False
