"""A4/A5 invalid inputs must fail before routing or attesting evidence."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_observation import _load as observation, _with_digest
from test_observation import _receipt_bytes, _materialize
import route_task as router
import validate_observation as obs

BASE = dict(route_schema_version=1, task_class="MECHANICAL", complexity=0,
            uncertainty=0, blast_radius=0, reversibility=0)


@pytest.mark.parametrize("field,value", [
    ("route_schema_version", True), ("route_schema_version", 1.0),
    ("reasoning_centric", "false"), ("reasoning_centric", 0), ("reasoning_centric", None),
    ("availability_snapshot", []), ("availability_snapshot", False), ("availability_snapshot", ""),
    ("flags", {}), ("flags", False), ("flags", {"security_sensitive": False}),
    ("availability_snapshot", {"isolation": "typo"}),
    ("availability_snapshot", {"isolation": False}),
    ("availability_snapshot", {"unavailable_models": ""}),
    ("availability_snapshot", {"unavailable_roles": {}}),
    ("availability_snapshot", {"isolation_evidence": False}),
])
def test_request_invalid_types_are_usage_errors(tmp_path, capsys, field, value):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({**BASE, field: value}))
    assert router.main(["--request-json", str(path), "--format", "json"]) == 2
    assert not capsys.readouterr().out


def test_valid_null_and_csv_compatibility():
    a = router.route(router.task_from_request_v1(BASE))
    b = router.route(router.task_from_request_v1({**BASE, "flags": None,
        "prior_failures": None, "availability_snapshot": None, "host_seat": None, "local_policy": None}))
    assert a == b
    csv = router.task_from_request_v1({**BASE, "flags": "tool_heavy,unfamiliar_codebase"})
    array = router.task_from_request_v1({**BASE, "flags": ["tool_heavy", "unfamiliar_codebase"]})
    assert router.route(csv) == router.route(array)


def test_null_isolation_with_explicit_evidence_and_repeated_failure_ids_are_preserved():
    task = router.task_from_request_v1({**BASE, "complexity": 1, "uncertainty": 1,
        "blast_radius": 1, "availability_snapshot": {"isolation": None, "isolation_evidence": ["session-1"]}})
    assert router.route(task)["review"]["review_independence"] == "enforced"
    model = router.default_config()["models"]["openai_worker_fast"]["id"]
    task = router.task_from_request_v1({**BASE, "prior_failures": [model, model]})
    assert task.prior_failures == 2 and task.prior_models == [model, model]


@pytest.mark.parametrize("mode", ["--json", "--request-json"])
@pytest.mark.parametrize("suffix", [',"complexity":1', ',"reversibility":NaN',
                                   ',"reversibility":Infinity', ',"reversibility":1e999'])
def test_router_json_rejects_ambiguous_or_nonfinite_values(tmp_path, capsys, mode, suffix):
    payload = dict(BASE)
    if mode == "--json":
        payload.pop("route_schema_version")
    raw = json.dumps(payload)[:-1] + suffix + "}"
    path = tmp_path / "request.json"
    path.write_text(raw)
    assert router.main([mode, str(path) if mode == "--request-json" else raw, "--format", "json"]) == 2
    assert not capsys.readouterr().out


@pytest.mark.parametrize("mode", ["--json", "--request-json"])
def test_explicit_empty_json_cannot_fall_back_to_valid_cli_flags(mode, capsys):
    args = [mode, "", "--class", "MECHANICAL", "--complexity", "0", "--uncertainty", "0",
            "--blast-radius", "0", "--reversibility", "0", "--format", "json"]
    assert router.main(args) == 2
    assert not capsys.readouterr().out


@pytest.mark.parametrize("value", [1, 0, 1.0, "true", [], {}])
@pytest.mark.parametrize("field", ["tests_passed", "accepted"])
def test_observation_success_fields_are_exact_booleans(value, field):
    doc = observation()
    if field == "tests_passed":
        doc["payload"]["objective_results"] = {"tests_passed": value}
    else:
        doc["payload"]["final"] = {"accepted": {
            "decided_by": "human", "verdict": value, "signals": [{"kind": "user-choice"}]}}
    with pytest.raises(obs.ValidateError):
        obs.validate(doc)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_observation_in_memory_rejects_nonfinite_extensions(number):
    doc = observation()
    doc["x-number"] = number
    with pytest.raises(obs.ValidateError):
        obs.validate(doc)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_observation_file_rejects_nonfinite_extensions(tmp_path, token):
    path = tmp_path / "observation.json"
    path.write_text(json.dumps(observation())[:-1] + ',"x-number":' + token + '}')
    with pytest.raises(obs.ValidateError):
        obs.validate_path(path, tmp_path)


@pytest.mark.parametrize("stamp", ["2026-99-99T99:99:99Z", "2026-02-29T10:00:00Z",
    "2026-09-06T24:00:00Z", "2026-09-06T10:00:00+00:99", "2026-09-06T10:00:00+24:00"])
def test_observation_dates_must_exist(stamp):
    doc = observation()
    doc["envelope"]["generated_at"] = stamp
    with pytest.raises(obs.ValidateError):
        obs.validate(doc)


@pytest.mark.parametrize("stamp", ["2024-02-29T10:00:00Z", "2026-09-06T10:00:00.123456789+09:00",
                                   "2026-09-06T10:00:00.1+09:00",
                                   "2026-09-06T10:00:00-00:00"])
def test_valid_timestamp_profile_is_preserved(stamp):
    doc = observation()
    doc["envelope"]["generated_at"] = stamp
    obs.validate(doc)


@pytest.mark.parametrize("kind", ["route", "observation"])
def test_invalid_utf8_is_an_input_error_without_traceback(tmp_path, capsys, kind):
    path = tmp_path / "invalid.json"
    path.write_bytes(b"\xff")
    if kind == "route":
        assert router.main(["--request-json", str(path)]) == 2
        assert "Traceback" not in capsys.readouterr().err
    else:
        with pytest.raises(obs.ValidateError):
            obs.validate_path(path, tmp_path)


@pytest.mark.parametrize("bad", [{1: "value"}, ("tuple",), "\ud800"])
def test_in_memory_non_json_values_are_not_silently_serialized(bad):
    doc = observation()
    doc["x-value"] = bad
    with pytest.raises(obs.ValidateError):
        obs.validate(doc)


def test_in_memory_cycle_is_a_validation_error():
    doc = observation()
    doc["x-cycle"] = doc
    with pytest.raises(obs.ValidateError):
        obs.validate(doc)


@pytest.mark.parametrize("field", ["linkage_quality", "evidence_kind", "seat", "state"])
@pytest.mark.parametrize("bad", [[], {}])
def test_observation_enum_types_are_rejected_as_validation_errors(field, bad):
    doc = observation()
    target = doc["payload"]["decision"] if field == "linkage_quality" else doc["payload"]["attempts"][0]
    target[field] = bad
    with pytest.raises(obs.ValidateError):
        obs.validate(doc)


@pytest.mark.parametrize("bad", [True, 1.0])
def test_observation_route_version_is_an_integer(bad):
    doc = observation()
    doc["payload"]["decision"]["route_schema_version"] = bad
    with pytest.raises(obs.ValidateError):
        obs.validate(doc)


@pytest.mark.parametrize("bad", [[], {}, 1])
def test_gate_ids_are_strings_before_set_operations(bad):
    doc = observation()
    doc["payload"]["objective_results"] = {"evidence_completeness": {
        "required_gate_ids": [bad], "satisfied_gate_ids": [bad],
        "missing_gate_ids": [], "complete": True}}
    with pytest.raises(obs.ValidateError):
        obs.validate(doc)


def test_observation_input_can_be_an_alias_outside_reference_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    path = tmp_path / "observation.json"
    path.write_text(json.dumps(observation()))
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    obs.validate_path(alias, root)


@pytest.mark.parametrize("addition", [',"attempt_id":"att-1"', ',"x-number":1e999', ',"x-string":"\\ud800"'])
def test_linked_receipt_json_gets_the_same_strict_decoder(tmp_path, addition):
    import hashlib
    raw = _receipt_bytes()[:-1] + addition.encode() + b"}"
    doc = observation()
    doc["payload"]["attempts"][0]["evidence_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()
    path, root = _materialize(tmp_path, doc, {"receipts/att-1.json": raw})
    with pytest.raises(obs.ValidateError):
        obs.validate_path(path, root, check_refs=True, check_receipts=root / "receipts")


@pytest.mark.parametrize("change", ["grow", "shrink"])
def test_reference_size_change_during_hashing_is_not_attested(tmp_path, monkeypatch, change):
    path = tmp_path / "artifact"
    path.write_bytes(b"abc")
    original = obs._hash_fd
    def mutate(fd, size):
        path.write_bytes(b"abcdef" if change == "grow" else b"a")
        return original(fd, size)
    monkeypatch.setattr(obs, "_hash_fd", mutate)
    with pytest.raises(obs.ValidateError):
        fd, _, _ = obs.open_under_root(tmp_path, "artifact")
        os.close(fd)


@pytest.mark.parametrize("where", ["input", "reference"])
def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path, where):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    path = fifo
    extra = []
    if where == "reference":
        doc = _with_digest(observation(), "pipe", "0" * 64)
        path = tmp_path / "obs.json"
        path.write_text(json.dumps(doc))
        extra = ["--check-refs"]
    try:
        proc = subprocess.run([sys.executable, obs.__file__, "--file", str(path),
                               "--root", str(tmp_path), *extra], capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail("validator blocked on FIFO before regular-file validation")
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
