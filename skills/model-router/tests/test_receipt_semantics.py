"""Observation claims must match their linked producer receipt."""
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from test_observation import _load, _materialize
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import validate_observation as obs


def receipt(**over):
    return dict(attempt_id="att-1", decision_fingerprint="f" * 64, policy_sha256="a" * 64,
        prompt_sha256=None, seat="worker", runtime="codex", model_id="requested-model",
        effort_native="high", transport_id=None, output_envelope=None,
        result={"state": "SUCCEEDED", "exit_status": 0, "termination_confirmed": True,
                "schema_valid": True, "output_sha256": "e" * 64, "invalid_reasons": None}, **over)


def materialize(tmp_path, rec, mutate=None):
    doc = _load()
    attempt = doc["payload"]["attempts"][0]
    attempt.update(expected_model_id="requested-model", effort_native="high")
    if mutate:
        mutate(attempt)
    raw = json.dumps(rec).encode()
    attempt["evidence_ref"]["sha256"] = hashlib.sha256(raw).hexdigest()
    return _materialize(tmp_path, doc, {"receipts/att-1.json": raw})


def check(path, root):
    return obs.validate_path(path, root, check_refs=True, check_receipts=root / "receipts")


@pytest.mark.parametrize("field,value", [("state", "failed"), ("expected_model_id", "other-model"),
    ("effort_native", "low"), ("runtime", "grok"), ("transport_id", "other.transport"), ("seat", "judge")])
def test_false_attempt_claim_is_rejected(tmp_path, field, value):
    path, root = materialize(tmp_path, receipt(), lambda a: a.update({field: value}))
    with pytest.raises(obs.ValidateError):
        check(path, root)


@pytest.mark.parametrize("state,normalized", [("FAILED", "failed"), ("TIMED_OUT", "timed_out"),
    ("CANCELLED", "cancelled"), ("START_FAILED", "failed"), ("INVALID_OUTPUT", "failed"),
    ("TERMINATION_UNCONFIRMED", "blocked"), ("STARTING", "in_progress"), ("RUNNING", "in_progress")])
def test_native_states_have_a_defined_normalization(tmp_path, state, normalized):
    rec = receipt()
    rec["result"]["state"] = state
    rec["result"]["termination_confirmed"] = (None if state in ("STARTING", "RUNNING", "START_FAILED")
                                              else state != "TERMINATION_UNCONFIRMED")
    rec["result"]["exit_status"] = 3 if state == "FAILED" else (0 if state == "INVALID_OUTPUT" else None)
    if state == "INVALID_OUTPUT":
        rec["result"]["schema_valid"] = False
    path, root = materialize(tmp_path, rec, lambda a: a.update(state=normalized))
    check(path, root)


@pytest.mark.parametrize("field,value", [("exit_status", 3), ("exit_status", False),
    ("termination_confirmed", False), ("schema_valid", None), ("output_sha256", None),
    ("invalid_reasons", ["schema_invalid"])])
def test_malformed_success_is_not_attested(tmp_path, field, value):
    rec = receipt();rec["result"][field] = value
    path, root = materialize(tmp_path, rec)
    with pytest.raises(obs.ValidateError):
        check(path, root)


def test_unpublished_success_is_not_attested(tmp_path):
    path, root = materialize(tmp_path, receipt())
    (root / "receipts/att-1.claim").touch()
    with pytest.raises(obs.ValidateError):
        check(path, root)


def test_nullable_unknown_claims_remain_unknown(tmp_path):
    path, root = materialize(tmp_path, receipt(), lambda a: a.update(expected_model_id=None, effort_native=None, runtime=None))
    check(path, root)


def observed(attempt):
    attempt.update(observed_model_id="served-model", observed_model_source="dispatch_envelope")


def served_receipt(models=None):
    rec = receipt()
    rec["output_envelope"] = "claude-print-json-v1"
    rec["result"]["envelope"] = dict(parse_ok=True, stop_reason="end_turn",
        served_models=models if models is not None else ["served-model"], error_type="success")
    return rec


def test_single_served_model_can_be_verified_without_equating_it_to_requested(tmp_path):
    path, root = materialize(tmp_path, served_receipt(), observed)
    result = check(path, root)
    assert result["payload"]["attempts"][0]["observed_model_id"] == "served-model"


def test_served_model_claim_requires_receipt_checks(tmp_path):
    path, root = materialize(tmp_path, served_receipt(), observed)
    with pytest.raises(obs.ValidateError):
        obs.validate_path(path, root, check_refs=True)
    with pytest.raises(obs.ValidateError):
        obs.validate(json.loads(path.read_text()))


@pytest.mark.parametrize("models", [[], ["different"], ["served-model", "auxiliary"]])
def test_absent_mismatching_or_ambiguous_served_identity_is_rejected(tmp_path, models):
    path, root = materialize(tmp_path, served_receipt(models), observed)
    with pytest.raises(obs.ValidateError):
        check(path, root)


@pytest.mark.parametrize("field,value", [("parse_ok", False), ("stop_reason", "cancelled"), ("error_type", "error")])
def test_success_cannot_contradict_its_envelope(tmp_path, field, value):
    rec = served_receipt()
    rec["result"]["envelope"][field] = value
    path, root = materialize(tmp_path, rec)
    with pytest.raises(obs.ValidateError):
        check(path, root)


def test_reviewer_seat_number_normalizes_to_reviewer(tmp_path):
    rec = receipt();rec["seat"] = "reviewer-2"
    path, root = materialize(tmp_path, rec, lambda a: a.update(seat="reviewer"))
    check(path, root)


@pytest.mark.parametrize("seat", ["reviewer-not-a-number", "reviewer-", "reviewer-0", "reviewer-２", " "])
def test_invalid_reviewer_seat_does_not_attest_reviewer(tmp_path, seat):
    rec = receipt(); rec["seat"] = seat
    path, root = materialize(tmp_path, rec, lambda a: a.update(seat="reviewer"))
    with pytest.raises(obs.ValidateError):
        check(path, root)


@pytest.mark.parametrize("error_type", [False, "unexpected-native-type", "success"])
def test_grok_success_requires_native_null_error_type(tmp_path, error_type):
    rec = served_receipt()
    rec["output_envelope"] = "grok-headless-json-v1"
    rec["result"]["envelope"]["error_type"] = error_type
    path, root = materialize(tmp_path, rec, observed)
    with pytest.raises(obs.ValidateError):
        check(path, root)


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED", "START_FAILED", "TERMINATION_UNCONFIRMED"])
def test_unpublished_terminal_receipt_is_not_observation_evidence(tmp_path, state):
    rec = receipt()
    rec["result"]["state"] = state
    path, root = materialize(tmp_path, rec, lambda a: a.update(state=obs.DISPATCH_STATE_MAP[state]))
    (root / "receipts/att-1.claim").touch()
    with pytest.raises(obs.ValidateError, match="published"):
        check(path, root)
