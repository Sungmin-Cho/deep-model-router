"""Part B rule ledger — every decision change since 1.16.1 has a named cause
(design 2026-09-25 DD-B11 invariant 3, plan B0).

Part B changes the review policy on purpose, so "never weaker than the last
release" stops being the oracle. What replaces it is this file: each rule is
declared HERE, independently of the router — a predicate over the input and the
replay state, and a check of which decision fields it may move and how — and the
grid below is replayed rule by rule:

1. The request is PROJECTED onto what 1.16.1 accepts (Part B request fields
   dropped) and routed by the vendored 1.16.1 snapshot. That is the replay's
   starting point.
2. The live router is run with every rule switched off. It must equal the
   snapshot on every decision field — a change no rule owns fails here.
3. The rules are switched on one at a time in `ORDER`. Each step may move only
   the fields its rule declares, only on inputs its predicate admits; a step
   whose predicate is false must move nothing.

"Switched off" is per rule: a request-field rule is off when its field is
projected away, a config rule when its key holds the pre-rule value, and the
one rule with no config key (`c4`) through a patched seam. The last state has
every rule on and is asserted to BE the shipped config, so the replay ends at
the route users get.

The route never explains itself here: predicates read the request and the
previous replay state, never a cause code the new route emits — a route that
changed by accident must not be able to account for itself.

Grid: 3 runtimes x 11 classes x 256 dimension points x the design §6 variants,
plus rule-specific variants on a quarter of the points. The default run is a
fixed stratified sample (< 30 s); `DMR_FULL_BASELINE=1` runs everything.
`python3 test_policy_rules.py` prints the per-rule counts for the ledger.
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import random
import sys
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE))

import route_task as rt  # noqa: E402
from _baseline import baseline_1161_cfg, load_baseline_1161  # noqa: E402

CFG = rt.load_config()
POLICY = rt.Policy.of(CFG)
ID = lambda key: CFG["models"][key]["id"]                       # noqa: E731
TIER_OF = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
FAMILY_OF = {m["id"]: m["family"] for m in CFG["models"].values()}
EFFORTS = list(CFG["effort_levels"])
BANDS = list(CFG["router"]["bands"])
FULL = os.environ.get("DMR_FULL_BASELINE") == "1"

# The closed rule vocabulary, in replay order (design DD-B11 ②). A rule not
# named here cannot explain anything.
ORDER = ("implementer_declared", "c1iii", "c4", "c2", "c3", "c5", "review_lead", "c7")

# Keys that are not decisions: provenance, prose, the explicit-cfg overlay, and
# the typed history echoed back as declared (a rule switched off projects its
# fields away, so the echo differs by the input and nothing else).
NOT_DECISIONS = frozenset({"notes", "rationale", "policy_sha256", "request_sha256",
                           "decision_fingerprint", "router_plugin_version", "model_overlay",
                           "attempt_outcomes"})


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """One Part B rule as the ledger states it.

    `predicate(req, prev)` — may this rule move anything on this input, given
    the replay state before it? `fields` — the decision fields it may move
    (`review.<key>` for the review block). `check(req, prev, cur)` — the
    direction of the move, as a list of problems. `changed` — how many grid
    inputs it moved, pinned for the sample and the full grid.
    """
    name: str
    predicate: Callable[[dict, dict], bool]
    fields: frozenset
    check: Callable[[dict, dict, dict], list]
    changed_sample: int
    changed_full: int


# Output keys Part B adds, with the value a route carries when no rule moved it.
# Stripped before the snapshot comparison, after asserting the default.
NEW_KEY_DEFAULTS: dict[str, object] = {
    "worker_seat_state": "to_dispatch", "implementer_declared": False,
    "implementer_source": None,
}
NEW_REVIEW_KEY_DEFAULTS: dict[str, object] = {"mode": "model_review"}

# Field groups the rules are declared in.
EFFORT = frozenset({"selected_effort", "selected_effort_effective", "selected_effort_native"})
REVIEW_SEATS = frozenset({"review.reviewers", "review.reviewer_models", "review.judge",
                          "review.judge_model", "review.self_review_avoided",
                          "review.independence_compromised", "review.judge_unavailable",
                          "review.review_depth_reduced", "review.band_floor_unsatisfiable",
                          "review.compensating_reviewers", "cross_family_review",
                          "effort_ceiling_applied"})
GATES = frozenset({"human_control_causes", "requires_human_confirmation",
                   "human_confirmation_deferred", "exit"})
# What a changed seat roster drags along: the fallback record and the
# confidence it feeds (0.06 per fallback), the orchestrator ask that reads the
# confidence, and the families the route seats.
SEAT_RECORDS = frozenset({"fallbacks_applied", "fallback_compensations_applied",
                          "routing_confidence", "host_seat_advisory", "selected_families"})


def _implementer_check(req, prev, cur):
    if "error" in cur:
        return [f"refused: {cur['error']}"]
    declared = req["implementer"]["model_id"]
    out = []
    if (cur["worker_seat_state"], cur["implementer_declared"], cur["implementer_source"]) != \
            ("already_executed", True, "caller_declared"):
        out.append("the worker seat is not reported as already executed")
    if not cur["terminal"]:
        if cur["selected_model"] != declared:
            out.append(f"selected_model {cur['selected_model']} is not the declared implementer")
        # 1.16.1's LOW review is the worker re-reading its own work by design
        # (`independent: false`), so there the declared implementer is that
        # reviewer; every independent band must keep it out of every seat.
        if (cur["review"]["independence_required"]
                and declared in {s["model_id"] for s in cur["dispatch_seats"]}):
            out.append("the declared implementer is a dispatch seat")
    if (not prev["terminal"] and TIER_OF[declared] < TIER_OF[prev["selected_model"]]
            and "implementer_below_worker_tier" not in cur["human_control_causes"]):
        out.append("an implementer below the 1.16.1 worker tier is not gated")
    return out


def _snapshot_band_once(req) -> tuple[str, str]:
    """(risk band, risk band with uncertainty weighted once), both after the
    overrides — computed by the 1.16.1 snapshot, so the predicate does not
    lean on the code it judges."""
    mod = load_baseline_1161()
    cfg = baseline_1161_cfg()
    policy = mod.Policy.of(cfg)
    task = mod.task_from_request_v1(project(req))
    task.validate(policy)
    score = mod.score(task, cfg)
    once = score - task.uncertainty * (cfg["router"]["score_weights"]["uncertainty"] - 1)
    band = mod.apply_overrides(task, mod.band_from_score(score, policy), policy)[0]
    return band, mod.apply_overrides(task, mod.band_from_score(once, policy), policy)[0]


def _c1iii_admits(req, prev):
    if "error" in prev or not any(o.startswith("low_routing_confidence_raised_review_to_")
                                  for o in prev["band_overrides_applied"]):
        return False
    band, once = _snapshot_band_once(req)
    return BANDS.index(band) > BANDS.index(once)


def _c1iii_check(req, prev, cur):
    out = []
    if not cur["terminal"] and not prev["terminal"]:
        drop = BANDS.index(prev["review"]["band"]) - BANDS.index(cur["review"]["band"])
        if drop not in (0, 1):
            out.append(f"review band moved {prev['review']['band']} -> {cur['review']['band']}")
        # A REVIEW task's lead is a review seat sized by the review band
        # (a source review's lead rises to the band's floor), so it moves with
        # the band. Every other worker keeps its tier (DD-B11 invariant 4).
        if (req["task_class"] != "REVIEW"
                and TIER_OF[cur["selected_model"]] < TIER_OF[prev["selected_model"]]):
            out.append(f"worker tier fell {prev['selected_model']} -> {cur['selected_model']}")
        if req["task_class"] != "REVIEW" and cur["selected_model"] != prev["selected_model"]:
            out.append(f"worker moved {prev['selected_model']} -> {cur['selected_model']}")
    return out


def _c4_check(req, prev, cur):
    """At or above max(MEDIUM floor, implementer tier) when any candidate
    reaches it, and never above what 1.16.1 seated unless that was below it."""
    if cur["terminal"] or prev["terminal"] or cur["review"]["band"] != "MEDIUM":
        return []
    [now] = cur["review"]["reviewer_models"] or [None]
    [was] = prev["review"]["reviewer_models"] or [None]
    if now is None or was is None:
        return []
    need = max(1, TIER_OF[cur["selected_model"]])
    out = []
    if TIER_OF[cur["selected_model"]] < TIER_OF[prev["selected_model"]]:
        out.append(f"worker tier fell {prev['selected_model']} -> {cur['selected_model']}")
    if cur["selected_model"] == prev["selected_model"] and TIER_OF[now] > max(TIER_OF[was], need):
        out.append(f"reviewer overshoots: {was} -> {now} (requirement {need})")
    if TIER_OF[now] < need and "review_below_band" not in cur["human_control_causes"]:
        out.append(f"reviewer {now} below requirement {need} without the shortfall gate")
    return out


def _c2_admits(req, prev):
    return ("error" not in prev and prev["risk_band"] == "LOW"
            and "unknown_root_cause" not in req["flags"]
            and not req.get("prior_failures") and not req.get("attempt_outcomes"))


def _c2_check(req, prev, cur):
    if cur["terminal"] or prev["terminal"]:
        return []
    out = []
    if EFFORTS.index(cur["selected_effort"]) > EFFORTS.index(prev["selected_effort"]):
        out.append(f"effort rose {prev['selected_effort']} -> {cur['selected_effort']}")
    if cur["selected_model"] != prev["selected_model"]:
        out.append("the worker moved")
    return out


def _c3_check(req, prev, cur):
    out = []
    rv = cur["review"]
    if rv["band"] == "LOW" and req["task_class"] != "REVIEW":
        if (rv["reviewers"], rv["mode"], rv["required_checks"]) != ([], "deterministic_checks",
                                                                    ["tests", "lint"]):
            out.append(f"a LOW review is not the deterministic checks: {rv}")
    elif rv["mode"] == "deterministic_checks" and rv["band"] != "LOW":
        out.append(f"a {rv['band']} review advertises deterministic checks")
    if BANDS.index(rv["band"]) < BANDS.index(prev["review"]["band"]) and not (
            prev["review"]["band"] != "LOW" and rv["band"] == "LOW"
            and any(o.startswith("low_routing_confidence") for o in prev["band_overrides_applied"])):
        out.append(f"review band fell {prev['review']['band']} -> {rv['band']}")
    if (not cur["terminal"] and not prev["terminal"] and req["task_class"] != "REVIEW"
            and TIER_OF[cur["selected_model"]] < TIER_OF[prev["selected_model"]]):
        out.append(f"worker tier fell {prev['selected_model']} -> {cur['selected_model']}")
    return out


OPERATIONAL = {"transport_failure", "launch_failure", "resolution_failure", "timeout",
               "max_turns_partial", "no_artifact", "invalid_output", "authentication_failure",
               "quota_exhausted", "publication_failure", "cancelled", "unknown"}


def _c5_target(req, on: frozenset | None = None):
    """DD-B6's four conditions from the request and a rule state — by default
    the replay state before C5, for the ledger step; invariant 4 passes the
    shipped one, whose failure-free plan (a REVIEW lead under `review_lead`,
    say) is what the router retries from (review i2) — (model, effort) when
    all hold, else None. Written out here, not taken from the router."""
    history = req.get("attempt_outcomes")
    if not history or "implementer" in req or "termination_unconfirmed" in req["flags"]:
        return None
    if any(r["kind"] == "termination_unconfirmed"
           or (r["kind"] in OPERATIONAL and not r.get("recovery_sha256")) for r in history):
        return None
    if len(history) >= CFG["retry"]["max_total_implementation_attempts"]:
        return None
    failures = [r for r in history if r["kind"] == "capability_failure"]
    if len({r["model_id"] for r in failures}) != 1:
        return None
    model = failures[0]["model_id"]
    if len(failures) > CFG["retry"]["same_model_higher_effort"]:
        return None
    last = failures[-1]
    if "effort" not in last or (CFG["retry"]["require_new_evidence_on_same_tier"]
                                and "retry_evidence_sha256" not in last):
        return None
    if on is None:
        on = frozenset(r for r in ORDER[:ORDER.index("c5")] if r in RULES)
    free = route_live({k: v for k, v in req.items() if k != "attempt_outcomes"}, on)
    if "error" in free or free["terminal"] or free["selected_model"] != model:
        return None
    ran = [EFFORTS.index(r["effort"]) for r in history if r["model_id"] == model and "effort" in r]
    assigned = EFFORTS.index(free["selected_effort_effective"])
    for seat in free.get("dispatch_seats") or []:
        if seat["model_id"] == model and seat["effort"] is not None:
            assigned = max(assigned, EFFORTS.index(seat["effort"]))
    above = max(ran + [assigned]) + 1
    ceiling = CFG["models"][next(k for k, m in CFG["models"].items() if m["id"] == model)].get(
        "effort_ceiling")
    if above >= len(EFFORTS) or (ceiling and above > EFFORTS.index(ceiling)):
        return None
    return model, EFFORTS[above]


def _c5_check(req, prev, cur):
    target = _c5_target(req)
    if target is None or cur.get("terminal"):
        return []
    if cur == prev:
        return []        # the settled plan could not keep the model: the ladder, unchanged
    model, effort = target
    out = []
    if cur["selected_model"] != model:
        out.append(f"the retry seats {cur['selected_model']}, not the failed {model}")
    if EFFORTS.index(cur["selected_effort"]) < EFFORTS.index(effort):
        out.append(f"retry effort {cur['selected_effort']} below {effort}")
    if model in cur["excluded_prior_failures"]:
        out.append("the retried model is still listed as excluded")
    if model in cur["review"]["reviewer_models"]:
        out.append("the retried model reviews itself")
    return out


REVIEW_SEATS_BY_BAND = {"LOW": 1, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 2}      # DD-B7's matrix


def _review_lead_check(req, prev, cur):
    if cur.get("terminal") or "error" in cur:
        return []
    rv = cur["review"]
    seats = [s for s in cur["dispatch_seats"] if s["seat"].startswith("reviewer")]
    judge = [s for s in cur["dispatch_seats"] if s["seat"] == "judge"]
    out = []
    if [s["model_id"] for s in seats] != rv["reviewer_models"]:
        out.append("dispatch_seats and reviewer_models disagree")
    if rv["reviewer_models"].count(cur["selected_model"]) != 1 or seats[0]["model_id"] != cur["selected_model"]:
        out.append("the lead is not reviewer-1, exactly once")
    if len(seats) != REVIEW_SEATS_BY_BAND[rv["band"]] + rv["compensating_reviewers"]:
        out.append(f"{rv['band']} REVIEW seats {len(seats)} reviewers")
    if judge and judge[0]["model_id"] in rv["reviewer_models"]:
        out.append("the judge is a party")
    return out


def _c7_check(req, prev, cur):
    if "error" in cur or cur["terminal"]:
        return []
    quota = req["availability_snapshot"]["family_quota"]
    exhausted = {f for f, level in quota.items() if level == "exhausted"}
    declared = (req.get("implementer") or {}).get("model_id")
    seated = [m for m in (cur["selected_model"], *cur["review"]["reviewer_models"],
                          cur["review"]["judge_model"]) if m and m != declared]
    out = [f"{m} seated from an exhausted family" for m in seated if FAMILY_OF[m] in exhausted]
    low = {f for f, level in quota.items() if level == "low"}
    if (not exhausted and cur["selected_model"] != prev.get("selected_model")
            and TIER_OF[cur["selected_model"]] != TIER_OF[prev["selected_model"]]
            and FAMILY_OF[prev["selected_model"]] in low):
        out.append("`low` moved the worker off its tier")
    return out


# The ledger, in ORDER (plan B0 Step 3). A rule task adds its row.
RULES: dict[str, Rule] = {
    "implementer_declared": Rule(
        "implementer_declared",
        predicate=lambda req, prev: "implementer" in req,
        # Widened from the B0 table by `selected_role` (ledger B1): with the
        # worker already run there is no execution-cell plan to weigh against
        # the review, so the role is the policy's execution-combined choice
        # where 1.16.1 may have yielded to the table cell.
        fields=(frozenset({"selected_role", "selected_model", "selected_capability_tier",
                           "worker_seat_state", "implementer_declared", "implementer_source",
                           "dispatch_seats"})
                | EFFORT | REVIEW_SEATS | GATES | SEAT_RECORDS),
        check=_implementer_check,
        changed_sample=486, changed_full=5184),
    # One band lower on the inputs it admits, and everything that follows from
    # the band: the seats, their efforts and records, the judge, the gates,
    # and the worker EFFORT a reviewer-fallback compensation drags along. Never
    # the worker's model (the adopted plan is chosen as before, DD-B2's guard).
    "c1iii": Rule(
        "c1iii", predicate=_c1iii_admits,
        # `selected_model`/`selected_capability_tier` for a REVIEW lead only
        # (ledger B2); the check refuses any other worker move.
        fields=(frozenset({"review.band", "review.effort", "review.required_checks",
                           "review.independence_required", "review.review_independence",
                           "band_overrides_applied", "dispatch_seats", "terminal",
                           "selected_model", "selected_capability_tier"})
                | EFFORT | REVIEW_SEATS | GATES | SEAT_RECORDS),
        check=_c1iii_check, changed_sample=891, changed_full=7791),
    # The settled MEDIUM reviewer and what follows from it: the seat, its
    # records, cross-family, the (implementer-inclusive) shortfall gate, and a
    # compensation's effort. The band is not declared: a reviewer choice made
    # inside a MEDIUM pass must not move it. Widened by the worker (ledger B3):
    # the execution-cell guard weighs the two plans' reviews, so a stronger
    # worker can now be adopted where an adequate reviewer exists for it — up
    # only, which the check holds.
    "c4": Rule(
        "c4", predicate=lambda req, prev: prev.get("review", {}).get("band") == "MEDIUM",
        fields=(frozenset({"dispatch_seats", "selected_role", "selected_model",
                           "selected_capability_tier"})
                | EFFORT | REVIEW_SEATS | GATES | SEAT_RECORDS),
        check=_c4_check, changed_sample=619, changed_full=8911),
    # The class table's effort for a LOW-risk task, capped at MEDIUM before
    # the floors: the worker's effort and nothing that does not read it.
    # `dispatch_seats`: a REVIEW task's lead is dispatched at the higher of
    # its own effort and the review's.
    "c2": Rule(
        "c2", predicate=_c2_admits,
        fields=EFFORT | frozenset({"effort_ceiling_applied", "dispatch_seats"}),
        check=_c2_check, changed_sample=459, changed_full=3132),
    # Every LOW-risk route: the review is the checks, or — escaped — a model
    # review at the lowest band that carries what the checks cannot. A route
    # 1.16.1 promoted off LOW may settle back on LOW: the promotion read the
    # fallback penalty of a LOW reviewer seat that no longer exists.
    "c3": Rule(
        "c3", predicate=lambda req, prev: "error" not in prev and prev["risk_band"] == "LOW",
        fields=(frozenset({"review.band", "review.effort", "review.required_checks",
                           "review.independence_required", "review.review_independence",
                           "review.mode", "band_overrides_applied", "dispatch_seats", "terminal",
                           "selected_role", "selected_model", "selected_capability_tier"})
                | EFFORT | REVIEW_SEATS | GATES | SEAT_RECORDS),
        check=_c3_check, changed_sample=1107, changed_full=6744),
    # The four conditions, all of them (plan B0 Step 3): the ladder's step up
    # is undone — role and model back to the failure-free plan's, the effort
    # one above every effort that failed — and what follows from the worker:
    # its de-conflicted reviewers, fallbacks, exclusions and gates. The worker
    # tier may fall below 1.16.1's here and only here (DD-B11 invariant 4).
    "c5": Rule(
        "c5", predicate=lambda req, prev: _c5_target(req) is not None,
        fields=(frozenset({"selected_role", "selected_model", "selected_capability_tier",
                           "excluded_prior_failures", "dispatch_seats", "review.band",
                           "review.effort", "review.required_checks", "review.mode",
                           "review.independence_required", "review.review_independence",
                           "band_overrides_applied", "terminal"})
                | EFFORT | REVIEW_SEATS | GATES | SEAT_RECORDS),
        check=_c5_check, changed_sample=302, changed_full=7468),
    # Every REVIEW route: the lead joins the reviewers (reviewer-1), the band
    # seats the matrix's count including it, `dispatch_seats` is the one list,
    # and a caller floor the smaller matrix cannot carry leaves the band.
    "review_lead": Rule(
        "review_lead", predicate=lambda req, prev: req["task_class"] == "REVIEW",
        # `excluded_prior_failures` (ledger B6): C5 reads the failure-free
        # plan, whose REVIEW lead this rule re-seats, so whether a REVIEW
        # retry stays on the failed model can follow.
        fields=(frozenset({"selected_role", "selected_model", "selected_capability_tier",
                           "dispatch_seats", "review.band", "review.effort",
                           "review.required_checks", "review.mode",
                           "review.independence_required", "review.review_independence",
                           "band_overrides_applied", "terminal", "excluded_prior_failures"})
                | EFFORT | REVIEW_SEATS | GATES | SEAT_RECORDS),
        check=_review_lead_check, changed_sample=426, changed_full=9024),
    # A caller's quota reading: `exhausted` withholds a family from every seat
    # (so anything can follow, a terminal included); `low` moves the worker
    # seat to a same-tier model of another family, and the reviewers
    # de-conflicted against that worker follow.
    "c7": Rule(
        "c7", predicate=lambda req, prev: "family_quota" in (req.get("availability_snapshot") or {}),
        fields=(frozenset({"selected_role", "selected_model", "selected_capability_tier",
                           "excluded_prior_failures", "dispatch_seats", "review.band",
                           "review.effort", "review.required_checks", "review.mode",
                           "review.independence_required", "review.review_independence",
                           "band_overrides_applied", "terminal"})
                | EFFORT | REVIEW_SEATS | GATES | SEAT_RECORDS),
        check=_c7_check, changed_sample=212, changed_full=2288),
}


# --------------------------------------------------------------------------
# Rule switches
# --------------------------------------------------------------------------

# Request fields 1.16.1 refuses, by owning rule. Projection drops all of them;
# a rule that is off drops its own.
def _drop_implementer(req):
    req.pop("implementer", None)


def _drop_attempt_effort(req):
    for row in req.get("attempt_outcomes") or []:
        row.pop("effort", None)
        row.pop("retry_evidence_sha256", None)


def _drop_family_quota(req):
    (req.get("availability_snapshot") or {}).pop("family_quota", None)


def _drop_checks_available(req):
    (req.get("availability_snapshot") or {}).pop("checks_available", None)


REQUEST_SWITCHES: dict[str, Callable[[dict], None]] = {
    "implementer_declared": _drop_implementer,
    "c3": _drop_checks_available,
    "c5": _drop_attempt_effort,
    "c7": _drop_family_quota,
}

# rule -> function(cfg) that restores the pre-rule config value in place.
CONFIG_SWITCHES: dict[str, Callable[[dict], None]] = {
    "c1iii": lambda cfg: cfg["router"]["confidence"].__setitem__(
        "skip_uncertainty_penalty_when_band_raised", False),
    "c2": lambda cfg: cfg.__setitem__("effort_caps", {}),
    "c3": lambda cfg: cfg["review"].__setitem__(
        "LOW", copy.deepcopy(baseline_1161_cfg()["review"]["LOW"])),
    "review_lead": lambda cfg: cfg["review"].__setitem__("review_class_lead_counts", False),
}

def _c4_off(stack: ExitStack) -> None:
    """1.16.1's MEDIUM reviewer: the per-implementer preference first, then
    the first cross-family candidate, then the first available — read from
    the SNAPSHOT's config, the only place that table still exists — and a
    shortfall measured against the band floor alone."""
    from unittest.mock import patch
    medium = baseline_1161_cfg()["review"]["MEDIUM"]

    def legacy(spec, worker, policy, resolver):
        worker_family = resolver.family_for_role(worker, write=True)
        preferred = medium["preferred_by_implementer"].get(worker)
        ranked = list(dict.fromkeys(c for c in ([preferred] if preferred else [])
                                    + list(medium["candidates"]) if c))
        for candidate in ranked:
            model = resolver.peek(candidate)
            if model and policy.family_of[model] != worker_family:
                return candidate
        for candidate in ranked:
            if resolver.peek(candidate):
                return candidate
        return ranked[0]

    stack.enter_context(patch.object(rt, "_seat_medium_reviewer", legacy))
    stack.enter_context(patch.object(
        rt, "_medium_reviewer_floor",
        lambda policy, band, worker_model: policy.band_reviewer_floor[band]))


# rule -> function(ExitStack) that installs the pre-rule behaviour.
PATCH_SWITCHES: dict[str, Callable[[ExitStack], None]] = {"c4": _c4_off}


def project(req: dict) -> dict:
    """The request 1.16.1 would have been sent (design DD-B11 "투영")."""
    out = copy.deepcopy(req)
    for drop in REQUEST_SWITCHES.values():
        drop(out)
    if out.get("availability_snapshot") == {}:
        out.pop("availability_snapshot")
    return out


_CFG_BY_RULES: dict[frozenset, dict] = {}


def config_for(on: frozenset) -> dict:
    key = frozenset(r for r in on if r in CONFIG_SWITCHES)
    if key not in _CFG_BY_RULES:
        cfg = copy.deepcopy(CFG)
        for name, restore in CONFIG_SWITCHES.items():
            if name not in on:
                restore(cfg)
        _CFG_BY_RULES[key] = cfg
    return _CFG_BY_RULES[key]


def request_for(req: dict, on: frozenset) -> dict:
    out = copy.deepcopy(req)
    for name, drop in REQUEST_SWITCHES.items():
        if name not in on:
            drop(out)
    if out.get("availability_snapshot") == {}:
        out.pop("availability_snapshot")
    return out


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

def exit_of(out: dict) -> int:
    if out["terminal"]:
        return 1
    if out["requires_human_confirmation"]:
        return 3
    return 4 if out["human_confirmation_deferred"] else 0


def decision(out: dict) -> dict:
    rec = {k: copy.deepcopy(v) for k, v in out.items() if k not in NOT_DECISIONS}
    rec["exit"] = exit_of(out)
    return rec


def route_live(req: dict, on: frozenset) -> dict:
    with ExitStack() as stack:
        for name, install in PATCH_SWITCHES.items():
            if name not in on:
                install(stack)
        try:
            return decision(rt.route(rt.task_from_request_v1(request_for(req, on)), config_for(on)))
        except rt.ValidationError as exc:
            return {"exit": 2, "error": str(exc)}


def route_snapshot(req: dict) -> dict:
    mod = load_baseline_1161()
    try:
        return decision(mod.route(mod.task_from_request_v1(project(req)), baseline_1161_cfg()))
    except mod.ValidationError as exc:
        return {"exit": 2, "error": str(exc)}


def strip_new_keys(rec: dict) -> tuple[dict, list[str]]:
    """(record without Part B's added keys, keys that held a non-default)."""
    rec = copy.deepcopy(rec)
    wrong = []
    for key, default in NEW_KEY_DEFAULTS.items():
        if key in rec and rec.pop(key) != default:
            wrong.append(key)
    review = rec.get("review")
    if isinstance(review, dict):
        for key, default in NEW_REVIEW_KEY_DEFAULTS.items():
            if key in review and review.pop(key) != default:
                wrong.append(f"review.{key}")
    return rec, wrong


def changed_fields(a: dict, b: dict) -> set[str]:
    out = set()
    for key in set(a) | set(b):
        if key == "review" and isinstance(a.get(key), dict) and isinstance(b.get(key), dict):
            out |= {f"review.{k}" for k in set(a[key]) | set(b[key])
                    if a[key].get(k) != b[key].get(k)}
        elif a.get(key) != b.get(key):
            out.add(key)
    return out


# What a terminal route withholds (every execution binding). When a rule may
# move `terminal`, the withholding that follows is part of that move, not a
# worker or seat change of its own.
WITHHELD = frozenset({"selected_role", "selected_model", "selected_effort",
                      "selected_effort_effective", "selected_effort_native",
                      "selected_capability_tier", "selected_families", "review.reviewer_models",
                      "review.effort", "review.judge_model", "review.review_depth_reduced",
                      "dispatch_seats", "fallbacks_applied", "effort_ceiling_applied"})


_INDEPENDENCE_ORDER = {"not_applicable": -1, "unavailable": 0, "degraded": 1, "planned": 2,
                       "enforced": 3}


def _seat_efforts(route_out: dict) -> list[int]:
    """Each review seat's effective effort (a ceiling record wins), strongest
    first. A REVIEW task's lead — reviewer-1 of a source review, dispatched
    by `dispatch_seats` — runs at the higher of the review's effort and its
    own, and at its own where the band has none (review i2)."""
    rv = route_out["review"]
    capped = {r["role"]: r["capped_at"] for r in route_out["effort_ceiling_applied"]}
    lead = (route_out.get("worker_seat_state") == "to_dispatch"
            and bool(route_out.get("dispatch_seats"))
            and route_out["selected_model"] in rv["reviewer_models"])
    out = []
    for role, model in zip(rv["reviewers"], rv["reviewer_models"]):
        level = capped.get(role, rv["effort"])
        if lead and model == route_out["selected_model"]:
            own = route_out["selected_effort_effective"]
            level = own if level is None else max(level, own, key=EFFORTS.index)
        if level is not None:
            out.append(EFFORTS.index(level))
    return sorted(out, reverse=True)


def _worker_floor_break(route_out: dict) -> dict | None:
    return next((r for r in route_out["effort_ceiling_applied"]
                 if r["floor_broken"] and r["role"] == route_out["selected_role"]), None)


def _worker_floor_worse(prev: dict, cur: dict) -> bool:
    """A newly broken worker floor, or one broken further — a higher floor or
    a lower cap — as `_contract_violation` reads it (review i2)."""
    cw = _worker_floor_break(cur)
    if cw is None:
        return False
    pw = _worker_floor_break(prev)
    return (pw is None or EFFORTS.index(cw["floor_requires"]) > EFFORTS.index(pw["floor_requires"])
            or EFFORTS.index(cw["capped_at"]) < EFFORTS.index(pw["capped_at"]))


def weaker(prev: dict, cur: dict) -> str | None:
    """The first way `cur` is weaker than `prev`, or None. DD-B11's "weaker"
    covers every `_contract_violation` row plus the worker tier and is not
    narrowed — but it is read in its safety DIRECTION here: that guard treats
    any difference as a violation (a stronger review is a contract change it
    will not adopt), and counting a raised band or an added reviewer as
    "weaker" would fill the CHANGELOG with routes that got stronger. A gate
    that fires where it did not is counted apart, by `gates_added`; a gate
    that no longer fires is weaker whatever the band did (review i1)."""
    if "error" in prev or "error" in cur or prev["terminal"]:
        return None
    if cur["terminal"]:
        return "terminal"
    if set(prev["human_control_causes"]) - set(cur["human_control_causes"]):
        return "gate_lost"
    if TIER_OF[cur["selected_model"]] < TIER_OF[prev["selected_model"]]:
        return "worker_tier"
    if _worker_floor_worse(prev, cur):
        return "worker_floor_broken"
    pr, cr = prev["review"], cur["review"]
    if BANDS.index(cr["band"]) < BANDS.index(pr["band"]):
        return "review.band"
    if BANDS.index(cr["band"]) > BANDS.index(pr["band"]):
        return None                           # a larger review by the band's own contract
    if len(cr["reviewers"]) < len(pr["reviewers"]):
        return "reviewer_count"
    if set(pr["required_checks"]) - set(cr["required_checks"]):
        return "required_checks"
    if pr["effort"] is not None and (cr["effort"] is None
                                     or EFFORTS.index(cr["effort"]) < EFFORTS.index(pr["effort"])):
        return "review.effort"
    if pr["independence_required"] and not cr["independence_required"]:
        return "independence_required"
    if _INDEPENDENCE_ORDER[cr["review_independence"]] < _INDEPENDENCE_ORDER[pr["review_independence"]]:
        return "review_independence"
    for flag in ("independence_compromised", "band_floor_unsatisfiable", "judge_unavailable"):
        if cr[flag] and not pr[flag]:
            return "review.flags"
    if pr["judge_model"] and (not cr["judge_model"]
                              or TIER_OF[cr["judge_model"]] < TIER_OF[pr["judge_model"]]):
        return "judge_tier"
    if prev["cross_family_review"] and not cur["cross_family_review"]:
        return "cross_family_review"
    pt = sorted((TIER_OF[m] for m in pr["reviewer_models"] if m), reverse=True)
    ct = sorted((TIER_OF[m] for m in cr["reviewer_models"] if m), reverse=True)
    if any(c < p for c, p in zip(ct, pt)):
        return "reviewer_tiers"
    if any(c < p for c, p in zip(_seat_efforts(cur), _seat_efforts(prev))):
        return "reviewer_efforts"
    if len(cr["review_depth_reduced"]) > len(pr["review_depth_reduced"]):
        return "review_depth_reduced"
    return None


def gates_added(prev: dict, cur: dict) -> list[str]:
    if "error" in prev or "error" in cur:
        return []
    return sorted(set(cur["human_control_causes"]) - set(prev["human_control_causes"]))


# --------------------------------------------------------------------------
# Grid
# --------------------------------------------------------------------------

RUNTIMES = sorted(CFG["runtimes"])
CLASSES = list(CFG["worker_selection"])
DIMS = list(itertools.product(range(4), repeat=4))
WRITE_CLASSES = [c for c in CLASSES if CFG["task_write_seat"][c] == "write"]
# A fixed stratified sample for the default run: the band corners and the
# uncertainty/blast points the rules key on, plus a seeded spread.
SAMPLE_DIMS = sorted(set([(0, 0, 0, 0), (1, 1, 0, 0), (0, 3, 0, 0), (2, 1, 1, 1), (3, 2, 0, 0),
                          (2, 2, 1, 1), (3, 3, 0, 1), (2, 2, 2, 2)]
                         + random.Random(20260928).sample(DIMS, 2)))
SAMPLE_EXTRA_DIMS = [(0, 0, 0, 0), (0, 3, 0, 0), (2, 1, 1, 1), (3, 2, 0, 0), (2, 2, 1, 1),
                     (2, 2, 2, 2)]
EXTRA_DIMS = DIMS[::4]
SHA_EVIDENCE, SHA_RETRY = "1" * 64, "2" * 64

# The design §6 variants: (name, flags, reasoning_centric).
CORE_VARIANTS = [("none", [], False), ("security_sensitive", ["security_sensitive"], False),
                 ("unknown_root_cause", ["unknown_root_cause"], False),
                 ("production_hotfix", ["production_hotfix"], False),
                 ("large_context", ["large_context"], False),
                 ("latency_sensitive", ["latency_sensitive"], False),
                 ("bridge_down", ["bridge_down"], False),
                 ("review_disagreement", ["review_disagreement"], False),
                 ("reasoning_centric", [], True)]


def _base(runtime, cls, dims, flags=(), rc=False):
    c, u, b, r = dims
    return {"route_schema_version": 1, "task_class": cls, "complexity": c, "uncertainty": u,
            "blast_radius": b, "reversibility": r, "runtime": runtime,
            "flags": list(flags), "reasoning_centric": rc}


# Rule-specific variants: (name, owning rule or None, classes, builder(req) -> req).
def _implementer(key):
    return lambda req: {**req, "implementer": {"model_id": ID(key)}}


def _snapshot(**avail):
    return lambda req: {**req, "availability_snapshot": dict(avail)}


def _local_policy(**lp):
    return lambda req: {**req, "local_policy": dict(lp)}


EXTRA_VARIANTS = [
    ("implementer_senior", "implementer_declared", WRITE_CLASSES, _implementer("claude_senior")),
    ("implementer_senior_openai_xai", "implementer_declared", WRITE_CLASSES,
     lambda req: _implementer("claude_senior")({**req, "local_policy": {"allowed_families": ["openai", "xai"]}})),
    ("implementer_fast", "implementer_declared", WRITE_CLASSES, _implementer("openai_worker_fast")),
    ("checks_unavailable", "c3", CLASSES, _snapshot(checks_available=False)),
    ("quota_openai_low", "c7", CLASSES, _snapshot(family_quota={"openai": "low"})),
    ("quota_openai_exhausted", "c7", CLASSES, _snapshot(family_quota={"openai": "exhausted"})),
    ("min_reviewers_1", None, CLASSES, _local_policy(minimum_reviewers=1)),
    ("min_reviewers_2", None, CLASSES, _local_policy(minimum_reviewers=2)),
    ("min_families_2", None, CLASSES, _local_policy(minimum_provider_families=2)),
    ("allowed_openai_xai", None, CLASSES, _local_policy(allowed_families=["openai", "xai"])),
    ("review_context_senior", None, ["REVIEW"],
     lambda req: {**req, "review_context": {"target_sha256": "3" * 64,
                                            "author_model_ids": [ID("claude_senior")],
                                            "author_families": []}}),
]


def _prior_failure(req: dict) -> dict | None:
    """One capability failure on the model the history-free 1.16.1 route
    seated, at the effort it ran — the retry the C5 rule is about. The
    effort and fresh-evidence fields exist only once C5 does."""
    free = route_snapshot(req)
    if "error" in free or free["terminal"] or not free["selected_model"]:
        return None
    row = {"attempt_id": "a1", "model_id": free["selected_model"], "kind": "capability_failure",
           "evidence_sha256": SHA_EVIDENCE}
    if "c5" in RULES:
        row.update(effort=free["selected_effort_effective"], retry_evidence_sha256=SHA_RETRY)
    return {**req, "attempt_outcomes": [row]}


def grid(full: bool) -> list[tuple[str, dict]]:
    core_dims = DIMS if full else SAMPLE_DIMS
    extra_dims = EXTRA_DIMS if full else SAMPLE_EXTRA_DIMS
    out = []
    for runtime, cls, dims in itertools.product(RUNTIMES, CLASSES, core_dims):
        tag = f"{runtime}/{cls}/{''.join(map(str, dims))}"
        for name, flags, rc in CORE_VARIANTS:
            out.append((f"{tag}/{name}", _base(runtime, cls, dims, flags, rc)))
        if (req := _prior_failure(_base(runtime, cls, dims))) is not None:
            out.append((f"{tag}/prior_failure", req))
    for runtime, cls, dims in itertools.product(RUNTIMES, CLASSES, extra_dims):
        tag = f"{runtime}/{cls}/{''.join(map(str, dims))}"
        for name, owner, classes, build in EXTRA_VARIANTS:
            if cls in classes and (owner is None or owner in RULES):
                out.append((f"{tag}/{name}", build(_base(runtime, cls, dims))))
    return out


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------

@dataclass
class Ledger:
    inputs: int = 0
    problems: list = field(default_factory=list)
    changed: Counter = field(default_factory=Counter)
    admitted: Counter = field(default_factory=Counter)
    weakened: dict = field(default_factory=dict)          # rule -> Counter(reason)
    gates: dict = field(default_factory=dict)             # rule -> Counter(cause added)

    def fail(self, name, text):
        if len(self.problems) < 40:
            self.problems.append(f"{name}: {text}")
        else:
            self.problems.append("...")


def replay(full: bool) -> Ledger:
    ledger = Ledger(weakened={r: Counter() for r in ORDER}, gates={r: Counter() for r in ORDER})
    for name, req in grid(full):
        ledger.inputs += 1
        base = route_snapshot(req)
        on: frozenset = frozenset()
        prev = route_live(req, on)
        stripped, wrong = strip_new_keys(prev)
        if wrong:
            ledger.fail(name, f"with every rule off, Part B keys hold non-defaults: {wrong}")
        if stripped != base:
            ledger.fail(name, f"with every rule off the route differs from 1.16.1 in "
                              f"{sorted(changed_fields(base, stripped))}")
            continue
        for rule_name in ORDER:
            rule = RULES.get(rule_name)
            if rule is None:
                continue
            on = on | {rule_name}
            cur = route_live(req, on)
            moved = changed_fields(prev, cur)
            if "terminal" in rule.fields and prev.get("terminal") != cur.get("terminal"):
                moved -= WITHHELD
            if not rule.predicate(req, prev):
                if moved:
                    ledger.fail(name, f"{rule_name} is not admitted here but moved {sorted(moved)}")
            else:
                ledger.admitted[rule_name] += 1
                if moved - rule.fields:
                    ledger.fail(name, f"{rule_name} moved undeclared {sorted(moved - rule.fields)}")
                for problem in rule.check(req, prev, cur):
                    ledger.fail(name, f"{rule_name}: {problem}")
                if moved:
                    ledger.changed[rule_name] += 1
                    if (why := weaker(prev, cur)) is not None:
                        ledger.weakened[rule_name][why] += 1
                    for cause in gates_added(prev, cur):
                        ledger.gates[rule_name][cause] += 1
            prev = cur
    return ledger


_LEDGER: dict[bool, Ledger] = {}


def ledger() -> Ledger:
    if FULL not in _LEDGER:
        _LEDGER[FULL] = replay(FULL)
    return _LEDGER[FULL]


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_rule_vocabulary_is_closed_and_in_replay_order():
    assert set(RULES) <= set(ORDER), set(RULES) - set(ORDER)
    assert list(RULES) == [r for r in ORDER if r in RULES]
    for name, rule in RULES.items():
        assert rule.name == name
        switched = (name in REQUEST_SWITCHES) + (name in CONFIG_SWITCHES) + (name in PATCH_SWITCHES)
        assert switched, f"{name} has no way to be switched off"


def test_every_rule_on_is_the_shipped_config():
    """The replay ends at the route users get: all rules on is `CFG` itself,
    not a hand-built config that happens to agree."""
    assert config_for(frozenset(ORDER)) == CFG


def test_the_grid_reaches_every_band_and_class():
    rows = grid(False)
    classes = {req["task_class"] for _, req in rows}
    assert classes == set(CLASSES)
    bands = Counter(route_snapshot(req)["review"]["band"] for _, req in rows[::7]
                    if "error" not in route_snapshot(req))
    assert set(bands) == set(CFG["router"]["bands"]), bands


def test_every_decision_change_since_1161_has_a_named_rule():
    led = ledger()
    assert not led.problems, "\n".join(led.problems)
    assert led.inputs > (80_000 if FULL else 3_000), led.inputs


def test_rule_counts_are_pinned():
    led = ledger()
    for name, rule in RULES.items():
        want = rule.changed_full if FULL else rule.changed_sample
        assert led.changed[name] == want, (name, led.changed[name], want)


# --------------------------------------------------------------------------
# Ledger printout (plan B0 Step 4): python3 test_policy_rules.py [--full]
# --------------------------------------------------------------------------

def _print(full: bool) -> None:
    led = replay(full)
    print(f"grid: {'full' if full else 'sample'}; inputs {led.inputs}; problems {len(led.problems)}")
    for line in led.problems[:20]:
        print("  !", line)
    print("| rule | admitted | changed | weaker (first row) | gates added |")
    print("|---|---|---|---|---|")
    for name in ORDER:
        if name in RULES:
            weak = ", ".join(f"{k} {v}" for k, v in sorted(led.weakened[name].items())) or "0"
            gate = ", ".join(f"{k} {v}" for k, v in sorted(led.gates[name].items())) or "0"
            print(f"| `{name}` | {led.admitted[name]} | {led.changed[name]} | {weak} | {gate} |")


if __name__ == "__main__":
    _print("--full" in sys.argv)


# --------------------------------------------------------------------------
# DD-B11 invariants 1, 2 and 4 on the shipped policy (plan B9)
# --------------------------------------------------------------------------

_FINALS: dict[bool, list] = {}


def finals() -> list[tuple[str, dict, dict]]:
    """(name, request, route with every rule on) over the grid, once."""
    if FULL not in _FINALS:
        _FINALS[FULL] = [(name, req, route_live(req, frozenset(ORDER))) for name, req in grid(FULL)]
    return _FINALS[FULL]


BAND_FLOOR = {"LOW": None, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 2}          # written out


def band_contract(req: dict, out: dict) -> list[str]:
    """Invariant 1: what each band owes, written out from design DD-B11 ①
    and the DD-B7 seat matrix, not recomputed from the router."""
    rv, band = out["review"], out["review"]["band"]
    review_task = req["task_class"] == "REVIEW"
    extra = rv["compensating_reviewers"]
    reviewers = rv["reviewer_models"]
    causes = set(out["human_control_causes"])
    gated = out["requires_human_confirmation"] or out["human_confirmation_deferred"]
    problems = []

    def disclosed(what):
        if "review_below_band" not in causes or not gated:
            problems.append(f"{band}: {what} without the review_below_band gate")

    if band == "LOW":
        if review_task:
            if len(reviewers) != 1 + extra:
                problems.append(f"LOW REVIEW seats {len(reviewers)}, not the lead alone")
        elif len(reviewers) != extra or (not extra and (rv["mode"] != "deterministic_checks"
                                                        or not rv["required_checks"])):
            problems.append(f"LOW is not the deterministic checks: {rv['mode']} {reviewers}")
        return problems
    seats = {"MEDIUM": 1, "HIGH": 2, "CRITICAL": 2}[band]
    if len(reviewers) != seats + extra:
        problems.append(f"{band} seats {len(reviewers)} reviewers")
    if not rv["independence_required"]:
        problems.append(f"{band} does not ask for independence")
    floor = BAND_FLOOR[band]
    if band == "MEDIUM" and not review_task:
        floor = max(floor, TIER_OF[out["selected_model"]])
    for model in reviewers:
        if TIER_OF[model] < floor:
            disclosed(f"{model} below tier {floor}")
    if band == "CRITICAL":
        if "critical_review_band" not in causes or not gated:
            problems.append("CRITICAL without its human gate")
        parties = [out["selected_model"], *reviewers]
        if rv["judge_model"]:
            if TIER_OF[rv["judge_model"]] < max(TIER_OF[m] for m in parties) \
                    or rv["judge_model"] in parties:
                problems.append("the judge is outranked or a party")
        elif not rv["judge_unavailable"]:
            problems.append("CRITICAL with no judge and no judge_unavailable")
    return problems


def _lead_route() -> dict:
    """A live REVIEW lead: (0,0,0,0) with checks unavailable escapes to MEDIUM,
    and its lead is dispatched at the review's HIGH above its own MEDIUM."""
    out = route_live({"route_schema_version": 1, "task_class": "REVIEW", "complexity": 0,
                      "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
                      "runtime": "claude_code", "flags": [],
                      "availability_snapshot": {"checks_available": False}}, frozenset(ORDER))
    assert out["terminal"] is None and out["worker_seat_state"] == "to_dispatch", out
    assert out["dispatch_seats"][0]["model_id"] == out["selected_model"], out
    return out


def test_weaker_reads_each_row_in_its_safety_direction():
    """Counterexamples for the rows review i1 and i2 found `weaker()` missing,
    each on a live route with one field moved."""
    base = _lead_route()
    assert weaker(base, base) is None

    def moved(**edit):
        cur = copy.deepcopy(base)
        for path, value in edit.items():
            node = cur
            *head, last = path.split("__")
            for key in head:
                node = node[key]
            node[last] = value
        return cur

    # A lost human control is weaker whatever the band did.
    gated = moved(human_control_causes=["critical_review_band"])
    assert weaker(gated, moved(review__band="HIGH")) == "gate_lost"
    # A larger band is its own contract; a smaller one is weaker.
    assert weaker(base, moved(review__band="HIGH", review__reviewers=[])) is None
    assert weaker(moved(review__band="HIGH"), base) == "review.band"
    # A removed required check at the same band.
    assert weaker(moved(review__required_checks=["tests", "lint"]),
                  moved(review__required_checks=["tests"])) == "required_checks"
    # The lead's dispatch effort falls while the review's effort stays.
    assert weaker(moved(selected_effort_effective="VERY_HIGH"),
                  moved(selected_effort_effective="MEDIUM")) == "reviewer_efforts"
    # ... and where the band has no review effort, the lead's own is the seat's.
    assert weaker(moved(review__effort=None, selected_effort_effective="HIGH"),
                  moved(review__effort=None, selected_effort_effective="MEDIUM")) == "reviewer_efforts"

    def broken(cap):
        return [{"role": base["selected_role"], "model": base["selected_model"], "requested": "MAX",
                 "capped_at": cap, "floor_broken": "review.CRITICAL.effort", "floor_requires": "MAX"}]
    # A worker floor broken further — a lower cap — though both routes break it.
    assert weaker(moved(effort_ceiling_applied=broken("VERY_HIGH")),
                  moved(effort_ceiling_applied=broken("HIGH"))) == "worker_floor_broken"
    assert weaker(base, moved(effort_ceiling_applied=broken("HIGH"))) == "worker_floor_broken"
    assert weaker(moved(effort_ceiling_applied=broken("HIGH")),
                  moved(effort_ceiling_applied=broken("VERY_HIGH"))) is None


def test_invariant_1_every_band_keeps_its_contract():
    checked = 0
    for name, req, out in finals():
        if "error" in out or out["terminal"]:
            continue
        checked += 1
        problems = band_contract(req, out)
        assert not problems, (name, problems)
    assert checked > (60_000 if FULL else 2_000), checked


EXHAUSTED = lambda req: "exhausted" in ((req.get("availability_snapshot") or {})  # noqa: E731
                                        .get("family_quota") or {}).values()


def test_invariant_4_the_worker_never_falls_below_1161_but_for_the_same_model_retry():
    """No failure history: the worker's tier is at least 1.16.1's. With one:
    the failed model again only where all four C5 conditions hold — and then
    above every effort it ran at, within its ceiling, for every class — else
    at least 1.16.1's. Out of the tier half, each for its stated reason: a
    REVIEW task's lead is a review seat sized by its band (DD-B7); a declared
    implementer is the caller's (gated when weaker, DD-B1); an exhausted
    family is withheld supply the 1.16.1 projection does not see."""
    checked = same_model = 0
    shipped = frozenset(r for r in ORDER if r in RULES)
    for name, req, out in finals():
        if "error" in out or out["terminal"] or "implementer" in req or EXHAUSTED(req):
            continue
        target = _c5_target(req, shipped)
        failed = {r["model_id"] for r in req.get("attempt_outcomes") or []
                  if r["kind"] == "capability_failure"}
        if out["selected_model"] in failed and (target is None or out["selected_model"] != target[0]):
            # Reusing a failed model is C5's alone: without its conditions the
            # route keeps 1.16.1's choice (review i2).
            base = route_snapshot(req)
            assert base.get("selected_model") == out["selected_model"], (
                name, "failed model reused without C5", out["selected_model"])
        if target is not None and out["selected_model"] == target[0]:
            # The absolute limits hold for every class, REVIEW included: the
            # failed model, one level above every effort any of its records
            # ran at, within its ceiling — at the effort it is dispatched at.
            same_model += 1
            model = out["selected_model"]
            ran = [EFFORTS.index(r["effort"]) for r in req.get("attempt_outcomes") or []
                   if r["model_id"] == model and "effort" in r]
            seat = next((s for s in out.get("dispatch_seats") or [] if s["model_id"] == model), None)
            got = EFFORTS.index(seat["effort"] if seat else out["selected_effort_effective"])
            assert ran and got > max(ran), (name, got, ran)
            ceiling = CFG["models"][next(k for k, m in CFG["models"].items()
                                         if m["id"] == model)].get("effort_ceiling")
            assert ceiling is None or got <= EFFORTS.index(ceiling), name
            assert got >= EFFORTS.index(target[1]), name
            continue
        if req["task_class"] == "REVIEW":
            continue
        base = route_snapshot(req)
        if "error" in base or base["terminal"]:
            continue
        checked += 1
        assert TIER_OF[out["selected_model"]] >= TIER_OF[base["selected_model"]], (
            name, base["selected_model"], out["selected_model"])
    assert checked > (60_000 if FULL else 2_000) and same_model, (checked, same_model)


RAISING_FLAGS = list(CFG["flags"]["critical_domain"]) + list(CFG["flags"]["elevating"])


def _neighbors(req):
    for dim in ("complexity", "uncertainty", "blast_radius", "reversibility"):
        if req[dim] < 3:
            yield f"+{dim}", {**req, dim: req[dim] + 1}
    for flag in RAISING_FLAGS:
        if flag not in req["flags"]:
            yield f"+{flag}", {**req, "flags": req["flags"] + [flag]}


def monotonic(base: dict, raised: dict) -> list[str]:
    """Invariant 2, scoped: raising a dimension or adding a critical/elevating
    flag never lowers the review band, the band's reviewer count or its
    reviewer floor, and a gate that fired still fires (a production hotfix may
    DEFER it — the cause stays; that is the one sanctioned softening).

    The floor compared is the BAND's (MEDIUM 1, HIGH and CRITICAL 2). MEDIUM
    also asks its one reviewer to match the implementer (DD-B5); HIGH and
    CRITICAL answer the same risk with two independent seats at the frontier
    floor instead, so a tier-3 implementer's MEDIUM term does not carry into
    them — the one place invariant 1's per-band floor and this band-level
    floor differ (ledger, review i1)."""
    if "error" in base or "error" in raised or raised["terminal"]:
        return []
    if base["terminal"]:
        # Raising the risk turned a terminal into a route: nothing may have
        # been dropped on the way — every cause the terminal carried still fires.
        lost = set(base["human_control_causes"]) - set(raised["human_control_causes"])
        return [f"terminal -> route lost gates {sorted(lost)}"] if lost else []
    out = []
    b, r = base["review"], raised["review"]
    floor = lambda band: BAND_FLOOR[band] or 0                      # noqa: E731
    if floor(r["band"]) < floor(b["band"]):
        out.append(f"reviewer floor {floor(b['band'])} -> {floor(r['band'])}")
    if BANDS.index(r["band"]) < BANDS.index(b["band"]):
        out.append(f"band {b['band']} -> {r['band']}")
    elif r["band"] == b["band"] and (len(r["reviewers"]) - r["compensating_reviewers"]
                                     < len(b["reviewers"]) - b["compensating_reviewers"]):
        out.append("fewer reviewers")
    lost = set(base["human_control_causes"]) - set(raised["human_control_causes"])
    if lost:
        out.append(f"gates lost {sorted(lost)}")
    return out


def test_invariant_2_raising_the_risk_never_weakens_the_review():
    dims = [(0, 0, 0, 0), (1, 1, 0, 0), (0, 3, 0, 0), (2, 1, 1, 1), (3, 2, 0, 0), (2, 2, 1, 1),
            (1, 1, 2, 1), (2, 2, 2, 2)]
    checked, problems = 0, []
    for runtime, cls, d in itertools.product(RUNTIMES, CLASSES, dims if not FULL else DIMS):
        base_req = _base(runtime, cls, d)
        base = route_live(base_req, frozenset(ORDER))
        for step, req in _neighbors(base_req):
            checked += 1
            for problem in monotonic(base, route_live(req, frozenset(ORDER))):
                problems.append((runtime, cls, d, step, problem))
    assert not problems, (len(problems), problems[:12])
    assert checked > 2_000, checked
