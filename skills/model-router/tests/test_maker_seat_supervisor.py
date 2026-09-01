"""Supervisor-side prevention for the grok maker seat (issue #19 shipping half).

Fake children only. Live grok probes live in
docs/design/reviews/2026-09-01-maker-seat/probes/.

Run: python3 -m pytest skills/model-router/tests/test_maker_seat_supervisor.py -q
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from test_dispatch import (  # noqa: E402
    HAPPY,
    SCRIPT,
    run_dispatch,
    write_fake,
)


def _cwd_args(cwd: Path, *extra: str) -> tuple[str, ...]:
    return ("--child-cwd", str(cwd), "--require-single-linked-cwd", *extra)


def test_a_multiply_linked_file_in_child_cwd_is_refused_pre_spawn(tmp_path):
    """The measured A path: a regular file inside cwd that is a second name
    for an outside inode. Auditing only --require-artifact misses this.
    Refusal is pre-spawn: exit 2, no receipt, outside file untouched."""
    cwd = tmp_path / "wd"
    cwd.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("EXTERNAL")
    os.link(outside, cwd / "alias.txt")
    fake = write_fake(tmp_path, "would_write.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=_cwd_args(cwd))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None
    assert "alias.txt" in proc.stderr
    assert outside.read_text() == "EXTERNAL"


def test_require_single_linked_cwd_without_child_cwd_is_refused(tmp_path):
    """Auditing the supervisor's ambient cwd would inspect the wrong tree
    and is not a gate. The flag names a directory or it names nothing."""
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--require-single-linked-cwd",))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None
    assert "--child-cwd" in proc.stderr


def test_a_single_linked_child_cwd_still_spawns(tmp_path):
    cwd = tmp_path / "wd"
    cwd.mkdir()
    (cwd / "note.txt").write_text("ok\n")
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=_cwd_args(cwd))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["child_cwd"] == str(cwd.resolve())


def test_a_hidden_multiply_linked_file_is_refused(tmp_path):
    cwd = tmp_path / "wd"
    (cwd / ".secret").mkdir(parents=True)
    outside = tmp_path / "victim"
    outside.write_text("V")
    os.link(outside, cwd / ".secret" / "x")
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=_cwd_args(cwd))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None


def test_a_symlink_child_cwd_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=_cwd_args(link))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None
    assert "symlink" in proc.stderr


def test_grok_home_is_injected_only_into_the_child(tmp_path):
    home = tmp_path / "ghome"
    marker = tmp_path / "child-home.txt"
    fake = write_fake(tmp_path, "env.py", textwrap.dedent(f"""
        import os
        from pathlib import Path
        Path({str(marker)!r}).write_text(os.environ.get("GROK_HOME", "") + "\\n")
        print("verdict: PASS")
        print("confidence: 0.9")
    """))
    before = os.environ.get("GROK_HOME")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--grok-home", str(home)))
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text().strip() == str(home.resolve())
    assert os.environ.get("GROK_HOME") == before
    assert receipt["grok_home"] == str(home.resolve())


def test_auth_seed_is_copied_to_a_new_inode_and_source_is_untouched(tmp_path):
    home = tmp_path / "ghome"
    seed = tmp_path / "auth.json"
    seed.write_text('{"token":"seed"}\n')
    seed_ino = seed.stat().st_ino
    seed_text = seed.read_text()
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--grok-home", str(home), "--grok-auth-seed", str(seed)))
    assert proc.returncode == 0, proc.stderr
    copied = home / "auth.json"
    assert copied.exists()
    assert copied.read_text() == seed_text
    assert copied.stat().st_ino != seed_ino
    assert seed.stat().st_ino == seed_ino
    assert seed.read_text() == seed_text


def test_auth_seed_without_grok_home_is_refused(tmp_path):
    seed = tmp_path / "auth.json"
    seed.write_text("{}\n")
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--grok-auth-seed", str(seed)))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None


def test_an_existing_grok_home_is_refused(tmp_path):
    home = tmp_path / "ghome"
    home.mkdir()
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--grok-home", str(home)))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None
    assert "already exists" in proc.stderr


def test_symlink_grok_home_is_refused(tmp_path):
    real = tmp_path / "realhome"
    real.mkdir()
    link = tmp_path / "linkhome"
    link.symlink_to(real)
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--grok-home", str(link)))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None
    assert "symlink" in proc.stderr


def test_expect_sandbox_enforced_requires_grok_home(tmp_path):
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--expect-sandbox-enforced", "dmr-maker-v1"))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None
    assert "--grok-home" in proc.stderr


def _events_writer(home: Path, *, profile="dmr-maker-v1", enforced=True):
    event = {
        "event_type": "ProfileApplied",
        "profile": profile,
        "enforced": enforced,
        "workspace": "/tmp/wd",
        "platform": "macos/seatbelt",
    }
    return textwrap.dedent(f"""
        import json, os
        from pathlib import Path
        home = Path(os.environ["GROK_HOME"])
        p = home / "sandbox-events.jsonl"
        with p.open("a") as f:
            f.write(json.dumps({event!r}) + "\\n")
        print("verdict: PASS")
        print("confidence: 0.9")
    """)


def test_profile_applied_enforced_true_is_succeeded(tmp_path):
    home = tmp_path / "ghome"
    fake = write_fake(tmp_path, "ev.py", _events_writer(home, enforced=True))
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--grok-home", str(home),
               "--expect-sandbox-enforced", "dmr-maker-v1"))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["result"]["sandbox_events"]["enforced"] is True
    assert receipt["result"]["sandbox_events"]["profile"] == "dmr-maker-v1"


def test_profile_applied_enforced_false_is_invalid_output(tmp_path):
    home = tmp_path / "ghome"
    fake = write_fake(tmp_path, "ev.py", _events_writer(home, enforced=False))
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--grok-home", str(home),
               "--expect-sandbox-enforced", "dmr-maker-v1"))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert any(r.startswith("sandbox_profile_not_enforced")
               for r in receipt["result"]["invalid_reasons"])


def test_missing_profile_applied_is_invalid_output(tmp_path):
    home = tmp_path / "ghome"
    fake = write_fake(tmp_path, "ev.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--grok-home", str(home),
               "--expect-sandbox-enforced", "dmr-maker-v1"))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "sandbox_event_missing" in receipt["result"]["invalid_reasons"]


def test_wrong_profile_name_is_invalid_output(tmp_path):
    home = tmp_path / "ghome"
    fake = write_fake(tmp_path, "ev.py",
                      _events_writer(home, profile="workspace", enforced=True))
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--grok-home", str(home),
               "--expect-sandbox-enforced", "dmr-maker-v1"))
    assert proc.returncode == 6, proc.stderr
    assert any("sandbox_profile_event_mismatch" in r
               for r in receipt["result"]["invalid_reasons"])


def test_seat_profile_maker_requires_the_custom_sandbox_name(tmp_path):
    cwd = tmp_path / "wd"
    cwd.mkdir()
    seed = tmp_path / "auth.json"
    seed.write_text("{}\n")
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--seat-profile", "grok-maker-v1",
               "--child-cwd", str(cwd), "--require-single-linked-cwd",
               "--grok-home", str(tmp_path / "ghome"),
               "--grok-auth-seed", str(seed),
               "--expect-sandbox-enforced", "workspace",
               "--output-envelope", "grok-headless-json-v1",
               "--session-evidence", f"grok-session-v1:{tmp_path / 'sess'}",
               "--session-id", "11111111-2222-3333-4444-555555555555",
               "--expect-effective-agent", "general-purpose",
               "--expect-sandbox-profile", "workspace"))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None
    assert "dmr-maker-v1" in proc.stderr


def test_seat_profile_maker_requires_the_full_declaration(tmp_path):
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--seat-profile", "grok-maker-v1"))
    assert proc.returncode == 2, proc.stderr
    assert receipt is None
    assert "grok-maker-v1" in proc.stderr
