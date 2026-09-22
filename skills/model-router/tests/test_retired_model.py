import copy
import itertools
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from route_task import Task, default_config, route

CFG = default_config()
ID = lambda key: CFG["models"][key]["id"]          # noqa: E731
ARCHITECT_ID = ID("claude_architect")
CONF = CFG["router"]["confidence"]
EXTRA_REVIEW_BELOW = CONF["extra_review_below"]     # 0.80
ESCALATE_BELOW = CONF["escalate_below"]             # 0.60
BASE = dict(task_class="IMPLEMENTATION", complexity=1, uncertainty=1,
            blast_radius=1, reversibility=1, runtime="claude_code")


def _t(**over):
    return Task(**{**BASE, **over})


def _r(**over):
    return route(_t(**over), CFG)


RETIRED_KEY = "claude_architect_retired"

# Every retired row, not just the first one written. A generation bump that
# adds `<seat>_retired` and forgets it here would leave the new row untested
# while every assertion below still passed on the architect's.
RETIRED_KEYS = sorted(k for k in CFG["models"] if k.endswith("_retired"))

# What `--prior-failures 1 --prior-models <retired id>` must exit with, per
# key. The ladder's answer depends on the row's tier — a tier-3 failure
# exhausts it (1, HUMAN_REQUIRED) and a tier-1 failure escalates (0) — so the
# expectation is stated per key rather than relaxed to "0 or 1", which would
# have stopped pinning the architect's terminal status this test was written
# for. A new retired row must name its own expectation here.
PRIOR_FAILURE_EXIT = {"claude_architect_retired": 1, "xai_frontier_retired": 0}

# A host model must belong to its runtime's native family, so `--host-model`
# on a retired id has to be asked from the runtime that family is native to.
NATIVE_RUNTIME = {CFG["role_bindings"][spec["degraded_binding"]]["senior_engineer"]: rt
                  for rt, spec in CFG["runtimes"].items()}
RUNTIME_OF_FAMILY = {CFG["models"][key]["family"]: rt
                     for key, rt in NATIVE_RUNTIME.items()}


def _retired(key=RETIRED_KEY):
    row = CFG["models"].get(key)
    assert row is not None, f"{key} is not in the registry"
    return row


def test_every_retired_key_names_a_distinct_id_and_is_verified():
    """A retired row exists to answer for one concrete past id. Two rows on the
    same id, or an unverified one, would make it answer for nothing."""
    assert RETIRED_KEYS, "the registry has no retired row at all"
    ids = [_retired(k)["id"] for k in RETIRED_KEYS]
    assert len(set(ids)) == len(ids), ids
    live = {m["id"] for k, m in CFG["models"].items() if not k.endswith("_retired")}
    for key, rid in zip(RETIRED_KEYS, ids):
        assert rid not in live, (key, rid, "a retired id is still seated live")
        assert _retired(key)["verified"] is True, key


@pytest.mark.parametrize("key", RETIRED_KEYS)
def test_retired_key_is_bound_nowhere_and_not_dispatchable(key):
    assert _retired(key)["dispatchable"] is False
    for binding in CFG["role_bindings"].values():
        assert key not in binding.values(), binding
    for runtime, per_role in CFG["fallbacks"].items():
        for role, keys in per_role.items():
            assert key not in keys, (runtime, role, keys)


@pytest.mark.parametrize("key", RETIRED_KEYS)
def test_retired_id_is_valid_history_input_and_is_never_re_seated(key):
    retired_id = _retired(key)["id"]
    base = _r()
    withheld = _r(unavailable_models=[retired_id])
    assert withheld["selected_model"] == base["selected_model"]
    assert withheld["fallbacks_applied"] == []                       # nothing was removed
    failed = _r(prior_failures=1, prior_models=[retired_id])
    # Where the ladder goes depends on the retired row's tier — a tier-3
    # failure exhausts it, a tier-1 failure escalates — but a retired id is
    # never what comes back out.
    assert failed["selected_model"] != retired_id


def test_retired_architect_failure_exhausts_the_ladder():
    failed = _r(prior_failures=1, prior_models=[_retired()["id"]])
    assert failed["terminal"] == "HUMAN_REQUIRED"                    # a tier-3 failure has no tier above: valid input, exhausted ladder [P2-sol-F8]
    assert failed["selected_model"] is None


@pytest.mark.parametrize("key", RETIRED_KEYS)
def test_retired_id_cli_exit_statuses_are_never_invalid_input(key):
    """Valid history input means exit 0 or 1 — never 2 (invalid input)."""
    script = Path(__file__).resolve().parent.parent / "scripts" / "route_task.py"
    row = _retired(key)
    runtime = RUNTIME_OF_FAMILY[row["family"]]
    base = [sys.executable, str(script), "--runtime", runtime, "--class", "IMPLEMENTATION",
            "--complexity", "1", "--uncertainty", "1", "--blast-radius", "1", "--reversibility", "1"]
    rid = row["id"]
    run = lambda *extra: subprocess.run(base + list(extra), capture_output=True, text=True).returncode  # noqa: E731
    assert run("--unavailable-models", rid) == 0
    assert run("--host-model", rid, "--host-effort", "HIGH") == 0
    assert key in PRIOR_FAILURE_EXIT, (key, "name this key's expected exit status")
    assert run("--prior-failures", "1", "--prior-models", rid) == PRIOR_FAILURE_EXIT[key]


def test_retired_host_model_is_still_compared_by_tier():
    task = _t(task_class="MECHANICAL", complexity=1, uncertainty=0, blast_radius=0, reversibility=0)
    task._host_seat = {"model": _retired()["id"], "effort": "HIGH"}
    assert route(task, CFG)["host_seat_advisory"]["model_comparison"] == "above"


def test_live_architect_host_model_compares_above_on_low_work():
    task = _t(task_class="MECHANICAL", complexity=1, uncertainty=0, blast_radius=0, reversibility=0)
    task._host_seat = {"model": ARCHITECT_ID, "effort": "HIGH"}
    assert route(task, CFG)["host_seat_advisory"]["model_comparison"] == "above"
