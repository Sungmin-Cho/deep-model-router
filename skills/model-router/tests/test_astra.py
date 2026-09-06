"""A new frontier seat must be reachable, fall back, and emit valid CLI effort."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from route_task import ConfigError, Policy, Task, default_config, route

CFG = default_config()


def astra_id():
    assert "openai_frontier" in CFG["models"], "frontier model is missing from the registry"
    return CFG["models"]["openai_frontier"]["id"]


def task(**kw):
    return Task(**{**dict(task_class="MECHANICAL", complexity=0, uncertainty=0,
                        blast_radius=0, reversibility=0, runtime="codex"), **kw})


def investigation(**kw):
    return task(task_class="INVESTIGATION", complexity=2, uncertainty=2,
                blast_radius=1, reasoning_centric=True, **kw)


def test_frontier_is_used_when_default_reasoning_is_unavailable_on_every_host():
    for runtime in CFG["runtimes"]:
        out = route(investigation(runtime=runtime, unavailable_models=[CFG["models"]["openai_reasoning"]["id"]]), CFG)
        assert out["terminal"] is None
        assert out["selected_model"] == astra_id()
        assert out["selected_effort_native"] == "high"


def test_volume_work_keeps_the_fast_model():
    out = route(task(), CFG)
    assert out["selected_model"] == CFG["models"]["openai_worker_fast"]["id"]
    assert astra_id() not in out["review"]["reviewer_models"]


def test_unavailable_frontier_falls_back_to_sol_in_openai_only_binding():
    out = route(investigation(flags=["bridge_down"], unavailable_models=[astra_id()]), CFG)
    assert out["terminal"] is None
    assert out["selected_model"] == CFG["models"]["openai_reasoning"]["id"]
    assert out["fallbacks_applied"]
    assert astra_id() not in out["review"]["reviewer_models"]


@pytest.mark.parametrize("runtime", list(CFG["runtimes"]))
@pytest.mark.parametrize("rc", [False, True])
def test_frontier_addition_does_not_demote_hard_isolated_investigation(runtime, rc):
    out = route(task(task_class="INVESTIGATION", complexity=3, uncertainty=2,
                     reasoning_centric=rc, runtime=runtime), CFG)
    assert out["terminal"] is None
    assert out["selected_model"] == CFG["models"]["openai_reasoning"]["id"]
    assert out["review"]["reviewer_models"] == [CFG["models"]["claude_senior"]["id"]]


def test_frontier_is_an_architect_fallback():
    out = route(task(task_class="ARCHITECTURE", uncertainty=3,
                     unavailable_models=[CFG["models"]["claude_architect"]["id"]]), CFG)
    assert out["terminal"] is None
    assert out["selected_model"] == astra_id()


def test_openai_only_can_reach_tier_three():
    out = route(investigation(flags=["bridge_down"], _local_policy={"minimum_capability_tier": 3}), CFG)
    assert out["terminal"] is None
    assert out["selected_model"] == astra_id()
    assert out["selected_capability_tier"] == 3
    assert out["selected_families"] == ["openai"]


def test_failed_sol_can_escalate_to_frontier_but_frontier_failure_is_terminal():
    sol = CFG["models"]["openai_reasoning"]["id"]
    out = route(task(flags=["bridge_down"], prior_failures=1, prior_models=[sol]), CFG)
    assert out["terminal"] is None
    assert out["selected_model"] == astra_id()
    exhausted = route(task(prior_failures=1, prior_models=[astra_id()]), CFG)
    assert exhausted["terminal"] == "HUMAN_REQUIRED"
    assert exhausted["selected_model"] is None


def test_frontier_host_advisory_is_recognized_without_changing_route():
    base = route(task(), CFG)
    out = route(task(_host_seat={"model": astra_id(), "effort": "HIGH"}), CFG)
    assert out["host_seat_advisory"]["model_comparison"] == "above"
    assert out["selected_model"] == base["selected_model"]
    assert out["review"] == base["review"]


@pytest.mark.parametrize("conceptual,native", [
    ("MINIMAL", "low"), ("LOW", "low"), ("MEDIUM", "medium"),
    ("HIGH", "high"), ("VERY_HIGH", "xhigh"), ("MAX", "max"),
])
def test_frontier_emits_supported_effort_even_when_it_is_a_fast_seat_fallback(conceptual, native):
    cfg = copy.deepcopy(CFG)
    cfg["effort_by_work"]["formatting_rename"] = conceptual
    unavailable = [m["id"] for m in cfg["models"].values() if m["id"] != astra_id()]
    out = route(task(flags=["bridge_down"], unavailable_models=unavailable), cfg)
    assert out["terminal"] is None
    assert out["selected_model"] == astra_id()
    assert out["selected_effort_native"] == native


@pytest.mark.parametrize("override", [[], None, {"TYPO": "low"}, {"MINIMAL": None}, {"MINIMAL": ""}, {"MINIMAL": 1}])
def test_invalid_per_model_effort_map_is_rejected(override):
    cfg = copy.deepcopy(CFG)
    cfg["models"]["openai_worker_fast"]["effort_map"] = override
    with pytest.raises(ConfigError, match="effort_map"):
        Policy(cfg)


def test_effort_override_is_model_scoped_and_config_driven():
    cfg = copy.deepcopy(CFG)
    cfg["effort_by_work"]["formatting_rename"] = "MINIMAL"
    cfg["models"]["openai_worker_fast"]["effort_map"] = {"MINIMAL": "low"}
    out = route(task(), cfg)
    assert out["selected_effort_native"] == "low"
    assert cfg["effort_map"]["openai"]["MINIMAL"] == "none"
