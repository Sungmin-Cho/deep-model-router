"""Fake-executable tests for the dispatch supervisor.

No real model is ever invoked: every scenario is a tiny Python script the
test writes into tmp_path. See docs/design/2026-08-15-dispatch-layer-design.md
§5 for the scenario table.

Run:  python3 -m pytest skills/model-router/tests/test_dispatch.py -q
"""

import argparse
import errno
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
SCRIPT = SKILL / "scripts" / "dispatch_agent.py"

HAPPY = """
print("verdict: PASS")
print("confidence: 0.9")
"""

PROSE_ONLY = """
print("looks good to me, no issues found")
"""

EXIT_THREE = """
import sys
print("partial work")
sys.exit(3)
"""

SILENT_OK = """
pass
"""

SLEEPER = """
import time
time.sleep(60)
"""

TERM_IGNORER = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
"""

LATE_WRITER = """
import signal, sys, time
def bail(*_):
    print("verdict: PASS")
    sys.stdout.flush()
    sys.exit(0)
signal.signal(signal.SIGTERM, bail)
time.sleep(60)
"""


def write_fake(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


def run_dispatch(tmp_path, fake_argv, *, attempt_id="t1", deadline=30.0,
                 grace=1.0, schema="review", extra=(), harness_timeout=60):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id,
         "--receipt-dir", str(tmp_path / "receipts"),
         "--deadline-seconds", str(deadline),
         "--grace-seconds", str(grace),
         "--seat", "reviewer-1",
         "--output-schema", schema,
         *extra,
         "--", *map(str, fake_argv)],
        capture_output=True, text=True, timeout=harness_timeout)
    receipt_path = tmp_path / "receipts" / f"{attempt_id}.json"
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
    return proc, receipt


def test_happy_path_is_succeeded_with_a_digest(tmp_path):
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["result"]["exit_status"] == 0
    assert receipt["result"]["schema_valid"] is True
    assert receipt["result"]["output_sha256"]
    assert receipt["timing"]["started_at"] and receipt["timing"]["finished_at"]
    stdout = Path(receipt["result"]["stdout_path"]).read_text()
    assert "verdict: PASS" in stdout


def test_nonzero_exit_is_failed(tmp_path):
    fake = write_fake(tmp_path, "boom.py", EXIT_THREE)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 1
    assert receipt["result"]["state"] == "FAILED"
    assert receipt["result"]["exit_status"] == 3


def test_exit_zero_with_empty_stdout_is_invalid_output(tmp_path):
    """An empty success is not a success — this is the "empty stdout looks
    like a failed review / silent skip" trap from the research docs."""
    fake = write_fake(tmp_path, "silent.py", SILENT_OK)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 6
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert receipt["result"]["schema_valid"] is False


def test_prose_without_a_verdict_is_invalid_output_under_review_schema(tmp_path):
    fake = write_fake(tmp_path, "prose.py", PROSE_ONLY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 6
    assert receipt["result"]["state"] == "INVALID_OUTPUT"


def test_schema_none_accepts_any_nonempty_output(tmp_path):
    fake = write_fake(tmp_path, "prose.py", PROSE_ONLY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake], schema="none")
    assert proc.returncode == 0
    assert receipt["result"]["state"] == "SUCCEEDED"


def test_missing_binary_is_start_failed(tmp_path):
    proc, receipt = run_dispatch(tmp_path, ["/nonexistent/binary-xyz"])
    assert proc.returncode == 4
    assert receipt["result"]["state"] == "START_FAILED"
    assert receipt["result"]["exit_status"] is None


def test_prompt_file_feeds_stdin_and_is_hashed(tmp_path):
    reader = write_fake(tmp_path, "reader.py", """
    import sys
    data = sys.stdin.read()
    print(f"verdict: PASS")
    print(f"read {len(data)} bytes")
    """)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this diff please\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, reader],
                                 extra=("--prompt-file", str(prompt)))
    assert proc.returncode == 0
    assert receipt["prompt_sha256"]
    assert "read 24 bytes" in Path(receipt["result"]["stdout_path"]).read_text()


def test_no_prompt_file_means_stdin_is_closed_not_waiting(tmp_path):
    """The grok->openai stdin-hang class (F-06): with no prompt file the
    child's stdin must be /dev/null, so a stdin read returns immediately
    instead of blocking forever."""
    reader = write_fake(tmp_path, "stdin_reader.py", """
    import sys
    data = sys.stdin.read()      # returns "" instantly if stdin is DEVNULL
    print("verdict: PASS")
    """)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, reader],
                                 harness_timeout=15)
    assert proc.returncode == 0
    assert receipt["result"]["state"] == "SUCCEEDED"


def test_receipt_is_never_half_written(tmp_path):
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    leftovers = list((tmp_path / "receipts").glob("*.tmp"))
    assert leftovers == []


def test_a_traversal_attempt_id_is_rejected_before_any_path_is_built(tmp_path):
    """attempt_id is interpolated directly into receipt/stdout/stderr paths;
    a `../` segment must never let it escape receipt_dir."""
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 attempt_id="../../escape")
    assert proc.returncode == 2
    assert receipt is None
    assert not (tmp_path / "receipts").exists()
    assert not (tmp_path.parent / "escape.json").exists()


def test_a_second_run_with_an_existing_attempt_id_is_refused(tmp_path):
    """Two attempts sharing an id must never clobber each other's receipt or
    output files. Exclusive attempt creation claims a SEPARATE sentinel file
    (<attempt_id>.claim) via O_CREAT|O_EXCL, never the receipt path itself
    — the receipt only ever appears via write_receipt's atomic replace of a
    complete JSON document. This test exercises the ordinary case: the
    first attempt has already completed and removed its own claim at its
    terminal write, so the second `run` claims fine, then finds the
    receipt already there, gives back the claim it just took, and refuses
    without touching the existing receipt."""
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc1, receipt1 = run_dispatch(tmp_path, [sys.executable, fake],
                                   attempt_id="dupe-1")
    assert proc1.returncode == 0
    original = (tmp_path / "receipts" / "dupe-1.json").read_text()
    # The completed attempt's claim sentinel is gone — its terminal write
    # already removed it.
    assert not (tmp_path / "receipts" / "dupe-1.claim").exists()

    proc2, _ = run_dispatch(tmp_path, [sys.executable, fake],
                            attempt_id="dupe-1")
    assert proc2.returncode == 2
    assert (tmp_path / "receipts" / "dupe-1.json").read_text() == original
    # The second attempt's own (momentary) claim must not linger either.
    assert not (tmp_path / "receipts" / "dupe-1.claim").exists()


def test_a_missing_prompt_file_is_refused_before_any_receipt_exists(tmp_path):
    """Every pre-spawn input is validated before the receipt is claimed: a
    missing --prompt-file must never leave a permanent STARTING receipt
    behind (status/cancel only know how to unwind RUNNING)."""
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=("--prompt-file",
                                        str(tmp_path / "nonexistent.txt")))
    assert proc.returncode == 2
    assert receipt is None
    assert not (tmp_path / "receipts").exists()


def test_write_receipt_ignores_a_symlink_planted_at_the_old_predictable_tmp_name(
        tmp_path):
    """The vulnerability this closes: the round-2 tmp name was
    `<receipt>.<os.getpid()>.tmp` — guessable the instant a caller reads
    supervisor_pid off any receipt this same process already wrote.
    Sequence that reproduces it: write_receipt runs once (establishing this
    writer's own pid, exactly as a STARTING receipt would disclose it), an
    attacker plants a symlink at that now-known OLD predictable path
    pointing at an external file, then write_receipt runs again for the
    same attempt (standing in for the terminal write). mkstemp's
    unpredictable, O_CREAT|O_EXCL name means the second call never opens
    the planted path at all, so the external target is never touched and
    the planted symlink itself is left exactly as planted."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_agent", SCRIPT)
    dispatch_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch_agent)

    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir(mode=0o700)
    outside_target = tmp_path / "outside.txt"
    outside_target.write_text("do not touch")

    dispatch_agent.write_receipt(
        receipt_dir, {"attempt_id": "dupe-2", "result": {"state": "STARTING"}})

    old_predictable_tmp = receipt_dir / f"dupe-2.json.{os.getpid()}.tmp"
    old_predictable_tmp.symlink_to(outside_target)

    dispatch_agent.write_receipt(
        receipt_dir, {"attempt_id": "dupe-2", "result": {"state": "SUCCEEDED"}})

    assert outside_target.read_text() == "do not touch"
    assert os.path.islink(old_predictable_tmp)
    receipt = json.loads((receipt_dir / "dupe-2.json").read_text())
    assert receipt["result"]["state"] == "SUCCEEDED"


def test_output_open_failure_is_start_failed_with_no_leaked_fd_or_claim(tmp_path):
    """R3-3 moved the stdout/stderr `os.open(..., O_NOFOLLOW)` calls to AFTER
    the STARTING receipt exists and OUTSIDE the START_FAILED `OSError` arm
    that already covers a Popen failure — an ELOOP (a planted symlink), a
    permission failure, or ENOSPC there must terminate the same way: a
    terminal START_FAILED receipt, the claim released, and no leaked fd if
    the first open (stdout) succeeded before the second (stderr) raised. No
    path may leave a STARTING receipt behind. Planting a symlink at the
    stdout path makes O_NOFOLLOW raise ELOOP without needing real
    permission bits, and its target must stay untouched — O_NOFOLLOW
    refused the hop instead of opening (and truncating) through it."""
    fake = write_fake(tmp_path, "happy4.py", HAPPY)
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    outside_target = tmp_path / "outside2.txt"
    outside_target.write_text("do not touch")
    (receipts / "eloop1.stdout").symlink_to(outside_target)

    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 attempt_id="eloop1", schema="none")
    assert proc.returncode == 4, proc.stderr
    assert receipt["result"]["state"] == "START_FAILED"
    assert not (receipts / "eloop1.claim").exists()
    assert outside_target.read_text() == "do not touch"


def test_terminal_receipt_write_failure_is_best_effort_and_observable(
        tmp_path, monkeypatch, capsys):
    """DEFER-2: a failed terminal receipt write still leaves group cleanup
    and the attempt outcome intact. Persistence is best-effort: print
    `receipt write failed` on supervisor stderr, release the claim, do not
    turn the failure into crash exit 9, and leave any leftover tmp gone."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_agent", SCRIPT)
    dispatch_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch_agent)

    real_write = dispatch_agent.write_receipt

    def _write(receipt_dir, receipt):
        if receipt["result"]["state"] not in ("STARTING", "RUNNING"):
            raise OSError("disk full")
        return real_write(receipt_dir, receipt)

    monkeypatch.setattr(dispatch_agent, "write_receipt", _write)

    fake = write_fake(tmp_path, "happy.py", HAPPY)
    receipt_dir = tmp_path / "receipts"
    rc = dispatch_agent.main([
        "run", "--attempt-id", "t1", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "reviewer-1", "--output-schema", "review",
        "--", sys.executable, str(fake)])
    captured = capsys.readouterr()
    assert "receipt write failed" in captured.err
    leftovers = list(receipt_dir.glob("*.tmp")) if receipt_dir.exists() else []
    assert leftovers == []
    assert not (receipt_dir / "t1.claim").exists()
    assert rc == 0


def test_deadline_expiry_is_timed_out_and_confirmed(tmp_path):
    """Without a deadline the supervisor inherits the research docs' core
    finding: an unresponsive model waits forever. The harness timeout is the
    RED phase here — an unimplemented deadline hangs this test."""
    fake = write_fake(tmp_path, "sleeper.py", SLEEPER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=1.0, grace=1.0, harness_timeout=30)
    assert proc.returncode == 3
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["termination_confirmed"] is True
    assert receipt["timing"]["finished_at"]


def test_term_ignorer_is_killed_and_still_timed_out(tmp_path):
    fake = write_fake(tmp_path, "stubborn.py", TERM_IGNORER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=1.0, grace=0.5, harness_timeout=30)
    assert proc.returncode == 3
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["termination_confirmed"] is True


def test_a_verdict_written_after_the_deadline_stays_timed_out(tmp_path):
    """The invariant from DD-9: once the deadline fired, no output can
    produce SUCCEEDED. A partial dump after the kill is not a review."""
    fake = write_fake(tmp_path, "late.py", LATE_WRITER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=1.0, grace=2.0, harness_timeout=30)
    assert proc.returncode == 3
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["schema_valid"] is None
    # the late output exists on disk — and was still not graded
    assert "verdict: PASS" in Path(receipt["result"]["stdout_path"]).read_text()


def test_a_post_spawn_crash_still_confirms_termination_and_writes_a_terminal_receipt(
        tmp_path, monkeypatch):
    """Task 8's `cmd_run` wraps everything after Popen succeeds in
    try/except Exception precisely so a crash there cannot abandon a live
    process group behind a receipt stuck at RUNNING. This is the one
    in-process test in this file: it needs to monkeypatch a name inside the
    module before calling `main()`, which the subprocess-driven harness the
    rest of this file uses cannot do."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_agent", SCRIPT)
    dispatch_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch_agent)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch_agent, "_validate_output", _boom)
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    receipt_dir = tmp_path / "receipts"
    rc = dispatch_agent.main([
        "run", "--attempt-id", "crash1", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "review",
        "--", sys.executable, str(fake)])
    assert rc == 9
    receipt = json.loads((receipt_dir / "crash1.json").read_text())
    assert receipt["result"]["state"] == "CANCELLED"
    assert receipt["result"]["termination_confirmed"] is True
    assert receipt["timing"]["finished_at"]
    pgid = receipt["process"]["process_group_id"]
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


GRANDCHILD_SPAWNER_TIMEOUT = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(60)
"""

ORPHAN_LEAVER = """
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print("verdict: PASS")
"""

FLOODER = """
import sys
chunk = "x" * 65536
for _ in range(160):          # ~10 MB — enough to jam any pipe buffer
    sys.stdout.write(chunk)
sys.stdout.write("\\nverdict: PASS\\n")
"""


def _group_is_dead(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True


def test_timeout_kills_the_grandchild_too(tmp_path):
    """F-02: killing only the leader leaves a live writer behind — the
    duplicate-writer scenario. The whole group must be confirmed dead."""
    fake = write_fake(tmp_path, "spawner.py", GRANDCHILD_SPAWNER_TIMEOUT)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=1.0, grace=2.0, harness_timeout=30)
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["termination_confirmed"] is True
    assert _group_is_dead(receipt["process"]["process_group_id"])


def test_a_clean_exit_that_leaves_an_orphan_is_cleaned_and_confirmed(tmp_path):
    """The leader exiting 0 is not the end of the attempt: an orphaned
    grandchild is still our dispatch. It is reaped before the result is
    called a result."""
    fake = write_fake(tmp_path, "orphan.py", ORPHAN_LEAVER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 grace=2.0, harness_timeout=30)
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["result"]["termination_confirmed"] is True
    assert _group_is_dead(receipt["process"]["process_group_id"])


def test_a_flooding_child_cannot_deadlock_the_supervisor(tmp_path):
    """stdout goes to a file, not a pipe — 10 MB must complete, not jam."""
    fake = write_fake(tmp_path, "flooder.py", FLOODER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=30.0, harness_timeout=60)
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert Path(receipt["result"]["stdout_path"]).stat().st_size > 10_000_000


GRANDCHILD_TERM_IGNORER_QUICK_LEADER = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
time.sleep(0.3)
print("verdict: PASS")
"""


def test_grading_is_gated_on_the_deadline_even_after_the_leader_exits_0(tmp_path):
    """DD-9's invariant ("no output can produce SUCCEEDED once the deadline
    has expired") is about the moment GRADING happens, not merely the
    moment the leader exited. Here the leader exits 0 well inside the
    deadline, but its TERM-ignoring grandchild forces the normal-exit
    branch's group-confirmation cleanup to run past the deadline before
    grading is ever reached. The gate sits between confirmation and
    grading: past the deadline, the state is TIMED_OUT regardless of the
    leader's exit status, exit_status is still recorded, and schema_valid
    stays null — a post-deadline result is never graded."""
    fake = write_fake(tmp_path, "quick_leader_stuck_grandchild.py",
                      GRANDCHILD_TERM_IGNORER_QUICK_LEADER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=0.5, grace=1.0, harness_timeout=30)
    assert proc.returncode == 3
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["exit_status"] == 0
    assert receipt["result"]["schema_valid"] is None
    assert receipt["result"]["termination_confirmed"] is True


def _start_supervised_sleeper(tmp_path, attempt_id="bg1"):
    fake = write_fake(tmp_path, "sleeper_bg.py", SLEEPER)
    supervisor = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id,
         "--receipt-dir", str(tmp_path / "receipts"),
         "--deadline-seconds", "60", "--grace-seconds", "1",
         "--seat", "worker", "--output-schema", "none",
         "--", sys.executable, str(fake)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    receipt_path = tmp_path / "receipts" / f"{attempt_id}.json"
    for _ in range(100):                       # wait for RUNNING, <=5 s
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt["result"]["state"] == "RUNNING":
                return supervisor, receipt
        time.sleep(0.05)
    supervisor.kill()
    pytest.fail("supervisor never reached RUNNING")


def _agent(args, tmp_path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args,
         "--receipt-dir", str(tmp_path / "receipts")],
        capture_output=True, text=True, timeout=30)


def test_status_reports_running_with_liveness(tmp_path):
    supervisor, _ = _start_supervised_sleeper(tmp_path)
    try:
        proc = _agent(["status", "--attempt-id", "bg1"], tmp_path)
        assert proc.returncode == 0
        shown = json.loads(proc.stdout)
        assert shown["result"]["state"] == "RUNNING"
        assert shown["process_alive"] is True
        assert shown["supervision"] == "supervised"
    finally:
        _agent(["cancel", "--attempt-id", "bg1"], tmp_path)
        supervisor.wait(timeout=30)


def test_status_detects_an_orphaned_child_when_the_supervisor_died(tmp_path):
    """The receipt's own supervisor_pid is what makes this detectable: if the
    `run` process dies while the child lives on, process_alive alone (which
    only checks the child's group) would report a plain RUNNING attempt as
    if someone were still watching its deadline. `status` must say
    'orphaned' instead — a stale/orphaned RUNNING receipt requires cancel
    before any retry (F-02: a retry behind a possibly-live writer is two
    writers on the same files)."""
    supervisor, receipt = _start_supervised_sleeper(tmp_path, attempt_id="bg5")
    try:
        path = tmp_path / "receipts" / "bg5.json"
        on_disk = json.loads(path.read_text())
        dead_pid = 999999  # not our process — the group's own child is
        # still alive, so this cannot collide with a real pid this test uses
        on_disk["process"]["supervisor_pid"] = dead_pid
        path.write_text(json.dumps(on_disk))
        proc = _agent(["status", "--attempt-id", "bg5"], tmp_path)
        assert proc.returncode == 0
        shown = json.loads(proc.stdout)
        assert shown["process_alive"] is True
        assert shown["supervision"] == "orphaned"
    finally:
        # `cancel` cannot clean this up: the on-disk supervisor_pid above
        # was overwritten to a dead pid to simulate the orphaned case, so
        # cancel correctly refuses to signal (the stale-refusal rule this
        # test's sibling exercises directly) — using it here would leave
        # the real 60s sleeper outliving supervisor.wait below. Kill the
        # recorded process group directly instead.
        pgid = receipt["process"]["process_group_id"]
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        supervisor.wait(timeout=30)


def test_cancel_confirms_and_the_run_supervisor_preserves_it(tmp_path):
    """cancel and run race on the receipt; the cancel verdict wins — the
    attempt WAS killed, whatever the child's exit looked like to run."""
    supervisor, receipt = _start_supervised_sleeper(tmp_path, attempt_id="bg2")
    proc = _agent(["cancel", "--attempt-id", "bg2", "--grace-seconds", "2"],
                  tmp_path)
    assert proc.returncode == 7, proc.stderr
    supervisor.wait(timeout=30)
    final = json.loads((tmp_path / "receipts" / "bg2.json").read_text())
    assert final["result"]["state"] == "CANCELLED"
    assert final["result"]["termination_confirmed"] is True
    assert _group_is_dead(receipt["process"]["process_group_id"])


def test_cancel_of_a_finished_attempt_is_refused(tmp_path):
    fake = write_fake(tmp_path, "happy2.py", HAPPY)
    run_dispatch(tmp_path, [sys.executable, fake], attempt_id="done1")
    proc = _agent(["cancel", "--attempt-id", "done1"], tmp_path)
    assert proc.returncode == 0            # already SUCCEEDED — nothing to kill
    assert "not RUNNING" in proc.stderr


def test_cancel_refuses_to_signal_when_the_recorded_supervisor_is_dead(tmp_path):
    """A stale receipt's recorded pgid may have been reused by an unrelated
    process once the supervisor that watched it is gone — cancel must not
    kill blind. POSIX has no portable check that a pgid still identifies
    the same process group, so a dead supervisor means refuse, not
    signal."""
    supervisor, receipt = _start_supervised_sleeper(tmp_path, attempt_id="bg6")
    pgid = receipt["process"]["process_group_id"]
    try:
        path = tmp_path / "receipts" / "bg6.json"
        on_disk = json.loads(path.read_text())
        dead_pid = 999999  # not our process — the child group is still
        # alive, so this cannot collide with a real pid this test uses
        on_disk["process"]["supervisor_pid"] = dead_pid
        path.write_text(json.dumps(on_disk))

        proc = _agent(["cancel", "--attempt-id", "bg6"], tmp_path)
        assert proc.returncode == 5, proc.stderr
        final = json.loads(path.read_text())
        assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"
        assert final["result"]["termination_confirmed"] is False
        # cancel must not have touched the group — it is still alive
        assert not _group_is_dead(pgid)
    finally:
        os.killpg(pgid, signal.SIGKILL)  # test cleanup, not the code under test
        supervisor.wait(timeout=30)


def test_cancel_of_a_stale_starting_receipt_is_terminal_with_no_signal(tmp_path):
    """A STARTING receipt whose supervisor died before ever reaching RUNNING
    is stale exactly like a stale RUNNING receipt — there is no live
    supervisor to trust a pgid's identity against, and a STARTING receipt
    may not even have a pgid yet. cancel must still resolve it to a
    terminal state (so a caller is not stuck polling a receipt nothing will
    ever finish) without sending any signal — there is nothing recorded
    that it could safely signal."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    dead_pid = 999999  # not our process
    starting = {
        "attempt_id": "stale-starting", "seat": "worker",
        "process": {"pid": None, "process_group_id": None,
                    "supervisor_pid": dead_pid},
        "timing": {"started_at": None, "deadline_at": None, "finished_at": None},
        "result": {"state": "STARTING", "exit_status": None,
                  "stdout_path": None, "stderr_path": None,
                  "output_sha256": None, "schema_valid": None,
                  "termination_confirmed": None},
    }
    (receipts / "stale-starting.json").write_text(json.dumps(starting))
    proc = _agent(["cancel", "--attempt-id", "stale-starting"], tmp_path)
    assert proc.returncode == 5, proc.stderr
    final = json.loads((receipts / "stale-starting.json").read_text())
    assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"
    assert final["result"]["termination_confirmed"] is False


def test_cancel_of_a_live_starting_receipt_refuses_without_a_signal(tmp_path):
    """A STARTING receipt whose supervisor is alive but has not yet reached
    RUNNING has no child process group to signal — cancel refuses instead
    of guessing, and must leave the receipt exactly as it found it (simulate
    the live supervisor with this test process's own pid, since it is
    guaranteed alive for the duration of the call)."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    starting = {
        "attempt_id": "live-starting", "seat": "worker",
        "process": {"pid": None, "process_group_id": None,
                    "supervisor_pid": os.getpid()},
        "timing": {"started_at": None, "deadline_at": None, "finished_at": None},
        "result": {"state": "STARTING", "exit_status": None,
                  "stdout_path": None, "stderr_path": None,
                  "output_sha256": None, "schema_valid": None,
                  "termination_confirmed": None},
    }
    payload = json.dumps(starting)
    (receipts / "live-starting.json").write_text(payload)
    proc = _agent(["cancel", "--attempt-id", "live-starting"], tmp_path)
    assert proc.returncode == 2, proc.stderr
    assert "not yet RUNNING" in proc.stderr
    assert (receipts / "live-starting.json").read_text() == payload


def test_status_reports_a_claim_with_no_receipt_yet_as_claimed(tmp_path):
    """CLAIMED is a report-only label for the window between a successful
    O_CREAT|O_EXCL claim (Task 8) and the first receipt write — normally too
    narrow to observe by racing a real `run`, so simulated directly here by
    writing only the sentinel file. It must never be confused with a
    receipt state: there is no receipt to read one out of yet."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "claimed-only.claim").write_text("")
    proc = _agent(["status", "--attempt-id", "claimed-only"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    shown = json.loads(proc.stdout)
    assert shown["state"] == "CLAIMED"


def test_run_preserves_a_termination_unconfirmed_receipt_too(tmp_path):
    """The preservation check must not stop at CANCELLED: a cancel whose own
    kill ladder could not confirm the group dead writes
    TERMINATION_UNCONFIRMED, and that verdict is just as authoritative — it
    is the one state that must block a write-capable retry (DD-10), so
    `run` must never overwrite it with its own conclusion (typically
    TIMED_OUT once the signal actually lands). Direct-state simulation:
    writing TERMINATION_UNCONFIRMED onto the on-disk receipt stands in for
    an external cancel process reaching that same verdict — the
    preservation check in `cmd_run` reads whatever state is on disk, not
    who wrote it."""
    fake = write_fake(tmp_path, "sleeper_bg2.py", SLEEPER)
    attempt_id = "bg4"
    supervisor = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id,
         "--receipt-dir", str(tmp_path / "receipts"),
         "--deadline-seconds", "1", "--grace-seconds", "1",
         "--seat", "worker", "--output-schema", "none",
         "--", sys.executable, str(fake)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    receipt_path = tmp_path / "receipts" / f"{attempt_id}.json"
    for _ in range(100):                       # wait for RUNNING, <=5 s
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt["result"]["state"] == "RUNNING":
                break
        time.sleep(0.05)
    else:
        supervisor.kill()
        pytest.fail("supervisor never reached RUNNING")
    on_disk = json.loads(receipt_path.read_text())
    on_disk["result"]["state"] = "TERMINATION_UNCONFIRMED"
    on_disk["result"]["termination_confirmed"] = False
    receipt_path.write_text(json.dumps(on_disk))
    supervisor.wait(timeout=30)
    final = json.loads(receipt_path.read_text())
    assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"


def test_a_post_spawn_crash_never_relabels_an_already_terminal_receipt(
        tmp_path, monkeypatch):
    """Task 8's post-spawn `except Exception` handler used to write its own
    conclusion (CANCELLED/TERMINATION_UNCONFIRMED) unconditionally — if an
    external `cancel` had already landed a terminal state for this same
    attempt in the narrow window before the crash handler's own write, the
    crash handler's write would relabel it: exactly the relabeling hazard
    design doc DD-9 documents for the run/cancel race, now reachable from
    the crash path too (ITEM-V-4). The fix: both the normal tail (the test
    above) and this crash handler now go through the shared
    `_commit_terminal` helper, so whichever terminal state is on disk right
    before the final atomic write wins, no matter which of the two code
    paths gets there last. Direct-state simulation stands in for an actual
    concurrent `cancel`, exactly as the test above does for the normal
    tail — the preservation check reads whatever state is on disk, not who
    wrote it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_agent", SCRIPT)
    dispatch_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch_agent)

    receipt_dir = tmp_path / "receipts"

    def _plant_termination_unconfirmed_then_boom(stdout_path, output_schema):
        on_disk = json.loads((receipt_dir / "crash3.json").read_text())
        on_disk["result"]["state"] = "TERMINATION_UNCONFIRMED"
        on_disk["result"]["termination_confirmed"] = False
        (receipt_dir / "crash3.json").write_text(json.dumps(on_disk))
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch_agent, "_validate_output",
                        _plant_termination_unconfirmed_then_boom)
    fake = write_fake(tmp_path, "happy3.py", HAPPY)
    rc = dispatch_agent.main([
        "run", "--attempt-id", "crash3", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "review",
        "--", sys.executable, str(fake)])
    assert rc == 9
    receipt = json.loads((receipt_dir / "crash3.json").read_text())
    # The crash handler's own conclusion would have been CANCELLED (the
    # group WAS confirmed dead — the child had already exited 0 before
    # _validate_output ever ran) — the pre-planted TERMINATION_UNCONFIRMED
    # must win instead.
    assert receipt["result"]["state"] == "TERMINATION_UNCONFIRMED"
    assert receipt["result"]["termination_confirmed"] is False
    assert not (receipt_dir / "crash3.claim").exists()


def test_cancel_of_a_claim_only_attempt_refuses_without_touching_the_claim(tmp_path):
    """A claim sentinel with no receipt yet (Task 8's narrow claim-then-
    STARTING window, or a supervisor that crashed inside it) must not crash
    `cancel` — `read_receipt` would raise `FileNotFoundError` on a bare
    attempt id with only a claim (ITEM-V-5). There is nothing recorded to
    signal yet (no pgid, no supervisor_pid to check liveness against), so
    `cancel` refuses without touching the claim; the documented remedy
    (confirm the claimer is dead, then delete the claim manually) stays
    manual — no automatic claim garbage collection."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "claim-only-1.claim").write_text("")
    proc = _agent(["cancel", "--attempt-id", "claim-only-1"], tmp_path)
    assert proc.returncode == 2, proc.stderr
    assert "claimed but never started" in proc.stderr
    assert (receipts / "claim-only-1.claim").exists()
    assert not (receipts / "claim-only-1.json").exists()


def test_cancel_of_a_stale_running_receipt_removes_the_claim_too(tmp_path):
    """`cancel`'s stale-supervisor branch writes a terminal
    TERMINATION_UNCONFIRMED receipt — the rule that any command writing a
    terminal receipt also removes the claim (ITEM-V-5) applies here exactly
    as it does to `run`'s own terminal writes. Leaving the claim behind
    would contradict `status`'s CLAIMED report: a claim with no attempt
    behind it, when a terminal receipt in fact already exists."""
    supervisor, receipt = _start_supervised_sleeper(tmp_path, attempt_id="bg7")
    pgid = receipt["process"]["process_group_id"]
    try:
        path = tmp_path / "receipts" / "bg7.json"
        claim_path = tmp_path / "receipts" / "bg7.claim"
        assert claim_path.exists()          # still RUNNING — not yet released
        on_disk = json.loads(path.read_text())
        dead_pid = 999999  # not our process — the child group is still
        # alive, so this cannot collide with a real pid this test uses
        on_disk["process"]["supervisor_pid"] = dead_pid
        path.write_text(json.dumps(on_disk))

        proc = _agent(["cancel", "--attempt-id", "bg7"], tmp_path)
        assert proc.returncode == 5, proc.stderr
        final = json.loads(path.read_text())
        assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"
        assert not claim_path.exists()
    finally:
        os.killpg(pgid, signal.SIGKILL)  # test cleanup, not the code under test
        supervisor.wait(timeout=30)


def _fake_receipt(tmp_path, attempt_id, seat, state, output_schema="review",
                   schema_valid=True, model_id=None, decision_fingerprint=None):
    receipts = tmp_path / "receipts"
    receipts.mkdir(exist_ok=True)
    payload = {
        "attempt_id": attempt_id, "seat": seat,
        "result": {"state": state},
        "output_schema": output_schema,
        # design §4 B2/B3 — the declared model and the decision this seat
        # was dispatched under. Both null unless a test names them, which is
        # exactly the shape a caller that passed neither arg produces.
        "model_id": model_id,
        "decision_fingerprint": decision_fingerprint,
    }
    if schema_valid is not None:
        payload["result"]["schema_valid"] = schema_valid
    (receipts / f"{attempt_id}.json").write_text(json.dumps(payload))


def _verify(tmp_path, ids, count, extra=()):
    return _agent(["verify-evidence", "--ids", ids,
                   "--expect-count", str(count), *extra], tmp_path).returncode


def test_verify_evidence_accepts_exactly_the_valid_set(tmp_path):
    _fake_receipt(tmp_path, "r1", "reviewer-1", "SUCCEEDED")
    _fake_receipt(tmp_path, "r2", "reviewer-2", "SUCCEEDED")
    proc = _agent(["verify-evidence", "--ids", "r1,r2", "--expect-count", "2"],
                  tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_verify_evidence_rejects_a_seat_that_did_not_succeed(tmp_path):
    _fake_receipt(tmp_path, "r1", "reviewer-1", "SUCCEEDED")
    _fake_receipt(tmp_path, "r2", "reviewer-2", "TIMED_OUT")
    proc = _agent(["verify-evidence", "--ids", "r1,r2", "--expect-count", "2"],
                  tmp_path)
    assert proc.returncode == 1
    assert "TIMED_OUT" in proc.stderr


def test_verify_evidence_rejects_surplus_missing_and_duplicate_seats(tmp_path):
    _fake_receipt(tmp_path, "r1", "reviewer-1", "SUCCEEDED")
    _fake_receipt(tmp_path, "r2", "reviewer-1", "SUCCEEDED")   # same seat twice
    for ids, count in (("r1,r2,r3", "2"), ("r1", "2"), ("r1,r2", "2")):
        proc = _agent(["verify-evidence", "--ids", ids, "--expect-count", count],
                      tmp_path)
        assert proc.returncode == 1, (ids, count, proc.stderr)


def test_verify_evidence_rejects_a_receipt_without_a_valid_review_schema(tmp_path):
    """A SUCCEEDED receipt is not enough — verify-evidence must also confirm
    the attempt actually produced a schema-valid review, not merely that the
    process exited 0. schema_valid absent or output_schema != "review" is a
    weak receipt regardless of state."""
    _fake_receipt(tmp_path, "r1", "reviewer-1", "SUCCEEDED",
                  output_schema="none", schema_valid=None)
    _fake_receipt(tmp_path, "r2", "reviewer-2", "SUCCEEDED")
    proc = _agent(["verify-evidence", "--ids", "r1,r2", "--expect-count", "2"],
                  tmp_path)
    assert proc.returncode == 1
    assert "r1" in proc.stderr


def test_status_cancel_and_verify_evidence_reject_a_traversal_id_untouched(tmp_path):
    """The validation chokepoint (_validated_attempt_id) is shared across
    every subcommand, not just `run` — a crafted id must never reach a path
    built from it, whether it names an attempt to inspect, kill, or verify,
    and the rejection must happen before any filesystem access."""
    traversal = "../../escape"
    for cli_args in (["status", "--attempt-id", traversal],
                     ["cancel", "--attempt-id", traversal],
                     ["verify-evidence", "--ids", traversal,
                      "--expect-count", "1"]):
        proc = _agent(cli_args, tmp_path)
        assert proc.returncode == 2, (cli_args, proc.stderr)
    assert not (tmp_path / "receipts").exists()
    assert not (tmp_path.parent / "escape.json").exists()




def test_status_on_an_unknown_attempt_id_is_a_usage_error_not_a_crash(tmp_path):
    """B5 (audit 2026-08-18). With neither receipt nor claim, `status` fell
    through to `read_receipt`, and the FileNotFoundError escaped to the crash
    guard: a traceback and exit 9, the status this module reserves for "a
    crash, never an attempt outcome". `cancel` has always answered the same
    situation with one sentence and exit 2; asking about an id nobody created
    is a usage error either way."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    proc = _agent(["status", "--attempt-id", "never-created"], tmp_path)
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr
    assert "never-created" in proc.stderr
    assert "no receipt and no claim" in proc.stderr


def test_status_on_a_missing_receipt_dir_is_a_usage_error_too(tmp_path):
    """The directory itself not existing is the same question with the same
    answer — not a different failure mode to be discovered by traceback."""
    proc = _agent(["status", "--attempt-id", "never-created"], tmp_path)
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr


# ---------------------------------------------------------------------------
# Coverage top-ups from the 2026-08-18 audit §5
# ---------------------------------------------------------------------------

RECEIPT_KEYS = {
    "attempt_id", "seat", "runtime", "model_id", "effort_native",
    "permission_mode", "argv", "prompt_sha256", "output_schema",
    "process", "timing", "result",
    # design §4 B2 — decision linkage and the requested-vs-served slots.
    "decision_fingerprint", "policy_sha256", "transport_id",
    "host_cli_version", "observed_model_id", "observed_model_source",
    # 2026-08-25 grok seat integrity — DD-1 declares the envelope contract,
    # DD-3 the effective-policy evidence read from the session directory.
    "output_envelope", "session_evidence",
    # Issue #19 maker-seat prevention — always present, null when undeclared.
    "child_cwd", "grok_home", "seat_profile", "require_single_linked_cwd",
}
RECEIPT_PROCESS_KEYS = {"pid", "process_group_id", "supervisor_pid"}
RECEIPT_TIMING_KEYS = {
    "started_at", "deadline_at", "finished_at",
    # DD-3: the untruncated pre-Popen anchor the session freshness window
    # opens at. `started_at` is stamped AFTER Popen, so it is unusable as a
    # lower bound.
    "launch_anchor_at",
}
RECEIPT_RESULT_KEYS = {
    "state", "exit_status", "stdout_path", "stderr_path", "output_sha256",
    "schema_valid", "termination_confirmed",
    # 2026-08-25 grok seat integrity — DD-1 (envelope evidence) and DD-5
    # (the shared cause vocabulary). Both are always present and null on
    # the undeclared path, which is what keeps this an equality contract
    # over a single key set rather than one set per declaration combination.
    "envelope", "invalid_reasons",
    # DD-2 — the per-artifact proof set, null when nothing was required and
    # on every non-grading termination.
    "artifacts",
    # Issue #19 — ProfileApplied.enforced evidence, null when undeclared.
    "sandbox_events",
}


def test_receipt_carries_exactly_the_documented_fields(tmp_path):
    """Equality, not membership. Every other receipt assertion in this file
    names the fields it cares about, so a field silently added (or a terminal
    write dropping one) is invisible to all of them — and the receipt is the
    only durable record of what an attempt did."""
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    _, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert set(receipt) == RECEIPT_KEYS
    assert set(receipt["process"]) == RECEIPT_PROCESS_KEYS
    assert set(receipt["timing"]) == RECEIPT_TIMING_KEYS
    assert set(receipt["result"]) == RECEIPT_RESULT_KEYS


def test_a_route_can_be_dispatched_and_verified_end_to_end(tmp_path):
    """The two layers are tested apart and never together, so nothing checks
    that a route's own reviewer count is the number `verify-evidence` will
    accept. This walks the seam: route, dispatch one fake reviewer per seat the
    route asked for, then verify the evidence set against that same count."""
    router = SKILL / "scripts" / "route_task.py"
    routed = subprocess.run(
        [sys.executable, str(router), "--class", "IMPLEMENTATION",
         "--complexity", "2", "--uncertainty", "2", "--blast-radius", "2",
         "--reversibility", "1", "--format", "json"],
        capture_output=True, text=True, timeout=60)
    assert routed.returncode in (0, 3, 4), routed.stderr
    decision = json.loads(routed.stdout)
    seats = decision["review"]["reviewers"]
    assert seats and decision["terminal"] is None

    fake = write_fake(tmp_path, "reviewer.py", HAPPY)

    def dispatch(attempt_id, seat, model_id):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "run",
             "--attempt-id", attempt_id,
             "--receipt-dir", str(tmp_path / "receipts"),
             "--deadline-seconds", "30", "--grace-seconds", "1",
             "--seat", seat, "--model-id", model_id,
             "--effort-native", decision["review"]["effort"],
             "--output-schema", "review",
             "--", sys.executable, str(fake)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return attempt_id

    ids = [dispatch(f"e2e-{i}", seat, decision["review"]["reviewer_models"][i])
           for i, seat in enumerate(seats)]

    # The count the route asked for is the count the evidence check enforces.
    # That equality is the whole seam, and neither file could see it alone.
    verdict = _agent(["verify-evidence", "--ids", ",".join(ids),
                      "--expect-count", str(len(seats))], tmp_path)
    assert verdict.returncode == 0, verdict.stderr

    # And the route's own rule is exact-count: one id short must fail on both
    # sides of the seam.
    short = _agent(["verify-evidence", "--ids", ids[0],
                    "--expect-count", str(len(seats))], tmp_path)
    assert short.returncode != 0

    routed_back = subprocess.run(
        [sys.executable, str(router), "--class", "IMPLEMENTATION",
         "--complexity", "2", "--uncertainty", "2", "--blast-radius", "2",
         "--reversibility", "1", "--format", "json",
         "--isolation", "available", "--isolation-evidence", ",".join(ids)],
        capture_output=True, text=True, timeout=60)
    assert json.loads(routed_back.stdout)["review"]["review_independence"] == "enforced"


def test_new_linkage_args_round_trip_into_the_receipt(tmp_path):
    """design §4 B2: 네 caller-supplied 값은 그대로 receipt에 실리고,
    observed 쌍은 이 트랜치에서 항상 기본값이다 (자리 확정 — unknown을
    verified로 승격하는 경로는 없다)."""
    fp, ps = "ab" * 32, "cd" * 32
    fake = write_fake(tmp_path, "rt.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake], attempt_id="rt1",
        extra=["--decision-fingerprint", fp, "--policy-sha256", ps,
               "--transport-id", "claude_code.to_openai",
               "--host-cli-version", "codex 0.148.0"])
    assert proc.returncode == 0, proc.stderr
    assert receipt["decision_fingerprint"] == fp
    assert receipt["policy_sha256"] == ps
    assert receipt["transport_id"] == "claude_code.to_openai"
    assert receipt["host_cli_version"] == "codex 0.148.0"
    assert receipt["observed_model_id"] is None
    assert receipt["observed_model_source"] == "unavailable"


def test_malformed_digest_args_are_refused_pre_spawn(tmp_path):
    """오타가 receipt에 영구 기록되기 전에 죽는다 — 비-hex, 대문자, 길이
    오류 전부 exit 2이고 receipt는 만들어지지 않는다."""
    fake = write_fake(tmp_path, "bad.py", HAPPY)
    # The trailing-newline case is round-1 review F1: Python's `$` also matches
    # just before a final newline, so `^[0-9a-f]{64}$` passed a 65-char value
    # and the malformed digest was written verbatim into the permanent
    # receipt — the one outcome this gate exists to prevent.
    for i, bad in enumerate(("xyz", "AB" * 32, "ab" * 31, "ab" * 32 + "\n")):
        attempt = f"bad-{i}"
        proc, receipt = run_dispatch(
            tmp_path, [sys.executable, fake], attempt_id=attempt,
            extra=["--decision-fingerprint", bad])
        assert proc.returncode == 2, (bad, proc.stderr)
        assert receipt is None, (
            "a malformed digest must fail before any receipt exists")


def test_expect_models_matches_declared_models_as_a_multiset(tmp_path):
    _fake_receipt(tmp_path, "e1", "reviewer-1", "SUCCEEDED",
                  model_id="claude-opus-5")
    _fake_receipt(tmp_path, "e2", "reviewer-2", "SUCCEEDED",
                  model_id="gpt-5.6-sol")
    ok = _verify(tmp_path, "e1,e2", 2,
                 extra=["--expect-models", "gpt-5.6-sol,claude-opus-5"])
    assert ok == 0                      # 순서 무관
    bad = _verify(tmp_path, "e1,e2", 2,
                  extra=["--expect-models", "claude-opus-5,grok-4.6"])
    assert bad == 1                     # 불일치는 문제로 보고


def test_expect_models_rejects_duplicates_empties_and_count_mismatch(tmp_path):
    _fake_receipt(tmp_path, "e3", "reviewer-1", "SUCCEEDED",
                  model_id="claude-opus-5")
    assert _verify(tmp_path, "e3", 1, extra=["--expect-models", "a,,b"]) == 2
    assert _verify(tmp_path, "e3", 1, extra=["--expect-models", "a,a"]) == 2
    assert _verify(tmp_path, "e3", 1, extra=["--expect-models", "a,b"]) == 2  # count 1 != 2


def test_expect_models_flags_a_null_model_receipt(tmp_path):
    _fake_receipt(tmp_path, "e4", "reviewer-1", "SUCCEEDED", model_id=None)
    assert _verify(tmp_path, "e4", 1,
                   extra=["--expect-models", "claude-opus-5"]) == 1


def test_expect_fingerprint_checks_every_receipt_and_grammar(tmp_path):
    fp = "ab" * 32
    _fake_receipt(tmp_path, "e5", "reviewer-1", "SUCCEEDED",
                  model_id="claude-opus-5", decision_fingerprint=fp)
    _fake_receipt(tmp_path, "e6", "reviewer-2", "SUCCEEDED",
                  model_id="gpt-5.6-sol", decision_fingerprint=fp)
    assert _verify(tmp_path, "e5,e6", 2, extra=["--expect-fingerprint", fp]) == 0
    assert _verify(tmp_path, "e5,e6", 2,
                   extra=["--expect-fingerprint", "cd" * 32]) == 1
    assert _verify(tmp_path, "e5,e6", 2,
                   extra=["--expect-fingerprint", "nothex"]) == 2
    _fake_receipt(tmp_path, "e7", "reviewer-1", "SUCCEEDED",
                  model_id="claude-opus-5", decision_fingerprint=None)
    assert _verify(tmp_path, "e7", 1, extra=["--expect-fingerprint", fp]) == 1


def _run_fake_review(tmp_path, attempt_id, seat, model_id, extra=()):
    """One supervised fake reviewer that prints a PASS verdict — the same
    child the E2E above uses, with the caller's extra `run` args spliced in."""
    fake = write_fake(tmp_path, f"{attempt_id}.py", HAPPY)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id,
         "--receipt-dir", str(tmp_path / "receipts"),
         "--deadline-seconds", "30", "--grace-seconds", "1",
         "--seat", seat, "--model-id", model_id,
         "--output-schema", "review", *extra,
         "--", sys.executable, str(fake)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return attempt_id


def test_route_fingerprint_flows_into_receipts_and_verify(tmp_path):
    """design §4 B4: route JSON -> dispatch 인자 -> receipt -> verify-evidence
    --expect-fingerprint/--expect-models 로 이어지는 리뷰 증거 사슬."""
    sys.path.insert(0, str(SKILL / "scripts"))
    from route_task import Task, route
    out = route(Task(task_class="IMPLEMENTATION", complexity=2, uncertainty=1,
                     blast_radius=2, reversibility=1,
                     flags=["auth_sensitive"]))
    fp = out["decision_fingerprint"]
    models = out["review"]["reviewer_models"]
    assert len(models) == 2 and len(set(models)) == 2
    for i, model in enumerate(models, 1):
        _run_fake_review(tmp_path, f"seat{i}", seat=f"reviewer-{i}",
                         model_id=model,
                         extra=["--decision-fingerprint", fp,
                                "--policy-sha256", out["policy_sha256"]])
    assert _verify(tmp_path, "seat1,seat2", 2,
                   extra=["--expect-fingerprint", fp,
                          "--expect-models", ",".join(models)]) == 0
    # 불일치 주입: 다른 결정의 fingerprint는 거부된다
    other = route(Task(task_class="MECHANICAL", complexity=0, uncertainty=0,
                       blast_radius=0, reversibility=0))
    assert other["decision_fingerprint"] != fp
    assert _verify(tmp_path, "seat1,seat2", 2,
                   extra=["--expect-fingerprint",
                          other["decision_fingerprint"]]) == 1


def test_expect_fingerprint_rejects_a_trailing_newline_as_usage_error(tmp_path):
    """round-1 review F1, the verify side: a newline-terminated expectation
    must be a usage error (2), not an evidence problem (1) — misreporting a
    caller's typo as an integrity failure erases the distinction B3 draws."""
    _fake_receipt(tmp_path, "nl1", "reviewer-1", "SUCCEEDED",
                  model_id="claude-opus-5", decision_fingerprint="ab" * 32)
    assert _verify(tmp_path, "nl1", 1,
                   extra=["--expect-fingerprint", "ab" * 32 + "\n"]) == 2


def test_an_attempt_id_with_a_trailing_newline_is_refused(tmp_path):
    """round-2 review (pre-existing, fixed here because it is the identical
    `$` idiom two lines from the one this release introduced): a newline-
    terminated attempt id passed `^...$` and went on to name a receipt FILE.
    """
    fake = write_fake(tmp_path, "nlid.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 attempt_id="ok-id\n")
    assert proc.returncode == 2, proc.stderr
    assert receipt is None
    assert not list((tmp_path / "receipts").glob("*")) or not any(
        "\n" in q.name for q in (tmp_path / "receipts").glob("*"))


# ---------------------------------------------------------------------------
# DD-1 — grok stdout envelope contract (`--output-envelope`) and the
# declaration-consistency preflight. Design:
# docs/design/2026-08-25-grok-seat-integrity-design.md §4 DD-1.
# ---------------------------------------------------------------------------

SESSION_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "99999999-8888-7777-6666-555555555555"


def envelope_fake(doc=None, *, raw=None, exit_code=0):
    """A fake child whose stdout is one grok headless JSON document.

    `raw` writes bytes verbatim (non-JSON / oversized / truncated cases);
    `doc` is serialized. Nothing here invokes a real model.
    """
    payload = raw if raw is not None else json.dumps(doc)
    return (
        "import sys\n"
        f"sys.stdout.write({payload!r})\n"
        "sys.stdout.flush()\n"
        f"sys.exit({exit_code})\n"
    )


def grok_doc(stop_reason="end_turn", text="verdict: PASS\nconfidence: 0.9",
             session_id=SESSION_UUID, model_usage=("grok-4.6-build",)):
    doc = {"stopReason": stop_reason, "text": text, "sessionId": session_id,
           "num_turns": 1, "requestId": "req-1"}
    if model_usage is not None:
        doc["modelUsage"] = {name: {"modelCalls": 1} for name in model_usage}
    return doc


ENVELOPE_ARGS = ("--output-envelope", "grok-headless-json-v1")


def test_envelope_end_turn_with_verdict_in_text_is_succeeded(tmp_path):
    """The verdict grammar moves off raw stdout onto the envelope's `text`
    field: raw stdout here is a JSON document whose own bytes never match
    VERDICT_RE at line start, so a SUCCEEDED can only come from reading the
    envelope."""
    fake = write_fake(tmp_path, "env_ok.py", envelope_fake(grok_doc()))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=ENVELOPE_ARGS)
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["output_envelope"] == "grok-headless-json-v1"
    assert receipt["result"]["envelope"]["parse_ok"] is True
    assert receipt["result"]["envelope"]["stop_reason"] == "end_turn"
    assert receipt["result"]["envelope"]["session_id"] == SESSION_UUID
    assert receipt["result"]["envelope"]["served_models"] == ["grok-4.6-build"]
    assert receipt["result"]["envelope"]["error_type"] is None
    assert receipt["result"]["invalid_reasons"] is None


def test_envelope_cancelled_with_schema_none_is_invalid_output(tmp_path):
    """Issue #14's exact regression: a permission-cancelled grok turn exits
    0 with prose in `text` and no required output schema. Before DD-1 this
    was recorded SUCCEEDED."""
    doc = grok_doc(stop_reason="cancelled",
                   text="I'll create the plan file now.")
    fake = write_fake(tmp_path, "env_cancel.py", envelope_fake(doc))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none", extra=ENVELOPE_ARGS)
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert receipt["result"]["envelope"]["stop_reason"] == "cancelled"
    assert "envelope_stop_reason:cancelled" in receipt["result"]["invalid_reasons"]


def test_envelope_cancelled_with_verdict_text_is_still_invalid_output(tmp_path):
    """The envelope is graded before the output schema — a cancelled turn
    that happens to have emitted a well-formed verdict is still a cancelled
    turn."""
    doc = grok_doc(stop_reason="cancelled")
    fake = write_fake(tmp_path, "env_cancel2.py", envelope_fake(doc))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="review", extra=ENVELOPE_ARGS)
    assert proc.returncode == 6
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "envelope_stop_reason:cancelled" in receipt["result"]["invalid_reasons"]


@pytest.mark.parametrize("name,doc,raw,reason", [
    ("unknown_stop_reason", grok_doc(stop_reason="max_tokens"), None,
     "envelope_stop_reason:max_tokens"),
    ("novel_stop_reason", grok_doc(stop_reason="wandered_off"), None,
     "envelope_stop_reason:wandered_off"),
    ("absent_stop_reason", {"text": "verdict: PASS"}, None,
     "envelope_stop_reason:<absent>"),
    ("non_json", None, "verdict: PASS\nnot json at all\n",
     "envelope_unparseable"),
    ("json_but_not_an_object", None, '["verdict: PASS"]',
     "envelope_unparseable"),
    ("empty", None, "", "envelope_unparseable"),
])
def test_envelope_unknown_stop_reason_or_non_json_fails_closed(
        tmp_path, name, doc, raw, reason):
    """Fail-closed: the official [UG-14] vocabulary minus `end_turn`, any
    unknown value, an absent field, and anything that is not a single JSON
    object all land on INVALID_OUTPUT."""
    fake = write_fake(tmp_path, f"env_{name}.py",
                      envelope_fake(doc, raw=raw))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="review", extra=ENVELOPE_ARGS)
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert reason in receipt["result"]["invalid_reasons"]


def test_no_envelope_declared_keeps_legacy_grading_semantics(tmp_path):
    """Additive, not behavioural: with no `--output-envelope`, the verdict
    grammar still runs against raw stdout and every pre-existing field keeps
    its meaning. The new keys are present and null — that is what keeps the
    exact-field contract a single set rather than two."""
    fake = write_fake(tmp_path, "happy_legacy.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 0
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["result"]["schema_valid"] is True
    assert receipt["result"]["output_sha256"]
    assert receipt["output_envelope"] is None
    assert receipt["result"]["envelope"] is None
    assert receipt["result"]["invalid_reasons"] is None


def test_to_xai_transport_without_envelope_is_refused_pre_spawn(tmp_path):
    """Declaration consistency (DD-1): a `.to_xai` transport-id is the sole
    trigger. Refusal is pre-spawn — exit 2 with no receipt at all, the same
    shape as every other usage error."""
    fake = write_fake(tmp_path, "happy_xai.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--transport-id", "claude_code.to_xai"))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None
    assert not (tmp_path / "receipts" / "t1.claim").exists()


def test_to_xai_with_envelope_but_no_session_evidence_is_refused_pre_spawn(
        tmp_path):
    """Half a declaration is not a declaration: the preflight requires the
    envelope AND the session evidence for a `.to_xai` dispatch."""
    fake = write_fake(tmp_path, "happy_xai2.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--transport-id", "claude_code.to_xai", *ENVELOPE_ARGS))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None


def test_fully_declared_to_xai_dispatch_passes_preflight_and_spawns(tmp_path):
    """The positive half of the preflight — a complete declaration set runs
    normally."""
    session_dir = tmp_path / "session"
    doc = grok_doc()
    fake = write_fake(
        tmp_path, "env_xai.py",
        session_writer(session_dir)
        + f"import sys\nsys.stdout.write({json.dumps(doc)!r})\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--transport-id", "claude_code.to_xai", *ENVELOPE_ARGS,
               *session_args(session_dir)))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"


def test_grok_host_outbound_transports_pass_preflight(tmp_path):
    """`--runtime` names the HOST, never the child, so a grok-hosted
    dispatch OUT to claude/codex declares no envelope and must not be
    refused. The suffix is the whole trigger."""
    fake = write_fake(tmp_path, "happy_grok_host.py", HAPPY)
    for transport in ("grok.to_claude", "grok.to_openai"):
        proc, receipt = run_dispatch(
            tmp_path, [sys.executable, fake],
            attempt_id=f"gh-{transport.split('.')[1]}",
            extra=("--runtime", "grok", "--transport-id", transport))
        assert proc.returncode == 0, proc.stderr
        assert receipt["result"]["state"] == "SUCCEEDED"


def test_grok_hosted_reviewer_pair_forms_a_verifiable_evidence_set(tmp_path):
    """Issue #16: two grok-hosted outbound reviewer seats, distinct attempt
    ids, form a verify-evidence set. Fake children only — CI never invokes
    claude/codex/grok."""
    fake = write_fake(tmp_path, "happy_pair.py", HAPPY)
    receipts = tmp_path / "receipts"
    ids = []
    models = ("claude-fable-5", "gpt-5.6-sol")
    transports = ("grok.to_claude", "grok.to_openai")
    seats = ("reviewer-1", "reviewer-2")
    for model, transport, seat in zip(models, transports, seats):
        attempt = f"g16-t1-{seat}"
        ids.append(attempt)
        proc, receipt = run_dispatch(
            tmp_path, [sys.executable, fake],
            attempt_id=attempt,
            extra=("--runtime", "grok", "--transport-id", transport,
                   "--model-id", model, "--seat", seat,
                   "--decision-fingerprint", "ab" * 32,
                   "--policy-sha256", "cd" * 32))
        assert proc.returncode == 0, proc.stderr
        assert receipt["result"]["state"] == "SUCCEEDED"
        assert receipt["result"]["schema_valid"] is True
        assert receipt["seat"] == seat
        assert receipt["model_id"] == model
    verdict = subprocess.run(
        [sys.executable, str(SCRIPT), "verify-evidence",
         "--receipt-dir", str(receipts),
         "--ids", ",".join(ids), "--expect-count", "2",
         "--expect-models", ",".join(models),
         "--expect-fingerprint", "ab" * 32],
        capture_output=True, text=True, timeout=30)
    assert verdict.returncode == 0, verdict.stderr
    # Negative: duplicate id is a count failure, not a reused attempt.
    dup = subprocess.run(
        [sys.executable, str(SCRIPT), "verify-evidence",
         "--receipt-dir", str(receipts),
         "--ids", f"{ids[0]},{ids[0]}", "--expect-count", "2"],
        capture_output=True, text=True, timeout=30)
    assert dup.returncode != 0
    # Negative: a third attempt with the first seat collides on seat.
    third = "g16-t1-reviewer-1-again"
    proc, _ = run_dispatch(
        tmp_path, [sys.executable, fake],
        attempt_id=third,
        extra=("--runtime", "grok", "--transport-id", "grok.to_claude",
               "--model-id", "claude-fable-5", "--seat", "reviewer-1"))
    assert proc.returncode == 0, proc.stderr
    seat_clash = subprocess.run(
        [sys.executable, str(SCRIPT), "verify-evidence",
         "--receipt-dir", str(receipts),
         "--ids", f"{ids[0]},{third}", "--expect-count", "2"],
        capture_output=True, text=True, timeout=30)
    assert seat_clash.returncode != 0


def test_envelope_oversized_stdout_is_invalid_output(tmp_path):
    """The envelope is a GATE surface, so its read is bounded and an
    over-budget stdout is a typed refusal, not an unbounded parse."""
    body = ("import sys\n"
            "sys.stdout.write('{\"stopReason\": \"end_turn\", \"text\": \"')\n"
            "sys.stdout.write('x' * (4 * 1024 * 1024 + 16))\n"
            "sys.stdout.write('\"}')\n")
    fake = write_fake(tmp_path, "env_big.py", body)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none", extra=ENVELOPE_ARGS)
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "evidence_oversized" in receipt["result"]["invalid_reasons"]
    assert receipt["result"]["envelope"]["parse_ok"] is False


def test_envelope_text_is_not_serialized_into_receipt(tmp_path):
    """`text` is an internal field of the read: the raw output already lives
    in the stdout file, and a receipt carries abbreviated evidence only. The
    receipt's envelope is exactly five keys."""
    doc = grok_doc(text="verdict: PASS\nSECRET-PROMPT-ECHO-DO-NOT-COPY")
    fake = write_fake(tmp_path, "env_text.py", envelope_fake(doc))
    _, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                              extra=ENVELOPE_ARGS)
    assert set(receipt["result"]["envelope"]) == {
        "parse_ok", "stop_reason", "session_id", "served_models", "error_type"}
    assert "SECRET-PROMPT-ECHO-DO-NOT-COPY" not in json.dumps(receipt)


def test_envelope_recorded_on_failed_exit_without_relabel(tmp_path):
    """Recording is unconditional, gating is not: a non-zero exit stays
    FAILED and the error object is still preserved as evidence."""
    doc = {"type": "error", "message": "auth failed"}
    fake = write_fake(tmp_path, "env_err.py", envelope_fake(doc, exit_code=1))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none", extra=ENVELOPE_ARGS)
    assert proc.returncode == 1, proc.stderr
    assert receipt["result"]["state"] == "FAILED"
    assert receipt["result"]["envelope"]["parse_ok"] is True
    assert receipt["result"]["envelope"]["error_type"] == "error"
    assert receipt["result"]["envelope"]["stop_reason"] is None
    assert receipt["result"]["invalid_reasons"] is None


def test_envelope_collection_never_relabels_timeout(tmp_path):
    """A clean end_turn envelope written during the grace period is still a
    late fragment — TIMED_OUT wins, and the envelope is recorded beneath it."""
    body = (
        "import json, signal, sys, time\n"
        "def bail(*_):\n"
        f"    sys.stdout.write({json.dumps(grok_doc())!r})\n"
        "    sys.stdout.flush()\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bail)\n"
        "time.sleep(60)\n"
    )
    fake = write_fake(tmp_path, "env_late.py", body)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=1.0, grace=2.0, schema="none",
                                 extra=ENVELOPE_ARGS, harness_timeout=30)
    assert proc.returncode == 3, proc.stderr
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["envelope"]["stop_reason"] == "end_turn"
    assert receipt["result"]["schema_valid"] is None


def test_envelope_collection_never_relabels_termination_unconfirmed(tmp_path):
    """TERMINATION_UNCONFIRMED is the one state that holds a write-capable
    retry, so no new gate — envelope included — may relabel it. Direct-state
    simulation stands in for a concurrent `cancel`, as elsewhere in this
    file."""
    fake = write_fake(tmp_path, "env_sleep.py", SLEEPER)
    attempt_id = "envtu"
    receipt_dir = tmp_path / "receipts"
    supervisor = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id, "--receipt-dir", str(receipt_dir),
         "--deadline-seconds", "1", "--grace-seconds", "1",
         "--seat", "worker", "--output-schema", "none", *ENVELOPE_ARGS,
         "--", sys.executable, str(fake)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    receipt_path = receipt_dir / f"{attempt_id}.json"
    for _ in range(100):
        if receipt_path.exists():
            if json.loads(receipt_path.read_text())["result"]["state"] == "RUNNING":
                break
        time.sleep(0.05)
    else:
        supervisor.kill()
        pytest.fail("supervisor never reached RUNNING")
    on_disk = json.loads(receipt_path.read_text())
    on_disk["result"]["state"] = "TERMINATION_UNCONFIRMED"
    on_disk["result"]["termination_confirmed"] = False
    receipt_path.write_text(json.dumps(on_disk))
    supervisor.wait(timeout=30)
    assert json.loads(receipt_path.read_text())["result"]["state"] == \
        "TERMINATION_UNCONFIRMED"


def test_expect_effective_agent_without_session_evidence_is_refused_pre_spawn(
        tmp_path):
    """An expectation with nothing to compare against would be silently
    ignored — `--expect-effective-agent` reads `summary.agent_name`, which
    only `--session-evidence` supplies."""
    fake = write_fake(tmp_path, "happy_agent.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--expect-effective-agent", "general-purpose"))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None


@pytest.mark.parametrize("bad", [
    ("--session-evidence", "no-colon-here"),
    ("--session-evidence", "unknown-format-v9:/tmp"),
    ("--session-evidence", "grok-session-v1:"),
])
def test_session_evidence_syntax_is_validated_pre_spawn(tmp_path, bad):
    """Task 1 owns the grammar of the three session args; Task 3 owns what
    they mean at grading time."""
    fake = write_fake(tmp_path, "happy_se.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=(*bad, "--session-id", SESSION_UUID))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None


def test_session_id_must_be_a_uuid_pre_spawn(tmp_path):
    fake = write_fake(tmp_path, "happy_sid.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=("--session-id", "not-a-uuid"))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None


# ---------------------------------------------------------------------------
# DD-2 — the required-artifact contract: freshness, containment, hashes, and
# a deadline-aware grading hash. `output_schema: none` is not proof a seat
# finished; a file it actually wrote this attempt is.
# ---------------------------------------------------------------------------


def artifact_fake(writes=(), *, exit_code=0, stdout="done"):
    """A fake child that writes `(relative_path, content)` pairs under its
    own tmp root, then exits. `content=None` means "make a FIFO here",
    `content` starting with `->` means "make a symlink to the rest".

    A FIFO and a symlink can only be CREATED, never written in place, so
    those two displace whatever stands at the path first. Since R2-C1 the
    supervisor reserves an absent required path before spawn, and a child
    that plants a non-regular file there has to remove that reservation —
    which is exactly what a child doing this on purpose would do. The
    ordinary regular-file write stays an in-place write, because that is the
    case the identity contract must not cost anything.
    """
    body = ["import os, sys", "from pathlib import Path"]
    for path, content in writes:
        body.append(f"p = Path({str(path)!r})")
        body.append("p.parent.mkdir(parents=True, exist_ok=True)")
        if content is None:
            body.append("p.unlink(missing_ok=True)")
            body.append("os.mkfifo(p)")
        elif isinstance(content, str) and content.startswith("->"):
            body.append("p.unlink(missing_ok=True)")
            body.append(f"p.symlink_to({content[2:]!r})")
        else:
            body.append(f"p.write_text({content!r})")
    body.append(f"sys.stdout.write({stdout!r})")
    body.append(f"sys.exit({exit_code})")
    return "\n".join(body) + "\n"


def artifact_args(root, *paths, extra=()):
    args = ["--artifact-root", str(root)]
    for path in paths:
        args += ["--require-artifact", str(path)]
    return (*args, *extra)


def sha256_of(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def test_artifact_created_this_attempt_is_succeeded_with_proof(tmp_path):
    """G4: the receipt carries the PROOF — existence, containment, and the
    digest — not merely a state word."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_ok.py",
                      artifact_fake([(target, "PLAN BODY")]))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    (record,) = receipt["result"]["artifacts"]
    assert record["path"] == str(target)
    assert record["exists"] is True
    assert record["size"] == len("PLAN BODY")
    assert record["sha256"] == sha256_of("PLAN BODY")
    assert record["contained"] is True
    assert record["baseline_sha256"] is None      # did not exist pre-spawn
    assert record["changed"] is True
    assert record["expected_sha256_match"] is None
    assert receipt["result"]["invalid_reasons"] is None


def test_preexisting_unchanged_artifact_is_invalid_output(tmp_path):
    """A leftover file from a previous attempt is not this attempt's
    evidence. Without freshness, a seat that did nothing at all inherits
    someone else's success."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    target.write_text("STALE FROM A PRIOR ATTEMPT")
    fake = write_fake(tmp_path, "art_noop.py", artifact_fake())
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    (record,) = receipt["result"]["artifacts"]
    assert record["changed"] is False
    assert record["baseline_sha256"] == sha256_of("STALE FROM A PRIOR ATTEMPT")
    assert f"artifact_unchanged:{target}" in receipt["result"]["invalid_reasons"]


def test_preexisting_unchanged_artifact_allow_unchanged_opts_in(tmp_path):
    """A deterministic-regeneration contract is legitimate — but it is an
    explicit opt-in that shows up in the receipt, never a default."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    target.write_text("DETERMINISTIC")
    fake = write_fake(tmp_path, "art_same.py",
                      artifact_fake([(target, "DETERMINISTIC")]))
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake], schema="none",
        extra=artifact_args(root, target,
                            extra=("--require-artifact-allow-unchanged",)))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    (record,) = receipt["result"]["artifacts"]
    assert record["changed"] is False
    assert record["artifact_unchanged_accepted"] is True


def test_preexisting_changed_artifact_is_succeeded_with_baseline_recorded(
        tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    target.write_text("BEFORE")
    fake = write_fake(tmp_path, "art_changed.py",
                      artifact_fake([(target, "AFTER")]))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    (record,) = receipt["result"]["artifacts"]
    assert record["baseline_sha256"] == sha256_of("BEFORE")
    assert record["sha256"] == sha256_of("AFTER")
    assert record["changed"] is True


@pytest.mark.parametrize("kind,reason_prefix", [
    ("missing", "artifact_missing"),        # never written
    ("empty", "artifact_empty"),            # written but empty
    # A symlink OUT of the root is caught by containment first — the
    # stronger of the two refusals, and the one that matters for escape.
    ("symlink_out", "artifact_escaped_root"),
    # A symlink that stays INSIDE the root still is not the artifact: the
    # contract is a regular file at that path, so a link there is refused
    # on its own account rather than being followed to whatever it names.
    ("symlink_in", "artifact_not_regular_file"),
])
def test_missing_empty_symlink_or_escaping_artifact_is_invalid_output(
        tmp_path, kind, reason_prefix):
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    if kind == "missing":
        writes = []
    elif kind == "empty":
        writes = [(target, "")]
    elif kind == "symlink_out":
        outside = tmp_path / "outside.md"
        outside.write_text("OUTSIDE")
        writes = [(target, f"->{outside}")]
    else:
        writes = [(root / "real.md", "REAL"), (target, f"->{root / 'real.md'}")]
    fake = write_fake(tmp_path, "art_bad.py", artifact_fake(writes))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert f"{reason_prefix}:{target}" in receipt["result"]["invalid_reasons"]


def test_artifact_outside_root_declaration_is_refused_pre_spawn(tmp_path):
    """A declaration error is the caller's, not the attempt's: exit 2 with
    no receipt and no attempt-id burned."""
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "elsewhere.md"
    fake = write_fake(tmp_path, "art_out.py", artifact_fake())
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, outside))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None
    assert not (tmp_path / "receipts" / "t1.claim").exists()


def test_require_artifact_without_root_is_refused_pre_spawn(tmp_path):
    fake = write_fake(tmp_path, "art_noroot.py", artifact_fake())
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake], schema="none",
        extra=("--require-artifact", str(tmp_path / "x.md")))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None


def test_artifact_sha256_mismatch_is_invalid_output(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_hash.py",
                      artifact_fake([(target, "ACTUAL")]))
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake], schema="none",
        extra=artifact_args(root, target, extra=(
            "--require-artifact-sha256", f"{target}={sha256_of('EXPECTED')}")))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    (record,) = receipt["result"]["artifacts"]
    assert record["expected_sha256_match"] is False
    assert f"artifact_sha256_mismatch:{target}" in \
        receipt["result"]["invalid_reasons"]


def test_failed_exit_wins_over_present_artifacts(tmp_path):
    """Ladder order: a non-zero exit is decided before any new gate, and a
    non-grading termination carries no baseline (P2-W5)."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_failed.py",
                      artifact_fake([(target, "WRITTEN ANYWAY")], exit_code=3))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 1, proc.stderr
    assert receipt["result"]["state"] == "FAILED"
    assert receipt["result"]["artifacts"] is None
    assert receipt["result"]["invalid_reasons"] is None


def _in_process(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_agent", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_late_grading_past_deadline_is_timed_out(tmp_path, monkeypatch):
    """Grading itself consumes time — multi-file hashes, JSON parsing — so
    the deadline is re-checked immediately before SUCCEEDED is written.
    The clock helper is swapped rather than raced against a real large
    file: a timing race is an intermittent test, not a deterministic one."""
    dispatch_agent = _in_process(tmp_path)
    monkeypatch.setattr(dispatch_agent, "_deadline_expired", lambda _: True)
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_late.py",
                      artifact_fake([(target, "ON TIME")]))
    receipt_dir = tmp_path / "receipts"
    rc = dispatch_agent.main([
        "run", "--attempt-id", "late1", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "none",
        *artifact_args(root, target),
        "--", sys.executable, str(fake)])
    assert rc == 3
    receipt = json.loads((receipt_dir / "late1.json").read_text())
    assert receipt["result"]["state"] == "TIMED_OUT"


def test_oversized_artifact_hash_is_aborted_within_deadline(
        tmp_path, monkeypatch):
    """A sparse or enormous regular file must not delay the terminal receipt
    indefinitely: the grading hash checks the remaining budget per chunk and
    abandons the attempt as TIMED_OUT, recording `hash_aborted` and NO
    partial digest. The hash helper is swapped for determinism."""
    dispatch_agent = _in_process(tmp_path)
    monkeypatch.setattr(dispatch_agent, "_hash_artifact",
                        lambda fd, deadline: None)
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_huge.py",
                      artifact_fake([(target, "BIG")]))
    receipt_dir = tmp_path / "receipts"
    rc = dispatch_agent.main([
        "run", "--attempt-id", "huge1", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "none",
        *artifact_args(root, target),
        "--", sys.executable, str(fake)])
    assert rc == 3
    receipt = json.loads((receipt_dir / "huge1.json").read_text())
    assert receipt["result"]["state"] == "TIMED_OUT"
    (record,) = receipt["result"]["artifacts"]
    assert record["hash_aborted"] is True
    assert record["sha256"] is None


def test_oversized_baseline_is_refused_pre_spawn(tmp_path):
    """Pre-spawn there is no deadline anchor to bound a hash against, so the
    baseline half is bounded by SIZE instead — and over budget is exit 2
    with no receipt, keeping the preflight's no-receipt contract intact."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    target.write_text("x" * 4096)
    fake = write_fake(tmp_path, "art_bigbase.py", artifact_fake())
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake], schema="none",
        extra=artifact_args(root, target, extra=(
            "--require-artifact-baseline-max-bytes", "1024")))
    assert proc.returncode == 2, proc.stdout
    assert "baseline too large" in proc.stderr
    assert receipt is None


def test_unreadable_existing_artifact_baseline_is_refused_pre_spawn(tmp_path):
    """R2-C1: only a CONFIRMED `ENOENT` is absence.

    A baseline open that fails for any other reason leaves `baseline_sha256`
    at None — the exact value "the file was not there" produces — and that
    value makes grading report `changed: true` for a file this attempt may
    never have touched, which is a false freshness proof behind a SUCCEEDED.
    `ENOTDIR` is the deterministic non-`ENOENT` case (a regular file standing
    where a path component wants a directory) and it is refused with
    everything else: pre-spawn, exit 2, no receipt, no attempt-id burned.
    """
    root = tmp_path / "work"
    root.mkdir()
    blocker = root / "plan.md"
    blocker.write_text("A REGULAR FILE WHERE A PATH COMPONENT WANTS A DIR")
    target = blocker / "inner.md"
    fake = write_fake(tmp_path, "art_enotdir.py", artifact_fake())
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None
    assert not (tmp_path / "receipts" / "t1.claim").exists()
    assert "could not be read pre-spawn" in proc.stderr


def test_hard_linked_artifact_baseline_is_refused_pre_spawn(tmp_path):
    """R2-C2: containment is checked against a PATH, but a write lands on an
    INODE. A second name for an outside inode planted inside the root passes
    every path check there is, so a write through the in-root name overwrites
    a file outside the fence while the receipt records `contained: true`.

    A required artifact therefore has to be the only name for its inode.
    Pre-spawn that is a declaration/environment error: exit 2, no receipt.
    """
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("EXTERNAL INODE")
    target = root / "plan.md"
    os.link(outside, target)
    fake = write_fake(tmp_path, "art_linkbase.py", artifact_fake())
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None
    assert not (tmp_path / "receipts" / "t1.claim").exists()
    assert "more than one name" in proc.stderr
    assert outside.read_text() == "EXTERNAL INODE"


def test_hard_linked_artifact_written_this_attempt_is_invalid_output(tmp_path):
    """The grading half of R2-C2: the link is created by the CHILD, so no
    pre-spawn check can see it. The in-root name and the outside file are one
    inode, the child's write lands on both, and the supervisor must refuse to
    issue a contained SUCCEEDED proof over it."""
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("EXTERNAL INODE")
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_linkgrade.py", "\n".join([
        "import os",
        "from pathlib import Path",
        # The supervisor reserves an absent required path before spawn
        # (R2-C1), so the second name has to displace that reservation — the
        # in-root name and the outside file are still one inode afterwards,
        # which is the whole of what this regression is about.
        f"Path({str(target)!r}).unlink(missing_ok=True)",
        f"os.link({str(outside)!r}, {str(target)!r})",
        f"Path({str(target)!r}).write_text('OVERWRITTEN THROUGH THE FENCE')",
        "print('done')",
    ]) + "\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert f"artifact_multiply_linked:{target}" in \
        receipt["result"]["invalid_reasons"]
    (record,) = receipt["result"]["artifacts"]
    assert record["exists"] is True
    assert record["nlink"] == 2
    # No digest: a hash recorded here would read as proof that a contained
    # file holds this content, and the inode is not contained.
    assert record["sha256"] is None
    assert record["changed"] is None


def test_single_linked_artifact_still_grades_normally(tmp_path):
    """The nlink gate must not cost the ordinary case its proof."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_nlink1.py",
                      artifact_fake([(target, "PLAN BODY")]))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    (record,) = receipt["result"]["artifacts"]
    assert record["nlink"] == 1
    assert record["sha256"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the mode bits")
def test_permission_denied_artifact_baseline_is_refused_pre_spawn(tmp_path):
    """The reviewers' named example: an artifact that EXISTS but is not
    readable right now (`EACCES`) must not be recorded as absent, because a
    file that becomes readable later would then grade as fresh."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    target.write_text("PRESENT BUT UNREADABLE")
    target.chmod(0o000)
    fake = write_fake(tmp_path, "art_eacces.py", artifact_fake())
    try:
        proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                     schema="none",
                                     extra=artifact_args(root, target))
    finally:
        target.chmod(0o600)
    assert proc.returncode == 2, proc.stdout
    assert receipt is None
    assert not (tmp_path / "receipts" / "t1.claim").exists()
    assert "could not be read pre-spawn" in proc.stderr


def test_termination_unconfirmed_never_relabeled_by_new_gates(tmp_path):
    """The one state that holds a write-capable retry outranks every gate
    this tranche adds."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    target.write_text("PRESENT AND UNCHANGED")
    fake = write_fake(tmp_path, "art_tu.py", SLEEPER)
    attempt_id = "arttu"
    receipt_dir = tmp_path / "receipts"
    supervisor = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id, "--receipt-dir", str(receipt_dir),
         "--deadline-seconds", "1", "--grace-seconds", "1",
         "--seat", "worker", "--output-schema", "none",
         *artifact_args(root, target),
         "--", sys.executable, str(fake)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    receipt_path = receipt_dir / f"{attempt_id}.json"
    for _ in range(100):
        if receipt_path.exists():
            if json.loads(receipt_path.read_text())["result"]["state"] == "RUNNING":
                break
        time.sleep(0.05)
    else:
        supervisor.kill()
        pytest.fail("supervisor never reached RUNNING")
    on_disk = json.loads(receipt_path.read_text())
    on_disk["result"]["state"] = "TERMINATION_UNCONFIRMED"
    on_disk["result"]["termination_confirmed"] = False
    receipt_path.write_text(json.dumps(on_disk))
    supervisor.wait(timeout=30)
    final = json.loads(receipt_path.read_text())
    assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"


def test_fifo_or_special_evidence_file_is_refused_not_blocking(tmp_path):
    """A FIFO at an artifact path is the blocking-read hazard: an ordinary
    open() would wait for a writer that never comes and hold the terminal
    receipt past the deadline. O_NOFOLLOW|O_NONBLOCK plus an fstat regular-
    file check refuses it immediately instead."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_fifo.py", artifact_fake([(target, None)]))
    started = time.monotonic()
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none", deadline=30.0,
                                 extra=artifact_args(root, target))
    assert time.monotonic() - started < 20, "the FIFO read blocked"
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert f"artifact_not_regular_file:{target}" in \
        receipt["result"]["invalid_reasons"]


def test_artifact_root_pivot_after_spawn_fails_containment(tmp_path):
    """The root is resolved and PINNED before spawn, so a child that
    replaces the root's pathname with a symlink to somewhere else does not
    move the fence — containment is checked against the pinned value."""
    root = tmp_path / "work"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = root / "plan.md"
    body = (
        "import os, shutil\n"
        "from pathlib import Path\n"
        f"shutil.rmtree({str(root)!r})\n"
        f"Path({str(elsewhere)!r} + '/plan.md').write_text('PIVOTED')\n"
        f"os.symlink({str(elsewhere)!r}, {str(root)!r})\n"
    )
    fake = write_fake(tmp_path, "art_pivot.py", body)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    (record,) = receipt["result"]["artifacts"]
    assert record["contained"] is False
    assert f"artifact_escaped_root:{target}" in \
        receipt["result"]["invalid_reasons"]
    assert (elsewhere / "plan.md").read_text() == "PIVOTED"


def test_artifact_count_budget_exceeded_is_refused_pre_spawn(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    targets = [root / f"a{i}.md" for i in range(17)]
    fake = write_fake(tmp_path, "art_many.py", artifact_fake())
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, *targets))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None


def test_artifact_aggregate_baseline_budget_exceeded_is_refused_pre_spawn(
        tmp_path, monkeypatch):
    """Each baseline can be under the per-file cap and the set still be an
    unbounded pre-spawn hash. The aggregate budget is shrunk here rather
    than materializing 256 MiB of fixtures."""
    dispatch_agent = _in_process(tmp_path)
    monkeypatch.setattr(dispatch_agent, "ARTIFACT_BASELINE_BUDGET_BYTES", 100)
    root = tmp_path / "work"
    root.mkdir()
    targets = []
    for i in range(3):
        target = root / f"a{i}.md"
        target.write_text("y" * 60)
        targets.append(target)
    fake = write_fake(tmp_path, "art_budget.py", artifact_fake())
    rc = dispatch_agent.main([
        "run", "--attempt-id", "budget1", "--receipt-dir",
        str(tmp_path / "receipts"),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "none",
        *artifact_args(root, *targets),
        "--", sys.executable, str(fake)])
    assert rc == 2
    assert not (tmp_path / "receipts" / "budget1.json").exists()


@pytest.mark.parametrize("mapping,label", [
    ("unknown", "a PATH that was never declared"),
    ("dup", "the same PATH twice"),
    ("conflict", "two different digests for one PATH"),
    ("malformed", "a value that is not 64 lowercase hex"),
])
def test_sha256_mapping_unknown_dup_conflict_is_refused_pre_spawn(
        tmp_path, mapping, label):
    """The digest map must be 1:1 with the declarations it constrains: a
    mapping that names nothing, or names one thing twice, is a caller
    mistake that would otherwise be silently ignored."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    other = root / "other.md"
    digest = sha256_of("A")
    specs = {
        "unknown": (f"{other}={digest}",),
        "dup": (f"{target}={digest}", f"{target}={digest}"),
        "conflict": (f"{target}={digest}", f"{target}={sha256_of('B')}"),
        "malformed": (f"{target}=NOTAHEXDIGEST",),
    }[mapping]
    extra = []
    for spec in specs:
        extra += ["--require-artifact-sha256", spec]
    fake = write_fake(tmp_path, "art_map.py", artifact_fake())
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target,
                                                     extra=tuple(extra)))
    assert proc.returncode == 2, f"{label}: {proc.stdout}"
    assert receipt is None


# ---------------------------------------------------------------------------
# DD-3 — session evidence, attempt binding, and the effective-agent /
# effective-sandbox gates. The receipt must carry what a seat was GIVEN
# alongside what actually took effect, and an unrelated or leftover session
# directory must never stand in as this attempt's evidence.
# ---------------------------------------------------------------------------


def session_writer(session_dir, *, session_id=SESSION_UUID,
                   agent_name="general-purpose", sandbox_profile="workspace",
                   outcome="completed", cancellation_category=None,
                   events_padding=0, events_padding_after=False,
                   summary_padding=0, created_at=None, write_summary=True):
    """Python source for a fake child that writes a grok session directory
    WHILE IT RUNS.

    The timing matters: `created_at` must land inside
    [launch_anchor, grading], and a fixture written by the test before the
    supervisor ever spawns would sit before the anchor. Static fixtures are
    the natural shape only for the mismatch/staleness cases, which is
    exactly how they are written below.
    """
    event = {"ts": "2026-08-25T08:50:43.239Z", "type": "turn_ended",
             "outcome": outcome}
    if cancellation_category is not None:
        event["cancellation_category"] = cancellation_category
    summary = {
        "info": {"id": session_id, "cwd": "/tmp/x"},
        "current_model_id": "grok-4.6", "reasoning_effort": "low",
        "agent_name": agent_name, "sandbox_profile": sandbox_profile,
    }
    return "\n".join([
        "import datetime, json",
        "from pathlib import Path",
        f"d = Path({str(session_dir)!r})",
        "d.mkdir(parents=True, exist_ok=True)",
        f"summary = {summary!r}",
        (f"summary['created_at'] = {created_at!r}" if created_at else
         "summary['created_at'] = datetime.datetime.now("
         "datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')"),
        f"summary['pad'] = 'p' * {summary_padding}",
        (f"(d / 'summary.json').write_text(json.dumps(summary))"
         if write_summary else "pass"),
        f"pad = [json.dumps({{'type': 'phase_changed', 'pad': 'q' * 64}})] * {events_padding}",
        f"lines = ([json.dumps({event!r})] + pad) if {events_padding_after!r} "
        f"else (pad + [json.dumps({event!r})])",
        "(d / 'events.jsonl').write_text('\\n'.join(lines) + '\\n')",
    ]) + "\n"


def session_args(session_dir, *, session_id=SESSION_UUID, extra=()):
    return ("--session-evidence", f"grok-session-v1:{session_dir}",
            "--session-id", session_id, *extra)


def test_bound_fresh_session_evidence_is_succeeded_and_recorded(tmp_path):
    """The receipt now carries requested and EFFECTIVE side by side — G2's
    whole point. Issue #14's `requested acceptEdits / effective
    grok-build-plan` mismatch becomes visible on one page."""
    session_dir = tmp_path / "session"
    fake = write_fake(tmp_path, "sess_ok.py",
                      session_writer(session_dir) + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    evidence = receipt["session_evidence"]
    assert evidence["summary"]["agent_name"] == "general-purpose"
    assert evidence["summary"]["current_model_id"] == "grok-4.6"
    assert evidence["summary"]["reasoning_effort"] == "low"
    assert evidence["summary"]["sandbox_profile"] == "workspace"
    assert evidence["session_id"] == SESSION_UUID
    assert evidence["created_at"]
    assert evidence["terminal_event"] == {"outcome": "completed",
                                          "cancellation_category": None}
    assert receipt["timing"]["launch_anchor_at"]


def test_unreadable_summary_blocks_success_but_not_failures(tmp_path):
    """Fail-closed tightens the SUCCESS direction only: a FAILED attempt is
    not relabeled by what its evidence does or does not say."""
    session_dir = tmp_path / "session"
    fake = write_fake(tmp_path, "sess_none.py",
                      session_writer(session_dir, write_summary=False)
                      + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "session_evidence_unreadable" in receipt["result"]["invalid_reasons"]

    fail_fake = write_fake(
        tmp_path, "sess_fail.py",
        session_writer(session_dir, write_summary=False)
        + "import sys\nprint('verdict: PASS')\nsys.exit(4)\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fail_fake],
                                 attempt_id="t2",
                                 extra=session_args(session_dir))
    assert proc.returncode == 1
    assert receipt["result"]["state"] == "FAILED"
    assert receipt["result"]["invalid_reasons"] is None


def test_failed_attempt_still_records_available_session_evidence(tmp_path):
    """R2-W1: gating and RECORDING are different jobs. A non-zero exit is
    decided before any evidence gate runs, but the effective agent, the
    effective sandbox profile and the turn's cancellation category are
    exactly what an operator needs to explain the failure — and they were
    being dropped because collection lived inside the success branch. The
    already-decided terminal state is not relabeled by what is collected."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_failev.py",
        session_writer(session_dir, agent_name="grok-build-plan",
                       outcome="cancelled",
                       cancellation_category="max_output_tokens")
        + "import sys\nprint('verdict: PASS')\nsys.exit(4)\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 1, proc.stderr
    assert receipt["result"]["state"] == "FAILED"
    assert receipt["result"]["invalid_reasons"] is None
    evidence = receipt["session_evidence"]
    assert evidence["format"] == "grok-session-v1"
    assert evidence["summary"]["agent_name"] == "grok-build-plan"
    assert evidence["summary"]["sandbox_profile"] == "workspace"
    assert evidence["session_id"] == SESSION_UUID
    assert evidence["terminal_event"] == {
        "outcome": "cancelled",
        "cancellation_category": "max_output_tokens"}


def test_timed_out_attempt_still_records_available_session_evidence(tmp_path):
    """The same tail, reached through the OTHER branch — the one that also
    produces TERMINATION_UNCONFIRMED. Past the deadline nothing the attempt
    writes can change the state, and this collection does not: it is bounded
    (`SUMMARY_MAX_BYTES` / `EVENTS_TAIL_BYTES`) and it only records."""
    session_dir = tmp_path / "session"
    fake = write_fake(tmp_path, "sess_timeev.py",
                      session_writer(session_dir) + "import time\ntime.sleep(60)\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=2.0, grace=1.0,
                                 extra=session_args(session_dir))
    assert proc.returncode == 3, proc.stderr
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["invalid_reasons"] is None
    evidence = receipt["session_evidence"]
    assert evidence["summary"]["agent_name"] == "general-purpose"
    assert evidence["session_id"] == SESSION_UUID


def test_session_evidence_absent_at_a_terminal_state_is_still_terminal(tmp_path):
    """Collection is BEST-EFFORT. A child that never wrote a session
    directory must still get its FAILED receipt, with the same always-present
    shape and nulls where there was nothing to read."""
    session_dir = tmp_path / "session"
    fake = write_fake(tmp_path, "sess_noev.py",
                      "import sys\nprint('nothing here')\nsys.exit(5)\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 1, proc.stderr
    assert receipt["result"]["state"] == "FAILED"
    assert receipt["result"]["invalid_reasons"] is None
    assert receipt["session_evidence"]["summary"] is None
    assert receipt["session_evidence"]["session_id"] is None
    assert receipt["session_evidence"]["format"] == "grok-session-v1"


def test_termination_unconfirmed_is_not_relabeled_by_evidence_collection(tmp_path):
    """TERMINATION_UNCONFIRMED holds the write-capable retry, so it outranks
    everything the tail collects.

    Only the NON-relabeling half is asserted here, and deliberately so: a
    genuine in-process TERMINATION_UNCONFIRMED needs a process group that
    survives SIGKILL, which no test can arrange without becoming a race, and
    the pre-planted route used below is preserved verbatim by
    `_commit_terminal` (so it can never show what the supervisor collected).
    Persistence is covered by the TIMED_OUT test above, which reaches the
    same unconditional tail through the same branch.
    """
    session_dir = tmp_path / "session"
    fake = write_fake(tmp_path, "sess_tu.py",
                      session_writer(session_dir) + "import time\ntime.sleep(60)\n")
    attempt_id = "sesstu"
    receipt_dir = tmp_path / "receipts"
    supervisor = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id, "--receipt-dir", str(receipt_dir),
         "--deadline-seconds", "2", "--grace-seconds", "1",
         "--seat", "worker", "--output-schema", "review",
         *session_args(session_dir),
         "--", sys.executable, str(fake)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    receipt_path = receipt_dir / f"{attempt_id}.json"
    for _ in range(100):
        if receipt_path.exists():
            if json.loads(receipt_path.read_text())["result"]["state"] == "RUNNING":
                break
        time.sleep(0.05)
    else:
        supervisor.kill()
        pytest.fail("supervisor never reached RUNNING")
    on_disk = json.loads(receipt_path.read_text())
    on_disk["result"]["state"] = "TERMINATION_UNCONFIRMED"
    on_disk["result"]["termination_confirmed"] = False
    receipt_path.write_text(json.dumps(on_disk))
    supervisor.wait(timeout=30)
    final = json.loads(receipt_path.read_text())
    assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"
    assert final["result"]["invalid_reasons"] is None


def test_stale_or_unrelated_session_dir_is_unbound(tmp_path):
    """A leftover directory that happens to carry the expected agent_name is
    not this attempt's evidence — `info.id` binds it or nothing does."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_stale.py",
        session_writer(session_dir, session_id=OTHER_UUID)
        + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=session_args(session_dir,
                           extra=("--expect-effective-agent", "general-purpose")))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "session_evidence_unbound" in receipt["result"]["invalid_reasons"]


def test_envelope_session_id_mismatch_is_unbound(tmp_path):
    """The stdout document and the session directory must name the SAME
    session — that cross-proof is why `session_id` is kept in the receipt's
    envelope evidence."""
    session_dir = tmp_path / "session"
    doc = grok_doc(session_id=OTHER_UUID)
    fake = write_fake(
        tmp_path, "sess_xid.py",
        session_writer(session_dir)
        + f"import sys\nsys.stdout.write({json.dumps(doc)!r})\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=(*ENVELOPE_ARGS, *session_args(session_dir)))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "session_evidence_unbound" in receipt["result"]["invalid_reasons"]


def test_envelope_without_a_session_id_is_unbound(tmp_path):
    """R2-C3: the cross-proof was conditional on the envelope HAVING a
    `sessionId`, so an envelope that simply omits it skipped the check
    entirely and a perfectly fresh, perfectly bound session directory then
    carried the attempt all the way to SUCCEEDED. Once an envelope is
    declared, exact equality is unconditional — an absent value cannot
    equal the declared session, so it is `session_evidence_unbound`."""
    session_dir = tmp_path / "session"
    doc = grok_doc()
    doc.pop("sessionId")
    fake = write_fake(
        tmp_path, "sess_noid.py",
        session_writer(session_dir)
        + f"import sys\nsys.stdout.write({json.dumps(doc)!r})\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=(*ENVELOPE_ARGS, *session_args(session_dir)))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "session_evidence_unbound" in receipt["result"]["invalid_reasons"]
    assert receipt["result"]["envelope"]["session_id"] is None


def test_envelope_with_a_non_string_session_id_is_unbound(tmp_path):
    """Same gate, the other half: `_read_envelope` keeps only string values,
    so a numeric or object `sessionId` reaches grading as None exactly like
    an absent one, and must be refused exactly like one."""
    session_dir = tmp_path / "session"
    doc = grok_doc()
    doc["sessionId"] = 12345
    fake = write_fake(
        tmp_path, "sess_intid.py",
        session_writer(session_dir)
        + f"import sys\nsys.stdout.write({json.dumps(doc)!r})\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=(*ENVELOPE_ARGS, *session_args(session_dir)))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "session_evidence_unbound" in receipt["result"]["invalid_reasons"]
    assert receipt["result"]["envelope"]["session_id"] is None


def test_created_at_outside_launch_window_is_unbound(tmp_path):
    """Freshness: the session is created by the child at launch, so a
    `created_at` before this attempt's anchor belongs to a different run."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_old.py",
        session_writer(session_dir, created_at="2020-01-01T00:00:00.000000Z")
        + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "session_evidence_unbound" in receipt["result"]["invalid_reasons"]


def test_sub_second_success_with_untruncated_comparison_is_succeeded(tmp_path):
    """R3 regression. The receipt's own timestamps are truncated to whole
    seconds; grok's `created_at` has microseconds. Comparing the truncated
    anchor against the precise value rejects every attempt that finishes
    inside one second — which is every fake-child fixture in this file, and
    plenty of real short turns. The comparison runs on untruncated internal
    values for exactly that reason."""
    session_dir = tmp_path / "session"
    fake = write_fake(tmp_path, "sess_fast.py",
                      session_writer(session_dir) + "print('verdict: PASS')\n")
    started = time.monotonic()
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert time.monotonic() - started < 5


def test_effective_agent_mismatch_is_invalid_output(tmp_path):
    """G3's detection axis: a write-capable seat that silently inherited the
    read-only default agent cannot reach SUCCEEDED by any path. This gate is
    required in EVERY branch, shipped maker recipe or not."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_agent.py",
        session_writer(session_dir, agent_name="grok-build-plan")
        + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=session_args(session_dir,
                           extra=("--expect-effective-agent", "general-purpose")))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "effective_agent_mismatch:grok-build-plan" in \
        receipt["result"]["invalid_reasons"]


def test_effective_sandbox_mismatch_is_invalid_output(tmp_path):
    """The sandbox fail-open guard: a recipe that ships a `--sandbox` flag
    must not succeed on a machine or version where the flag quietly did
    nothing. `sandbox_profile` is unofficial, so it gates only when the
    caller declares an expectation."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_sbx.py",
        session_writer(session_dir, sandbox_profile="off")
        + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=session_args(session_dir,
                           extra=("--expect-sandbox-profile", "workspace")))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "effective_sandbox_mismatch:off" in \
        receipt["result"]["invalid_reasons"]


def test_absent_sandbox_profile_fails_an_expectation_closed(tmp_path):
    """Absent is not "fine": a version that stopped recording the field is
    exactly the fail-open case this expectation exists to catch."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_nosbx.py",
        session_writer(session_dir, sandbox_profile=None)
        + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=session_args(session_dir,
                           extra=("--expect-sandbox-profile", "workspace")))
    assert proc.returncode == 6, proc.stderr
    assert "effective_sandbox_mismatch:<absent>" in \
        receipt["result"]["invalid_reasons"]


def test_terminal_event_cancelled_blocks_success_even_with_clean_stdout(
        tmp_path):
    """A second line of defence, independent of DD-1: even with a perfectly
    clean stdout, a session whose last turn ended `cancelled` is not a
    success."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_cancel.py",
        session_writer(session_dir, outcome="cancelled",
                       cancellation_category="permission_cancelled")
        + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert receipt["session_evidence"]["terminal_event"] == {
        "outcome": "cancelled",
        "cancellation_category": "permission_cancelled"}
    assert "session_terminal_event:cancelled" in \
        receipt["result"]["invalid_reasons"]


def test_oversized_events_jsonl_is_nongating_with_null_terminal_event(tmp_path):
    """R3, both reviewers converging: `events.jsonl` is undocumented, so it
    is a CIRCUMSTANTIAL surface, not a gate — and that has to hold on the
    size axis too. The 256 KiB tail is a read budget, not a contract: not
    finding a complete terminal event inside it leaves `terminal_event`
    null and changes no state."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_bigev.py",
        session_writer(session_dir, events_padding=8000,
                       events_padding_after=True)
        + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["session_evidence"]["terminal_event"] is None
    assert receipt["result"]["invalid_reasons"] is None


def test_oversized_summary_json_is_invalid_output(tmp_path):
    """The other half of the size rule: `summary.json` IS a gate surface
    (officially documented, and what the binding is proved against), so an
    over-budget one is a typed refusal rather than an unbounded parse."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_bigsum.py",
        session_writer(session_dir, summary_padding=1024 * 1024 + 64)
        + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "evidence_oversized" in receipt["result"]["invalid_reasons"]


def test_session_evidence_without_session_id_is_refused_pre_spawn(tmp_path):
    """Attempt binding has nothing to bind to without the id the child was
    given, so the two arguments are mutually required."""
    session_dir = tmp_path / "session"
    fake = write_fake(tmp_path, "sess_noid.py", HAPPY)
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--session-evidence", f"grok-session-v1:{session_dir}"))
    assert proc.returncode == 2, proc.stdout
    assert receipt is None


# ---------------------------------------------------------------------------
# DD-5 — one vocabulary for `result.invalid_reasons`. No new terminal state:
# every gate this tranche adds converges on INVALID_OUTPUT, and the CAUSE is
# what gained resolution.
# ---------------------------------------------------------------------------


def test_every_reason_this_supervisor_emits_is_in_the_vocabulary(tmp_path):
    """The vocabulary has teeth or it is decoration. `_reason` refuses an
    unregistered prefix at construction time, so a new cause cannot be
    f-strung in at a call site and quietly become a fourth thing retry
    policy has to guess about."""
    dispatch_agent = _in_process(tmp_path)
    for flag in dispatch_agent.INVALID_REASON_FLAGS:
        assert dispatch_agent._is_documented_reason(flag)
    for prefix in dispatch_agent.INVALID_REASON_PREFIXES:
        assert dispatch_agent._is_documented_reason(
            dispatch_agent._reason(prefix, "x"))
        # An absent detail is still a well-formed member, never a bare
        # prefix that a consumer would have to special-case.
        assert dispatch_agent._reason(prefix, None).endswith(":<absent>")
    with pytest.raises(ValueError):
        dispatch_agent._reason("a_prefix_nobody_registered", "x")
    assert not dispatch_agent._is_documented_reason("invented_out_of_band")


def test_reasons_from_a_multi_gate_failure_are_all_documented(tmp_path):
    """An end-to-end sweep: one attempt that trips the envelope, the session
    binding and the artifact contract at once must report every cause, and
    every reported cause must be a vocabulary member."""
    dispatch_agent = _in_process(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    target.write_text("LEFTOVER")
    session_dir = tmp_path / "session"
    doc = grok_doc(stop_reason="cancelled", session_id=OTHER_UUID)
    fake = write_fake(
        tmp_path, "multi.py",
        session_writer(session_dir, agent_name="grok-build-plan",
                       sandbox_profile="off")
        + f"import sys\nsys.stdout.write({json.dumps(doc)!r})\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake], schema="none",
        extra=(*ENVELOPE_ARGS,
               *session_args(session_dir,
                             extra=("--expect-effective-agent", "general-purpose",
                                    "--expect-sandbox-profile", "workspace")),
               *artifact_args(root, target)))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    reasons = receipt["result"]["invalid_reasons"]
    assert "envelope_stop_reason:cancelled" in reasons
    assert "session_evidence_unbound" in reasons
    assert "effective_agent_mismatch:grok-build-plan" in reasons
    assert "effective_sandbox_mismatch:off" in reasons
    assert f"artifact_unchanged:{target}" in reasons
    for reason in reasons:
        assert dispatch_agent._is_documented_reason(reason), reason
    # No new terminal state was invented for any of it.
    assert receipt["result"]["state"] in dispatch_agent.STATES


# ---------------------------------------------------------------------------
# DD-6 / DD-7 — the evidence chain catches a grok dispatch that forgot to
# declare its envelope, and the served model is preserved as envelope detail
# without disturbing the top-level observation pair.
# ---------------------------------------------------------------------------


def _xai_receipt(tmp_path, attempt_id, seat, *, transport_id="claude_code.to_xai",
                 envelope="end_turn", session_evidence=True):
    """A SUCCEEDED reviewer receipt shaped like a real to_xai dispatch."""
    receipts = tmp_path / "receipts"
    receipts.mkdir(exist_ok=True)
    payload = {
        "attempt_id": attempt_id, "seat": seat,
        "result": {"state": "SUCCEEDED", "schema_valid": True,
                   "envelope": None, "invalid_reasons": None},
        "output_schema": "review", "model_id": None,
        "decision_fingerprint": None, "transport_id": transport_id,
        "output_envelope": None, "session_evidence": None,
    }
    if envelope is not None:
        payload["output_envelope"] = "grok-headless-json-v1"
        payload["result"]["envelope"] = {
            "parse_ok": True, "stop_reason": envelope,
            "session_id": SESSION_UUID,
            "served_models": ["grok-4.6-build"], "error_type": None}
    if session_evidence:
        payload["session_evidence"] = {
            "format": "grok-session-v1", "dir": "/tmp/s",
            "summary": {"agent_name": "grok-build-plan",
                        "current_model_id": "grok-4.6",
                        "reasoning_effort": "low", "sandbox_profile": "read-only"},
            "session_id": SESSION_UUID, "created_at": "2026-08-25T08:50:40.219543Z",
            "terminal_event": {"outcome": "completed",
                               "cancellation_category": None}}
    (receipts / f"{attempt_id}.json").write_text(json.dumps(payload))


def test_verify_evidence_accepts_a_complete_to_xai_evidence_set(tmp_path):
    _xai_receipt(tmp_path, "x1", "reviewer-1")
    _xai_receipt(tmp_path, "x2", "reviewer-2")
    proc = _agent(["verify-evidence", "--ids", "x1,x2", "--expect-count", "2"],
                  tmp_path)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("missing", ["envelope", "session_evidence"])
def test_verify_evidence_requires_envelope_for_to_xai_receipts(tmp_path, missing):
    """A grok dispatch that forgot to declare its envelope produces a receipt
    that looks like an ordinary success. The pre-spawn preflight refuses a
    PARTIAL declaration; this is the second net, at the moment such a receipt
    is promoted to review evidence."""
    _xai_receipt(tmp_path, "x1", "reviewer-1",
                 envelope=None if missing == "envelope" else "end_turn",
                 session_evidence=missing != "session_evidence")
    _xai_receipt(tmp_path, "x2", "reviewer-2")
    proc = _agent(["verify-evidence", "--ids", "x1,x2", "--expect-count", "2"],
                  tmp_path)
    assert proc.returncode == 1
    assert "x1" in proc.stderr


def test_verify_evidence_rejects_non_end_turn_envelope(tmp_path):
    """Near-tautological next to SUCCEEDED — and that is the point: it catches
    a hand-assembled evidence set that a state word alone would not."""
    _xai_receipt(tmp_path, "x1", "reviewer-1", envelope="cancelled")
    _xai_receipt(tmp_path, "x2", "reviewer-2")
    proc = _agent(["verify-evidence", "--ids", "x1,x2", "--expect-count", "2"],
                  tmp_path)
    assert proc.returncode == 1
    assert "cancelled" in proc.stderr


def test_verify_evidence_leaves_non_xai_receipts_alone(tmp_path):
    """The suffix is the trigger here too: claude/codex transports declare no
    envelope and must not be refused for the absence."""
    _fake_receipt(tmp_path, "c1", "reviewer-1", "SUCCEEDED")
    _fake_receipt(tmp_path, "c2", "reviewer-2", "SUCCEEDED")
    assert _verify(tmp_path, "c1,c2", 2) == 0


def test_top_level_observed_pair_stays_null_unavailable_with_served_models_recorded(
        tmp_path):
    """DD-7. The served identifier (`grok-4.6-build`) is now observable, but
    promoting it to the top-level pair would make an honest copy of this
    receipt fail RouteObservationV1's I-OBS-MODEL rule — or force the
    observation to lie by recording `unavailable`. The evidence is preserved
    as envelope detail and the schema bump is a separate tranche."""
    session_dir = tmp_path / "session"
    doc = grok_doc()
    fake = write_fake(
        tmp_path, "served.py",
        session_writer(session_dir)
        + f"import sys\nsys.stdout.write({json.dumps(doc)!r})\n")
    proc, receipt = run_dispatch(
        tmp_path, [sys.executable, fake],
        extra=("--model-id", "grok-4.6", *ENVELOPE_ARGS,
               *session_args(session_dir)))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["envelope"]["served_models"] == ["grok-4.6-build"]
    assert receipt["model_id"] == "grok-4.6"
    # Observed as served, recorded verbatim: the supervisor does not
    # normalize a served identifier against the declared one.
    assert receipt["observed_model_id"] is None
    assert receipt["observed_model_source"] == "unavailable"


# ---------------------------------------------------------------------------
# DD-2 R2-C1 — artifact IDENTITY. `st_nlink` is sampled at exactly two
# instants (pre-spawn baseline, grading), and a child controls everything in
# between. The gap is not theoretical: link an outside inode at the required
# path, write through it, unlink that name, drop a clean single-link decoy,
# and both samples read 1 while an external file was overwritten under a
# SUCCEEDED receipt. The supervisor cannot stop an unrestricted child from
# writing outside its root — it can refuse to CERTIFY an attempt whose
# required path stopped naming the inode the supervisor pinned before spawn.
# ---------------------------------------------------------------------------


def launder_fake(target, victim, *, decoy="CLEAN DECOY", exit_code=0,
                 stdout="done"):
    """A child that hard-link-launders an outside inode through the required
    path and then hides the evidence before exiting."""
    return "\n".join([
        "import os, sys",
        "from pathlib import Path",
        f"target = Path({str(target)!r})",
        f"victim = Path({str(victim)!r})",
        "target.parent.mkdir(parents=True, exist_ok=True)",
        # Whatever stands at the required path now — nothing, or a
        # supervisor-held reservation — is removed so the outside inode can
        # take its name.
        "target.unlink(missing_ok=True)",
        "os.link(victim, target)",          # in-root NAME, outside INODE
        "target.write_text('LAUNDERED')",   # the write lands on the victim
        "target.unlink()",                  # the second name disappears
        f"target.write_text({decoy!r})",    # fresh, single-linked, 'clean'
        f"sys.stdout.write({stdout!r})",
        f"sys.exit({exit_code})",
    ]) + "\n"


def test_transient_hard_link_removed_before_grading_is_invalid_output(tmp_path):
    """The laundering sequence in full, against an artifact path that is
    ABSENT pre-spawn — the case with no baseline inode to compare against,
    and therefore the one the two `st_nlink` samples never covered.

    The victim IS overwritten; that is asserted, not hidden. A supervisor
    cannot fence a child it does not confine. What it must never do is hand
    that child a SUCCEEDED receipt whose `contained: true` / `changed: true`
    record reads as proof the write stayed inside the root.
    """
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"          # absent pre-spawn: no baseline inode
    victim = tmp_path / "victim.md"    # OUTSIDE --artifact-root
    victim.write_text("VICTIM CONTENT")
    fake = write_fake(tmp_path, "art_launder.py", launder_fake(target, victim))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    # The damage is real and outside this supervisor's reach.
    assert victim.read_text() == "LAUNDERED"
    # The proof is not.
    assert receipt["result"]["state"] != "SUCCEEDED"
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert f"artifact_identity_replaced:{target}" in \
        receipt["result"]["invalid_reasons"]
    (record,) = receipt["result"]["artifacts"]
    assert record["identity_pinned"] is False
    # No digest is recorded for an inode the supervisor never pinned: a hash
    # there would be the same false proof in a smaller font.
    assert record["sha256"] is None


def test_transient_hard_link_over_a_preexisting_artifact_is_invalid_output(
        tmp_path):
    """The same laundering against a path that DID exist pre-spawn. The
    baseline inode was pinned, so replacement is caught even though the
    decoy's bytes differ from the baseline (`changed` would have said true)."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    target.write_text("BEFORE")
    victim = tmp_path / "victim.md"
    victim.write_text("VICTIM CONTENT")
    fake = write_fake(tmp_path, "art_launder2.py",
                      launder_fake(target, victim, decoy="AFTER"))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert f"artifact_identity_replaced:{target}" in \
        receipt["result"]["invalid_reasons"]


def test_in_place_write_keeps_the_pinned_identity_and_succeeds(tmp_path):
    """The ability this gate must not cost: producing a previously ABSENT
    artifact. An in-place writer (`open(..., 'w')`, the ordinary case) keeps
    the inode the supervisor reserved, so the receipt still records a
    SUCCEEDED with a digest — and now also records the identity it kept."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_inplace.py",
                      artifact_fake([(target, "PLAN BODY")]))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    (record,) = receipt["result"]["artifacts"]
    assert record["identity_pinned"] is True
    assert record["changed"] is True
    assert record["sha256"] == sha256_of("PLAN BODY")


def replace_fake(target, *, content="RENAMED IN"):
    """A child that writes a temp file and `os.replace`s it over the required
    path — the atomic-write idiom, which necessarily installs a NEW inode."""
    return "\n".join([
        "import os, sys",
        "from pathlib import Path",
        f"target = Path({str(target)!r})",
        "target.parent.mkdir(parents=True, exist_ok=True)",
        "tmp = target.with_suffix('.tmp')",
        f"tmp.write_text({content!r})",
        "os.replace(tmp, target)",
        "sys.stdout.write('done')",
    ]) + "\n"


@pytest.mark.parametrize("preexisting", [False, True])
def test_atomic_rename_over_a_required_artifact_is_invalid_output(
        tmp_path, preexisting):
    """The documented cost of the identity contract, pinned by a test rather
    than left for a caller to discover.

    `os.replace(tmp, target)` is indistinguishable at grading time from the
    laundering sequence above: both end with a fresh, single-linked inode at
    the required path and no way to tell which one wrote the file it
    replaced. The supervisor refuses both. A seat that must produce a
    required artifact writes it IN PLACE.
    """
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    if preexisting:
        target.write_text("BEFORE")
    fake = write_fake(tmp_path, "art_replace.py", replace_fake(target))
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert f"artifact_identity_replaced:{target}" in \
        receipt["result"]["invalid_reasons"]


def _lowest_free_fd():
    """The lowest currently-free descriptor number.

    POSIX allocates the lowest free fd, so opening and immediately closing a
    probe reports where the free space starts. Comparing the value before and
    after a whole `run` is a portable leak assertion — a pin the supervisor
    forgot to close occupies a slot and pushes this number up. `/proc/self/fd`
    would be the direct read, and it does not exist on this project's macOS
    development platform.
    """
    fd = os.open(os.devnull, os.O_RDONLY)
    os.close(fd)
    return fd


def _raise_runtime_error(*_args, **_kwargs):
    raise RuntimeError("injected")


@pytest.mark.parametrize("outcome", ["succeeded", "invalid", "failed", "crash"])
def test_artifact_pins_are_released_on_every_exit_path(
        tmp_path, monkeypatch, outcome):
    """The pin is a descriptor the supervisor holds for the whole attempt, so
    every way out of `run` has to give it back — including the one that leaves
    through the crash handler and re-raises."""
    dispatch_agent = _in_process(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    if outcome == "succeeded":
        body = artifact_fake([(target, "PLAN BODY")])
    elif outcome == "invalid":
        body = artifact_fake()                       # never writes the artifact
    elif outcome == "failed":
        body = artifact_fake([(target, "X")], exit_code=3)
    else:
        body = artifact_fake([(target, "X")])
        monkeypatch.setattr(dispatch_agent, "_grade_artifacts",
                            _raise_runtime_error)
    fake = write_fake(tmp_path, f"art_fd_{outcome}.py", body)
    receipt_dir = tmp_path / "receipts"
    argv = ["run", "--attempt-id", f"fd-{outcome}",
            "--receipt-dir", str(receipt_dir),
            "--deadline-seconds", "30", "--grace-seconds", "1",
            "--seat", "worker", "--output-schema", "none",
            *artifact_args(root, target), "--", sys.executable, str(fake)]
    before = _lowest_free_fd()
    rc = dispatch_agent.main(argv)
    assert _lowest_free_fd() == before
    receipt = json.loads((receipt_dir / f"fd-{outcome}.json").read_text())
    assert receipt["result"]["state"] in dispatch_agent.STATES
    if outcome == "succeeded":
        assert rc == 0 and receipt["result"]["state"] == "SUCCEEDED"
    if outcome == "crash":
        assert rc == 9


def _pin_args(root, paths, attempt_id="pin1"):
    return argparse.Namespace(
        artifact_root=str(root),
        require_artifact=[str(p) for p in paths],
        require_artifact_sha256=[],
        require_artifact_baseline_max_bytes=1024 * 1024,
        attempt_id=attempt_id)


def test_artifact_pins_are_not_inherited_and_close_on_release(tmp_path):
    """A descriptor the supervisor holds open on the artifact for the whole
    attempt must not become a handle the child inherits.

    Non-inheritable is Python's default for `os.open`/`os.dup`, which is
    exactly why it is asserted rather than assumed: the default is one keyword
    away from being lost, and losing it hands the child the very inode this
    pin exists to protect. Holding the descriptor is also what makes the
    identity comparison sound — an unlinked inode whose number is free can be
    handed straight back to the next file created, and a pin that was already
    closed would let that recycled number read as "the same artifact".
    """
    dispatch_agent = _in_process(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    absent = root / "new.md"
    existing = root / "old.md"
    existing.write_text("BEFORE")
    captured = dispatch_agent._capture_artifact_baselines(
        _pin_args(root, [absent, existing]))
    assert not isinstance(captured, str), captured
    _root, entries = captured
    assert len(entries) == 2
    assert absent.exists(), "an absent required path is reserved before spawn"
    for entry in entries:
        fd = entry["pin_fd"]
        assert fd is not None and fd >= 0
        assert os.get_inheritable(fd) is False
        assert os.fstat(fd).st_ino == entry["pin_ino"]
    dispatch_agent._release_artifact_pins(entries)
    for entry in entries:
        assert entry["pin_fd"] is None
    # An untouched reservation is withdrawn, so a supervisor that reserved a
    # path and then crashed does not leave a file standing where the caller
    # declared there was none. A pre-existing artifact is never removed.
    assert not absent.exists()
    assert existing.read_text() == "BEFORE"


def test_a_written_reservation_is_never_withdrawn_on_release(tmp_path):
    """The other half of the withdrawal rule: once the child has written the
    reserved path, that file is the attempt's product and the supervisor must
    not delete it — not on the terminal path, and not on the crash path that
    reaches the same release."""
    dispatch_agent = _in_process(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    absent = root / "new.md"
    captured = dispatch_agent._capture_artifact_baselines(
        _pin_args(root, [absent], attempt_id="pin2"))
    _root, entries = captured
    with open(absent, "w") as f:          # in place: same inode, new bytes
        f.write("THE CHILD WROTE THIS")
    dispatch_agent._release_artifact_pins(entries)
    assert absent.read_text() == "THE CHILD WROTE THIS"


def test_an_unwritten_reservation_grades_as_missing_not_empty(tmp_path):
    """The reservation must not change what the caller is told. A required
    artifact the child never produced is `artifact_missing`, exactly as it was
    when the supervisor left the path absent — and the reservation it wrote to
    hold the path is gone from the receipt's view of the world."""
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, "art_unwritten.py", artifact_fake())
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 schema="none",
                                 extra=artifact_args(root, target))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert f"artifact_missing:{target}" in receipt["result"]["invalid_reasons"]
    (record,) = receipt["result"]["artifacts"]
    assert record["exists"] is False
    assert record["sha256"] is None
    assert not target.exists()


# ---------------------------------------------------------------------------
# DD-3 R2-W1 — the terminal evidence tail is BEST-EFFORT, and best-effort has
# to mean it. Once a terminal state is selected, the unconditional backfill
# below it records evidence and records nothing else. An exception escaping
# that backfill reaches `cmd_run`'s post-spawn crash handler, which re-runs
# the termination ladder and rewrites `result.state` to CANCELLED /
# TERMINATION_UNCONFIRMED with exit 9 — a FAILED attempt reported as a
# supervisor crash, and a real exit status erased by an unreadable log file.
# ---------------------------------------------------------------------------


def _evidence_read_error(*_args, **_kwargs):
    """`_tail_bytes` opens inside a try and then seeks/reads outside it. EIO
    on a rotating or truncated `events.jsonl` is the deterministic stand-in
    for every post-open read failure that surface can produce."""
    raise OSError(errno.EIO, "Input/output error")


@pytest.mark.parametrize("state,rc,child", [
    ("FAILED", 1, "import sys\nprint('verdict: PASS')\nsys.exit(4)\n"),
    ("TIMED_OUT", 3, "import time\ntime.sleep(60)\n"),
])
def test_a_selected_terminal_state_survives_an_evidence_read_error(
        tmp_path, monkeypatch, state, rc, child):
    """Both already-selected states named in the finding, through the same
    injected failure. The state, the exit mapping and the termination proof
    all have to come out the other side unchanged."""
    dispatch_agent = _in_process(tmp_path)
    monkeypatch.setattr(dispatch_agent, "_tail_bytes", _evidence_read_error)
    session_dir = tmp_path / "session"
    fake = write_fake(tmp_path, f"sess_eio_{state}.py",
                      session_writer(session_dir) + child)
    receipt_dir = tmp_path / "receipts"
    attempt = f"eio-{state.lower()}"
    assert dispatch_agent.main([
        "run", "--attempt-id", attempt, "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "2", "--grace-seconds", "1",
        "--seat", "reviewer-1", "--output-schema", "review",
        *session_args(session_dir),
        "--", sys.executable, str(fake)]) == rc
    receipt = json.loads((receipt_dir / f"{attempt}.json").read_text())
    assert receipt["result"]["state"] == state
    assert receipt["result"]["termination_confirmed"] is True
    assert receipt["result"]["invalid_reasons"] is None
    # The evidence that could be read is kept; the part that could not is the
    # documented null, not a missing key and not a crash.
    evidence = receipt["session_evidence"]
    assert evidence["format"] == "grok-session-v1"
    assert evidence["session_id"] == SESSION_UUID
    assert evidence["terminal_event"] is None
    assert evidence["unreadable"] is True


def _unparseable_summary(session_dir, session_id=SESSION_UUID):
    """A `summary.json` that is valid JSON text but whose parse raises
    something that is neither `JSONDecodeError` nor `UnicodeDecodeError`.

    CPython refuses to convert an integer literal over 4300 digits and raises
    a bare `ValueError` doing it — a real parse edge with no monkeypatch
    anywhere, reached through the same `json.loads` the evidence reader uses.
    """
    payload = ('{"info": {"id": "%s"}, "created_at": "2026-08-25T08:00:00.0Z",'
               ' "n": %s}' % (session_id, "1" * 5000))
    return "\n".join([
        "from pathlib import Path",
        f"d = Path({str(session_dir)!r})",
        "d.mkdir(parents=True, exist_ok=True)",
        f"(d / 'summary.json').write_text({payload!r})",
    ]) + "\n"


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="the integer-literal digit limit lands in 3.11")
def test_an_unparseable_summary_does_not_relabel_a_failed_receipt(tmp_path):
    """The parse half of the finding, with nothing injected. A FAILED attempt
    stays FAILED and exits 1 even though reading its evidence raised."""
    session_dir = tmp_path / "session"
    fake = write_fake(
        tmp_path, "sess_bigint_fail.py",
        _unparseable_summary(session_dir)
        + "import sys\nprint('verdict: PASS')\nsys.exit(4)\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 1, proc.stderr
    assert receipt["result"]["state"] == "FAILED"
    assert receipt["session_evidence"]["summary"] is None
    assert receipt["session_evidence"]["unreadable"] is True


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="the integer-literal digit limit lands in 3.11")
def test_an_unparseable_summary_on_the_grading_path_is_a_typed_refusal(
        tmp_path):
    """The same document where success IS still on the table. Evidence that
    cannot be read proves no binding, so the attempt is INVALID_OUTPUT with
    the vocabulary's own reason — never a supervisor crash."""
    session_dir = tmp_path / "session"
    fake = write_fake(tmp_path, "sess_bigint_ok.py",
                      _unparseable_summary(session_dir)
                      + "print('verdict: PASS')\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=session_args(session_dir))
    assert proc.returncode == 6, proc.stderr
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "session_evidence_unreadable" in receipt["result"]["invalid_reasons"]
    assert receipt["session_evidence"]["unreadable"] is True


# ---------------------------------------------------------------------------
# DD-2 R3 — a reservation is only proof of what was actually WRITTEN, cleanup
# is not allowed to become the attempt's outcome, and a withdrawal that failed
# is not allowed to be reported as absence.
#
# Three defects, one theme: the supervisor's own I/O was assumed to succeed.
# A short `os.write` recorded the whole body's digest over a truncated file
# (so a no-op child inherited a marker that graded as its own product), a
# cleanup error escaped the release loop (leaking the remaining pins and
# replacing an already-persisted terminal exit with 9), and a failed unlink
# was swallowed while the receipt was rewritten to say `exists: false` about
# a path that is still there.
# ---------------------------------------------------------------------------


RESERVATION_PREFIX = b"deep-model-router reserved"


def _short_write(real_write, chunk=1):
    """One deterministic short write on the reservation body, then ordinary
    writes. POSIX allows a successful `write` to consume fewer bytes than it
    was handed; this is that, without a full filesystem or a signal race."""
    def fake(fd, data):
        if data.startswith(RESERVATION_PREFIX):
            return real_write(fd, data[:chunk])
        return real_write(fd, data)
    return fake


def _zero_write(real_write):
    """A successful write that makes no progress at all — the loop's own
    termination condition, and the case a `while written < len(body)` retry
    would otherwise spin on forever."""
    def fake(fd, data):
        if data.startswith(RESERVATION_PREFIX):
            return 0
        return real_write(fd, data)
    return fake


def test_a_short_reservation_write_is_completed_before_it_is_recorded(
        tmp_path, monkeypatch):
    """The recorded digest and size describe the reservation the supervisor
    MEANT to write. If a short write leaves fewer bytes on disk than that,
    grading compares a truncated file against the whole body's digest, does
    not recognise it, and hands a no-op child a `changed: true` artifact it
    never produced. The write has to complete, or it has to fail."""
    dispatch_agent = _in_process(tmp_path)
    monkeypatch.setattr(dispatch_agent.os, "write",
                        _short_write(dispatch_agent.os.write))
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    captured = dispatch_agent._capture_artifact_baselines(
        _pin_args(root, [target], attempt_id="short1"))
    assert not isinstance(captured, str), captured
    _root, entries = captured
    body = dispatch_agent._reservation_body("short1")
    assert target.read_bytes() == body, "a short write was recorded as whole"
    (entry,) = entries
    assert entry["reservation_size"] == len(body)
    assert entry["reservation_sha256"] == sha256_of(body.decode())
    # The child does nothing. The reservation must still grade as the absence
    # it is — never as the child's own output.
    records, reasons, aborted = dispatch_agent._grade_artifacts(
        entries, _root, time.monotonic() + 30, False)
    assert aborted is False
    assert f"artifact_missing:{target}" in reasons
    (record,) = records
    assert record["exists"] is False and record["sha256"] is None
    dispatch_agent._release_artifact_pins(entries)


def test_a_reservation_that_makes_no_progress_fails_preflight(
        tmp_path, monkeypatch):
    """Zero progress is not a reservation. It fails BEFORE the claim, so the
    attempt-id is not burned, no receipt exists to unwind, and the path the
    supervisor created for a pin it could not take is removed again."""
    dispatch_agent = _in_process(tmp_path)
    monkeypatch.setattr(dispatch_agent.os, "write",
                        _zero_write(dispatch_agent.os.write))
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    pins = []
    before = _lowest_free_fd()
    captured = dispatch_agent._capture_artifact_baselines(
        _pin_args(root, [target], attempt_id="zero1"), pins)
    assert isinstance(captured, str), "a zero-progress write was accepted"
    assert "reservation" in captured
    assert not target.exists(), "the failed reservation was left behind"
    assert [entry["pin_fd"] for entry in pins] == [None]
    assert _lowest_free_fd() == before

    receipt_dir = tmp_path / "receipts"
    fake = write_fake(tmp_path, "art_zero.py", artifact_fake())
    assert dispatch_agent.main([
        "run", "--attempt-id", "zero2", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "none",
        *artifact_args(root, target), "--", sys.executable, str(fake)]) == 2
    assert not (receipt_dir / "zero2.json").exists()
    assert not (receipt_dir / "zero2.claim").exists()


def _withdrawal_read_error(real_hash, failures=1):
    """EIO on the cleanup read of a reservation. `_hash_artifact` is handed
    the descriptor and owns it, so the fake closes it before raising —
    exactly as the real `os.fdopen` context manager does on a read error.
    Only the withdrawal path passes `deadline_monotonic=None` for a reserved
    (absent pre-spawn) artifact, so grading is untouched."""
    state = {"left": failures}
    def fake(fd, deadline_monotonic):
        if deadline_monotonic is None and state["left"] > 0:
            state["left"] -= 1
            os.close(fd)
            raise OSError(errno.EIO, "Input/output error")
        return real_hash(fd, deadline_monotonic)
    return fake


def test_a_cleanup_error_on_one_pin_still_releases_the_others(
        tmp_path, monkeypatch):
    """Release is a loop over descriptors this process owes back. An error
    withdrawing the first entry's reservation must not abort the loop: the
    first pin still closes, and every later entry is still cleaned up."""
    dispatch_agent = _in_process(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    first, second = root / "a.md", root / "b.md"
    before = _lowest_free_fd()
    captured = dispatch_agent._capture_artifact_baselines(
        _pin_args(root, [first, second], attempt_id="rel1"))
    assert not isinstance(captured, str), captured
    _root, entries = captured
    monkeypatch.setattr(dispatch_agent, "_hash_artifact",
                        _withdrawal_read_error(dispatch_agent._hash_artifact))
    dispatch_agent._release_artifact_pins(entries)   # must not raise
    assert [entry["pin_fd"] for entry in entries] == [None, None]
    assert _lowest_free_fd() == before
    # Truthful, both ways: the reservation whose identity could not be
    # re-read is NOT deleted, and the one that could be is.
    assert first.exists()
    assert not second.exists()


@pytest.mark.parametrize("state,rc,child", [
    ("FAILED", 1, "import sys\nsys.exit(3)\n"),
    ("TIMED_OUT", 3, "import time\ntime.sleep(60)\n"),
])
def test_a_cleanup_error_never_replaces_the_persisted_outcome(
        tmp_path, monkeypatch, state, rc, child):
    """The receipt is written first and the pins are released after, from
    `cmd_run`'s `finally`. An exception there reaches `main`'s crash guard
    and returns 9 — a command whose exit status contradicts the terminal
    receipt already on disk. Cleanup records; it never decides."""
    dispatch_agent = _in_process(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    fake = write_fake(tmp_path, f"rel_{state}.py", child)
    monkeypatch.setattr(dispatch_agent, "_hash_artifact",
                        _withdrawal_read_error(dispatch_agent._hash_artifact))
    receipt_dir = tmp_path / "receipts"
    attempt = f"rel-{state.lower()}"
    before = _lowest_free_fd()
    assert dispatch_agent.main([
        "run", "--attempt-id", attempt, "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "2", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "none",
        *artifact_args(root, target),
        "--", sys.executable, str(fake)]) == rc
    receipt = json.loads((receipt_dir / f"{attempt}.json").read_text())
    assert receipt["result"]["state"] == state
    assert _lowest_free_fd() == before


def _unlink_refused(real_unlink, refused):
    """EPERM on one exact path. Every other unlink — the claim sentinel, the
    receipt tmp file — goes through, so the injection tests the withdrawal
    and nothing else."""
    def fake(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(refused):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_unlink(path, *args, **kwargs)
    return fake


def test_a_reservation_whose_unlink_fails_is_not_reported_as_absent(
        tmp_path, monkeypatch):
    """`exists: false` about a path that is still on disk is a receipt that
    contradicts the filesystem — and the next attempt reading that path finds
    a file the receipt promised was not there. The record follows the unlink,
    not the intention to unlink, and the failure gets its own reason."""
    dispatch_agent = _in_process(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    monkeypatch.setattr(dispatch_agent.os, "unlink",
                        _unlink_refused(dispatch_agent.os.unlink, target))
    fake = write_fake(tmp_path, "art_nounlink.py", artifact_fake())
    receipt_dir = tmp_path / "receipts"
    assert dispatch_agent.main([
        "run", "--attempt-id", "nounlink", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "none",
        *artifact_args(root, target), "--", sys.executable, str(fake)]) == 6
    receipt = json.loads((receipt_dir / "nounlink.json").read_text())
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    reasons = receipt["result"]["invalid_reasons"]
    # The child still produced nothing — that diagnosis does not change.
    assert f"artifact_missing:{target}" in reasons
    # And the supervisor says, in the vocabulary, that its own cleanup left
    # something behind.
    assert f"artifact_reservation_cleanup_failed:{target}" in reasons
    for reason in reasons:
        assert dispatch_agent._is_documented_reason(reason), reason
    (record,) = receipt["result"]["artifacts"]
    assert target.exists(), "the reservation is still there"
    assert record["exists"] is True, "the receipt disagrees with the disk"
    assert record["size"] == len(dispatch_agent._reservation_body("nounlink"))
    assert record["sha256"] is None, "supervisor bytes are never a child digest"
    assert record["identity_pinned"] is True


def test_a_withdrawal_whose_unlink_fails_keeps_the_reservation_identity(
        tmp_path, monkeypatch):
    """The release path has the same rule as the grading path: the metadata
    that says "this file is the supervisor's placeholder" is what lets anyone
    tell it apart from a child's output, so it is cleared only once the file
    is actually gone. Clearing it over a file that is still there leaves an
    entry claiming a reservation it still holds is not one."""
    dispatch_agent = _in_process(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    target = root / "plan.md"
    captured = dispatch_agent._capture_artifact_baselines(
        _pin_args(root, [target], attempt_id="withdraw1"))
    assert not isinstance(captured, str), captured
    _root, entries = captured
    (entry,) = entries
    digest = entry["reservation_sha256"]
    monkeypatch.setattr(dispatch_agent.os, "unlink",
                        _unlink_refused(dispatch_agent.os.unlink, target))
    assert dispatch_agent._withdraw_reservation(entry) is False
    assert target.exists()
    assert entry["reservation_sha256"] == digest
    monkeypatch.undo()
    dispatch_agent._release_artifact_pins(entries)
    assert not target.exists()
