"""RouteObservationV1 contract and validator-core tests."""

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
SCRIPT = SKILL / "scripts" / "validate_observation.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "observation"
sys.path.insert(0, str(SKILL / "scripts"))

from validate_observation import (  # noqa: E402
    ATTEMPT_ID_RE,
    ValidateError,
    subject_sha256,
    validate,
)


def _load(name="pass-full"):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _write(tmp_path, document, name="observation.json", *, ensure_ascii=True):
    path = tmp_path / name
    path.write_text(json.dumps(document, ensure_ascii=ensure_ascii), encoding="utf-8")
    return path


def _cli(tmp_path, path, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(path), "--root", str(tmp_path), *extra],
        capture_output=True, text=True,
    )


def _reject(tmp_path, document, *, raw=None, name="reject.json"):
    path = tmp_path / name
    if raw is None:
        _write(tmp_path, document, name)
    else:
        path.write_bytes(raw)
    result = _cli(tmp_path, path)
    assert result.returncode == 1, result.stderr


def _valid_override(reason="reviewed"):
    return {"from": "pending", "to": "accepted", "reason": reason,
            "at": "2026-08-20T10:00:00Z", "scope": "task"}


@pytest.mark.parametrize("name", [
    "pass-full", "pass-identity-only", "pass-none", "pass-native-none-attempt",
    "pass-deep-review-native-source", "pass-deep-loop-run", "pass-m3-optional",
])
def test_pass_fixtures(name, tmp_path):
    document = _load(name)
    path = _write(tmp_path, document)
    result = _cli(tmp_path, path)
    assert result.returncode == 0, result.stderr
    validate(document, root=tmp_path)


def test_worked_subject_hashes():
    assert subject_sha256("deep-loop", "01ARZ3NDEKTSV4RRFFQ69G5FAV", "ep-01") == \
        "a1a6ccd20d42089aa5bdacfba8e80f6176f785383be380961597e856b9d3966c"
    assert subject_sha256("deep-model-router", "grp-01", None) == \
        "a4639d1b61339690dd157e980e60f93609265f26fec3267a61bec7483b9243f2"


def test_digest_and_attempt_grammars_match_dispatch_agent():
    source = (SKILL / "scripts" / "dispatch_agent.py").read_text(encoding="utf-8")
    pattern = r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z"
    assert pattern in source
    assert ATTEMPT_ID_RE.pattern == pattern
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", "p" * 128)


def test_i_json_duplicate_key(tmp_path):
    raw = b'{"schema_version":"1.0","schema_version":"1.0"}'
    _reject(tmp_path, {}, raw=raw, name="duplicate.json")


def test_i_json_bool_as_count(tmp_path):
    doc = _load()
    doc["payload"]["review_results"] = {"severity_counts": {"vocabulary": "x", "counts": {"critical": True}}}
    _reject(tmp_path, doc)


def test_i_json_file_too_large(tmp_path):
    _reject(tmp_path, {}, raw=b"{" + b" " * (32 * 1024) + b"}", name="large.json")


def test_i_size_canonical_too_large(tmp_path):
    doc = _load()
    doc["x-pad"] = "é" * 12000
    path = _write(tmp_path, doc, ensure_ascii=False)
    result = _cli(tmp_path, path)
    assert result.returncode == 1, result.stderr


def test_i_no_diff_hunk_header(tmp_path):
    doc = _load()
    doc["x-trace"] = "@@ -1"
    _reject(tmp_path, doc)


def test_i_struct_unknown_key(tmp_path):
    doc = _load()
    doc["payload"]["hidden_tests_passed"] = True
    _reject(tmp_path, doc)


def test_i_struct_all_null_usage(tmp_path):
    doc = _load()
    doc["payload"]["attempts"][0]["usage"] = {"turns": None, "tokens": None,
                                                    "input_tokens": None, "output_tokens": None,
                                                    "source": None}
    _reject(tmp_path, doc)


def test_i_struct_schema_version_one(tmp_path):
    doc = _load()
    doc["envelope"]["schema"]["version"] = "1"
    _reject(tmp_path, doc)


def test_i_contract_missing_schema_version(tmp_path):
    doc = _load()
    del doc["schema_version"]
    _reject(tmp_path, doc)


def test_i_contract_missing_git(tmp_path):
    doc = _load()
    del doc["envelope"]["git"]
    _reject(tmp_path, doc)


def test_i_contract_sha256_on_source_artifact(tmp_path):
    doc = _load()
    doc["envelope"]["provenance"]["source_artifacts"][0]["sha256"] = "0" * 64
    _reject(tmp_path, doc)


def test_i_owner_mismatch(tmp_path):
    doc = _load()
    doc["envelope"]["producer"] = "deep-work"
    _reject(tmp_path, doc)


@pytest.mark.parametrize("key", ["argv", "goal", "red_verification_output"])
def test_i_no_raw_keys_payload(tmp_path, key):
    doc = _load()
    doc["payload"][key] = "raw"
    _reject(tmp_path, doc)


def test_i_no_raw_keys_envelope_prompt(tmp_path):
    doc = _load()
    doc["envelope"]["prompt"] = "raw"
    _reject(tmp_path, doc)


def test_i_no_diff_in_reason(tmp_path):
    doc = _load()
    doc["payload"]["final"] = {"human": {"overrides": [_valid_override("diff --git a/x b/x")]}}
    _reject(tmp_path, doc)


def test_i_string_reason_too_long(tmp_path):
    doc = _load()
    doc["payload"]["final"] = {"human": {"overrides": [_valid_override("x" * 257)]}}
    _reject(tmp_path, doc)


def test_i_string_identity_absolute(tmp_path):
    doc = _load()
    doc["payload"]["subject"]["run_id"] = "/Users/x"
    _reject(tmp_path, doc)


def test_i_subject_hash_mismatch(tmp_path):
    doc = _load()
    doc["payload"]["subject"]["subject_sha256"] = "0" * 64
    _reject(tmp_path, doc)


def test_i_subject_deep_loop_null_artifact(tmp_path):
    doc = _load()
    doc["payload"]["subject"]["artifact_id"] = None
    _reject(tmp_path, doc)


def test_i_subject_round_zero_padded(tmp_path):
    doc = _load()
    subject = doc["payload"]["subject"]
    subject.update(producer="deep-review", run_id="loop-1", artifact_id="round-01")
    subject["subject_sha256"] = subject_sha256(subject["producer"], subject["run_id"], subject["artifact_id"])
    doc["envelope"]["producer"] = "deep-review"
    _reject(tmp_path, doc)


def test_i_grain_episode_with_episodes_done(tmp_path):
    doc = _load()
    doc["payload"]["final"] = {"implementation_attempts": {"slices_total": 0, "rework_total": 0,
                                                   "test_retries": 0, "episodes_done": 1}}
    _reject(tmp_path, doc)


def test_i_link_full_without_fingerprint(tmp_path):
    doc = _load()
    doc["payload"]["decision"]["decision_fingerprint"] = None
    _reject(tmp_path, doc)


def test_i_link_fingerprint_without_policy(tmp_path):
    doc = _load()
    doc["payload"]["decision"]["policy_sha256"] = None
    _reject(tmp_path, doc)


def test_i_link_identity_missing_route_schema(tmp_path):
    doc = _load("pass-identity-only")
    doc["payload"]["decision"]["route_schema_version"] = None
    _reject(tmp_path, doc)


def test_i_link_none_with_request_sha(tmp_path):
    doc = _load("pass-none")
    doc["payload"]["decision"]["request_sha256"] = "r" * 64
    _reject(tmp_path, doc)


def test_i_obs_model_native_in_canonical(tmp_path):
    doc = _load()
    doc["payload"]["attempts"][0]["observed_model_source"] = "requested-but-unverified"
    _reject(tmp_path, doc)


def test_i_attempt_none_with_id(tmp_path):
    doc = _load("pass-native-none-attempt")
    doc["payload"]["attempts"][0]["attempt_id"] = "x"
    _reject(tmp_path, doc)


def test_i_attempt_producer_record_without_ref(tmp_path):
    doc = _load("pass-native-none-attempt")
    attempt = doc["payload"]["attempts"][0]
    attempt.update(evidence_kind="producer_record", attempt_id="producer-1")
    _reject(tmp_path, doc)


def test_i_attempt_duplicate_dispatch_id(tmp_path):
    doc = _load()
    doc["payload"]["attempts"].append(copy.deepcopy(doc["payload"]["attempts"][0]))
    _reject(tmp_path, doc)


def test_i_accepted_loop_with_user_choice(tmp_path):
    doc = _load()
    doc["payload"]["final"] = {"accepted": {"decided_by": "deep-loop-kernel", "verdict": True,
                                  "signals": [{"kind": "user-choice"}]}}
    _reject(tmp_path, doc)


def test_i_accepted_work_marker_only(tmp_path):
    doc = _load()
    doc["payload"]["final"] = {"accepted": {"decided_by": "deep-work-finish", "verdict": True,
                                  "signals": [{"kind": "test-marker"}]}}
    _reject(tmp_path, doc)


def test_i_gates_overlap(tmp_path):
    doc = _load()
    doc["payload"]["objective_results"] = {"evidence_completeness": {
        "required_gate_ids": ["a"], "satisfied_gate_ids": ["a"],
        "missing_gate_ids": ["a"], "complete": False}}
    _reject(tmp_path, doc)


def test_i_gates_complete_with_missing(tmp_path):
    doc = _load()
    doc["payload"]["objective_results"] = {"evidence_completeness": {
        "required_gate_ids": ["a"], "satisfied_gate_ids": [],
        "missing_gate_ids": ["a"], "complete": True}}
    _reject(tmp_path, doc)


def test_i_digest_uppercase(tmp_path):
    doc = _load()
    doc["payload"]["decision"]["decision_fingerprint"] = "F" * 64
    _reject(tmp_path, doc)


def test_i_root_required(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{}", encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "--file", str(path)],
                            capture_output=True, text=True)
    assert result.returncode == 2


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_cli(obs, root, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(obs), "--root", str(root), *extra],
        capture_output=True, text=True,
    )


def _materialize(tmp_path, document, files):
    root = tmp_path / "root"
    root.mkdir()
    for rel, data in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    obs = tmp_path / "obs.json"
    obs.write_text(json.dumps(document), encoding="utf-8")
    return obs, root


def _with_digest(document, rel, digest):
    document["envelope"]["provenance"]["source_artifacts"].append({"path": rel})
    document["payload"].setdefault("artifact_digests", []).append(
        {"path": rel, "sha256": digest})
    return document


def _receipt_bytes(attempt_id="att-1", fingerprint="f" * 64, prompt=None, policy="a" * 64):
    body = {
        "attempt_id": attempt_id,
        "decision_fingerprint": fingerprint,
        "prompt_sha256": prompt,
        "policy_sha256": policy,
        "seat": "worker", "runtime": "codex", "model_id": None,
        "effort_native": None, "transport_id": None,
        "result": {"stdout_path": "stdout.txt", "state": "SUCCEEDED",
                   "exit_status": 0, "termination_confirmed": True,
                   "schema_valid": True, "output_sha256": "e" * 64},
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def test_i_refs_root_escape_dotdot(tmp_path):
    doc = _with_digest(_load(), "../outside", "0" * 64)
    obs, root = _materialize(tmp_path, doc, {})
    result = _run_cli(obs, root, "--check-refs")
    assert result.returncode == 1, result.stderr


def test_i_refs_absolute_path_outside_root(tmp_path):
    doc = _with_digest(_load(), "/etc/passwd", "0" * 64)
    obs, root = _materialize(tmp_path, doc, {})
    result = _run_cli(obs, root, "--check-refs")
    assert result.returncode == 1, result.stderr


def test_i_refs_leaf_symlink(tmp_path):
    data = b"leaf-target"
    doc = _with_digest(_load(), "link.txt", _sha(data))
    obs, root = _materialize(tmp_path, doc, {"real.txt": data})
    os.symlink(root / "real.txt", root / "link.txt")
    result = _run_cli(obs, root, "--check-refs")
    assert result.returncode == 1, result.stderr


def test_i_refs_intermediate_symlink(tmp_path):
    data = b"nested"
    doc = _with_digest(_load(), "alias/file.txt", _sha(data))
    obs, root = _materialize(tmp_path, doc, {"real/file.txt": data})
    os.symlink(root / "real", root / "alias")
    result = _run_cli(obs, root, "--check-refs")
    assert result.returncode == 1, result.stderr


def test_i_receipts_evidence_path_mismatch(tmp_path):
    raw = _receipt_bytes()
    doc = _load()
    doc["payload"]["attempts"][0]["evidence_ref"] = {
        "path": "receipts/wrong.json", "sha256": _sha(raw)}
    obs, root = _materialize(tmp_path, doc, {
        "receipts/wrong.json": raw,
        "receipts/att-1.json": raw,
    })
    result = _run_cli(obs, root, "--check-refs", "--check-receipts", str(root / "receipts"))
    assert result.returncode == 1, result.stderr


def test_i_receipts_attempt_id_field_mismatch(tmp_path):
    raw = _receipt_bytes(attempt_id="other")
    doc = _load()
    doc["payload"]["attempts"][0]["evidence_ref"] = {
        "path": "receipts/att-1.json", "sha256": _sha(raw)}
    obs, root = _materialize(tmp_path, doc, {"receipts/att-1.json": raw})
    result = _run_cli(obs, root, "--check-refs", "--check-receipts", str(root / "receipts"))
    assert result.returncode == 1, result.stderr


def test_i_receipts_full_fingerprint_null(tmp_path):
    raw = _receipt_bytes(fingerprint=None)
    doc = _load()
    doc["payload"]["attempts"][0]["evidence_ref"] = {
        "path": "receipts/att-1.json", "sha256": _sha(raw)}
    obs, root = _materialize(tmp_path, doc, {"receipts/att-1.json": raw})
    result = _run_cli(obs, root, "--check-refs", "--check-receipts", str(root / "receipts"))
    assert result.returncode == 1, result.stderr


def test_i_receipts_prompt_sha_mismatch(tmp_path):
    raw = _receipt_bytes(prompt="c" * 64)
    doc = _load()
    doc["payload"]["attempts"][0]["prompt_sha256"] = "d" * 64
    doc["payload"]["attempts"][0]["evidence_ref"] = {
        "path": "receipts/att-1.json", "sha256": _sha(raw)}
    obs, root = _materialize(tmp_path, doc, {"receipts/att-1.json": raw})
    result = _run_cli(obs, root, "--check-refs", "--check-receipts", str(root / "receipts"))
    assert result.returncode == 1, result.stderr


def test_i_receipts_does_not_read_stdout(tmp_path):
    raw = _receipt_bytes()
    doc = _load()
    doc["payload"]["attempts"][0]["evidence_ref"] = {
        "path": "receipts/att-1.json", "sha256": _sha(raw)}
    obs, root = _materialize(tmp_path, doc, {
        "receipts/att-1.json": raw,
        "stdout.txt": b"diff --git a/secret b/secret\n@@ -1,1 +1,1 @@\n",
    })
    result = _run_cli(obs, root, "--check-refs", "--check-receipts", str(root / "receipts"))
    assert result.returncode == 0, result.stderr


def test_i_receipts_identity_only_policy_null_ok(tmp_path):
    raw = _receipt_bytes(fingerprint=None, policy=None)
    doc = _load("pass-identity-only")
    attempt = copy.deepcopy(_load()["payload"]["attempts"][0])
    attempt["evidence_ref"] = {"path": "receipts/att-1.json", "sha256": _sha(raw)}
    doc["payload"]["attempts"] = [attempt]
    obs, root = _materialize(tmp_path, doc, {"receipts/att-1.json": raw})
    result = _run_cli(obs, root, "--check-refs", "--check-receipts", str(root / "receipts"))
    assert result.returncode == 0, result.stderr


def test_i_refs_digest_mismatch(tmp_path):
    data = b"actual-bytes"
    doc = _with_digest(_load(), "blob.bin", "0" * 64)
    obs, root = _materialize(tmp_path, doc, {"blob.bin": data})
    result = _run_cli(obs, root, "--check-refs")
    assert result.returncode == 1, result.stderr


def test_i_receipts_sha256_mismatch(tmp_path):
    raw = _receipt_bytes()
    doc = _load()
    doc["payload"]["attempts"][0]["evidence_ref"] = {
        "path": "receipts/att-1.json", "sha256": "0" * 64}
    obs, root = _materialize(tmp_path, doc, {"receipts/att-1.json": raw})
    result = _run_cli(obs, root, "--check-refs", "--check-receipts", str(root / "receipts"))
    assert result.returncode == 1, result.stderr


def test_i_refs_file_over_8mib(tmp_path):
    data = b"x" * (8 * 1024 * 1024 + 1)
    doc = _with_digest(_load(), "big.bin", _sha(data))
    obs, root = _materialize(tmp_path, doc, {"big.bin": data})
    result = _run_cli(obs, root, "--check-refs")
    assert result.returncode == 1, result.stderr


def test_i_refs_duplicate_digest_path(tmp_path):
    data = b"dup"
    digest = _sha(data)
    doc = _with_digest(_load(), "dup.bin", digest)
    doc["payload"]["artifact_digests"].append({"path": "dup.bin", "sha256": digest})
    obs, root = _materialize(tmp_path, doc, {"dup.bin": data})
    result = _run_cli(obs, root, "--check-refs")
    assert result.returncode == 1, result.stderr


def test_i_check_receipts_requires_check_refs(tmp_path):
    doc = _load()
    obs, root = _materialize(tmp_path, doc, {})
    result = _run_cli(obs, root, "--check-receipts", str(root / "receipts"))
    assert result.returncode == 2, result.stderr


def test_i_check_receipts_skips_none_and_producer_record(tmp_path):
    doc = _load("pass-native-none-attempt")
    producer = copy.deepcopy(doc["payload"]["attempts"][0])
    producer.update(
        evidence_kind="producer_record",
        attempt_id="producer-1",
        evidence_ref={"path": "records/missing.json", "sha256": "e" * 64},
    )
    doc["payload"]["attempts"].append(producer)
    obs, root = _materialize(tmp_path, doc, {})
    result = _run_cli(obs, root, "--check-refs", "--check-receipts", str(root / "receipts"))
    assert result.returncode == 0, result.stderr
