#!/usr/bin/env python3
"""Validate a RouteObservationV1 JSON record.

CLI: --file and --root are required. Invariant failures exit 1. Usage
errors (missing --root, --check-receipts without --check-refs) exit 2.
The record is never modified and raw producer files are never copied in.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from strict_json import loads as strict_json_loads, ensure_json_value

HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
ATTEMPT_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")  # dispatch_receipt
PRODUCER_ATTEMPT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
PRODUCER_RE = re.compile(r"\A[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")
GIT_HEAD_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")
RFC3339_RE = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
SEMVER_RE = re.compile(r"\A\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")
DIFF_HUNK_RE = re.compile(r"(?m)^@@ -[0-9]+")
DIFF_GIT_RE = re.compile(r"(?m)^diff --git ")

MAX_FILE_BYTES = 32 * 1024
MAX_REF_BYTES = 8 * 1024 * 1024
FORBIDDEN_KEYS = frozenset({
    "git_diff", "stdout", "stderr", "report_body", "prompt",
    "task_description", "changes", "diff", "body", "output_text",
    "canonical_report_text", "argv", "red_verification_output", "goal",
})
GRAIN_PRODUCERS = frozenset({"deep-loop", "deep-work", "deep-review"})
SENTINELS = frozenset({"run", "session"})
TASK_CLASSES = frozenset({
    "MECHANICAL", "DOCUMENTATION", "TESTING", "IMPLEMENTATION",
    "REFACTORING", "DEBUGGING", "INVESTIGATION", "MIGRATION",
    "ARCHITECTURE", "REVIEW", "OPERATIONS",
})
RISK_BANDS = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
SEATS = frozenset({"worker", "reviewer", "judge", "other"})
STATES = frozenset({
    "succeeded", "failed", "timed_out", "cancelled",
    "in_progress", "blocked", "unknown",
})
RUNTIMES = frozenset({"claude_code", "codex", "grok"})
EVIDENCE_KINDS = frozenset({"dispatch_receipt", "producer_record", "none"})
LINKAGES = frozenset({"full", "identity_only", "none"})
DECIDED_BY = frozenset({
    "deep-loop-kernel", "deep-work-finish", "deep-review-readiness", "human",
})
SIGNAL_KINDS = frozenset({
    "proof-verdict", "test-marker", "report-parse", "user-choice",
})
ACCEPTED_TABLE = {
    "deep-loop-kernel": (frozenset({"proof-verdict"}), frozenset({True, False})),
    "deep-work-finish": (frozenset({"user-choice", "test-marker"}), frozenset({True, False, None})),
    "deep-review-readiness": (frozenset({"report-parse"}), frozenset({True, False})),
    "human": (frozenset({"user-choice"}), frozenset({True, False, None})),
}

ROOT_KEYS = frozenset({"$schema", "schema_version", "envelope", "payload"})
ENVELOPE_KEYS = frozenset({
    "producer", "producer_version", "artifact_kind", "run_id",
    "session_id", "parent_run_id", "generated_at", "schema", "git",
    "provenance",
})
GIT_KEYS = frozenset({"head", "branch", "dirty", "worktree"})
PROVENANCE_KEYS = frozenset({"source_artifacts", "tool_versions"})
SOURCE_ARTIFACT_KEYS = frozenset({"path", "run_id"})
PAYLOAD_KEYS = frozenset({
    "contract", "artifact_digests", "decision", "subject", "task",
    "attempts", "objective_results", "review_results", "final",
})
CONTRACT_KEYS = frozenset({"plugin", "observation_schema_version"})
DECISION_KEYS = frozenset({
    "route_schema_version", "router_plugin_version", "policy_sha256",
    "request_sha256", "decision_fingerprint", "linkage_quality",
})
SUBJECT_KEYS = frozenset({
    "producer", "run_id", "artifact_id", "subject_sha256", "base_commit",
})
TASK_KEYS = frozenset({"class", "risk_band", "producer_phase"})
ATTEMPT_KEYS = frozenset({
    "attempt_id", "evidence_kind", "evidence_ref", "prompt_sha256",
    "seat", "seat_source", "expected_model_id", "observed_model_id",
    "observed_model_source", "observed_model_source_native", "runtime",
    "transport_id", "effort_native", "state", "state_source", "timing",
    "usage",
})
ATTEMPT_REQUIRED = frozenset({
    "attempt_id", "evidence_kind", "evidence_ref", "prompt_sha256",
    "seat", "seat_source", "expected_model_id", "observed_model_id",
    "observed_model_source", "runtime", "transport_id", "effort_native",
    "state", "state_source",
})
USAGE_KEYS = frozenset({"turns", "tokens", "input_tokens", "output_tokens", "source"})
TIMING_KEYS = frozenset({"started_at", "finished_at", "duration"})
OBJECTIVE_KEYS = frozenset({"tests_passed", "gates", "evidence_completeness"})
COMPLETENESS_KEYS = frozenset({
    "required_gate_ids", "satisfied_gate_ids", "missing_gate_ids", "complete",
})
REVIEW_KEYS = frozenset({"verdicts", "severity_counts", "rounds"})
FINAL_KEYS = frozenset({"accepted", "implementation_attempts", "human"})
ACCEPTED_KEYS = frozenset({"decided_by", "verdict", "signals"})
IMPL_ATTEMPT_KEYS = frozenset({
    "slices_total", "rework_total", "test_retries", "episodes_done",
})
HUMAN_KEYS = frozenset({
    "overrides", "tdd_overrides", "comprehension_ack", "corrections",
})


class ValidateError(Exception):
    """Invariant or usage failure inside a loaded record."""


def _nonneg_int(v, label):
    if type(v) is not int:  # bool is not allowed
        raise ValidateError(f"I-JSON: {label} is not a JSON integer")
    if v < 0:
        raise ValidateError(f"I-JSON: {label} < 0")
    return v


def _is_x_key(key):
    return isinstance(key, str) and key.startswith("x-")


def _bytes_len(value):
    if not isinstance(value, str):
        raise ValidateError("I-STRING: expected a string")
    return len(value.encode("utf-8"))


def _kebab(value, label, *, max_bytes=64):
    if not isinstance(value, str) or not PRODUCER_RE.match(value):
        raise ValidateError(f"I-STRING: {label} is not kebab-case")
    if _bytes_len(value) > max_bytes:
        raise ValidateError(f"I-STRING: {label} exceeds {max_bytes} bytes")
    if value.startswith("/"):
        raise ValidateError(f"I-STRING: {label} starts with /")
    return value


def _identity(value, label, *, max_bytes=128, allow_null=False):
    if value is None:
        if allow_null:
            return None
        raise ValidateError(f"I-STRING: {label} is null")
    if not isinstance(value, str) or value == "":
        raise ValidateError(f"I-STRING: {label} is empty")
    if value.startswith("/"):
        raise ValidateError(f"I-STRING: {label} starts with /")
    if _bytes_len(value) > max_bytes:
        raise ValidateError(f"I-STRING: {label} exceeds {max_bytes} bytes")
    return value


def _hex64(value, label, *, allow_null=False):
    if value is None:
        if allow_null:
            return None
        raise ValidateError(f"I-DIGEST: {label} is null")
    if not isinstance(value, str) or not HEX64_RE.match(value):
        raise ValidateError(f"I-DIGEST: {label} is not lowercase hex64")
    return value


def _rfc3339(value, label, *, allow_null=False):
    if value is None:
        if allow_null:
            return None
        raise ValidateError(f"I-STRING: {label} is null")
    if not isinstance(value, str) or not RFC3339_RE.match(value):
        raise ValidateError(f"I-STRING: {label} is not RFC3339")
    # Newer Python versions normalize 24:00:00, and offsets such as +00:60.
    # This observation profile uses ordinary clock seconds, not leap seconds.
    if int(value[11:13]) > 23 or int(value[14:16]) > 59 or int(value[17:19]) > 59:
        raise ValidateError(f"I-STRING: {label} has an invalid clock time")
    if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValidateError(f"I-STRING: {label} has an invalid timezone offset")
    try:
        # Check only calendar components here. Fraction parsing differs by
        # Python version; the validated fractional spelling stays unchanged.
        datetime(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise ValidateError(f"I-STRING: {label} is not a supported calendar timestamp") from None
    return value


def _semver(value, label, *, allow_null=False):
    if value is None:
        if allow_null:
            return None
        if not isinstance(value, str) or not value:
            raise ValidateError(f"I-STRING: {label} is empty")
        if not SEMVER_RE.match(value):
            raise ValidateError(f"I-STRING: {label} is not semver")
        return value
    if not isinstance(value, str) or not SEMVER_RE.match(value):
        raise ValidateError(f"I-STRING: {label} is not semver")
    return value


def _expect_object(value, label):
    if not isinstance(value, dict):
        raise ValidateError(f"I-STRUCT: {label} is not an object")
    return value


def _expect_list(value, label):
    if not isinstance(value, list):
        raise ValidateError(f"I-STRUCT: {label} is not an array")
    return value


def _reject_unknown(obj, allowed, label, *, x_ok=False):
    for key in obj:
        if key in allowed:
            continue
        if x_ok and _is_x_key(key):
            continue
        raise ValidateError(f"I-STRUCT: unknown key {key!r} under {label}")


def _all_null(obj):
    return obj is not None and isinstance(obj, dict) and obj and all(v is None for v in obj.values())


def subject_sha256(producer, run_id, artifact_id):
    blob = json.dumps(
        {"artifact_id": artifact_id, "producer": producer, "run_id": run_id},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _walk_forbidden(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                raise ValidateError(f"I-NO-RAW-KEYS: {key} at {path}")
            _walk_forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _walk_forbidden(child, f"{path}[{i}]")


def _walk_diff(value):
    if isinstance(value, str):
        if DIFF_GIT_RE.search(value) or DIFF_HUNK_RE.search(value):
            raise ValidateError("I-NO-DIFF: diff marker in string")
    elif isinstance(value, dict):
        for child in value.values():
            _walk_diff(child)
    elif isinstance(value, list):
        for child in value:
            _walk_diff(child)


def _source_pair(obj, label):
    obj = _expect_object(obj, label)
    _reject_unknown(obj, {"producer", "value"}, label)
    if "producer" not in obj or "value" not in obj:
        raise ValidateError(f"I-STRUCT: {label} missing producer/value")
    _kebab(obj["producer"], f"{label}.producer")
    value = obj["value"]
    if not isinstance(value, str) or _bytes_len(value) > 128:
        raise ValidateError(f"I-STRING: {label}.value")
    return obj


def _load(path: Path) -> dict:
    # Input may be outside --root and may be an alias to a regular file.
    # Inspect the opened fd, so a FIFO (including through an alias) cannot
    # block before file-type validation. References have stricter no-follow rules.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValidateError("I-JSON: input is not a regular file")
        if st.st_size > MAX_FILE_BYTES:
            raise ValidateError("I-JSON: file exceeds 32KiB")
        raw = fh.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValidateError("I-JSON: file exceeds 32KiB")

    try:
        data = strict_json_loads(raw)
    except ValueError as exc:
        raise ValidateError(f"I-JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidateError("I-JSON: root is not an object")
    return data


def _canonical_size(document):
    try:
        ensure_json_value(document)
        blob = json.dumps(document, ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidateError(f"I-JSON: {exc}") from exc
    if len(blob.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValidateError("I-SIZE: canonical JSON exceeds 32KiB")


def _git(obj):
    obj = _expect_object(obj, "envelope.git")
    _reject_unknown(obj, GIT_KEYS, "envelope.git")
    for key in ("head", "branch", "dirty"):
        if key not in obj:
            raise ValidateError(f"I-CONTRACT: envelope.git.{key} missing")
    if not isinstance(obj["head"], str) or not GIT_HEAD_RE.match(obj["head"]):
        raise ValidateError("I-CONTRACT: envelope.git.head")
    _identity(obj["branch"], "envelope.git.branch")
    dirty = obj["dirty"]
    if type(dirty) is not bool and dirty != "unknown":
        raise ValidateError("I-CONTRACT: envelope.git.dirty")
    if "worktree" in obj and obj["worktree"] is not None:
        _identity(obj["worktree"], "envelope.git.worktree")


def _provenance(obj):
    obj = _expect_object(obj, "envelope.provenance")
    _reject_unknown(obj, PROVENANCE_KEYS, "envelope.provenance")
    if "source_artifacts" not in obj or "tool_versions" not in obj:
        raise ValidateError("I-CONTRACT: provenance requires source_artifacts and tool_versions")
    artifacts = _expect_list(obj["source_artifacts"], "source_artifacts")
    seen = set()
    for i, item in enumerate(artifacts):
        item = _expect_object(item, f"source_artifacts[{i}]")
        if "sha256" in item:
            raise ValidateError("I-CONTRACT: source_artifacts sha256 is forbidden")
        _reject_unknown(item, SOURCE_ARTIFACT_KEYS, f"source_artifacts[{i}]")
        if "path" not in item:
            raise ValidateError("I-CONTRACT: source_artifacts path missing")
        path = _identity(item["path"], f"source_artifacts[{i}].path", max_bytes=512)
        if path in seen:
            raise ValidateError("I-STRUCT: duplicate source_artifacts path")
        seen.add(path)
        if "run_id" in item and item["run_id"] is not None:
            _identity(item["run_id"], f"source_artifacts[{i}].run_id")
    tools = _expect_object(obj["tool_versions"], "tool_versions")
    for key, val in tools.items():
        if not isinstance(val, (str, dict)):
            raise ValidateError("I-CONTRACT: tool_versions values must be string or object")
    return seen


def _contract(obj):
    obj = _expect_object(obj, "payload.contract")
    _reject_unknown(obj, CONTRACT_KEYS, "payload.contract")
    if obj.get("plugin") != "deep-model-router":
        raise ValidateError("I-CONTRACT: plugin is not deep-model-router")
    version = obj.get("observation_schema_version")
    if type(version) is not int or version != 1:
        raise ValidateError("I-CONTRACT: observation_schema_version must be 1")


def _decision(obj):
    obj = _expect_object(obj, "payload.decision")
    _reject_unknown(obj, DECISION_KEYS, "payload.decision")
    for key in DECISION_KEYS:
        if key not in obj:
            raise ValidateError(f"I-STRUCT: decision.{key} missing")
    quality = obj["linkage_quality"]
    if not isinstance(quality, str) or quality not in LINKAGES:
        raise ValidateError("I-LINK: linkage_quality")
    fingerprint = obj["decision_fingerprint"]
    policy = obj["policy_sha256"]
    route_schema = obj["route_schema_version"]
    plugin_version = obj["router_plugin_version"]
    request = obj["request_sha256"]
    if quality == "full":
        _hex64(fingerprint, "decision_fingerprint")
        _hex64(policy, "policy_sha256")
        if type(route_schema) is not int or route_schema != 1:
            raise ValidateError("I-LINK: full requires route_schema_version 1")
        _semver(plugin_version, "router_plugin_version")
        _hex64(request, "request_sha256", allow_null=True)
    elif quality == "identity_only":
        if fingerprint is not None:
            raise ValidateError("I-LINK: identity_only fingerprint must be null")
        _hex64(policy, "policy_sha256")
        if type(route_schema) is not int or route_schema != 1:
            raise ValidateError("I-LINK: identity_only requires route_schema_version 1")
        _semver(plugin_version, "router_plugin_version")
        _hex64(request, "request_sha256", allow_null=True)
    else:
        if fingerprint is not None or policy is not None or route_schema is not None or plugin_version is not None:
            raise ValidateError("I-LINK: none requires identity fields null")
        if request is not None:
            raise ValidateError("I-LINK: none request_sha256 must be null")


def _round_ok(artifact_id):
    if not isinstance(artifact_id, str) or not artifact_id.startswith("round-"):
        return False
    rest = artifact_id[6:]
    if not rest.isdigit():
        return False
    return rest == str(int(rest))


def _subject(obj):
    obj = _expect_object(obj, "payload.subject")
    _reject_unknown(obj, SUBJECT_KEYS, "payload.subject")
    for key in ("producer", "run_id", "artifact_id", "subject_sha256"):
        if key not in obj:
            raise ValidateError(f"I-STRUCT: subject.{key} missing")
    producer = _kebab(obj["producer"], "subject.producer")
    run_id = _identity(obj["run_id"], "subject.run_id")
    artifact_id = obj["artifact_id"]
    if artifact_id is not None:
        artifact_id = _identity(artifact_id, "subject.artifact_id")
    _hex64(obj["subject_sha256"], "subject_sha256")
    if "base_commit" in obj and obj["base_commit"] is not None:
        if not isinstance(obj["base_commit"], str) or not GIT_HEAD_RE.match(obj["base_commit"]):
            raise ValidateError("I-DIGEST: subject.base_commit")
    expected = subject_sha256(producer, run_id, artifact_id)
    if obj["subject_sha256"] != expected:
        raise ValidateError("I-SUBJECT: subject_sha256 mismatch")
    if producer == "deep-loop":
        if artifact_id is None:
            raise ValidateError("I-SUBJECT: deep-loop artifact_id cannot be null")
    elif producer == "deep-work":
        if artifact_id is None:
            raise ValidateError("I-SUBJECT: deep-work artifact_id cannot be null")
    elif producer == "deep-review":
        if not _round_ok(artifact_id):
            raise ValidateError("I-SUBJECT: deep-review artifact_id must be unpadded round-N")
    return producer, artifact_id


def _task(obj):
    obj = _expect_object(obj, "payload.task")
    _reject_unknown(obj, TASK_KEYS, "payload.task")
    if ("class" in obj and obj["class"] is not None
            and (not isinstance(obj["class"], str) or obj["class"] not in TASK_CLASSES)):
        raise ValidateError("I-STRUCT: task.class")
    if "risk_band" in obj and obj["risk_band"] is not None:
        band = obj["risk_band"]
        if not isinstance(band, str) or band.upper() not in RISK_BANDS:
            raise ValidateError("I-STRUCT: task.risk_band")
        if band != band.upper():
            raise ValidateError("I-STRUCT: task.risk_band must be uppercase")
    if "producer_phase" in obj and obj["producer_phase"] is not None:
        _source_pair(obj["producer_phase"], "task.producer_phase")


def _evidence_ref(obj, label):
    obj = _expect_object(obj, label)
    _reject_unknown(obj, {"path", "sha256"}, label)
    if "path" not in obj or "sha256" not in obj:
        raise ValidateError(f"I-ATTEMPT: {label} requires path and sha256")
    _identity(obj["path"], f"{label}.path", max_bytes=512)
    _hex64(obj["sha256"], f"{label}.sha256")


def _usage(obj):
    obj = _expect_object(obj, "usage")
    _reject_unknown(obj, USAGE_KEYS, "usage")
    if not obj or _all_null(obj):
        raise ValidateError("I-STRUCT: usage is all-null")
    present = 0
    for key, val in obj.items():
        if val is None:
            continue
        present += 1
        if key == "source":
            _identity(val, "usage.source")
        else:
            _nonneg_int(val, f"usage.{key}")
    if present < 1:
        raise ValidateError("I-STRUCT: usage has no values")


def _timing(obj):
    obj = _expect_object(obj, "timing")
    _reject_unknown(obj, TIMING_KEYS, "timing")
    if "started_at" in obj:
        _rfc3339(obj["started_at"], "timing.started_at", allow_null=True)
    if "finished_at" in obj:
        _rfc3339(obj["finished_at"], "timing.finished_at", allow_null=True)
    if "duration" in obj and obj["duration"] is not None:
        dur = _expect_object(obj["duration"], "timing.duration")
        _reject_unknown(dur, {"value", "unit"}, "timing.duration")
        _nonneg_int(dur.get("value"), "timing.duration.value")
        if dur.get("unit") not in ("second", "minute"):
            raise ValidateError("I-STRUCT: timing.duration.unit")


def _attempt(obj, seen_dispatch):
    obj = _expect_object(obj, "attempts[]")
    _reject_unknown(obj, ATTEMPT_KEYS, "attempts[]")
    missing = ATTEMPT_REQUIRED - set(obj)
    if missing:
        raise ValidateError(f"I-STRUCT: attempts[] missing {sorted(missing)}")
    kind = obj["evidence_kind"]
    if not isinstance(kind, str) or kind not in EVIDENCE_KINDS:
        raise ValidateError("I-ATTEMPT: evidence_kind")
    attempt_id = obj["attempt_id"]
    ref = obj["evidence_ref"]
    if kind == "none":
        if attempt_id is not None or ref is not None:
            raise ValidateError("I-ATTEMPT: none requires null attempt_id and evidence_ref")
    elif kind == "dispatch_receipt":
        if not isinstance(attempt_id, str) or not ATTEMPT_ID_RE.match(attempt_id):
            raise ValidateError("I-ATTEMPT: dispatch_receipt attempt_id")
        if attempt_id in seen_dispatch:
            raise ValidateError("I-ATTEMPT: duplicate dispatch_receipt attempt_id")
        seen_dispatch.add(attempt_id)
        _evidence_ref(ref, "evidence_ref")
    else:
        if attempt_id is not None:
            if not isinstance(attempt_id, str) or not PRODUCER_ATTEMPT_RE.match(attempt_id):
                raise ValidateError("I-ATTEMPT: producer_record attempt_id")
        if ref is None:
            raise ValidateError("I-ATTEMPT: producer_record requires evidence_ref")
        _evidence_ref(ref, "evidence_ref")
    _hex64(obj["prompt_sha256"], "prompt_sha256", allow_null=True)
    if not isinstance(obj["seat"], str) or obj["seat"] not in SEATS:
        raise ValidateError("I-STRUCT: seat")
    _source_pair(obj["seat_source"], "seat_source")
    if obj["expected_model_id"] is not None:
        _identity(obj["expected_model_id"], "expected_model_id")
    if obj["observed_model_id"] is not None:
        raise ValidateError("I-OBS-MODEL: observed_model_id must be null")
    if obj["observed_model_source"] != "unavailable":
        raise ValidateError("I-OBS-MODEL: observed_model_source must be unavailable")
    if "observed_model_source_native" in obj and obj["observed_model_source_native"] is not None:
        _source_pair(obj["observed_model_source_native"], "observed_model_source_native")
    runtime = obj["runtime"]
    if runtime is not None and (not isinstance(runtime, str) or runtime not in RUNTIMES):
        raise ValidateError("I-STRUCT: runtime")
    if obj["transport_id"] is not None:
        _identity(obj["transport_id"], "transport_id")
    if obj["effort_native"] is not None:
        _identity(obj["effort_native"], "effort_native")
    if not isinstance(obj["state"], str) or obj["state"] not in STATES:
        raise ValidateError("I-STRUCT: state")
    _source_pair(obj["state_source"], "state_source")
    if "timing" in obj and obj["timing"] is not None:
        _timing(obj["timing"])
    if "usage" in obj and obj["usage"] is not None:
        _usage(obj["usage"])


def _gates(obj):
    obj = _expect_object(obj, "evidence_completeness")
    _reject_unknown(obj, COMPLETENESS_KEYS, "evidence_completeness")
    for key in COMPLETENESS_KEYS:
        if key not in obj:
            raise ValidateError(f"I-GATES: {key} missing")
    required = _expect_list(obj["required_gate_ids"], "required_gate_ids")
    satisfied = _expect_list(obj["satisfied_gate_ids"], "satisfied_gate_ids")
    missing = _expect_list(obj["missing_gate_ids"], "missing_gate_ids")
    if type(obj["complete"]) is not bool:
        raise ValidateError("I-GATES: complete is not bool")
    for label, values in (("required_gate_ids", required), ("satisfied_gate_ids", satisfied),
                          ("missing_gate_ids", missing)):
        for value in values:
            if not isinstance(value, str) or not value:
                raise ValidateError(f"I-GATES: {label} must contain non-empty strings")
    req = set(required)
    sat = set(satisfied)
    miss = set(missing)
    if sat & miss:
        raise ValidateError("I-GATES: satisfied ∩ missing is not empty")
    if sat | miss != req:
        raise ValidateError("I-GATES: satisfied ∪ missing != required")
    if obj["complete"] is not (len(miss) == 0):
        raise ValidateError("I-GATES: complete iff missing is empty")


def _objective(obj):
    obj = _expect_object(obj, "objective_results")
    _reject_unknown(obj, OBJECTIVE_KEYS, "objective_results")
    if obj.get("tests_passed") is not None and type(obj["tests_passed"]) is not bool:
        raise ValidateError("I-STRUCT: tests_passed")
    if "gates" in obj and obj["gates"] is not None:
        for i, item in enumerate(_expect_list(obj["gates"], "gates")):
            item = _expect_object(item, f"gates[{i}]")
            _reject_unknown(item, {"id", "tier", "status"}, f"gates[{i}]")
            _identity(item.get("id"), f"gates[{i}].id")
            if item.get("tier") not in ("required", "advisory", "insight"):
                raise ValidateError("I-STRUCT: gate.tier")
            if item.get("status") not in ("PASS", "FAIL"):
                raise ValidateError("I-STRUCT: gate.status")
    if "evidence_completeness" in obj and obj["evidence_completeness"] is not None:
        _gates(obj["evidence_completeness"])


def _review(obj):
    obj = _expect_object(obj, "review_results")
    _reject_unknown(obj, REVIEW_KEYS, "review_results")
    if not obj:
        raise ValidateError("I-STRUCT: review_results empty")
    if "verdicts" in obj and obj["verdicts"] is not None:
        for i, item in enumerate(_expect_list(obj["verdicts"], "verdicts")):
            item = _expect_object(item, f"verdicts[{i}]")
            _reject_unknown(item, {"producer", "value"}, f"verdicts[{i}]")
            _kebab(item.get("producer"), f"verdicts[{i}].producer")
            value = item.get("value")
            if not isinstance(value, str) or _bytes_len(value) > 64:
                raise ValidateError("I-STRING: verdicts[].value")
    if "severity_counts" in obj and obj["severity_counts"] is not None:
        sc = _expect_object(obj["severity_counts"], "severity_counts")
        _reject_unknown(sc, {"vocabulary", "counts"}, "severity_counts")
        vocab = sc.get("vocabulary")
        if not isinstance(vocab, str) or _bytes_len(vocab) > 128:
            raise ValidateError("I-STRING: severity_counts.vocabulary")
        counts = _expect_object(sc.get("counts"), "severity_counts.counts")
        for key, val in counts.items():
            if not isinstance(key, str) or _bytes_len(key) > 64:
                raise ValidateError("I-STRING: severity_counts.counts key")
            _nonneg_int(val, f"severity_counts.counts.{key}")
    if "rounds" in obj and obj["rounds"] is not None:
        _nonneg_int(obj["rounds"], "review_results.rounds")


def _accepted(obj):
    obj = _expect_object(obj, "final.accepted")
    _reject_unknown(obj, ACCEPTED_KEYS, "final.accepted")
    decided = obj.get("decided_by")
    if not isinstance(decided, str) or decided not in DECIDED_BY:
        raise ValidateError("I-ACCEPTED: decided_by")
    signals = _expect_list(obj.get("signals"), "final.accepted.signals")
    if not signals:
        raise ValidateError("I-ACCEPTED: signals empty")
    kinds = []
    for i, item in enumerate(signals):
        item = _expect_object(item, f"signals[{i}]")
        _reject_unknown(item, {"kind"}, f"signals[{i}]")
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in SIGNAL_KINDS:
            raise ValidateError("I-ACCEPTED: signal kind")
        kinds.append(kind)
    expected_kinds, allowed_verdict = ACCEPTED_TABLE[decided]
    if frozenset(kinds) != expected_kinds:
        raise ValidateError("I-ACCEPTED: signal kinds")
    verdict = obj.get("verdict")
    if (verdict is not None and type(verdict) is not bool) or verdict not in allowed_verdict:
        raise ValidateError("I-ACCEPTED: verdict")


def _final(obj, artifact_id):
    obj = _expect_object(obj, "payload.final")
    _reject_unknown(obj, FINAL_KEYS, "payload.final")
    if "accepted" in obj and obj["accepted"] is not None:
        _accepted(obj["accepted"])
    if "implementation_attempts" in obj and obj["implementation_attempts"] is not None:
        impl = _expect_object(obj["implementation_attempts"], "implementation_attempts")
        _reject_unknown(impl, IMPL_ATTEMPT_KEYS, "implementation_attempts")
        for key, val in impl.items():
            _nonneg_int(val, f"implementation_attempts.{key}")
        if "episodes_done" in impl and artifact_id not in SENTINELS:
            raise ValidateError("I-GRAIN: episodes_done only on run/session")
    if "human" in obj and obj["human"] is not None:
        human = _expect_object(obj["human"], "final.human")
        _reject_unknown(human, HUMAN_KEYS, "final.human")
        if "overrides" in human and human["overrides"] is not None:
            for i, item in enumerate(_expect_list(human["overrides"], "overrides")):
                item = _expect_object(item, f"overrides[{i}]")
                _reject_unknown(item, {"from", "to", "reason", "at", "scope"}, f"overrides[{i}]")
                reason = item.get("reason")
                if not isinstance(reason, str) or _bytes_len(reason) > 256:
                    raise ValidateError("I-STRING: human.overrides[].reason")
                if "at" in item and item["at"] is not None:
                    _rfc3339(item["at"], f"overrides[{i}].at")
        if "tdd_overrides" in human and human["tdd_overrides"] is not None:
            _nonneg_int(human["tdd_overrides"], "tdd_overrides")
        if "comprehension_ack" in human and human["comprehension_ack"] is not None:
            if type(human["comprehension_ack"]) is not bool:
                raise ValidateError("I-STRUCT: comprehension_ack")
            if artifact_id not in SENTINELS:
                raise ValidateError("I-GRAIN: comprehension_ack only on run/session")
        if "corrections" in human and human["corrections"] is not None:
            _nonneg_int(human["corrections"], "corrections")


def _artifact_digests(obj, provenance_paths):
    items = _expect_list(obj, "artifact_digests")
    seen = set()
    for i, item in enumerate(items):
        item = _expect_object(item, f"artifact_digests[{i}]")
        _reject_unknown(item, {"path", "sha256"}, f"artifact_digests[{i}]")
        path = _identity(item.get("path"), f"artifact_digests[{i}].path", max_bytes=512)
        _hex64(item.get("sha256"), f"artifact_digests[{i}].sha256")
        if path in seen:
            raise ValidateError("I-DIGEST: duplicate artifact_digests path")
        seen.add(path)
        if provenance_paths is not None and path not in provenance_paths:
            raise ValidateError("I-DIGEST: artifact_digests path not in provenance")


def validate(document, root=None):
    """Validate an in-memory RouteObservationV1 object."""
    if not isinstance(document, dict):
        raise ValidateError("I-JSON: root is not an object")
    _canonical_size(document)
    _walk_forbidden(document)
    _walk_diff(document)
    _reject_unknown(document, ROOT_KEYS, "root", x_ok=True)
    if document.get("schema_version") != "1.0":
        raise ValidateError("I-CONTRACT: schema_version must be \"1.0\"")
    if "$schema" in document and not isinstance(document["$schema"], str):
        raise ValidateError("I-STRUCT: $schema")
    envelope = _expect_object(document.get("envelope"), "envelope")
    _reject_unknown(envelope, ENVELOPE_KEYS, "envelope", x_ok=True)
    for key in ("producer", "producer_version", "artifact_kind", "run_id",
                "generated_at", "schema", "git", "provenance"):
        if key not in envelope:
            raise ValidateError(f"I-CONTRACT: envelope.{key} missing")
    producer = _kebab(envelope["producer"], "envelope.producer")
    _semver(envelope["producer_version"], "envelope.producer_version")
    _identity(envelope["run_id"], "envelope.run_id")
    _rfc3339(envelope["generated_at"], "envelope.generated_at")
    if "session_id" in envelope:
        _identity(envelope["session_id"], "envelope.session_id")
    if "parent_run_id" in envelope:
        _identity(envelope["parent_run_id"], "envelope.parent_run_id")
    schema = _expect_object(envelope["schema"], "envelope.schema")
    _reject_unknown(schema, {"name", "version"}, "envelope.schema")
    if schema.get("name") != "route-observation" or envelope["artifact_kind"] != "route-observation":
        raise ValidateError("I-CONTRACT: artifact_kind/schema.name")
    if schema.get("version") != "1.0":
        raise ValidateError("I-CONTRACT: envelope.schema.version must be \"1.0\"")
    _git(envelope["git"])
    provenance_paths = _provenance(envelope["provenance"])
    payload = _expect_object(document.get("payload"), "payload")
    _reject_unknown(payload, PAYLOAD_KEYS, "payload")
    if "contract" not in payload or "decision" not in payload or "subject" not in payload or "attempts" not in payload:
        raise ValidateError("I-STRUCT: payload missing required blocks")
    _contract(payload["contract"])
    _decision(payload["decision"])
    subject_producer, artifact_id = _subject(payload["subject"])
    if producer != subject_producer:
        raise ValidateError("I-OWNER: envelope.producer != subject.producer")
    if "task" in payload and payload["task"] is not None:
        _task(payload["task"])
    attempts = _expect_list(payload["attempts"], "attempts")
    seen_dispatch = set()
    for item in attempts:
        _attempt(item, seen_dispatch)
    if "artifact_digests" in payload and payload["artifact_digests"] is not None:
        _artifact_digests(payload["artifact_digests"], provenance_paths)
    if "objective_results" in payload and payload["objective_results"] is not None:
        _objective(payload["objective_results"])
    if "review_results" in payload and payload["review_results"] is not None:
        _review(payload["review_results"])
    if "final" in payload and payload["final"] is not None:
        _final(payload["final"], artifact_id)
    return document


def _split_rel(rel):
    if not isinstance(rel, str) or rel == "" or "\x00" in rel:
        raise ValidateError("I-REFS: empty or NUL path")
    if rel.startswith("/") or rel.startswith("\\"):
        raise ValidateError("I-REFS: absolute path")
    parts = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValidateError("I-REFS: path escapes root")
        parts.append(part)
    if not parts:
        raise ValidateError("I-REFS: empty path")
    return parts


def _hash_fd(fd, size):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            raise ValidateError("I-REFS: file shrank during hashing")
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        raise ValidateError("I-REFS: file grew during hashing")
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def open_under_root(root, rel):
    """Open `rel` under `root` with O_NOFOLLOW. Caller owns the returned fd."""
    parts = _split_rel(rel)
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    root_fd = os.open(os.fspath(root), os.O_RDONLY | os.O_DIRECTORY)
    owned = [root_fd]
    try:
        current = root_fd
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            try:
                nxt = os.open(part, file_flags if last else dir_flags, dir_fd=current)
            except OSError as exc:
                raise ValidateError(f"I-REFS: cannot open {part!r}: {exc.strerror}") from exc
            owned.append(nxt)
            st = os.fstat(nxt)
            if last:
                if not stat.S_ISREG(st.st_mode):
                    raise ValidateError("I-REFS: not a regular file")
                if st.st_size > MAX_REF_BYTES:
                    raise ValidateError("I-REFS: file exceeds 8MiB")
                digest = _hash_fd(nxt, st.st_size)
                owned.remove(nxt)
                return nxt, st, digest
            if not stat.S_ISDIR(st.st_mode):
                raise ValidateError("I-REFS: intermediate not a directory")
            current = nxt
        raise ValidateError("I-REFS: empty path")
    finally:
        for fd in owned:
            os.close(fd)


def _check_refs(document, root):
    digests = (document.get("payload") or {}).get("artifact_digests") or []
    for item in digests:
        fd, _st, digest = open_under_root(root, item["path"])
        try:
            if digest != item["sha256"]:
                raise ValidateError("I-REFS: digest mismatch")
        finally:
            os.close(fd)


def _receipt_field(receipt, key):
    if not isinstance(receipt, dict):
        return None
    if key in receipt:
        return receipt[key]
    result = receipt.get("result")
    if isinstance(result, dict) and key in result:
        return result[key]
    return None


def _check_receipts(document, root, receipts_dir):
    payload = document["payload"]
    decision = payload["decision"]
    quality = decision["linkage_quality"]
    for attempt in payload.get("attempts") or []:
        if attempt.get("evidence_kind") != "dispatch_receipt":
            continue
        ref = attempt["evidence_ref"]
        attempt_id = attempt["attempt_id"]
        rel = ref["path"]
        base = rel.rsplit("/", 1)[-1]
        if base != f"{attempt_id}.json":
            raise ValidateError("I-RECEIPTS: evidence_ref basename must be {attempt_id}.json")
        fd1, st1, digest1 = open_under_root(root, rel)
        try:
            if digest1 != ref["sha256"]:
                raise ValidateError("I-RECEIPTS: evidence_ref.sha256 mismatch")
            fd2, st2, _digest2 = open_under_root(receipts_dir, f"{attempt_id}.json")
            try:
                if (st1.st_dev, st1.st_ino) != (st2.st_dev, st2.st_ino):
                    raise ValidateError("I-RECEIPTS: receipt is not the evidence_ref file")
                raw = os.read(fd2, st2.st_size + 1)
                if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                    raise ValidateError("I-RECEIPTS: receipt changed during read")
                try:
                    receipt = strict_json_loads(raw)
                except ValueError as exc:
                    raise ValidateError(f"I-RECEIPTS: receipt is not JSON: {exc}") from exc
                if not isinstance(receipt, dict):
                    raise ValidateError("I-RECEIPTS: receipt is not an object")
                if _receipt_field(receipt, "attempt_id") != attempt_id:
                    raise ValidateError("I-RECEIPTS: receipt attempt_id mismatch")
                rec_fp = _receipt_field(receipt, "decision_fingerprint")
                rec_policy = _receipt_field(receipt, "policy_sha256")
                rec_prompt = _receipt_field(receipt, "prompt_sha256")
                if quality == "full":
                    if rec_fp is None or rec_fp != decision["decision_fingerprint"]:
                        raise ValidateError("I-RECEIPTS: full fingerprint mismatch")
                elif quality == "identity_only":
                    if rec_policy is not None and rec_policy != decision["policy_sha256"]:
                        raise ValidateError("I-RECEIPTS: identity_only policy mismatch")
                obs_prompt = attempt.get("prompt_sha256")
                if rec_prompt != obs_prompt:
                    raise ValidateError("I-RECEIPTS: prompt_sha256 mismatch")
            finally:
                os.close(fd2)
        finally:
            os.close(fd1)


def validate_path(file_path: Path, root: Path, *, check_refs=False, check_receipts=None):
    document = _load(file_path)
    validate(document, root=root)
    if check_refs:
        _check_refs(document, root)
    if check_receipts is not None:
        _check_receipts(document, root, check_receipts)
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(prog="validate_observation.py")
    parser.add_argument("--file", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--check-refs", action="store_true")
    parser.add_argument("--check-receipts", default=None)
    args = parser.parse_args(argv)
    if args.check_receipts is not None and not args.check_refs:
        print("usage: --check-receipts requires --check-refs", file=sys.stderr)
        return 2
    try:
        validate_path(
            Path(args.file), Path(args.root),
            check_refs=args.check_refs,
            check_receipts=args.check_receipts,
        )
    except ValidateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"I-REFS: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
