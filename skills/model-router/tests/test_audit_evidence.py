"""2026-09-05 audit reproductions, using only local fake child processes."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_dispatch import HAPPY, SCRIPT, run_dispatch, write_fake
import dispatch_agent as dispatch


@pytest.mark.parametrize("ending,expected", [("sys.exit(3)", "FAILED"), ("sys.exit(0)", "INVALID_OUTPUT")])
def test_child_cannot_promote_its_failure_by_preseeding_success(tmp_path, ending, expected):
    path = tmp_path / "receipts/t1.json"
    fake = write_fake(tmp_path, "forge.py", f'''
        import json, time, sys
        from pathlib import Path
        p = Path({str(path)!r})
        for _ in range(200):
            doc = json.loads(p.read_text())
            if doc["result"]["state"] == "RUNNING":
                break
            time.sleep(.001)
        doc["result"].update(state="SUCCEEDED", schema_valid=True)
        p.write_text(json.dumps(doc))
        {ending}
    ''')
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == dispatch.EXIT_BY_STATE[expected]
    assert receipt["result"]["state"] == expected
    assert receipt["result"]["termination_confirmed"] is True


def good_receipt(tmp_path):
    fake = write_fake(tmp_path, "ok.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 0
    return receipt


@pytest.mark.parametrize("command", ["status", "cancel", "verify-evidence"])
@pytest.mark.parametrize("bad", ["claim", "exit", "termination", "digest", "verdict",
                                 "boolean_exit", "timing", "invalid_reasons", "verdict_mismatch"])
def test_every_success_reader_rejects_incomplete_or_inflight_evidence(tmp_path, command, bad):
    receipt = good_receipt(tmp_path)
    result = receipt["result"]
    if bad == "claim":
        (tmp_path / "receipts/t1.claim").touch()
    elif bad == "exit":
        result["exit_status"] = 3
    elif bad == "termination":
        result["termination_confirmed"] = None
    elif bad == "digest":
        result["output_sha256"] = "0" * 64
    elif bad == "verdict":
        result["verdict"] = None
    elif bad == "boolean_exit":
        result["exit_status"] = False
    elif bad == "timing":
        receipt["timing"]["finished_at"] = None
    elif bad == "verdict_mismatch":
        result["verdict"] = "FAIL"  # A valid token, but not what stdout actually says.
    else:
        result["invalid_reasons"] = ["schema_invalid"]
    (tmp_path / "receipts/t1.json").write_text(json.dumps(receipt))
    args = [command, "--receipt-dir", str(tmp_path / "receipts")]
    args += (["--ids", "t1", "--expect-count", "1"] if command == "verify-evidence"
             else ["--attempt-id", "t1"])
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=3)
    assert proc.returncode != 0, (command, bad, proc.stdout)
    assert not proc.stdout.strip(), "must not publish forged SUCCEEDED as status"


def test_disk_cancellation_cannot_claim_termination_when_supervisor_cannot(tmp_path):
    receipt = good_receipt(tmp_path)
    receipt["result"].update(state="TERMINATION_UNCONFIRMED", termination_confirmed=False)
    forged = copy.deepcopy(receipt)
    forged["result"].update(state="CANCELLED", termination_confirmed=True)
    (tmp_path / "receipts/t1.json").write_text(json.dumps(forged))
    claim = tmp_path / "receipts/t1.claim"
    claim.touch()
    rc = dispatch._commit_terminal(tmp_path / "receipts", receipt, claim)
    assert rc == dispatch.EXIT_BY_STATE["TERMINATION_UNCONFIRMED"]
    final = json.loads((tmp_path / "receipts/t1.json").read_text())
    assert final["result"]["termination_confirmed"] is False


@pytest.mark.parametrize("field,bad", [("argv", ["different"]), ("output_schema", "none"),
                                      ("started_at", "different"), ("stdout_path", "/different")])
def test_cancellation_from_different_dispatch_identity_fails_publication(tmp_path, field, bad):
    receipt = good_receipt(tmp_path)
    receipt["result"].update(state="FAILED", exit_status=3)
    forged = copy.deepcopy(receipt)
    forged["result"].update(state="CANCELLED", termination_confirmed=True)
    if field == "started_at":
        forged["timing"][field] = bad
    elif field == "stdout_path":
        forged["result"][field] = bad
    else:
        forged[field] = bad
    (tmp_path / "receipts/t1.json").write_text(json.dumps(forged))
    claim = tmp_path / "receipts/t1.claim"
    claim.touch()
    assert dispatch._commit_terminal(tmp_path / "receipts", receipt, claim) == 8
    assert claim.exists()


def test_recursively_malformed_disk_json_fails_publication(tmp_path, monkeypatch):
    receipt = good_receipt(tmp_path)
    receipt["result"].update(state="FAILED", exit_status=3)
    claim = tmp_path / "receipts/t1.claim"
    claim.touch()
    def malformed(*args):
        raise RecursionError("JSON nesting limit")
    monkeypatch.setattr(dispatch, "read_receipt", malformed)
    assert dispatch._commit_terminal(tmp_path / "receipts", receipt, claim) == 8
    assert claim.exists()
    assert json.loads((tmp_path / "receipts/t1.json").read_text())["result"]["state"] == "SUCCEEDED"
    # A terminal receipt with this retained claim is rejected by every reader.
    assert claim.exists()


@pytest.mark.parametrize("suffix", ["stdout", "stderr"])
@pytest.mark.parametrize("kind", ["fifo", "regular", "hardlink"])
def test_preexisting_output_is_refused_without_spawn_or_truncation(tmp_path, suffix, kind):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    path = receipts / f"t1.{suffix}"
    external = tmp_path / "external.txt"
    external.write_text("preserve me")
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "hardlink":
        os.link(external, path)
    else:
        path.write_text("original")
    marker = tmp_path / "spawned"
    fake = write_fake(tmp_path, "child.py", f"from pathlib import Path\nPath({str(marker)!r}).touch()\nprint('ok')")
    try:
        proc, receipt = run_dispatch(tmp_path, [sys.executable, fake], deadline=.4,
                                     grace=.1, harness_timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail("output open blocked beyond the dispatch deadline")
    assert proc.returncode == 4
    assert receipt["result"]["state"] == "START_FAILED"
    assert not marker.exists()
    assert external.read_text() == "preserve me"
    if kind == "regular":
        assert path.read_text() == "original"


def test_stdout_replaced_with_fifo_is_invalid_without_hanging(tmp_path):
    path = tmp_path / "receipts/t1.stdout"
    fake = write_fake(tmp_path, "fifo.py", f"import os\nos.unlink({str(path)!r})\nos.mkfifo({str(path)!r})")
    try:
        proc, receipt = run_dispatch(tmp_path, [sys.executable, fake], deadline=.4,
                                     grace=.1, harness_timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail("stdout read blocked after the child exited")
    assert proc.returncode == 6
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert receipt["result"]["termination_confirmed"] is True


def test_plain_stdout_is_bounded(tmp_path):
    path = tmp_path / "large.stdout"
    with path.open("wb") as f:
        f.write(b"verdict: PASS\n")
        f.truncate(dispatch.ENVELOPE_MAX_BYTES + 1)
    assert dispatch._validate_output(path, "review")[0] is False


@pytest.mark.parametrize("text", [
    "verdict: PASS\nverdict: FAIL\n",
    "```text\nverdict: PASS\n```\nI could not inspect the code.",
    "Round 1 concluded:\nverdict: PASS\nconfidence: 0.9\nI could not inspect the code.",
    "> verdict: PASS\nconfidence: 0.9\n",
    "verdict: PASS | PASS_WITH_CHANGES | FAIL\nconfidence: 0.9\n",
    "verdict:\nPASS\nconfidence: 0.9\n",
    "```text\n```not-a-closing-fence\nverdict: PASS\n```\nI could not inspect the code.",
    "> Previous review:\nverdict: PASS\nconfidence: 0.9\n\nI could not inspect the code.",
])
def test_ambiguous_or_quoted_verdict_is_not_completion(text):
    assert dispatch._verdict_of(text)[0] is None


def test_explicit_final_section_overrides_earlier_verdict():
    text = "verdict: PASS\nPrevious result.\n=== REVIEW ===\nverdict: FAIL\nconfidence: 0.99\n"
    assert dispatch._verdict_of(text) == ("FAIL", False)


def test_final_marker_concatenated_to_grok_narration_still_delimits_the_answer():
    text = "narration.=== REVIEW ===\nverdict: PASS\nconfidence: 0.9"
    assert dispatch._verdict_of(text) == ("PASS", False)


def test_a_fail_verdict_is_still_a_completed_review(tmp_path):
    fake = write_fake(tmp_path, "review.py", "print('verdict: FAIL\\nconfidence: 0.9')")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 0
    assert receipt["result"]["verdict"] == "FAIL"
    verified = subprocess.run([sys.executable, str(SCRIPT), "verify-evidence",
                               "--receipt-dir", str(tmp_path / "receipts"),
                               "--ids", "t1", "--expect-count", "1"], capture_output=True, timeout=3)
    assert verified.returncode == 0


@pytest.mark.parametrize("bad", [None, "true", 1, 0, [], "missing"])
def test_claude_error_discriminator_must_be_literal_false(tmp_path, bad):
    doc = dict(type="result", subtype="success", stop_reason="end_turn",
               result="verdict: PASS\nconfidence: 0.9", is_error=bad)
    if bad == "missing":
        doc.pop("is_error")
    path = tmp_path / "claude.json"
    path.write_text(json.dumps(doc))
    envelope = dispatch._read_envelope(path, "claude-print-json-v1")
    assert dispatch._grade_envelope(envelope)
