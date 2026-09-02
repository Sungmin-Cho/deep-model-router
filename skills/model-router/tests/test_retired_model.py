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


def _retired():
    row = CFG["models"].get(RETIRED_KEY)
    assert row is not None, f"{RETIRED_KEY} is not in the registry"
    return row


def test_retired_key_is_bound_nowhere_and_not_dispatchable():
    assert _retired()["dispatchable"] is False
    for binding in CFG["role_bindings"].values():
        assert RETIRED_KEY not in binding.values(), binding
    for runtime, per_role in CFG["fallbacks"].items():
        for role, keys in per_role.items():
            assert RETIRED_KEY not in keys, (runtime, role, keys)


def test_retired_id_is_valid_history_input_and_changes_nothing():
    retired_id = _retired()["id"]
    base = _r()
    withheld = _r(unavailable_models=[retired_id])
    assert withheld["selected_model"] == base["selected_model"]
    assert withheld["fallbacks_applied"] == []                       # nothing was removed
    failed = _r(prior_failures=1, prior_models=[retired_id])
    assert failed["terminal"] == "HUMAN_REQUIRED"                    # a tier-3 failure has no tier above: valid input, exhausted ladder [P2-sol-F8]
    assert failed["selected_model"] is None


def test_retired_id_cli_exit_statuses_are_never_invalid_input():
    """Valid history input means exit 0 or 1 — never 2 (invalid input)."""
    script = Path(__file__).resolve().parent.parent / "scripts" / "route_task.py"
    base = [sys.executable, str(script), "--runtime", "claude_code", "--class", "IMPLEMENTATION",
            "--complexity", "1", "--uncertainty", "1", "--blast-radius", "1", "--reversibility", "1"]
    rid = _retired()["id"]
    run = lambda *extra: subprocess.run(base + list(extra), capture_output=True, text=True).returncode  # noqa: E731
    assert run("--unavailable-models", rid) == 0
    assert run("--host-model", rid, "--host-effort", "HIGH") == 0
    assert run("--prior-failures", "1", "--prior-models", rid) == 1     # HUMAN_REQUIRED, not 2


def test_retired_host_model_is_still_compared_by_tier():
    task = _t(task_class="MECHANICAL", complexity=1, uncertainty=0, blast_radius=0, reversibility=0)
    task._host_seat = {"model": _retired()["id"], "effort": "HIGH"}
    assert route(task, CFG)["host_seat_advisory"]["model_comparison"] == "above"


def test_live_architect_host_model_compares_above_on_low_work():
    task = _t(task_class="MECHANICAL", complexity=1, uncertainty=0, blast_radius=0, reversibility=0)
    task._host_seat = {"model": ARCHITECT_ID, "effort": "HIGH"}
    assert route(task, CFG)["host_seat_advisory"]["model_comparison"] == "above"
