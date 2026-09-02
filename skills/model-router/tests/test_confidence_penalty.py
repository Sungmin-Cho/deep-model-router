import copy
import itertools
import os
import re
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


LUNA, HAIKU = ID("openai_worker_fast"), ID("claude_worker_fast")

# (uncertainty, prior_models, flags, expected confidence AFTER the change,
#  expect review-band promotion, expect ESCALATE_ROUTING terminal)
BOUNDARY_ROWS = [
    (2, [],            [],                                   0.87, False, False),
    (2, [],            ["bridge_down"],                      0.81, False, False),   # the one intended change
    (1, [],            ["bridge_down"],                      0.89, False, False),
    (2, [LUNA],        ["bridge_down"],                      0.76, True,  False),
    (3, [],            [],                                   0.75, True,  False),
    (3, [],            ["bridge_down"],                      0.69, True,  False),
    (2, [],            ["unknown_root_cause"],               0.77, True,  False),
    (1, [],            ["unknown_root_cause", "bridge_down"], 0.79, True,  False),
    (1, [LUNA],        ["bridge_down"],                      0.84, False, False),
    (1, [LUNA, HAIKU], ["bridge_down"],                      0.74, True,  False),
    (3, [],            ["unknown_root_cause", "bridge_down"], 0.59, False, True),
    (3, [LUNA],        ["bridge_down"],                      0.64, True,  False),
    (2, [LUNA, HAIKU], ["unknown_root_cause", "bridge_down"], 0.56, False, True),
]


@pytest.mark.parametrize("u,priors,flags,expected,promoted,terminal", BOUNDARY_ROWS)
def test_boundary_table(u, priors, flags, expected, promoted, terminal):
    out = _r(uncertainty=u, prior_failures=len(priors), prior_models=list(priors), flags=list(flags))
    assert out["routing_confidence"] == expected, out["routing_confidence"]
    if terminal:
        assert out["terminal"] == "ESCALATE_ROUTING" and out["selected_model"] is None
        return
    assert out["terminal"] is None, out["terminal"]
    overrides = [o for o in out["band_overrides_applied"] if o.startswith("low_routing_confidence")]
    assert bool(overrides) == promoted, (out["band_overrides_applied"], out["review"]["band"])


def test_a_lone_fallback_never_promotes_at_modal_uncertainty():
    """DD-3 principle, reconstructed from INPUTS: no prior failures, no
    unknown_root_cause, uncertainty <= 2, and a recorded fallback -> the
    confidence stays at or above extra_review_below."""
    for u in (0, 1, 2):
        out = _r(uncertainty=u, flags=["bridge_down"])
        assert out["fallbacks_applied"], "bridge_down must record the degraded binding"
        assert out["routing_confidence"] >= EXTRA_REVIEW_BELOW, (u, out["routing_confidence"])
        assert not any(o.startswith("low_routing_confidence") for o in out["band_overrides_applied"])


def test_u3_unknown_root_cause_fallback_stays_terminal():
    """The 0.60 gate on the riskiest profile must survive the penalty change."""
    out = _r(uncertainty=3, flags=["unknown_root_cause", "bridge_down"])
    assert out["routing_confidence"] < ESCALATE_BELOW
    assert out["terminal"] == "ESCALATE_ROUTING"
    assert out["selected_model"] is None
