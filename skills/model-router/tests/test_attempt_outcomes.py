"""Capability escalation, operational recovery, and total attempts are different axes."""
import copy
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import route_task as rt

CFG = rt.default_config()
FAST = CFG["models"]["openai_worker_fast"]["id"]
BASE = dict(route_schema_version=1, task_class="IMPLEMENTATION", complexity=0,
            uncertainty=0, blast_radius=0, reversibility=0, runtime="codex")


def record(kind, i=1, recovered=False, model=FAST):
    r = dict(attempt_id=f"attempt-{i}", model_id=model, kind=kind, evidence_sha256="a" * 64)
    if recovered:
        r["recovery_sha256"] = "b" * 64
    return r


def task(history=None, **over):
    return rt.task_from_request_v1({**BASE, **over, "attempt_outcomes": history})


def test_capability_failure_alone_excludes_and_escalates():
    out = rt.route(task([record("capability_failure")]), CFG)
    assert out["selected_capability_tier"] > CFG["models"]["openai_worker_fast"]["capability_tier"]
    assert out["excluded_prior_failures"] == [FAST]
    assert out["retry_count"] == out["escalation_count"] == 1


@pytest.mark.parametrize("kind", ["transport_failure", "launch_failure", "resolution_failure",
    "timeout", "max_turns_partial", "no_artifact", "invalid_output", "authentication_failure",
    "quota_exhausted", "cancelled", "unknown"])
def test_operational_failure_requires_recovery_but_never_capability_escalation(kind):
    held = rt.route(task([record(kind)]), CFG)
    assert held["terminal"] == "OPERATIONAL_RECOVERY_REQUIRED"
    assert held["selected_model"] is None
    assert held["excluded_prior_failures"] == []
    resumed = rt.route(task([record(kind, recovered=True)]), CFG)
    assert resumed["terminal"] is None
    assert resumed["selected_model"] == FAST
    assert resumed["excluded_prior_failures"] == []
    assert resumed["retry_count"] == 1 and resumed["escalation_count"] == 0
    assert resumed["routing_confidence"] == .95


def test_operational_attempts_cannot_bypass_total_retry_budget():
    cap = CFG["retry"]["max_total_implementation_attempts"]
    out = rt.route(task([record("transport_failure", i, True) for i in range(cap)]), CFG)
    assert out["terminal"] == "HUMAN_REQUIRED"
    assert out["retry_count"] == cap
    assert out["selected_model"] is None


def test_unconfirmed_termination_is_not_a_retry_even_under_notify_policy():
    cfg = copy.deepcopy(CFG)
    cfg["human_in_the_loop"]["on_termination_unconfirmed"] = "notify_human"
    out = rt.route(task([record("termination_unconfirmed")]), cfg)
    assert out["terminal"] == "TERMINATION_UNCONFIRMED"
    assert out["requires_human_confirmation"] and out["selected_model"] is None


def test_mixed_history_counts_all_attempts_but_only_excludes_capability_failures():
    other = CFG["models"]["claude_senior"]["id"]
    history = [record("capability_failure"), record("max_turns_partial", 2, True, other)]
    out = rt.route(task(history), CFG)
    assert out["retry_count"] == 2 and out["escalation_count"] == 1
    assert out["excluded_prior_failures"] == [FAST]


def test_normalization_is_repeatable_and_input_is_not_mutated():
    history = [record("capability_failure")]
    before = copy.deepcopy(history)
    t = task(history)
    a = rt.route(t, CFG)
    b = rt.route(t, CFG)
    assert a == b and history == before and t.prior_failures == 0
    assert a["attempt_outcomes"][0]["model_id"] == FAST


def test_request_hash_describes_original_validated_request_before_projection():
    t = task([record("capability_failure")])
    t.validate(rt.Policy.of(CFG))
    expected = rt.request_sha256_of(t)
    assert rt.route(t, CFG)["request_sha256"] == expected


def test_recovery_of_later_attempt_does_not_clear_an_earlier_hold():
    out = rt.route(task([record("authentication_failure"), record("transport_failure", 2, True)]), CFG)
    assert out["terminal"] == "OPERATIONAL_RECOVERY_REQUIRED"


def test_hotfix_cannot_defer_typed_unconfirmed_termination():
    out = rt.route(task([record("termination_unconfirmed")], flags=["production_hotfix"]), CFG)
    assert out["terminal"] == "TERMINATION_UNCONFIRMED"
    assert not out["human_confirmation_deferred"]


def test_absent_or_null_history_preserves_legacy_request_and_route():
    assert rt.route(task(), CFG) == rt.route(rt.task_from_request_v1(BASE), CFG)


def test_kind_and_recovery_are_bound_to_request_identity():
    a = rt.route(task([record("invalid_output")]), CFG)
    b = rt.route(task([record("invalid_output", recovered=True)]), CFG)
    c = rt.route(task([record("transport_failure", recovered=True)]), CFG)
    assert len({r["request_sha256"] for r in [a, b, c]}) == 3


@pytest.mark.parametrize("bad", [False, {}, "bad", [None],
    [dict(attempt_id="x", model_id=FAST, kind="running", evidence_sha256="a" * 64)],
    [dict(attempt_id="../x", model_id=FAST, kind="transport_failure", evidence_sha256="a" * 64)],
    [dict(attempt_id="x", model_id="unknown", kind="capability_failure", evidence_sha256="a" * 64)],
    [dict(attempt_id="x", model_id=FAST, kind="capability_failure", evidence_sha256="bad")],
    [record("capability_failure", recovered=True)], [record("termination_unconfirmed", recovered=True)],
    [record("transport_failure"), record("transport_failure")]])
def test_invalid_or_ambiguous_history_is_refused(bad):
    with pytest.raises(rt.ValidationError):
        rt.route(task(bad), CFG)


def test_nonempty_legacy_and_typed_history_cannot_be_combined():
    with pytest.raises(rt.ValidationError):
        rt.route(task([record("transport_failure")], prior_failures=[FAST]), CFG)


def test_failure_evidence_cannot_be_reused_as_its_own_recovery():
    row = record("invalid_output", recovered=True)
    row["recovery_sha256"] = row["evidence_sha256"]
    with pytest.raises(rt.ValidationError):
        rt.route(task([row]), CFG)


@pytest.mark.parametrize("hotfix", [False, True])
def test_legacy_unconfirmed_flag_cannot_be_softened_by_notify_policy(hotfix):
    cfg = copy.deepcopy(CFG)
    cfg["human_in_the_loop"]["on_termination_unconfirmed"] = "notify_human"
    flags = ["termination_unconfirmed"] + (["production_hotfix"] if hotfix else [])
    out = rt.route(rt.Task(task_class="IMPLEMENTATION", complexity=0, uncertainty=0,
                           blast_radius=0, reversibility=0, flags=flags), cfg)
    assert out["terminal"] == "TERMINATION_UNCONFIRMED"
    assert out["selected_model"] is None and out["requires_human_confirmation"]
