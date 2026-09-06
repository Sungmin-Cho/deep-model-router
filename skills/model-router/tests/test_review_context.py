"""Explicit source authors are distinct from the host and review executor."""
import copy
import json
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import route_task as rt

CFG = rt.default_config()
ID = lambda key: CFG["models"][key]["id"]
BASE = dict(route_schema_version=1, task_class="REVIEW", complexity=2,
            uncertainty=2, blast_radius=1, reversibility=0, runtime="codex")


def context(**over):
    return dict(target_sha256="a" * 64, author_model_ids=[ID("openai_reasoning")],
                author_families=[], **over)


def request(ctx=None, **over):
    return rt.task_from_request_v1({**BASE, **over, "review_context": ctx})


def models(out):
    return {out["selected_model"], *out["review"]["reviewer_models"], out["review"]["judge_model"]} - {None}


@pytest.mark.parametrize("runtime", list(CFG["runtimes"]))
@pytest.mark.parametrize("key", ["claude_senior", "openai_reasoning", "claude_architect", "openai_frontier"])
def test_declared_author_is_excluded_from_every_review_task_seat(runtime, key):
    ctx = context()
    ctx["author_model_ids"] = [ID(key)]
    out = rt.route(request(ctx, runtime=runtime), CFG)
    assert ID(key) not in models(out)
    assert out["review_context"] == ctx
    assert out["terminal"] is None


def test_host_declaration_does_not_stand_in_for_source_authorship():
    ctx = context()
    out = rt.route(request(ctx, host_seat={"model": ID("openai_frontier"), "effort": "HIGH"}), CFG)
    assert ID("openai_reasoning") not in models(out)
    assert ID("openai_frontier") in models(out)


def test_author_family_exclusion_is_explicit_and_global_to_review_task():
    ctx = context()
    ctx["author_families"] = ["openai"]
    out = rt.route(request(ctx), CFG)
    family = {m["id"]: m["family"] for m in CFG["models"].values()}
    assert all(family[m] != "openai" for m in models(out))
    # Two eligible Claude reviewers suffice; the lead is no phantom third seat.
    assert len(set(out["review"]["reviewer_models"])) == 2
    assert not out["review"]["review_depth_reduced"]


def test_exclusion_is_not_reported_as_model_unavailability():
    ctx = context()
    ctx["author_model_ids"] = [ID("claude_senior")]
    out = rt.route(request(ctx), CFG)
    assert out["fallbacks_applied"] == []
    assert out["unavailable_models"] == []
    assert out["routing_confidence"] == .87


def test_real_outage_behind_author_exclusion_still_has_a_penalty():
    ctx = context()
    ctx["author_model_ids"] = [ID("claude_senior")]
    out = rt.route(request(ctx, availability_snapshot={"unavailable_models": [ID("openai_reasoning")]}), CFG)
    assert out["fallbacks_applied"]
    assert out["routing_confidence"] < .87


def test_author_constraint_can_exhaust_supply_without_leaking_a_route():
    ctx = context()
    ctx["author_families"] = list(CFG["effort_map"])
    out = rt.route(request(ctx), CFG)
    assert out["terminal"]
    assert not models(out)
    assert out["requires_human_confirmation"]


@pytest.mark.parametrize("runtime", list(CFG["runtimes"]))
def test_exclusion_survives_degraded_bindings_and_retry_history(runtime):
    ctx = context()
    ctx["author_model_ids"] = [ID("openai_frontier"), ID("claude_senior")]
    out = rt.route(request(ctx, runtime=runtime, flags=["bridge_down"],
                           prior_failures=[ID("openai_worker_fast")]), CFG)
    assert not (set(ctx["author_model_ids"]) & models(out))


def test_target_and_authors_are_echoed_only_as_input_on_terminal_routes():
    ctx = context()
    ctx["author_families"] = list(CFG["effort_map"])
    out = rt.route(request(ctx), CFG)
    assert out["review_context"]["target_sha256"] == ctx["target_sha256"]
    assert out["selected_model"] is None and out["selected_families"] == []


def test_omitted_or_null_context_preserves_the_old_route_and_fingerprint():
    old = rt.route(rt.task_from_request_v1(BASE), CFG)
    new = rt.route(request(), CFG)
    assert old == new
    assert "review_context" not in new


def test_context_is_fingerprint_bound_canonical_and_does_not_mutate_input():
    ctx = context()
    ctx["author_model_ids"] = [ID("openai_reasoning"), ID("claude_architect"), ID("openai_reasoning")]
    original = copy.deepcopy(ctx)
    a = rt.route(request(ctx), CFG)
    assert ctx == original
    ctx["author_model_ids"] = sorted(set(ctx["author_model_ids"]))
    b = rt.route(request(ctx), CFG)
    assert a["decision_fingerprint"] == b["decision_fingerprint"]
    ctx["target_sha256"] = "b" * 64
    c = rt.route(request(ctx), CFG)
    assert b["decision_fingerprint"] != c["decision_fingerprint"]


@pytest.mark.parametrize("ctx", [False, [], {}, {"target_sha256": "a" * 64},
    {"target_sha256": "bad", "author_families": ["openai"]},
    {"target_sha256": "a" * 64, "author_model_ids": ["unknown"]},
    {"target_sha256": "a" * 64, "author_families": ["unknown"]},
    {"target_sha256": "a" * 64, "author_model_ids": "not-list"},
    {"target_sha256": "a" * 64, "author_families": ["openai"], "extra": True}])
def test_context_validation_is_fail_closed(ctx):
    with pytest.raises(rt.ValidationError):
        rt.route(request(ctx), CFG)


def test_context_is_rejected_for_implementation_and_write_review_seats():
    for over in ({"task_class": "IMPLEMENTATION"}, {"worker_seat": "write"}):
        with pytest.raises(rt.ValidationError):
            rt.route(request(context(), **over), CFG)


def test_context_checks_the_effective_class_default_seat():
    cfg = copy.deepcopy(CFG)
    cfg["task_write_seat"]["REVIEW"] = "write"
    with pytest.raises(rt.ValidationError):
        rt.route(request(context()), cfg)
    assert rt.route(request(context(), worker_seat="read_only"), cfg)["worker_seat"]["kind"] == "read_only"


def test_cli_accepts_review_context(tmp_path, capsys):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({**BASE, "review_context": context()}))
    assert rt.main(["--request-json", str(path), "--format", "json"]) in (0, 3)
    out = json.loads(capsys.readouterr().out)
    assert ID("openai_reasoning") not in models(out)
