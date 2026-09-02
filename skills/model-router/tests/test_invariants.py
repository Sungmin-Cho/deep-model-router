"""Exhaustive safety invariants, swept over the whole reachable input space.

Why this file exists, stated plainly: across five rounds of independent review,
every round after the first found a Critical defect *introduced by the previous
round's fix*. Each round I wrote an ad-hoc exhaustive probe, confirmed the fix,
and threw the probe away — so the next change was only checked against the
example-based tests, which is exactly how the same class kept coming back one
seam over.

The example tests in `test_routing.py` and `test_defects.py` document specific
scenarios and specific past defects. This file does something different: it
enumerates the input space and asserts the safety properties over all of it, so
a change cannot satisfy the examples while breaking the property somewhere the
examples do not look.

Every invariant here was learned from a real defect that shipped past a green
suite. The comment on each says which.

Run:  python3 -m pytest skills/model-router/tests/test_invariants.py -q
"""

import copy
import itertools
import json
import re
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    ROLES,
    TASK_CLASSES,
    Task,
    ValidationError,
    load_config,
    route,
)

CFG = load_config()
ARCHITECT_ID = CFG["models"]["claude_architect"]["id"]
MODEL_IDS = sorted(m["id"] for m in CFG["models"].values())
ID = lambda key: CFG["models"][key]["id"]                       # noqa: E731
DISPATCHABLE_IDS = sorted(m["id"] for m in CFG["models"].values() if m.get("dispatchable", True))


def _whole_token(needle: str, text: str) -> bool:
    return re.search(r"(^|[^A-Za-z0-9._-])" + re.escape(needle) + r"([^A-Za-z0-9._-]|$)", text) is not None
FAMILY_OF = {m["id"]: m["family"] for m in CFG["models"].values()}
LOCAL_FAMILY = {
    rt: FAMILY_OF[CFG["models"][next(iter(CFG["role_bindings"][spec["degraded_binding"]].values()))]["id"]]
    for rt, spec in CFG["runtimes"].items()
}
TIER_OF = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
NOMINAL_TIER = {role: TIER_OF[CFG["models"][key]["id"]]
                for role, key in CFG["role_bindings"]["default"].items()}
# Written out, not recomputed. An oracle that repeats the implementation's
# formula agrees with it even when the formula is wrong — including when a
# renamed role makes a band's reviewer list empty and the floor silently
# becomes 0, disabling the gate on both sides at once.
BAND_FLOOR = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 2}

# Dimension corners plus a midpoint. Bands are determined by the weighted sum,
# so the corners cover every band and the midpoint catches boundary handling.
DIMENSIONS = [(0, 0, 0, 0), (1, 1, 1, 1), (2, 2, 2, 0), (3, 3, 3, 3), (0, 3, 0, 0), (3, 0, 3, 2)]

# Flag sets chosen to reach every distinct code path: none, each override
# family, the disagreement route (which binds a judge at ANY band — the blind
# spot that produced round 5's Critical), and the degraded binding.
FLAG_SETS = [
    [],
    ["auth_sensitive"],
    ["financial_sensitive"],
    ["migration", "data_integrity_sensitive"],
    ["unknown_root_cause"],
    ["review_disagreement"],
    ["review_disagreement", "bridge_down"],
    ["long_horizon", "review_disagreement"],
    ["bridge_down"],
    ["auth_sensitive", "bridge_down"],
    ["production_hotfix", "concurrency_sensitive"],
]

# Scarcity is what forces roles onto shared models. Without it the degenerate
# bindings are unreachable and every seat invariant passes vacuously.
def _all_but(*keep):
    """Withhold everything except `keep`. Index slices went stale the moment a
    model was appended to the registry — the new id sorted last and no slice
    reached it."""
    return sorted(set(DISPATCHABLE_IDS) - set(keep))


SCARCITY = [
    [],
    [ID("claude_architect")],                                                  # was MODEL_IDS[0]
    [ID("claude_architect"), ID("claude_worker_fast")],                        # was MODEL_IDS[:2]
    [ID("claude_worker_fast"), ID("claude_senior")],                           # was MODEL_IDS[1:3]
    [ID("claude_senior"), ID("claude_worker_balanced")],                       # was MODEL_IDS[2:5] minus the id-spelling-only row
    [ID("claude_architect"), ID("claude_worker_fast"), ID("claude_senior")],   # was MODEL_IDS[:3]
    [ID("claude_architect"), ID("claude_worker_fast"), ID("claude_senior"), ID("claude_worker_balanced")],  # was MODEL_IDS[:5]
    [ID("claude_senior"), ID("claude_worker_balanced"), ID("openai_worker_fast"), ID("openai_reasoning")],  # was MODEL_IDS[2:7]
    _all_but(ID("claude_senior"), ID("openai_reasoning"), ID("xai_frontier")),
    _all_but(ID("xai_frontier")),
    _all_but(ID("claude_senior"), ID("xai_frontier")),
]

RUNTIMES = sorted(CFG["runtimes"])

# Round 6: without this dimension `excluded_prior_failures` was empty on all
# 8,712 routes, so the invariant that a failed model is never re-emitted
# intersected with the empty set every single time. Reverting round 2's fix
# left all twenty invariants green. Both spellings are swept — a role alias,
# which the router resolves through the *current* binding, and a concrete id,
# which it does not — because they take different paths through the exclusion.
# Round 7: `prior_models` was varied but `prior_failures` was left at 0, so the
# retry-escalation branch — where a role moved up while the resolved model
# stayed at the same capability tier — was never entered by any of the 8,712
# routes. Half a dimension is not a dimension.
# Round 13: `PRIOR` and `PRIOR_FAILURES` used to be independent dimensions, and
# making an unusable history terminal turned 10 of their 15 combinations into
# routes that emit no bindings at all. Every seat invariant below opens with
# `if out["terminal"]: continue`, so two thirds of this sweep's population
# vanished while all 131 tests stayed green — the exact failure mode this file
# exists to prevent, caused by the round that deleted the mechanism.
#
# They are chosen as a PAIR now, so each entry is a history that is valid by
# construction, plus two deliberately invalid ones to keep the terminal path
# populated. `test_the_sweep_reaches_enough_retry_routes` guards the ratio.
PRIOR_HISTORY = [
    ([], 0),
    ([ID("claude_senior")], 1),
    ([ID("claude_worker_fast"), ID("openai_worker_fast")], 2),   # two tier-0 failures: headroom above
    ([ID("claude_worker_balanced")] * 2, 2),                      # the same model twice, truthfully
    ([ID("openai_reasoning")], 1),
    ([ID("claude_architect"), ID("openai_worker_fast")], 2),      # tier-3 failure: exhausts on purpose
    (["senior_engineer"], 1),            # invalid on purpose: alias
    ([], 2),                             # invalid on purpose: unaccounted failures
]



_WITHHELD = 0
TASKS: list = []


def _routes():
    """Every reachable route. Terminal ones are yielded too — several
    invariants are specifically about what a terminal route must withhold."""
    for task_class, dims, flags, runtime, scarce, (prior, failures) in itertools.product(
        TASK_CLASSES, DIMENSIONS, FLAG_SETS, RUNTIMES, SCARCITY, PRIOR_HISTORY
    ):
        c, u, b, rev = dims
        try:
            task = Task(
                task_class=task_class, complexity=c, uncertainty=u,
                blast_radius=b, reversibility=rev, flags=list(flags),
                runtime=runtime, unavailable_models=list(scarce),
                prior_models=list(prior), prior_failures=failures,
            )
            # The task is recorded as CONSTRUCTED, not as intended. This is the
            # only way to tell "the sweep lists two runtimes" from "two runtimes
            # reach the router" — round 7 mutated `runtime=runtime` to a
            # constant and every invariant stayed green, because the old check
            # compared the sweep's own list against itself.
            # Projections, not objects: the sweep is ~200k routes and holding
            # every Task alive for the process lifetime buys nothing the
            # dimension guard needs.
            out = route(task, CFG)
            # Recorded only after the route succeeds, so TASKS and routes()
            # stay index-aligned and a class can be measured on its own.
            TASKS.append((task.task_class,
                          (task.complexity, task.uncertainty,
                           task.blast_radius, task.reversibility),
                          tuple(task.flags), task.runtime,
                          tuple(task.unavailable_models),
                          tuple(task.prior_models), task.prior_failures))
            yield out
        except ValidationError:
            # Every candidate withheld: failing closed is a correct outcome.
            global _WITHHELD
            _WITHHELD += 1
            continue


ALL = None


def routes():
    global ALL
    if ALL is None:
        ALL = list(_routes())
    return ALL


def parties(out):
    return [m for m in [out["selected_model"], *out["review"]["reviewer_models"]] if m]


def test_the_sweep_is_large_and_reaches_the_interesting_states():
    """Guards every invariant below. A sweep that never reaches a degraded
    binding, a substitution, a terminal, or a seated judge proves nothing about
    them — and that is precisely how earlier invariant tests passed while the
    invariants were false."""
    all_routes = routes()
    product = len(TASK_CLASSES) * len(DIMENSIONS) * len(FLAG_SETS) * len(RUNTIMES) \
        * len(SCARCITY) * len(PRIOR_HISTORY)
    # Computed, not a magic threshold: `> 5000` would still have passed with
    # 97% of the sweep gone. Withheld routes are a correct outcome, but they
    # are bounded — if most of the space starts failing closed, that is a
    # change worth failing on rather than absorbing.
    assert len(all_routes) == product - _WITHHELD
    assert _WITHHELD < product * 0.25, (
        f"{_WITHHELD}/{product} routes now fail closed; the sweep is measuring "
        f"validation, not routing")

    reached = {
        "terminal": sum(1 for o in all_routes if o["terminal"]),
        "substitution": sum(1 for o in all_routes if o["review"]["self_review_avoided"]),
        "judge_seated": sum(1 for o in all_routes if o["review"]["judge_model"]),
        "judge_unavailable": sum(1 for o in all_routes if o["review"]["judge_unavailable"]),
        "compromised": sum(1 for o in all_routes if o["review"]["independence_compromised"]),
        "fallback": sum(1 for o in all_routes if o["fallbacks_applied"]),
        "compensation": sum(1 for o in all_routes if o["fallback_compensations_applied"]),
        "bridge_down": sum(1 for o in all_routes if "bridge down" in o["rationale"]),
        # Round 6 added the three below. Each names the precondition of an
        # invariant that was passing on zero routes.
        "prior_exclusion": sum(1 for o in all_routes if o["excluded_prior_failures"]),
        "depth_reduced": sum(1 for o in all_routes if o["review"]["review_depth_reduced"]),
        "fallback_note": sum(1 for o in all_routes
                             if any("->" in n for n in o["fallbacks_applied"])),
    }
    missing = [k for k, v in reached.items() if v == 0]
    assert not missing, f"the sweep never reached: {missing}"


def test_the_sweep_reaches_enough_retry_routes():
    """Round 13. The counts above are all `> 0`, which a surviving third
    satisfies. When an unusable retry history became terminal, two thirds of
    this sweep stopped filling a seat and every invariant guarded by
    `if terminal: continue` quietly lost its population — with the suite green.
    A ratio is what notices that; a presence check is not."""
    all_routes = routes()
    # Per history class, not in aggregate. Round 14: a single `> 10%` bar was
    # satisfied while an entire valid history class terminalised — the surviving
    # classes carried the ratio. A dimension that stops contributing has to be
    # visible on its own, which is the same lesson as
    # `test_every_swept_dimension_reaches_the_router`, one level up.
    # A class whose strongest failure is the top tier exhausts the ladder by
    # construction, so it is valid input that can never be dispatchable — it
    # covers `ceiling_exhausted` and is measured for that instead.
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    top = max(tier.values())
    valid = [(tuple(p), f) for p, f in PRIOR_HISTORY if f == 0
             or (len(p) == f and all(m in set(MODEL_IDS) for m in p)
                 and max((tier[m] for m in p), default=0) < top)]
    exhausting = [(tuple(p), f) for p, f in PRIOR_HISTORY
                  if f and len(p) == f and all(m in set(MODEL_IDS) for m in p)
                  and max(tier[m] for m in p) == top]
    # Exactly one. Exhaustion needs a representative, and more than one means a
    # productive class was converted into a terminal one — which is how a
    # dimension stops contributing without any count going to zero. Round 14
    # caught the aggregate ratio surviving exactly that.
    assert len(exhausting) == 1, (
        f"{len(exhausting)} history classes exhaust the ladder by construction "
        f"({exhausting}); each one is a class the seat invariants never see")
    assert len(valid) >= 4, "the sweep no longer carries enough valid history classes"
    by_class: dict = {}
    for row, out in zip(TASKS, all_routes):
        by_class.setdefault((row[5], row[6]), []).append(out)
    for key in valid:
        population = by_class.get(key, [])
        assert population, f"history class {key} never reached the router"
        if key[1] == 0:
            continue
        dispatchable = [o for o in population if not o["terminal"]]
        assert len(dispatchable) > len(population) * 0.10, (
            f"history class {key}: only {len(dispatchable)}/{len(population)} routes "
            f"are dispatchable, so the seat invariants see almost none of it")

    for key in exhausting:
        population = by_class.get(key, [])
        assert population, f"history class {key} never reached the router"
        assert all(o["terminal"] for o in population), (
            f"history class {key} names a top-tier failure yet {key} produced a "
            f"dispatchable route — nothing is stronger than what already failed")

    blocked = [o for o in all_routes if o["terminal"] == "RETRY_HISTORY_REQUIRED"]
    assert blocked, "no route exercises the retry-history terminal"
    assert len(blocked) < len(all_routes) * 0.40, (
        f"{len(blocked)}/{len(all_routes)} routes are terminal for want of a "
        f"history; the sweep is measuring the guard, not the router")


def test_every_swept_dimension_reaches_the_router():
    """The generalisation of the round-6 failure, corrected in round 7.

    Round 6's version compared the sweep's own lists against themselves and
    then asserted `len(routes()) + _WITHHELD == len(product)` — an identity
    that holds however the loop body is written. A reviewer proved it: mutating
    `runtime=runtime` to `runtime="claude_code"` collapsed the runtime actually
    reaching `Task` and all 22 invariants stayed green, which is precisely the
    defect this test names in its own docstring.

    So it asserts on the tasks the sweep CONSTRUCTED, at the router boundary.
    A value listed in a dimension but dropped on the way into `Task()` shows up
    here as a field with one distinct value.
    """
    routes()                     # force the sweep
    assert TASKS, "no task was constructed"

    fields = ("task_class", "dimensions", "flags", "runtime",
              "unavailable_models", "prior_models", "prior_failures")
    observed = {name: {row[i] for row in TASKS} for i, name in enumerate(fields)}
    expected = {
        "task_class": len(set(TASK_CLASSES)),
        "dimensions": len({tuple(d) for d in DIMENSIONS}),
        "flags": len({tuple(f) for f in FLAG_SETS}),
        "runtime": len(set(RUNTIMES)),
        "unavailable_models": len({tuple(s) for s in SCARCITY}),
        "prior_models": len({tuple(p) for p, _ in PRIOR_HISTORY}),
        "prior_failures": len({f for _, f in PRIOR_HISTORY}),
    }
    for field, seen in observed.items():
        assert len(seen) == expected[field], (
            f"{field}: the sweep lists {expected[field]} distinct values but "
            f"{len(seen)} reached the router — a value is being dropped"
        )


# ---------------------------------------------------------------------------
# Seat invariants — five rounds of Criticals live here
# ---------------------------------------------------------------------------

def test_no_reviewer_holds_the_implementer_model_where_independence_is_required():
    """Rounds 3 and 4. Collision was judged on role labels while a degraded
    binding maps several roles to one model."""
    for out in routes():
        rv = out["review"]
        if out["terminal"] or not rv["independence_required"]:
            continue
        models = [m for m in rv["reviewer_models"] if m]
        assert out["selected_model"] not in models, (
            f"{out['task_class']}/{rv['band']}: implementer {out['selected_model']} "
            f"is also a reviewer"
        )


def test_no_two_reviewers_share_a_model_where_independence_is_required():
    for out in routes():
        rv = out["review"]
        if out["terminal"] or not rv["independence_required"]:
            continue
        models = [m for m in rv["reviewer_models"] if m]
        assert len(models) == len(set(models)), (
            f"{out['task_class']}/{rv['band']}: duplicate reviewer models {models}"
        )


def test_the_judge_is_never_a_party_at_any_band():
    """Round 5. The judge is bound by the disagreement path at ANY band, but
    seat allocation sat behind `if review["independent"]` and LOW declares
    independence false — so LOW + disagreement skipped it entirely. LOW's
    worker-reviews-itself is documented design; the judge is not covered by
    that exemption."""
    for out in routes():
        rv = out["review"]
        if out["terminal"] or not rv["judge_model"]:
            continue
        assert rv["judge_model"] not in parties(out), (
            f"{out['task_class']}/{rv['band']}: judge {rv['judge_model']} is also a party"
        )


def test_a_seated_judge_is_never_weaker_than_any_party_it_adjudicates():
    """Round 6. The previous spelling compared ROLE ORDINALS, and under
    scarcity a role holds whatever model is left: `worker_fast` on the frontier
    model, `worker_balanced` on a mid one. Twenty-seven routes seated a tier-1
    judge over a tier-3 implementer and its reviewer, and the role comparison
    called it well ordered. The implementer counts as a party — it is one side
    of any dispute about its own work."""
    for out in routes():
        rv = out["review"]
        if out["terminal"] or not rv["judge_model"]:
            continue
        involved = parties(out)
        assert involved, "a judge was seated with no party to adjudicate"
        assert TIER_OF[rv["judge_model"]] >= max(TIER_OF[m] for m in involved), (
            f"{out['task_class']}/{rv['band']}: judge {rv['judge_model']} "
            f"(tier {TIER_OF[rv['judge_model']]}) is outranked by {involved}"
        )


def test_a_review_below_its_band_floor_is_disclosed_and_gated():
    """Round 6. Fallbacks and de-confliction both re-seat reviewers without
    consulting the band, so a HIGH review could be staffed at tier 0 and emitted
    as if the band had been met. The router cannot restore the depth; what it
    must not do is stay quiet about spending it."""
    for out in routes():
        rv = out["review"]
        if out["terminal"]:
            continue
        floor = BAND_FLOOR[rv["band"]]
        under = [m for m in rv["reviewer_models"] if m and TIER_OF[m] < floor]
        if not under:
            assert not rv["review_depth_reduced"], "reported a shortfall that is not there"
            continue
        assert rv["review_depth_reduced"], (
            f"{rv['band']} staffed at {under} (floor tier {floor}) without disclosure")
        assert out["requires_human_confirmation"], (
            f"{rv['band']} staffed below its floor and still dispatchable")


# ---------------------------------------------------------------------------
# Executability — a route must be runnable exactly as written
# ---------------------------------------------------------------------------

def test_no_route_emits_a_model_the_caller_withheld():
    """Round 1's Critical. Five of ten fallback paths substituted the same
    model back and recorded a downgrade that never happened."""
    for out in routes():
        blocked = set(out["unavailable_models"])
        emitted = set(parties(out)) | {out["review"]["judge_model"]} - {None}
        assert not (emitted & blocked), f"emitted withheld model(s) {emitted & blocked}"


def test_no_route_emits_a_model_that_already_failed():
    """Round 2. Role-tier escalation moved the label while a degraded binding
    kept the same model behind it."""
    for out in routes():
        failed = set(out["excluded_prior_failures"])
        emitted = set(parties(out)) | {out["review"]["judge_model"]} - {None}
        assert not (emitted & failed)


def test_bridge_down_never_names_an_unreachable_family():
    """Round 2. A model on the far side of a downed bridge cannot be invoked,
    so naming it produces a route that cannot be executed."""
    for out in routes():
        if "bridge down" not in out["rationale"]:
            continue
        # The rationale names the degraded binding; derive the family from it
        # rather than from a two-way guess that a third binding silently loses.
        binding = next(b for b in CFG["role_bindings"] if f"binding degraded to {b}" in out["rationale"])
        local = FAMILY_OF[CFG["models"][next(iter(CFG["role_bindings"][binding].values()))]["id"]]
        emitted = [m for m in parties(out) + [out["review"]["judge_model"]] if m]
        foreign = {m for m in emitted if FAMILY_OF[m] != local}
        assert not foreign, f"bridge down but emitted {sorted(foreign)} (local={local})"


# Fields that legitimately name a model on a terminal route: they echo what the
# CALLER supplied or withheld, and are not bindings anyone could dispatch.
_ECHOES_CALLER_INPUT = (
    "unavailable_models", "excluded_prior_failures",
)


def test_a_terminal_route_withholds_every_execution_binding():
    """Rounds 3 and 4 — and round 7, through a field added in round 6.

    The previous spelling ENUMERATED five fields. `review_depth_reduced` was
    added later carrying a concrete model id, was not one of the five, and so
    a terminal route handed back a dispatchable binding with all 114 tests
    green. Enumerating the fields you remember re-opens this defect every time
    the schema grows, so the property is asserted instead: on a terminal route,
    no concrete model id appears anywhere except where the caller put it.
    """
    ids = {m["id"] for m in CFG["models"].values()}
    seen = 0
    for out in routes():
        if not out["terminal"]:
            continue
        seen += 1
        rv = out["review"]
        assert out["selected_model"] is None
        assert out["selected_effort"] is None
        assert rv["reviewer_models"] == []
        assert rv["judge_model"] is None
        assert rv["effort"] is None
        echoed = set().union(*(set(out[k]) for k in _ECHOES_CALLER_INPUT))
        # The one nested caller echo: exactly host_seat_advisory.declared.model.
        # Scrub that single path, then scan — a global exemption set would excuse
        # the same id leaking anywhere else in the route.
        scrubbed = copy.deepcopy(out)
        declared = (scrubbed.get("host_seat_advisory") or {}).get("declared")
        if declared:
            declared["model"] = None
        named = {i for i in ids if _whole_token(i, json.dumps(scrubbed))} - echoed
        assert not named, (
            f"terminal route ({out['terminal']}) still names {sorted(named)} — "
            f"a consumer can dispatch it")
    assert seen, "no terminal route in the sweep"


def test_a_reduced_depth_route_is_not_dispatchable_without_a_human():
    """Round 7, from the security review. `requires_human_confirmation` is a
    boolean in a JSON blob; the exit status is what a shell can act on. A route
    that needs a human and exits 0 is a gate any caller treating success as
    authorisation walks straight through."""
    import subprocess
    probe = ["--class", "IMPLEMENTATION", "--complexity", "0", "--uncertainty", "0",
             "--blast-radius", "0", "--reversibility", "0",
             "--flags", "auth_sensitive,bridge_down", "--runtime", "codex"]
    proc = subprocess.run([sys.executable, str(SKILL / "scripts" / "route_task.py"), *probe],
                          capture_output=True, text=True)
    out = json.loads(subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "route_task.py"), *probe, "--format", "json"],
        capture_output=True, text=True).stdout)
    assert out["requires_human_confirmation"], "probe no longer reaches the gate"
    assert out["review"]["review_depth_reduced"], "probe no longer reduces depth"
    assert proc.returncode == 3, (
        f"human-gated route exited {proc.returncode}; a caller reading exit status "
        f"cannot tell it from a dispatchable one")


def test_an_unsatisfiable_band_floor_means_no_amount_of_availability_would_help():
    """Round 8. `band_floor_unsatisfiable` had no assertion anywhere — hard-code
    it either way and 120 tests stayed green — and its arithmetic counted the
    implementer against the reviewer floor unconditionally, so an ordinary
    recoverable shortage was reported to the human as permanent, with the
    message "will not clear by retrying". Both directions are checked here by
    re-running the same route with nothing withheld: that is what "the binding
    itself cannot supply this" means."""
    # Never claimed where there is no shortfall to explain.
    for out in routes():
        if not out["review"]["review_depth_reduced"]:
            assert not out["review"]["band_floor_unsatisfiable"], (
                "claimed a structural shortage with no shortfall")

    # Both directions, on routes whose answer is known independently of the
    # implementation: withhold a model that the binding could spare, versus a
    # binding that never had enough.
    claude_only_recoverable = route(Task(
        task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=1,
        reversibility=0, flags=["auth_sensitive", "bridge_down"],
        runtime="claude_code", unavailable_models=[ARCHITECT_ID]), CFG)
    assert claude_only_recoverable["review"]["review_depth_reduced"], "probe drifted"
    assert not claude_only_recoverable["review"]["band_floor_unsatisfiable"], (
        "claude_only supplies two tier-2+ models; withholding one is scarcity, "
        "not structural incapacity")
    restored = route(Task(
        task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=1,
        reversibility=0, flags=["auth_sensitive", "bridge_down"],
        runtime="claude_code"), CFG)
    assert not restored["review"]["review_depth_reduced"], (
        "the shortfall did not in fact clear — the probe is wrong, not the flag")

    openai_only_structural = route(Task(
        task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=1,
        reversibility=0, flags=["auth_sensitive", "bridge_down"], runtime="codex"), CFG)
    assert openai_only_structural["review"]["review_depth_reduced"], "probe drifted"
    assert openai_only_structural["review"]["band_floor_unsatisfiable"], (
        "openai_only holds one tier-2 model against two reviewer seats and "
        "nothing is withheld — no retry can clear this")

    # Round 9: the two probes above both survive DELETING the implementer from
    # the seat count, so they pinned only one direction. This one needs it: a
    # tier-2 implementer under `claude_only` occupies one of the two tier-2+
    # models, leaving one for two reviewer seats.
    implementer_consumes_a_seat = route(Task(
        task_class="ARCHITECTURE", complexity=2, uncertainty=2, blast_radius=2,
        reversibility=0, flags=["bridge_down"], runtime="claude_code"), CFG)
    rv = implementer_consumes_a_seat["review"]
    assert rv["review_depth_reduced"], "probe drifted"
    assert TIER_OF[implementer_consumes_a_seat["selected_model"]] >= BAND_FLOOR[rv["band"]], (
        "probe needs an implementer at or above the floor to exercise the term")
    assert rv["band_floor_unsatisfiable"], (
        "claude_only has two tier-2+ models; a tier-2 implementer plus two "
        "reviewer seats needs three, so this cannot be staffed at any "
        "availability — dropping the implementer term inverts it")


def test_the_shortfall_record_carries_the_band_the_gate_actually_used():
    """Round 7. The record's payload was asserted nowhere, so mutating
    `band_requires` to 0 kept 114 tests green while handing the human a number
    the router did not use. The floors are written out here rather than
    recomputed from the config, because an oracle that repeats the
    implementation's formula cannot detect an error in the formula."""
    from route_task import Policy
    assert Policy.of(CFG).band_reviewer_floor == BAND_FLOOR, "the router's floors drifted"
    seen = 0
    for out in routes():
        rv = out["review"]
        for short in rv["review_depth_reduced"]:
            seen += 1
            assert short["band_requires"] == BAND_FLOOR[rv["band"]]
            if short["model"]:
                assert short["capability_tier"] == TIER_OF[short["model"]]
                assert short["capability_tier"] < short["band_requires"]
            assert short["reviewer"] in rv["reviewers"]
    assert seen, "no shortfall record in the sweep"


# ---------------------------------------------------------------------------
# Honest reporting — "a recorded change must be a real change"
# ---------------------------------------------------------------------------

def test_every_recorded_fallback_actually_changed_the_model():
    for out in routes():
        for note in out["fallbacks_applied"]:
            if "->" not in note:
                continue
            before, after = (s.strip() for s in note.split("->", 1))
            # Round 6: the previous spelling compared "X unavailable" against
            # "X" and so could never fail. Ten paths emitting round 1's exact
            # Critical — a fallback that lands on the model it replaced — stayed
            # green. The marker word has to come off before the comparison.
            before = before.split(":")[-1].strip().removesuffix(" unavailable").strip()
            assert before and before != after, f"no-op fallback recorded: {note}"


def test_every_recorded_substitution_changed_who_sits_in_the_seat():
    """Round 6, twice over. The name promised seats and the body compared role
    labels, so a substitution that landed on the same model read as a change;
    and nothing checked the record against the final roster, so the judge retry
    could re-seat the very reviewer a record named and leave the rationale
    asserting a reviewer who is not on the route."""
    for out in routes():
        rv = out["review"]
        if out["terminal"]:
            continue
        seated = dict(zip(rv["reviewers"], rv["reviewer_models"]))
        for sub in rv["self_review_avoided"]:
            assert sub["replaced"] != sub["with"]
            assert sub["with"] in rv["reviewers"], (
                f"rationale names reviewer {sub['with']} but the seats hold "
                f"{rv['reviewers']}")
            assert seated.get(sub["with"]) != out["selected_model"] or not rv[
                "independence_required"], (
                f"substitution landed back on the implementer model "
                f"{out['selected_model']}")


def test_a_promoted_review_band_still_passes_every_emit_boundary_check():
    """Round 18, found independently by all three reviewers.

    The confidence-driven promotion was re-run AFTER the emit-boundary
    post-conditions had already passed, so a route promoted at the last moment
    shipped without the depth check, the family check, de-confliction or its
    disclosure. It is the mirror of the defect this file was built around — not
    a check placed where later code routes around it, but a CHANGE placed where
    the checks cannot see it. The plan and the promotion now live in the same
    iteration of the fixed point; this asserts the consequence.
    """
    promoted = [o for o in routes()
                if any("low_routing_confidence" in x for x in o["band_overrides_applied"])]
    assert len(promoted) > 100, f"only {len(promoted)} promoted routes in the sweep"
    # Round 19: the body skips terminal routes, so this passed with zero
    # non-terminal promoted routes — a regression that terminalised all of them
    # would have emptied the assertion instead of failing it.
    dispatchable = [o for o in promoted if not o["terminal"]]
    assert len(dispatchable) > 50, (
        f"only {len(dispatchable)} promoted routes are dispatchable; the checks "
        f"below run on almost nothing")
    for out in dispatchable:
        rv = out["review"]
        involved = parties(out)
        floor = BAND_FLOOR[rv["band"]]
        under = [m for m in rv["reviewer_models"] if m and TIER_OF[m] < floor]
        assert bool(under) == bool(rv["review_depth_reduced"]), (
            f"promoted to {rv['band']} with {under} and depth_reduced="
            f"{rv['review_depth_reduced']}")
        if rv["judge_model"] and involved:
            assert TIER_OF[rv["judge_model"]] >= max(TIER_OF[m] for m in involved)
        if rv["independence_required"]:
            models = [m for m in rv["reviewer_models"] if m]
            assert out["selected_model"] not in models or rv["independence_compromised"], (
                "the implementer sits in its own review on a promoted route")
        # Not asserted: that the final confidence is still below the threshold.
        # A promotion can seat better models and lift it back to the threshold,
        # and the promotion is deliberately not reverted — `promoted_once`
        # exists so the band does not oscillate. What must hold is that the
        # number reported follows from the fallbacks reported, which
        # `test_d20_the_emitted_confidence_matches_the_emitted_fallbacks` owns.


def test_the_fixed_point_body_is_idempotent():
    """Round 19. Round 18 moved the whole plan inside the bounded loop — the
    right repair — and `effort` was the one piece of plan state not rebuilt at
    the top of each pass. So a route that both compensated and promoted raised
    effort TWICE for one compensation, and shipped its notes, its record and
    its effort disagreeing: two "effort +1" notes, an empty
    `fallback_compensations_applied`, and an effort two levels up. "Raise one
    level" is the policy sentence; the route recorded none, said two, and did
    two.

    A fixed point whose body is not idempotent is a fold. What makes it
    checkable is that every compensation the notes claim must appear in the
    record, exactly once each."""
    seen = 0
    for out in routes():
        if out["terminal"]:
            continue
        notes = [n for n in out["notes"] if n.startswith("compensation:")]
        claimed = [n for n in notes if "NOT fully applied" not in n]
        assert len(claimed) == len(set(claimed)), (
            f"the same compensation was applied twice: {claimed}")
        # One record per compensation, and the second-review compensation
        # contributes two notes (effort + the seat) for its single record.
        records = out["fallback_compensations_applied"]
        assert len(records) == len(set(records)), f"duplicate record: {records}"
        if claimed:
            assert records, (
                f"notes claim {claimed} while the record is empty — the pass "
                f"that applied them is not the pass that shipped")
        # And the other direction. Round 19: checking only notes-implies-record
        # let a compensation be recorded with its note deleted — the operator
        # reading the prose would not know it happened, which is the same
        # silence in reverse.
        for record in records:
            expected = ("effort +1" if record == "raise_effort_one_level"
                        else "effort raised to MAX")
            assert any(expected in n for n in notes), (
                f"{record!r} is recorded but no note says so: {notes}")
        seen += len(claimed)
    assert seen, "no compensation in the sweep — the assertion was vacuous"


def test_a_compensation_is_recorded_only_when_it_was_applied():
    """Round 3. `raise_effort_to_MAX_and_add_second_review` reported itself
    applied while doing only the first half."""
    for out in routes():
        if "raise_effort_to_MAX_and_add_second_review" not in out["fallback_compensations_applied"]:
            continue
        if out["terminal"]:
            continue
        assert out["selected_effort"] == CFG["effort_levels"][-1]
        # The name promises a second review, so a second reviewer must be
        # seated. Round 10 changed WHICH field records that: the compensation
        # used to flip `independence_required`, which let a bonus seat upgrade
        # the band's own requirement — and then a LOW route whose compensating
        # review could not be isolated terminated the entire task. The seat is
        # what the name promises; the band's requirement was never part of it.
        assert out["review"]["compensating_reviewers"] >= 1, (
            "compensation recorded without the second reviewer it names")
        assert len(out["review"]["reviewers"]) >= 2


def test_independence_is_never_reported_as_enforced_without_evidence():
    """Round 2. The policy's requirement was being reported as if it were an
    observation of what happened."""
    for out in routes():
        rv = out["review"]
        if rv["review_independence"] != "enforced":
            continue
        pytest.fail("no route in this sweep supplies evidence, so none may claim enforced")


# ---------------------------------------------------------------------------
# Gates — a control that discloses but does not stop is not a control
# ---------------------------------------------------------------------------

def test_every_critical_review_requires_a_human_before_or_after():
    """Round 4, widened in round 20. The router cannot verify an isolation
    receipt's provenance, so it never treats one as proof — a CRITICAL review
    always involves a person.

    What round 20 added is WHEN. A production hotfix may dispatch first and owe
    the confirmation afterwards, because blocking a live incident before
    dispatch costs more than it buys. The obligation does not disappear; it
    moves. So the invariant is the disjunction, and the deferred half is held
    to the conditions that make it safe."""
    deferred_seen = 0
    for out in routes():
        rv = out["review"]
        if rv["band"] != "CRITICAL":
            continue
        assert out["requires_human_confirmation"] or out["human_confirmation_deferred"], (
            f"{out['task_class']}: a CRITICAL review with no human, before or after")
        if out["human_confirmation_deferred"]:
            deferred_seen += 1
            # Deferral is for a review that can be trusted to run properly. A
            # review that cannot is not made acceptable by an incident.
            assert "production_hotfix" in out["critical_flags"] or any(
                "hotfix" in n for n in out["notes"]), "deferred without a hotfix"
            assert not rv["independence_compromised"]
            assert not rv["review_depth_reduced"]
            # `judge_unavailable` is deliberately allowed: an adjudicator is
            # needed only if the reviewers disagree, which happens after the
            # review, which is where the deferred confirmation already is.
            assert out["terminal"] is None
            assert rv["independence_required"], "deferral weakened the review"
            assert len(rv["reviewers"]) >= 2, "deferral cost the review a seat"
    assert deferred_seen, "no deferred route in the sweep — the branch is untested"


def test_compromised_independence_is_terminal():
    for out in routes():
        if out["review"]["independence_compromised"]:
            assert out["terminal"] is not None
            assert out["requires_human_confirmation"] is True


def test_an_unavailable_judge_gates_even_though_it_does_not_stop():
    """Round 20 widened this the same way as the CRITICAL gate: a deferred
    hotfix answers the judge question after the review, not before it."""
    for out in routes():
        if out["review"]["judge_unavailable"] and not out["terminal"]:
            assert (out["requires_human_confirmation"]
                    or out["human_confirmation_deferred"]), (
                "no adjudicator and no human asked, before or after")
            assert out["review"]["judge"] is None


def test_a_terminal_route_always_requires_a_human():
    for out in routes():
        if out["terminal"]:
            assert out["requires_human_confirmation"] is True


# ---------------------------------------------------------------------------
# Structural invariants that predate the review loop
# ---------------------------------------------------------------------------

def test_a_critical_domain_flag_always_reaches_high_review_and_worker():
    critical_flags = set(CFG["flags"]["critical_domain"])
    bands = sorted(CFG["router"]["bands"], key=lambda b: CFG["router"]["bands"][b]["ordinal"])
    for out in routes():
        if not (set(out["critical_flags"]) & critical_flags):
            continue
        assert bands.index(out["review"]["band"]) >= bands.index("HIGH")
        if out["selected_model"]:
            # On the resolved tier, not the role label. Round 8: the retry
            # ladder can legitimately leave the worker on a low-ordinal role
            # holding a strong model, and the floor's meaning is "not the
            # cheapest model", not "not that label".
            floor_role = CFG["router"]["floors"]["critical_domain_worker"]
            floor_tier = NOMINAL_TIER[floor_role]
            assert TIER_OF[out["selected_model"]] >= floor_tier, (
                f"critical-domain route dispatched {out['selected_model']} "
                f"(tier {TIER_OF[out['selected_model']]}) below floor tier {floor_tier}")


def test_the_critical_domain_worker_floor_is_currently_a_guard_not_an_active_rule():
    """Round 8, and an honest one to record.

    The floor cannot be mutation-tested: disabling it entirely leaves every one
    of 570,240 paired critical-domain routes with the same dispatched model and
    the same terminal state, because `worker_selection` already places every
    class at `worker_balanced` or above once a critical-domain flag forces the
    band to HIGH. So this test asserts the redundancy rather than pretending to
    cover the rule — if an edit to `worker_selection` makes the floor
    load-bearing, this fails and says so, which is the moment someone needs to
    know the guard has become the thing holding the line."""
    floor_role = CFG["router"]["floors"]["critical_domain_worker"]
    floor_tier = NOMINAL_TIER[floor_role]
    table = CFG["worker_selection"]
    weak = []
    for task_class, cells in table.items():
        for band in ("HIGH", "CRITICAL"):
            role = cells[band]
            if role == "by_reasoning_centric":
                continue                     # both branches are frontier roles
            if NOMINAL_TIER[role] < floor_tier:
                weak.append(f"{task_class}/{band} -> {role}")
    assert not weak, (
        f"worker_selection now places {weak} below the critical-domain floor "
        f"({floor_role}, tier {floor_tier}); the floor has become load-bearing "
        f"and needs real coverage, not this redundancy assertion")


def test_review_depth_is_never_weaker_than_its_band_requires():
    """The property that makes every invariant above checkable: no
    worker-selection branch can weaken a review.

    Stated precisely, because the absolute form is not true: a review may be
    *stronger* than its band's baseline — a fallback compensation can add a
    second independent review to a LOW-band route, which is the whole point of
    the compensation. What must never happen is a review weaker than the band
    asked for, because that is the direction in which the worker's identity
    could quietly influence how hard its own output is checked.
    """
    raised_without_reason = []
    for out in routes():
        rv = out["review"]
        baseline = CFG["review"][rv["band"]]
        want_independent = baseline.get("independent", False)
        want_checks = set(baseline.get("required_checks", []))

        assert rv["independence_required"] >= want_independent, (
            f"{out['task_class']}/{rv['band']}: independence downgraded below the band"
        )
        assert want_checks <= set(rv["required_checks"]), (
            f"{out['task_class']}/{rv['band']}: required checks dropped"
        )
        # Anything above the baseline must have a recorded cause, or it is a
        # silent policy change rather than a disclosed compensation.
        if rv["independence_required"] and not want_independent:
            if not out["fallback_compensations_applied"]:
                raised_without_reason.append((out["task_class"], rv["band"]))
    assert not raised_without_reason, (
        f"review strengthened above its band with nothing recorded: "
        f"{sorted(set(raised_without_reason))[:5]}"
    )


def test_the_rationale_always_names_the_band_and_any_fallback():
    for out in routes():
        assert out["risk_band"] in out["rationale"]
        if out["fallbacks_applied"]:
            assert "Fallback" in out["rationale"]


_KEY_OF = {m["id"]: k for k, m in CFG["models"].items()}


def _route_of(**kwargs):
    return route(Task(**kwargs))


def test_served_model_caveat_discloses_exactly_when_flag_and_seat_meet():
    """design §4 B5: caveat flag ∧ 해당 모델 착석 ∧ executable일 때 모델당
    정확히 1회 공시; 그 외에는 route가 기존과 동일. note는 registry key로
    지칭한다 — 모델 id는 terminal-withholding invariant의 스캔 대상이다."""
    marker = "may substitute another"
    hit = _route_of(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
                    blast_radius=3, reversibility=2, flags=["security_sensitive"])
    # round-1 review I2: assert executability outright. Behind `if terminal is
    # None` the only positive assertions this feature has would vacate in
    # silence the day a policy change made this fixture terminal.
    assert hit["terminal"] is None
    assert hit["requires_human_confirmation"]
    assert "claude_architect" in [_KEY_OF[m] for m in
                                  [hit["selected_model"], *hit["review"]["reviewer_models"]]
                                  if m], "fixture must seat the caveat-bearing model"
    notes = [n for n in hit["notes"] if marker in n]
    assert len(notes) == 1
    assert "claude_architect" in notes[0]
    assert "security_sensitive" in notes[0]
    # The note names the seated model's FAMILY, never a model id (design §4 B5
    # + round-1 review F3: a caveat on a non-claude row must not produce a note
    # claiming "Claude").
    assert "claude model" in notes[0]
    assert ARCHITECT_ID not in json.dumps(hit["notes"])
    # 같은 좌석, flag 없음 -> 공시 없음
    miss = _route_of(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
                     blast_radius=3, reversibility=1, flags=[])
    assert not any(marker in n for n in miss["notes"])
    # terminal + security_sensitive -> 공시 없음 (withholding과 정합)
    terminal = _route_of(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
                         blast_radius=3, reversibility=2,
                         flags=["security_sensitive", "bridge_down"],
                         runtime="grok")
    assert terminal["terminal"] == "INDEPENDENCE_UNAVAILABLE"
    assert not any(marker in n for n in terminal["notes"])


def test_a_caveat_on_a_non_claude_row_does_not_claim_the_wrong_vendor():
    """round-1 review F3: `served_model_caveats` is accepted on ANY registry
    row — Policy validates the flag vocabulary, never the family — so a note
    that hard-codes one vendor is a factually false disclosure one config edit
    away. The wording is derived from the seated model's family instead."""
    cfg = copy.deepcopy(CFG)
    cfg["models"]["openai_reasoning"]["served_model_caveats"] = ["security_sensitive"]
    out = route(Task(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
                     blast_radius=3, reversibility=2,
                     flags=["security_sensitive"]), cfg)
    assert out["terminal"] is None
    notes = [n for n in out["notes"] if "may substitute another" in n]
    assert any("openai_reasoning" in n and "openai model" in n for n in notes), notes
    assert not any("openai_reasoning" in n and "claude model" in n for n in notes)


def test_sweep_populations_name_only_dispatchable_models():
    """Sorted-index fixtures move when a registry row is added; named fixtures
    do not. Non-dispatchable ids (retired, id-spelling-only) are no-ops in a
    scarcity list and exhaust nothing in a history — keep them out."""
    for scarce in SCARCITY:
        assert set(scarce) <= set(DISPATCHABLE_IDS), scarce
    for prior, failures in PRIOR_HISTORY:
        if failures and all(m in set(MODEL_IDS) for m in prior):
            assert set(prior) <= set(DISPATCHABLE_IDS), prior


def test_a_retired_model_never_sits_in_any_seat():
    retired = {m["id"] for m in CFG["models"].values() if m.get("dispatchable", True) is False}
    for out in routes():
        seated = set(parties(out)) | {out["review"].get("judge_model")}
        assert not (retired & seated), (out["selected_model"], out["review"]["reviewer_models"], out["review"].get("judge_model"))


def test_a_low_confidence_promotion_never_contradicts_the_confidence_it_ships():
    """The promotion decision reads the plan BEFORE the promotion; the emitted
    confidence is the promoted plan's, and reseating reviewers can retire the
    fallback whose penalty triggered the promotion. Where that happens the route
    used to carry `low_routing_confidence_raised_review_to_X` beside a
    confidence at or above the threshold — a reason the same route disproves.
    The band still stands (un-promoting oscillates), so the recovery must be
    stated, and it must be stated ONLY where it happened."""
    threshold = CFG["router"]["confidence"]["extra_review_below"]
    recovered = still_low = 0
    for out in routes():
        promoted = [o for o in out["band_overrides_applied"]
                    if o.startswith("low_routing_confidence")]
        note = [n for n in out["notes"] if "promoted plan resolves at" in n]
        if not promoted:
            assert not note, (out["routing_confidence"], note)
            continue
        if out["routing_confidence"] >= threshold:
            recovered += 1
            assert len(note) == 1, (out["routing_confidence"], promoted, out["notes"])
            # Both numbers, at a fixed width: `str(0.8)` is a substring of the
            # threshold's own rendering, so a note that printed only the
            # threshold would have satisfied a looser check.
            assert f"resolves at {out['routing_confidence']:.2f} " in note[0], note
            assert "pre-promotion confidence " in note[0], note
            pre = float(note[0].split("pre-promotion confidence ")[1].split(";")[0])
            assert pre < threshold <= out["routing_confidence"], (pre, out["routing_confidence"])
            # "the promotion stands" is the note's whole claim; a future change
            # that un-promoted while keeping the override string would leave
            # this green without it.
            ladder = list(CFG["router"]["bands"])
            assert ladder.index(out["review"]["band"]) > ladder.index(out["risk_band"]), (
                out["review"]["band"], out["risk_band"])
        else:
            still_low += 1
            assert not note, (out["routing_confidence"], note)
    assert recovered, "the sweep no longer reaches a promotion whose confidence recovered"
    assert still_low, "the sweep no longer reaches an ordinary low-confidence promotion"
