#!/usr/bin/env python3
"""Deterministic half of the model router.

Classification is a judgment call and stays with the model: this script never
decides a task's class, its dimension scores, or its flags. It takes those as
input and computes everything downstream — score, band, overrides, worker,
effort, review policy, and the concrete model bindings — the same way every
time.

Two properties are load-bearing and were each broken once before:

1. **The emitted route must be executable as written.** Every model named has
   been checked against what the caller said is unavailable, against the
   provider boundary when the bridge is down, and against what already failed.
2. **A recorded change must be a real change.** A fallback is recorded only
   when the model actually differs; an escalation only when the model actually
   moves. Recording a degradation or a promotion that did not happen is worse
   than recording nothing, because it reads as a managed decision.

The taxonomy and the override rules are read from the config rather than
restated here — a constant duplicated in code is a second source of truth that
drifts silently.

Usage:
    route_task.py --class DEBUGGING --complexity 2 --uncertainty 3 \\
                  --blast-radius 2 --reversibility 1 \\
                  --flags auth_sensitive,unknown_root_cause

Exit status: 0 dispatchable as written; 1 terminal (no route to execute);
2 invalid input; 3 executable only after a human confirms; 4 dispatchable
with a human confirmation owed after the fix ships (production hotfix);
5 internal error — a crash, never a route outcome. 3 exists because a gate a
caller cannot act on from a shell is not a gate; 5 exists because a crash
that borrows 1 or 2 reports a terminal state or an input error that never
happened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import traceback
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace

import lineage
import model_state
from policy_digest import canonical_policy_sha256, policy_sha256
from strict_json import loads as strict_json_loads, ensure_json_value
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "model-routing.yaml"
ROUTE_SCHEMA_VERSION = 1
# The dimension scale the four inputs are validated against.
MAX_DIMENSION_SCORE = 3
REQUEST_V1_KEYS = frozenset({
    "route_schema_version", "task_class", "complexity", "uncertainty",
    "blast_radius", "reversibility", "reasoning_centric", "flags",
    "runtime", "prior_failures", "availability_snapshot", "local_policy",
    "host_seat", "worker_seat", "review_context", "attempt_outcomes",
    "policy_pin", "implementer",
})
AVAIL_KEYS = frozenset({
    "unavailable_roles", "unavailable_models", "isolation", "isolation_evidence",
    "checks_available", "family_quota",
})
# availability_snapshot.family_quota values (design 2026-09-25 DD-B8, C7).
FAMILY_QUOTA_LEVELS = ("ok", "low", "exhausted")
LOCAL_POLICY_KEYS = frozenset({
    "minimum_capability_tier", "minimum_effort", "minimum_reviewers",
    "minimum_provider_families", "allowed_families",
})
HOST_SEAT_KEYS = frozenset({"model", "effort"})
REVIEW_CONTEXT_KEYS = frozenset({"target_sha256", "author_model_ids", "author_families"})
# RouteRequestV1 `implementer` (design 2026-09-25 DD-B1): the model that already
# did this write work. The route then plans its review, not its dispatch.
IMPLEMENTER_KEYS = frozenset({"model_id"})
OPERATIONAL_OUTCOMES = frozenset({"transport_failure", "launch_failure", "resolution_failure",
    "timeout", "max_turns_partial", "no_artifact", "invalid_output", "authentication_failure",
    "quota_exhausted", "publication_failure", "cancelled", "unknown"})
ATTEMPT_OUTCOME_KINDS = OPERATIONAL_OUTCOMES | {"capability_failure", "termination_unconfirmed"}
# Whether a route's WORKER needs a write-capable dispatch recipe. Two values,
# not a boolean: the route records which one it applied, and `read_only` has to
# read as a decision on the seat rather than as "false".
WORKER_SEAT_KINDS = ("write", "read_only")


_PLUGIN_VERSION_CACHE: dict[str, str] = {}


def plugin_manifest_version(start: Path | None = None) -> str:
    here = Path(start) if start is not None else Path(__file__).resolve()
    cache_key = str(here)
    if cache_key in _PLUGIN_VERSION_CACHE:
        return _PLUGIN_VERSION_CACHE[cache_key]
    for parent in [here, *here.parents]:
        manifest = parent / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            version = data.get("version")
            if not isinstance(version, str) or not version:
                raise ConfigError(f"{manifest} has no string version")
            _PLUGIN_VERSION_CACHE[cache_key] = version
            return version
    raise ConfigError("cannot find .claude-plugin/plugin.json above route_task.py")

# Every terminal this router can emit. Named here so documentation tests can
# assert the set rather than a sample of it.
TERMINAL_STATES = (
    "HUMAN_REQUIRED", "ESCALATE_ROUTING", "INDEPENDENCE_UNAVAILABLE",
    "RETRY_HISTORY_REQUIRED", "SUPPLY_EXHAUSTED", "UNSATISFIABLE_LOCAL_POLICY",
    "OPERATIONAL_RECOVERY_REQUIRED", "TERMINATION_UNCONFIRMED",
    "MODEL_STATE_UNAVAILABLE",
)

MAX_PROMOTION_PASSES = 4   # bounded fixed point; the band ladder is only 4 deep


class ValidationError(ValueError):
    """Raised for any malformed routing input, from the CLI or the API."""


class ConfigError(RuntimeError):
    """Raised when the policy config cannot be read or is internally invalid."""


class UnknownCompensationError(ConfigError):
    """A compensation was declared in the config with an effect nothing implements.

    The router dispatches on the effect string, not on the key, so a renamed or
    misspelled value used to fall through every branch: the compensation was
    reported as applied, nothing happened, and the tests — which checked the
    KEYS — stayed green. Inert policy reading as active is the thing this
    module keeps having to remove, so an unimplemented effect stops the route.
    """


# The prose a cause is allowed to carry. Round 16: pairing a cause code with a
# predicate closed half the gap — the half where the predicate drifts — and left
# the other half open, because nothing observed `reason`. Round 14's Critical
# was exactly a reason string narrowing while the predicate stayed put. A cause
# now owns its wording, and `test_d19` checks the emitted rationale against this
# table rather than against a comment.
CAUSE_REASONS = {
    "caller_declared_isolation_gap":
        "the caller reported that isolation cannot be achieved here",
    "critical_review_band": "a CRITICAL review cannot be accepted automatically",
    "no_adjudicator": "no adjudicator is available",
    "review_below_band": "the review is staffed below its band",
    "effort_below_floor":
        "the selected model cannot reach the effort a floor required",
    "unconfirmed_prior_termination":
        "a prior attempt's process tree could not be confirmed dead — "
        "dispatching a retry risks two concurrent writers",
    "implementer_below_worker_tier":
        "the declared implementer is weaker than the worker this work routes to",
}


@dataclass(frozen=True)
class Control:
    """One configurable human-in-the-loop control.

    `cause` is the machine-readable name of the condition `fired` tests, and it
    is emitted on the route. It exists because six rounds of this artifact's
    defects were a comment claiming a predicate did one thing while it did
    another — unmeasurable as prose, measurable as a code paired with a
    predicate and asserted over the whole input space.
    """
    key: str
    cause: str
    fired: bool
    terminal: str

    @property
    def reason(self) -> str:
        return CAUSE_REASONS[self.cause]


class SupplyExhausted(Exception):
    """No usable model exists for a role the route needs.

    Round 14: this was a `ValidationError`, so the CLI reported exit 2,
    "invalid input", for an input that obeyed every documented contract —
    `bridge_down` plus four concrete prior failures leaves the local family
    empty, which is an operational shortage the caller cannot fix by editing
    its command. A scheduler that distinguishes "call a human" (1) from "fix
    your input and retry" (2) was told the wrong one.
    """


class RouterInvariantError(AssertionError):
    """A post-condition of the router itself did not hold.

    Distinct from `ValidationError` (the caller's input is wrong) and
    `ConfigError` (the policy is wrong): this one means the code produced a
    state it promises never to produce, and mapping it onto "invalid input"
    would blame the caller for a defect here.
    """


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read the policy. Raises rather than exiting, so importing this module
    can never terminate the interpreter."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ConfigError(
            "route_task.py needs PyYAML to read the policy config (pip install pyyaml). "
            "The policy is not duplicated in this script on purpose — one source of truth."
        ) from exc
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except OSError as exc:
        raise ConfigError(f"cannot read policy config at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        # Same shape as the OSError arm, and for the same reason the eager
        # `human_gate_exit_status` validation exists: an uncontained raise
        # lands on the interpreter's exit 1, which the contract reserves for
        # "terminal (no route to execute)".
        raise ConfigError(
            f"policy config at {path} is not valid YAML: {exc}") from exc


# --------------------------------------------------------------------------
# Policy — the taxonomy derived from one config, so `route(task, cfg)` honours
# the config it was given all the way down to input validation.
# --------------------------------------------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_int_map(name: str, node, exact_keys: set[str]) -> None:
    """Exact key set, non-negative non-bool ints. Type-checks the node itself
    first so a malformed policy raises ConfigError, never TypeError
    (design DD-1, [P1-sol-F6])."""
    if not isinstance(node, Mapping):
        raise ConfigError(f"{name} must be a mapping, got {type(node).__name__}")
    keys = set(node)
    if keys != exact_keys:
        raise ConfigError(
            f"{name} must name exactly {sorted(exact_keys)}; "
            f"missing {sorted(exact_keys - keys)}, unknown {sorted(keys - exact_keys)}")
    for key, value in node.items():
        if not _is_int(value) or value < 0:
            raise ConfigError(f"{name}.{key} must be a non-negative int, got {value!r}")


def _validate_bands(name: str, node, ceiling: int) -> list[str]:
    """Ordered band names. Contiguous over 0..ceiling, unique 0-based ordinals,
    exact {min, max, ordinal} int keys — checked BEFORE anything sorts by
    ordinal or sums a weight (design DD-1, [R3-sol-F6])."""
    if not isinstance(node, Mapping) or not node:
        raise ConfigError(f"{name} must be a non-empty mapping")
    for band, spec in node.items():
        if not isinstance(spec, Mapping) or set(spec) != {"min", "max", "ordinal"}:
            raise ConfigError(f"{name}.{band} must have exactly min/max/ordinal")
        if not all(_is_int(spec[k]) for k in ("min", "max", "ordinal")):
            raise ConfigError(f"{name}.{band} min/max/ordinal must be ints, not bools")
    ordinals = sorted(spec["ordinal"] for spec in node.values())
    if ordinals != list(range(len(node))):
        raise ConfigError(f"{name} ordinals must be 0..{len(node) - 1} without gaps or duplicates; got {ordinals}")
    ordered = sorted(node, key=lambda b: node[b]["ordinal"])
    expected = 0
    for band in ordered:
        spec = node[band]
        if spec["min"] != expected or spec["max"] < spec["min"]:
            raise ConfigError(f"{name}.{band} must start at {expected} (got {spec['min']}..{spec['max']})")
        expected = spec["max"] + 1
    if expected != ceiling + 1:
        raise ConfigError(f"{name} must end exactly at {ceiling}, ends at {expected - 1}")
    return ordered


class Policy:
    """Everything derivable from a config, computed once per config."""

    # id(cfg) -> (content digest, Policy): ONE entry per config object, holding
    # its current revision. Identity keys are safe because `__init__` stores
    # `self.cfg = cfg`, so the live entry keeps its config alive and that id
    # cannot be recycled — which is why the entry is REPLACED rather than
    # dropped when the content moves. Any future eviction must preserve that
    # property: evicting the last entry for a live id reintroduces the
    # id-recycling hazard this design has always depended on not having.
    # Round 9 added a redundant (cfg, policy) tuple to "fix" a hazard that this
    # line already prevented, in the same commit that removed inert policy
    # elsewhere — round 10 caught the irony. The digest half arrived in 1.2.0:
    # identity alone answered a mutated config from a stale entry while the
    # emitted `policy_sha256` attested the new content. The cost that IS real:
    # the cache is unbounded in the number of distinct config OBJECTS, so a
    # process routing against many of them retains all of them — but no longer
    # unbounded in how often any one of them is edited.
    #
    # Bounded since 2026-09-25 (DD-A2): effective policies are built per state
    # generation, so a long-lived process would otherwise keep one per
    # generation it ever routed on. Eviction keeps the identity guarantee: an
    # entry still in the cache holds its cfg alive, and an evicted id that is
    # later recycled simply misses and rebuilds.
    _cache: "_LRU" = None  # set below the class (needs _LRU)

    def __init__(self, cfg: dict):
        self.cfg = cfg
        # Set by `of()`; None for a Policy built directly or from a non-dict
        # Mapping, both of which have no content digest to publish.
        self.content_sha: str | None = None
        # --- validation preamble: shape before any derivation (DD-1) ---
        # Parents first, so a malformed policy raises ConfigError and never a
        # KeyError/TypeError from an index on the way to the check [P2-sol-F3].
        if not isinstance(cfg, Mapping):
            raise ConfigError("policy config must be a mapping")
        router = cfg.get("router")
        if not isinstance(router, Mapping):
            raise ConfigError("router must be a mapping")
        flags_node = cfg.get("flags")
        if not isinstance(flags_node, Mapping) or not isinstance(flags_node.get("context"), list) \
                or not all(isinstance(f, str) for f in flags_node["context"]):
            raise ConfigError("flags.context must be a list of flag names")   # [P3-sol-F5]
        _validate_int_map("router.score_weights", router.get("score_weights"),
                          {"complexity", "uncertainty", "blast_radius", "reversibility"})
        max_risk = MAX_DIMENSION_SCORE * sum(router["score_weights"].values())
        risk_order = _validate_bands("router.bands", router.get("bands"), max_risk)
        ex = cfg.get("execution")
        if not isinstance(ex, Mapping) or set(ex) != {"score_weights", "flag_weights", "bands"}:
            raise ConfigError("execution must be a mapping with exactly score_weights/flag_weights/bands")
        _validate_int_map("execution.score_weights", ex["score_weights"], {"complexity", "uncertainty"})
        exact_flags = {"unfamiliar_codebase", "tool_heavy", "cross_service_change"}
        if not exact_flags <= set(flags_node["context"]):
            raise ConfigError("execution.flag_weights names a flag outside flags.context")
        _validate_int_map("execution.flag_weights", ex["flag_weights"], exact_flags)
        max_exec = (MAX_DIMENSION_SCORE * sum(ex["score_weights"].values())
                    + sum(ex["flag_weights"].values()))
        exec_order = _validate_bands("execution.bands", ex["bands"], max_exec)

        self.bands: list[str] = risk_order
        self.execution_bands: list[str] = exec_order
        self.max_execution_score: int = max_exec
        # Score -> band, fixed at load. Load-time validation covers 0..max
        # exactly, so both tables are total over the reachable scores.
        self._risk_band_of: list[str] = [
            next(b for b in risk_order if router["bands"][b]["min"] <= s <= router["bands"][b]["max"])
            for s in range(max_risk + 1)]
        self._exec_band_of: list[str] = [
            next(b for b in exec_order if ex["bands"][b]["min"] <= s <= ex["bands"][b]["max"])
            for s in range(max_exec + 1)]
        self.efforts: list[str] = list(cfg["effort_levels"])
        self.roles: list[str] = list(cfg["role_tiers"])
        self.task_classes: list[str] = list(cfg["worker_selection"])
        ex_sel = cfg.get("execution_selection")
        if not isinstance(ex_sel, Mapping):
            raise ConfigError("execution_selection must be a mapping")            # before any set() [P3-sol-F5]
        if set(ex_sel) != set(self.task_classes):
            raise ConfigError(
                f"execution_selection must name exactly the task classes; "
                f"missing {sorted(set(self.task_classes) - set(ex_sel))}, "
                f"unknown {sorted(set(ex_sel) - set(self.task_classes))}")
        for task_class, row in ex_sel.items():
            if not isinstance(row, Mapping) or set(row) != set(self.execution_bands):
                raise ConfigError(f"execution_selection.{task_class} must name exactly the execution bands")
            for band, cell in row.items():
                if cell != "by_reasoning_centric" and cell not in cfg["role_tiers"]:
                    raise ConfigError(f"execution_selection.{task_class}.{band} = {cell!r} is not a role")
        self.execution_selection: dict = {k: dict(v) for k, v in ex_sel.items()}
        # Which classes need a write-capable worker seat. Completeness is
        # checked here rather than at lookup time: a class the table forgets
        # would otherwise reach `worker_seat_kind` and need a default invented
        # on the spot, and the only default available there is the fail-open
        # one ("assume it does not write"), which is the exact shape this
        # table exists to remove.
        self.task_write_seat: dict[str, str] = dict(cfg["task_write_seat"])
        missing = sorted(set(self.task_classes) - set(self.task_write_seat))
        unknown = sorted(set(self.task_write_seat) - set(self.task_classes))
        if missing or unknown:
            raise ConfigError(
                f"task_write_seat must name exactly the task classes; "
                f"missing {missing}, unknown {unknown}")
        for task_class, kind in self.task_write_seat.items():
            if kind not in WORKER_SEAT_KINDS:
                raise ConfigError(
                    f"task_write_seat.{task_class} is {kind!r}; "
                    f"expected one of {list(WORKER_SEAT_KINDS)}")
        self.critical_domain_flags: tuple[str, ...] = tuple(cfg["flags"]["critical_domain"])
        # Every dimension at its maximum. The band table is declared over 0..this.
        self.max_risk_score: int = max_risk
        self.known_flags: frozenset[str] = frozenset(f for g in cfg["flags"].values() for f in g)
        self.runtimes: frozenset[str] = frozenset(cfg["runtimes"])
        # Before any id-keyed table is built: a dict comprehension over two
        # rows holding one id keeps the LAST row and drops the first without
        # a word, so every table below would describe a registry that does
        # not exist (design 2026-09-25 DD-A1).
        seen_ids: dict[str, str] = {}
        for key, model in cfg["models"].items():
            mid = model["id"]
            if mid in seen_ids:
                raise ConfigError(
                    f"models.{seen_ids[mid]} and models.{key} hold the same id "
                    f"{mid!r}; an id must name exactly one registry row")
            seen_ids[mid] = key
        self.lineage_of: dict[str, dict] = self._validate_lineages(cfg)
        # Local revocations merged in from model state (design 2026-09-25
        # DD-A2 rule 8): never a seat candidate, from any source.
        local_state = cfg.get("local_state", {})
        if (not isinstance(local_state, Mapping) or set(local_state) - {"blocked_ids"}
                or not isinstance(local_state.get("blocked_ids", []), list)
                or not all(isinstance(b, str) and b for b in local_state.get("blocked_ids", []))):
            raise ConfigError("local_state must be {blocked_ids: [model id, ...]}")
        self.local_blocked_ids: frozenset[str] = frozenset(local_state.get("blocked_ids", []))
        self._validate_history_rows(cfg, self.lineage_of)
        self.model_ids: frozenset[str] = frozenset(m["id"] for m in cfg["models"].values())
        self.id_to_key: dict[str, str] = {m["id"]: k for k, m in cfg["models"].items()}
        self.family_of: dict[str, str] = {m["id"]: m["family"] for m in cfg["models"].values()}

        # The two blocks describe different axes and must both be complete:
        # a runtime with no degraded binding cannot survive a downed bridge,
        # and a family with no effort map cannot have its effort spelled.
        for runtime, spec in cfg["runtimes"].items():
            binding = spec["degraded_binding"]
            if binding not in cfg["role_bindings"]:
                raise ConfigError(
                    f"runtimes.{runtime}.degraded_binding names {binding!r}, "
                    f"which is not a role binding")
            fams = {cfg["models"][k]["family"] for k in cfg["role_bindings"][binding].values()}
            if len(fams) != 1:
                raise ConfigError(
                    f"role_bindings.{binding} spans {sorted(fams)}; a degraded "
                    f"binding is what survives when the bridge is down, so it "
                    f"must name exactly one family")
        families = set(self.family_of.values())
        self.families: frozenset[str] = frozenset(families)
        if set(cfg["effort_map"]) != families:
            raise ConfigError(
                f"effort_map is keyed by model family; it covers "
                f"{sorted(cfg['effort_map'])} but the registry holds "
                f"{sorted(families)}")

        # Model generations within one family need not accept the same native
        # tokens. Merge once so fallback seats use their resolved model's map.
        self.effort_map_of: dict[str, dict[str, str]] = {}
        for key, model in cfg["models"].items():
            overrides = model.get("effort_map", {})
            if (not isinstance(overrides, Mapping)
                    or set(overrides) - set(self.efforts)
                    or any(not isinstance(v, str) or not v.strip()
                           for v in overrides.values())):
                raise ConfigError(
                    f"models.{key}.effort_map must map known conceptual efforts "
                    "to non-empty native effort strings")
            self.effort_map_of[model["id"]] = {
                **cfg["effort_map"][model["family"]], **overrides,
            }

        # Strength, measured on the model rather than on the role holding it.
        # Every comparison that used `roles.index(...)` as a proxy for capability
        # was wrong the moment scarcity made a role resolve to something other
        # than its nominal binding, which is the whole reason a fallback exists.
        self.tier_of: dict[str, int] = {
            m["id"]: m["capability_tier"] for m in cfg["models"].values()
        }

        # The highest conceptual effort this model's CLI will accept, or None
        # when it accepts every level. Optional: absence means no ceiling, so
        # existing models need no entry. Validated here because a ceiling that
        # does not name a real level is a control that silently does nothing.
        self.ceiling_of: dict[str, str | None] = {}
        for key, model in cfg["models"].items():
            ceiling = model.get("effort_ceiling")
            if ceiling is not None and ceiling not in self.efforts:
                raise ConfigError(
                    f"models.{key}.effort_ceiling = {ceiling!r} is not one of "
                    f"{self.efforts}; a ceiling naming no real level cannot be "
                    f"compared and would be skipped in silence")
            self.ceiling_of[model["id"]] = ceiling

        # served_model_caveats: a disclosure trigger list. Strict-loud like
        # every other vocabulary — an unknown flag or a duplicate reads as
        # policy and discloses nothing.
        for key, model in cfg["models"].items():
            caveats = model.get("served_model_caveats")
            if caveats is None:
                continue
            if len(set(caveats)) != len(caveats):
                raise ConfigError(f"models.{key}.served_model_caveats has duplicates")
            unknown = set(caveats) - self.known_flags
            if unknown:
                raise ConfigError(
                    f"models.{key}.served_model_caveats names unknown flag(s) "
                    f"{sorted(unknown)}; a trigger outside the flags vocabulary "
                    f"can never fire")

        orch = cfg["router"]["default_orchestrator"]
        orch_effort = cfg["router"]["default_orchestrator_effort"]
        if orch not in cfg["role_tiers"]:
            raise ConfigError(
                f"router.default_orchestrator names {orch!r}, which is not a role")
        if orch not in cfg["role_bindings"]["default"]:
            raise ConfigError(
                f"router.default_orchestrator {orch!r} has no seat in "
                f"role_bindings.default")
        if orch_effort not in self.efforts:
            raise ConfigError(
                f"router.default_orchestrator_effort {orch_effort!r} is not one of "
                f"{list(self.efforts)}")

        # What each band demands of a reviewer, expressed as a model tier and
        # derived from the band's own configured reviewer roles under the
        # canonical binding. Computed, never written down twice: a floor kept
        # in a second place is a floor that drifts from the policy it guards.
        nominal = {
            role: self.tier_of[cfg["models"][key]["id"]]
            for role, key in cfg["role_bindings"]["default"].items()
        }
        # Every action, not just the exit status. Round 11: the five action
        # keys were compared against string literals with no validation, so a
        # one-character typo in `on_any_critical_review` silently deleted the
        # strongest gate in the policy — no terminal, no exception, no note.
        # `apply_overrides` in this same file already refuses an unknown effect
        # key for exactly this reason; being strict there and lax here is the
        # asymmetry that turns a safety rule into decoration.
        # Per key, against the actions THAT KEY'S CONSUMER IMPLEMENTS — not a
        # union of every word the vocabulary contains. Round 12: validating
        # against the union let the strictest-sounding word disable four of the
        # five controls. `on_any_critical_review: terminal` passed validation
        # and removed the CRITICAL gate entirely; `on_independence_unachievable:
        # require_human_confirmation` removed both the terminal AND the gate.
        # The error message promised the opposite of what the check did.
        implemented = {"terminal", "require_human_confirmation", "notify_human"}
        # `on_production_hotfix` has its own vocabulary: deferring is not one of
        # the three general actions, and the general three are not all
        # meaningful for it (a hotfix policy that terminates would stop the
        # incident response it exists to serve).
        per_key = dict.fromkeys((
            "on_independence_unachievable", "on_any_critical_review",
            "on_judge_unavailable", "on_review_depth_reduced",
            "on_effort_below_floor"), implemented)
        per_key["on_production_hotfix"] = {
            "require_human_confirmation", "defer_human_confirmation"}
        for key, allowed in per_key.items():
            if key not in cfg["human_in_the_loop"]:
                raise ConfigError(f"human_in_the_loop is missing {key!r}")
            value = cfg["human_in_the_loop"][key]
            if value not in allowed:
                raise ConfigError(
                    f"human_in_the_loop.{key} = {value!r}; this router implements "
                    f"{sorted(allowed)} for that key. A word it does not implement "
                    f"reads as policy and disables the control it names.")

        gate = cfg["human_in_the_loop"]["human_gate_exit_status"]
        # Process exit status is truncated to eight bits, so 256 is 0 — a
        # human-gated route reporting success, which is the single hazard this
        # value exists to remove. Validated here rather than at the point of
        # use: `main()` reads it after the route has already been printed, so a
        # raise there escapes the handler and lands on exit 1 ("terminal") for
        # a route it just emitted as executable.
        if isinstance(gate, bool) or not isinstance(gate, int) or not 3 <= gate <= 255:
            raise ConfigError(
                f"human_gate_exit_status must be an integer in 3..255 "
                f"(0/1/2 are taken, and >255 truncates to a success code); got {gate!r}")
        self.human_gate_exit_status: int = gate
        skip = cfg["router"]["confidence"].get("skip_uncertainty_penalty_when_band_raised")
        if not isinstance(skip, bool):
            raise ConfigError(
                "router.confidence.skip_uncertainty_penalty_when_band_raised must be true or "
                f"false, got {skip!r}")
        self.skip_uncertainty_penalty: bool = skip
        # review.review_class_lead_counts (DD-B7, U-8): a REVIEW task's lead is
        # reviewer-1 and one of the band's reviewers, with or without a
        # declared review_context.
        lead = cfg["review"].get("review_class_lead_counts")
        if not isinstance(lead, bool):
            raise ConfigError(
                f"review.review_class_lead_counts must be true or false, got {lead!r}")
        self.review_lead_counts: bool = lead
        # effort_caps: {band_<BAND>: level} (DD-B3, C2) — the class table's
        # effort for that risk band is capped before any floor applies.
        caps = cfg.get("effort_caps")
        if not isinstance(caps, Mapping):
            raise ConfigError("effort_caps must be a mapping (possibly empty)")
        self.effort_caps: dict[str, str] = {}
        for key, level in caps.items():
            band = key[len("band_"):] if isinstance(key, str) and key.startswith("band_") else None
            if band not in self.bands or level not in self.efforts:
                raise ConfigError(
                    f"effort_caps.{key} = {level!r}: keys are band_<risk band>, values effort levels")
            self.effort_caps[band] = level

        # `None` for a band that seats no model reviewer — only the lowest band
        # may (design 2026-09-25 DD-B4: LOW review is deterministic checks).
        # The sentinel is read only where the settled band is that one; any
        # other reader meeting it raises rather than taking it for 0.
        self.band_reviewer_floor: dict[str, int | None] = {}
        for band in self.bands:
            spec = cfg["review"][band]
            if spec.get("reviewers") == [] and "candidates" not in spec:
                if band != self.bands[0]:
                    raise ConfigError(
                        f"review.{band} seats no reviewer; only the lowest band "
                        f"({self.bands[0]}) may be reviewed by deterministic checks alone")
                if spec.get("effort") is not None or spec.get("independent"):
                    raise ConfigError(
                        f"review.{band} seats no reviewer, so its effort must be null and "
                        f"it cannot be independent")
                checks = spec.get("required_checks")
                if not isinstance(checks, list) or not checks or not all(
                        isinstance(c, str) and c for c in checks):
                    raise ConfigError(
                        f"review.{band} seats no reviewer, so it must name the "
                        f"deterministic checks it consists of (required_checks)")
                self.band_reviewer_floor[band] = None
                continue
            if spec.get("effort") not in self.efforts:
                raise ConfigError(f"review.{band}.effort {spec.get('effort')!r} is not an effort level")
            roles = [r for r in (spec.get("reviewers") or spec.get("candidates") or [])
                     if r in nominal]
            if not roles:
                # Failing open to 0 would silently disable the depth gate for
                # that band — the failure mode is invisible because the test
                # oracle would compute the same 0.
                raise ConfigError(
                    f"review band {band!r} names no reviewer role present in the "
                    f"default binding; its reviewer floor is undefined")
            self.band_reviewer_floor[band] = min(nominal[r] for r in roles)

        # Which family survives when the cross-provider bridge is down. Read
        # from the config rather than a hardcoded pair: a third runtime used to
        # mean editing three separate two-way branches, and the one that used an
        # `else` silently sent an unknown runtime to the wrong binding.
        # `degraded_binding` is validated single-family above, so any role in it
        # names the same family.
        self.degraded_binding: dict[str, str] = {
            runtime: spec["degraded_binding"] for runtime, spec in cfg["runtimes"].items()
        }
        self.local_family: dict[str, str] = {
            runtime: cfg["models"][next(iter(cfg["role_bindings"][binding].values()))]["family"]
            for runtime, binding in self.degraded_binding.items()
        }
        self._validate_native_families(cfg)
        self._validate_write_seats(cfg)

    @staticmethod
    def _validate_lineages(cfg: dict) -> dict[str, dict]:
        """Every dispatchable row declares its lineage, and the declaration is
        well-formed (design 2026-09-25 DD-A1): `line` globally unique, the
        template parses, and the row's own id is a spelling of it. Returns
        registry key -> lineage for the rows that have one.

        `catalog_name` and `served_forms` are shape-checked here but consumed
        by model_sync; the router routes on `id` alone.
        """
        allowed = {"line", "template", "catalog_name", "served_forms"}
        lines: dict[str, str] = {}
        out: dict[str, dict] = {}
        for key, model in cfg["models"].items():
            lin = model.get("lineage")
            if lin is None:
                if model.get("dispatchable", True) is not False:
                    raise ConfigError(
                        f"models.{key} is dispatchable and declares no lineage; "
                        f"a seat whose successor cannot be recognised is a seat "
                        f"that silently falls behind its vendor")
                continue
            if not isinstance(lin, Mapping):
                raise ConfigError(f"models.{key}.lineage must be a mapping")
            if (unknown := set(lin) - allowed) or not {"line", "template"} <= set(lin):
                raise ConfigError(
                    f"models.{key}.lineage must hold line and template (optional "
                    f"catalog_name, served_forms); unknown {sorted(unknown)}")
            line, template = lin["line"], lin["template"]
            if not isinstance(line, str) or not line.strip():
                raise ConfigError(f"models.{key}.lineage.line must be a non-empty string")
            if line in lines:
                raise ConfigError(
                    f"models.{lines[line]} and models.{key} declare the same "
                    f"lineage line {line!r}; a line names one registry row")
            lines[line] = key
            try:
                gen = lineage.parse(template, model["id"])
            except ValueError as exc:
                raise ConfigError(f"models.{key}.lineage.template: {exc}") from None
            if gen is None:
                raise ConfigError(
                    f"models.{key}.id {model['id']!r} does not match its own "
                    f"lineage template {template!r}")
            if "catalog_name" in lin and not (
                    isinstance(lin["catalog_name"], str) and lin["catalog_name"].strip()):
                raise ConfigError(f"models.{key}.lineage.catalog_name must be a non-empty string")
            if "served_forms" in lin:
                forms = lin["served_forms"]
                if (not isinstance(forms, list) or not forms
                        or not all(isinstance(f, str) and "{id}" in f for f in forms)):
                    raise ConfigError(
                        f"models.{key}.lineage.served_forms must be a non-empty "
                        f"list of strings each containing {{id}}")
            out[key] = {"line": line, "template": template}
        return out

    @staticmethod
    def _validate_history_rows(cfg: dict, lineage_of: dict[str, dict]) -> None:
        """History rows (design 2026-09-25 DD-A3): a superseded id kept as
        valid HISTORY input and never seated.

        The key is `<history_of>@<id>` with the id verbatim — a slug would fold
        `gpt-6.0-sol` and `gpt-6-0-sol` into one key and turn one of them into
        invalid input. The live row must carry a lineage whose template the
        history id matches at a strictly LOWER generation, in the same family;
        the row is non-dispatchable and no binding or fallback list names it.
        The router still reads only `dispatchable` at route time.
        """
        bound = {k for binding in cfg["role_bindings"].values() for k in binding.values()}
        in_fallback = {k for per_role in cfg["fallbacks"].values()
                       for keys in per_role.values() for k in keys}
        for key, model in cfg["models"].items():
            live_key = model.get("history_of")
            if live_key is None:
                if "@" in key:
                    raise ConfigError(
                        f"models.{key}: a `<live key>@<id>` key is reserved for "
                        f"history rows, and this row declares no history_of")
                continue
            mid = model["id"]
            if key != f"{live_key}@{mid}":
                raise ConfigError(
                    f"models.{key} is a history row of {live_key!r}; its key must "
                    f"be exactly {live_key}@{mid}")
            live = cfg["models"].get(live_key) if isinstance(live_key, str) else None
            if live is None or "history_of" in live:
                raise ConfigError(
                    f"models.{key}.history_of names {live_key!r}, which is not a "
                    f"live registry row")
            if live_key not in lineage_of:
                raise ConfigError(
                    f"models.{key}.history_of names {live_key!r}, which declares no "
                    f"lineage to order the two ids by")
            if model["family"] != live["family"]:
                raise ConfigError(
                    f"models.{key} is family {model['family']!r} but its live row "
                    f"{live_key!r} is {live['family']!r}")
            if model.get("dispatchable") is not False:
                raise ConfigError(
                    f"models.{key} is a history row and must be dispatchable: false")
            template = lineage_of[live_key]["template"]
            past, current = lineage.parse(template, mid), lineage.parse(template, live["id"])
            if past is None:
                raise ConfigError(
                    f"models.{key}.id {mid!r} does not match the lineage template "
                    f"{template!r} of {live_key!r}")
            source = model.get("history_source")
            if source is not None and source not in ("base", "probe"):
                raise ConfigError(
                    f"models.{key}.history_source is {source!r}; only rows "
                    f"synthesized from local model state carry it (base|probe)")
            # A row synthesized from a local generation's history record may be
            # NEWER than the live id: a reverted overlay id stays valid history
            # input while the live row is back on its base id (DD-A2 rule 7).
            if source is None and lineage.compare(past, current) >= 0:
                raise ConfigError(
                    f"models.{key}.id {mid!r} is not an older generation than "
                    f"{live_key!r}'s {live['id']!r}")
            if key in bound:
                raise ConfigError(f"models.{key} is a history row and a role binding names it")
            if key in in_fallback:
                raise ConfigError(f"models.{key} is a history row and a fallback list names it")

    def _validate_native_families(self, cfg: dict) -> None:
        """`local_family` and `transports` must answer "which family is native
        here?" the same way.

        `write_capable`'s first branch trusts `local_family`, which is derived
        from `runtimes.<rt>.degraded_binding` — a different table from the
        `transports.<rt>.native` its own docstring names, and the only branch
        with no verification flag or recipe behind it. Nothing held the two in
        step, so pointing a runtime's degraded binding at another family made a
        genuine CROSS-family direction answer "native, therefore write-capable"
        without reading anything else: fail-open in the one place that cannot
        afford it. A host does not bridge to itself, so the agreement is
        checkable — the native family is exactly the one with no `to_<family>`
        entry.
        """
        for runtime, family in self.local_family.items():
            entries = cfg["transports"].get(runtime) or {}
            if f"to_{family}" in entries:
                raise ConfigError(
                    f"runtimes.{runtime}.degraded_binding makes {family!r} the native "
                    f"family, but transports.{runtime}.to_{family} exists — a host does "
                    f"not bridge to itself, so one of the two tables is wrong")

    def _validate_write_seats(self, cfg: dict) -> None:
        """`write_verified` must be backed by a recipe and agree with the ledger.

        The flag is the sole authorization for cross-family write dispatch, so
        the two ways it could lie are both closed here rather than left to a
        reader: a direction claiming a verified write seat with no write-capable
        mechanism string behind it, and one claiming it while the verification
        ledger still records that seat as anything but verified. The ledger is
        this file's record of what was actually probed; a second source that can
        silently disagree with it is exactly the sand its own header refuses to
        build on.
        """
        ledger = (cfg.get("verification_ledger") or {}).get("entries") or []
        for runtime, entries in cfg["transports"].items():
            for name, entry in entries.items():
                if name == "native" or not isinstance(entry, Mapping):
                    continue
                if entry.get("write_verified") is not True:
                    continue
                recipe = entry.get("mechanism_maker") or entry.get("mechanism")
                if not (isinstance(recipe, str) and recipe.strip()):
                    raise ConfigError(
                        f"transports.{runtime}.{name} declares write_verified: true "
                        f"with no write-capable mechanism string to dispatch")
                if "mechanism_maker" not in entry:
                    # A direction with no maker seat authorises write dispatch on
                    # `mechanism` alone. Until 1.10.0 that flag needed nothing but
                    # a non-empty string: the ledger rule below only ever ran for
                    # maker entries, so "Policy refuses a `true` the ledger
                    # contradicts" was true of one shape of write seat and silent
                    # about the other four.
                    needle = f"transports.{runtime}.{name}.write_verified"
                    matching = [
                        item for item in ledger
                        if isinstance(item, Mapping)
                        and needle in str(item.get("item", ""))
                    ]
                    if not matching:
                        raise ConfigError(
                            f"transports.{runtime}.{name} declares write_verified: "
                            f"true with no maker seat, and verification_ledger has "
                            f"no row naming {needle}")
                    bad = [item for item in matching
                           if item.get("status") != "verified"]
                    if bad:
                        raise ConfigError(
                            f"transports.{runtime}.{name} declares write_verified: "
                            f"true, but verification_ledger records "
                            f"{bad[0].get('item')!r} as {bad[0].get('status')!r}")
                if "mechanism_maker" in entry:
                    needle = f"{runtime}.{name}.mechanism_maker"
                    matching = [
                        item for item in ledger
                        if isinstance(item, Mapping)
                        and needle in str(item.get("item", ""))
                    ]
                    if not matching:
                        raise ConfigError(
                            f"transports.{runtime}.{name} declares write_verified: true, "
                            f"but verification_ledger has no maker-seat row")
                    bad = [item for item in matching
                           if item.get("status") != "verified"]
                    if bad:
                        raise ConfigError(
                            f"transports.{runtime}.{name} declares write_verified: true, but "
                            f"verification_ledger records {bad[0].get('item')!r} as "
                            f"{bad[0].get('status')!r}")

    def worker_seat_kind(self, task_class: str) -> str:
        """The class default for whether this route's worker writes.

        Validated at build time, so this is a lookup and never a guess: a
        missing class has no honest default to fall back on, and a default
        invented here would be a fail-open one.
        """
        return self.task_write_seat[task_class]

    def write_capable(self, runtime: str, family: str) -> bool:
        """Does this host have a recipe of record for dispatching WRITE work
        to this family?

        The router names models; it does not dispatch them. But a route that
        names a model no conforming dispatcher can execute for the work it was
        selected to do is exactly the "executable as written" failure this
        module's docstring forbids — so the seat capability is read here, from
        the same table the caller will follow.

        Two ways to be write-capable, and neither is inferred from a spelling:

        1. The family is the host's own. `transports.<runtime>.native` is the
           host session itself, not a bridge, so requiring a recipe there would
           refuse the one seat that needs none. `_validate_native_families`
           holds `local_family` and the transports table to the same answer, so
           this branch cannot be opened by editing the other table.
        2. The direction declares `write_verified: true`. `verified` attests
           the DIRECTION — for `to_xai` it attests the reviewer seat, while the
           verification ledger records the maker seat as not shipped — so it
           was never authorization for write dispatch, and reusing it meant one
           added config line re-opened the escape paths the maker gate exists
           to close. `_validate_write_seats` requires a write-capable recipe
           behind the flag and refuses a value the ledger contradicts.

        This stays a lookup rather than a hardcoded family exclusion: the day a
        maker recipe passes its gate, shipping it and recording the probe in
        the ledger is all it takes to let the family back in.
        """
        if family == self.local_family.get(runtime):
            return True
        entry = self.cfg["transports"].get(runtime, {}).get(f"to_{family}")
        # `Mapping`, not `dict`: `Policy.of` supports a non-dict Mapping config
        # on purpose, and the config audit's read-recorder is one. An
        # `isinstance(entry, dict)` guard short-circuited to False for every
        # cross-family direction under such a config — taking the verified
        # openai and claude bridges down with the unshipped xai maker, and
        # never reading `verified` at all.
        if not isinstance(entry, Mapping) or entry.get("verified") is not True:
            return False
        return entry.get("write_verified") is True

    @classmethod
    def of(cls, cfg: dict) -> "Policy":
        """Derived policy for `cfg`, rebuilt whenever its content moves.

        Identity alone was not enough once `policy_sha256` became a CONTENT
        digest (design §4 B1): a caller that mutates a cfg dict in place
        between routes got semantics precomputed from the pre-mutation
        content while the emitted digest attested the post-mutation content
        — one fingerprint over two different decisions, which is precisely
        what the fingerprint claims cannot happen.

        The digest is computed once here and published as `content_sha`, so
        `route()` emits the same value it keyed on instead of dumping the
        same object a second time; two independent canonicalisations agreeing
        only because nothing happened in between is not a guarantee.

        A non-dict Mapping (test instrumentation) has no content digest —
        `route()` routes those to the on-disk digest for the same reason —
        so it keeps the identity-only behaviour it always had.
        """
        digest = canonical_policy_sha256(cfg) if isinstance(cfg, dict) else None
        cached = cls._cache.get(id(cfg))
        if cached is None or cached[0] != digest:
            policy = cls(cfg)
            policy.content_sha = digest
            cls._cache[id(cfg)] = (digest, policy)
            return policy
        return cached[1]

    # ordered-enum helpers, bound to this policy's vocabulary
    def band_max(self, a, b): return self.bands[max(self.bands.index(a), self.bands.index(b))]

    def band_of(self, score: int) -> str: return self._risk_band_of[score]

    def execution_band_of(self, score: int) -> str: return self._exec_band_of[score]
    def effort_max(self, a, b): return self.efforts[max(self.efforts.index(a), self.efforts.index(b))]
    def effort_up(self, e, n=1): return self.efforts[min(self.efforts.index(e) + n, len(self.efforts) - 1)]

    def native_effort(self, model: str, effort: str) -> str:
        """Spell effective conceptual effort for the resolved model."""
        return self.effort_map_of[model][effort]

    def role_max(self, a, b): return self.roles[max(self.roles.index(a), self.roles.index(b))]
    def role_above(self, r, n=1): return self.roles[min(self.roles.index(r) + n, len(self.roles) - 1)]
    def at_ceiling(self, r): return self.roles.index(r) == len(self.roles) - 1


class _LRU(OrderedDict):
    """A dict that keeps its `maxsize` most recently used entries."""

    def __init__(self, maxsize: int):
        super().__init__()
        self.maxsize = maxsize

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
            return super().__getitem__(key)
        return default

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.maxsize:
            self.popitem(last=False)


Policy._cache = _LRU(8)

_DEFAULT_CFG: dict | None = None


def default_config() -> dict:
    """Lazy so a missing or broken config surfaces at call time, not import."""
    global _DEFAULT_CFG
    if _DEFAULT_CFG is None:
        _DEFAULT_CFG = load_config()
    return _DEFAULT_CFG


def _default_policy() -> Policy:
    return Policy.of(default_config())


# Module-level vocabulary, kept for callers that import these names. They
# describe the default config; `route(task, cfg)` uses the cfg it was handed.
def __getattr__(name: str):
    p = _default_policy()
    mapping = {
        "BANDS": p.bands, "EFFORTS": p.efforts, "ROLES": p.roles,
        "TASK_CLASSES": p.task_classes,
        "CRITICAL_DOMAIN_FLAGS": p.critical_domain_flags,
        "KNOWN_FLAGS": p.known_flags, "MODEL_IDS": p.model_ids,
        "RUNTIMES": p.runtimes,
    }
    if name in mapping:
        return mapping[name]
    raise AttributeError(name)


# --------------------------------------------------------------------------
# Effective policy — base config + local model state (design 2026-09-25 DD-A2)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EffectivePolicy:
    """The config a route runs on and where it came from. `config` is None
    exactly when the state cannot be used: the route is then the
    MODEL_STATE_UNAVAILABLE terminal, whose reason `provenance` carries."""
    config: dict | None
    provenance: "model_state.Provenance | None"


# digest -> effective config: one object per content, so `Policy.of` (keyed
# on identity + digest) hits its cache for every route on the same generation.
_EFFECTIVE_CACHE = _LRU(8)


def _interned(cfg: dict, digest: str) -> dict:
    cached = _EFFECTIVE_CACHE.get(digest)
    if cached is not None:
        return cached
    _EFFECTIVE_CACHE[digest] = cfg
    return cfg


def _unavailable(base_sha: str, reason: str, detail: str | None = None) -> EffectivePolicy:
    return EffectivePolicy(None, model_state.Provenance(
        status="unavailable", base_policy_sha256=base_sha, state_reason=reason,
        state_detail=detail))


def resolve_effective_policy(env: Mapping[str, str], home: Path,
                             pin: str | None = None) -> EffectivePolicy:
    """Base config + the committed generation, read afresh on every call.

    Reads exactly `committed/current.json`, the generation it names, and the
    probe summary of each entry that reaches the summary check — no directory
    scan. `DEEP_MODEL_ROUTER_OVERLAY=off` drops the entries only; history and
    revocations still apply, and a damaged state fails closed either way.
    """
    base = default_config()
    base_sha = canonical_policy_sha256(base)
    off = model_state.overlay_off(env)
    root_path = model_state.state_root_path(env, home)
    with model_state.read_state(root_path) as state:
        if state.shape == "unreadable" and state.root_unadmitted:
            return _unavailable(base_sha, "root_unadmitted",
                                f"{state.detail}; fix: {model_state.root_fix(root_path)}")
        if state.shape == "unreadable":
            return _unavailable(base_sha, "unreadable")
        if state.shape == "absent":
            cfg, prov = base, None
        else:
            cfg, prov = model_state.effective_config(
                base, state.generation, apply_entries=not off, summary=state.summary,
                generation_sha256=state.generation_sha256, base_sha=base_sha)
        digest = base_sha if cfg is base else canonical_policy_sha256(cfg)
        if pin is None or pin == digest:
            return EffectivePolicy(cfg if cfg is base else _interned(cfg, digest), prov)
        return _resolve_pin(pin, base, base_sha, state, off)


def _check_pin(pin) -> None:
    if not model_state.is_hex64(pin):
        raise ValidationError("policy_pin must be a lowercase 64-hex policy_sha256")


def _resolve_pin(pin: str, base: dict, base_sha: str, state: "model_state.StateRead",
                 off: bool) -> EffectivePolicy:
    """Walk `parent_generation_sha256` from the current generation (at most
    MAX_PIN_CHAIN steps) and recompute each one as current base + that
    generation's entries (unless off) and history + the CURRENT generation's
    revocations; the null parent at the end of the chain is the stateless
    policy (base + the current revocations). The first digest equal to the
    pin is the policy. Only
    committed generations are on the chain, so an unpublished one is
    unreachable; a later revocation changes the recomputed digest, so revert
    beats pin.

    No match: one reason, in a fixed order — off (entries back on would
    match), revoked (the generation's OWN revocations would match), base
    changed (a generation on the chain recorded another base), missing.
    """
    if state.shape != "ok":
        return _unavailable(base_sha, "pin_generation_missing")
    current_blocked = state.generation["blocked_ids"]
    suppressed = revoked = base_changed = False
    sha, gen = state.generation_sha256, state.generation

    def digest(g, g_sha, apply, blocked):
        cfg, prov = model_state.effective_config(
            base, g, apply_entries=apply, summary=state.summary, blocked_ids=blocked,
            generation_sha256=g_sha, base_sha=base_sha)
        return cfg, prov, (base_sha if cfg is base else canonical_policy_sha256(cfg))

    for _ in range(model_state.MAX_PIN_CHAIN):
        cfg, prov, d = digest(gen, sha, not off, current_blocked)
        if d == pin:
            prov.status = "pinned"
            return EffectivePolicy(cfg if cfg is base else _interned(cfg, d), prov)
        if off and digest(gen, sha, True, current_blocked)[2] == pin:
            suppressed = True
        if digest(gen, sha, not off, gen["blocked_ids"])[2] == pin:
            revoked = True
        if gen["base_policy_sha256"] != base_sha:
            base_changed = True
        sha = gen["parent_generation_sha256"]
        if sha is None:
            # The chain's end is the stateless policy the first generation
            # replaced: base + nothing, still under the CURRENT revocations. A
            # route taken before any `committed/` existed pinned exactly this.
            empty = model_state.empty_generation(base_sha)
            cfg, prov, d = digest(empty, None, True, current_blocked)
            if d == pin:
                prov.status = "pinned"
                return EffectivePolicy(cfg if cfg is base else _interned(cfg, d), prov)
            if digest(empty, None, True, [])[2] == pin:
                revoked = True
            break
        try:
            gen = state.generation_at(sha)
        except (OSError, ValueError):
            break
    reason = ("pin_suppressed_by_off" if suppressed else "pin_revoked" if revoked
              else "pin_base_changed" if base_changed else "pin_generation_missing")
    return _unavailable(base_sha, reason)


# The per-model disclosure for a seat whose id came from the local overlay.
OVERLAY_SEAT_NOTE = ("{key}: id from local model overlay; capability tier inherited "
                     "from the lineage (not re-measured); price unavailable; maker seat "
                     "not re-probed for this id (containment is the transport recipe's)")

STATE_UNAVAILABLE_MESSAGES = {
    "unreadable": ("local model state under committed/ cannot be read; no seat is "
                   "named while revocations are unknown — run model_sync.py repair "
                   "(model_sync.py status shows the detail), or delete committed/ to "
                   "start from the bundled policy"),
    "root_unadmitted": ("the local model state root failed admission ({detail}); no "
                        "seat is named while revocations are unknown"),
}

# The "Every route emits" inventory (references/control-loop.md), as the
# values a route that decided nothing carries: null for a scalar, [] for a
# list. The terminal below emits every key so a consumer indexing a
# documented field never raises.
_NULL_ROUTE_LISTS = frozenset({
    "selected_families", "band_overrides_applied", "critical_flags",
    "band_overrides_redundant", "fallbacks_applied", "effort_ceiling_applied",
    "fallback_compensations_applied", "unavailable_models", "excluded_prior_failures",
    "human_control_causes"})
_NULL_ROUTE_SCALARS = (
    "task_class", "complexity", "uncertainty", "blast_radius", "reversibility",
    "effective_policy", "selected_capability_tier", "local_policy_applied",
    "reasoning_centric", "risk_score", "risk_band", "execution_score", "execution_band",
    "route_path", "selected_role", "selected_model", "selected_effort",
    "selected_effort_effective", "selected_effort_native", "cross_family_review",
    "escalation_count", "retry_count", "routing_confidence", "routing_confidence_kind",
    "worker_seat", "host_seat_advisory", "worker_seat_state", "implementer_declared",
    "implementer_source")
_NULL_REVIEW_LISTS = frozenset({"reviewers", "reviewer_models", "review_depth_reduced",
                                "self_review_avoided", "required_checks"})
_NULL_REVIEW_SCALARS = ("band", "mode", "effort", "independence_required", "review_independence",
                        "independence_compromised", "judge_unavailable",
                        "band_floor_unsatisfiable", "compensating_reviewers", "judge",
                        "judge_model")


def _state_unavailable_route(prov: "model_state.Provenance") -> dict:
    """The MODEL_STATE_UNAVAILABLE terminal. Built before any request is
    validated, so it carries no request digest, no request echo and names no
    model at all — every documented key is present, holding null or []."""
    terminal = "MODEL_STATE_UNAVAILABLE"
    template = STATE_UNAVAILABLE_MESSAGES.get(prov.state_reason)
    message = template.format(detail=prov.state_detail) if template else (
        f"the pinned policy cannot be reproduced ({prov.state_reason}) — start the "
        f"loop afresh or drop the pin")
    out: dict = {k: None for k in _NULL_ROUTE_SCALARS}
    out.update({k: [] for k in _NULL_ROUTE_LISTS})
    review: dict = {k: None for k in _NULL_REVIEW_SCALARS}
    review.update({k: [] for k in _NULL_REVIEW_LISTS})
    out.update({
        "route_schema_version": ROUTE_SCHEMA_VERSION,
        "router_plugin_version": plugin_manifest_version(),
        "policy_sha256": None,
        "request_sha256": None,
        "decision_fingerprint": None,
        "terminal": terminal,
        "review": review,
        "requires_human_confirmation": False,
        "human_confirmation_deferred": False,
        "model_overlay": prov.to_json(),
        "notes": [message],
        "rationale": f"TERMINAL {terminal}: {message}",
    })
    return out


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

@dataclass
class Task:
    """Everything the model must decide before the deterministic part runs.

    Validation is strict and loud. Silently ignoring an unknown flag is how a
    classifier ends up believing it asked for a protection it never got.
    """

    task_class: str
    complexity: int
    uncertainty: int
    blast_radius: int
    reversibility: int
    reasoning_centric: bool = False
    flags: list[str] = field(default_factory=list)
    prior_failures: int = 0
    prior_models: list[str] = field(default_factory=list)
    runtime: str = "claude_code"
    # Does THIS route's worker need a write-capable dispatch recipe? None means
    # "use the class default" (`task_write_seat`). Declared, it wins in both
    # directions — the class generalisation is wrong both ways, and a caller
    # who can only tighten it would still be reaching for
    # `--unavailable-models` in the other direction.
    worker_seat: str | None = None
    unavailable_roles: list[str] = field(default_factory=list)
    unavailable_models: list[str] = field(default_factory=list)
    # Caller's attestation that reviewer isolation *can* be achieved. This is a
    # capability claim, not proof that it happened — see `isolation_evidence`.
    isolation_available: bool | None = None
    # Post-dispatch proof: one distinct session/process identifier per
    # reviewer. Only this can raise independence to `enforced`.
    isolation_evidence: list[str] = field(default_factory=list)
    # Set by route(); not part of the caller's input contract.
    _policy: Any = field(default=None, repr=False, compare=False)
    # RouteRequestV1 local_policy. None = omitted. Not accepted via --json.
    _local_policy: dict | None = field(default=None, repr=False, compare=False)
    # RouteRequestV1's optional declaration of the host's actual seat.
    _host_seat: dict | None = field(default=None, repr=False, compare=False)
    _review_context: dict | None = field(default=None, repr=False, compare=False)
    _attempt_outcomes: list[dict] | None = field(default=None, repr=False, compare=False)
    # RouteRequestV1 / --policy-pin: the policy digest to reproduce (DD-A9). A
    # policy SELECTOR, not request content — never part of request_sha256.
    _policy_pin: str | None = field(default=None, repr=False, compare=False)
    # RouteRequestV1 `implementer` (DD-B1): caller-declared, like review_context.
    _implementer: dict | None = field(default=None, repr=False, compare=False)
    # availability_snapshot.checks_available (DD-B4): false when this repository
    # cannot run the deterministic checks a LOW review consists of. None = true.
    _checks_available: bool | None = field(default=None, repr=False, compare=False)
    # availability_snapshot.family_quota (DD-B8): {family: ok|low|exhausted},
    # the caller's reading of each provider's remaining quota.
    _family_quota: dict | None = field(default=None, repr=False, compare=False)
    # Set by route() when the same-model retry rule holds (DD-B6): {"model": id,
    # "effort": level}. Not caller input and never part of request_sha256.
    _same_model_retry: dict | None = field(default=None, repr=False, compare=False)

    @property
    def total_prior_attempts(self) -> int:
        return len(self._attempt_outcomes) if self._attempt_outcomes is not None else self.prior_failures

    def validate(self, policy: Policy) -> None:
        self._require_choice("task_class", self.task_class, policy.task_classes)
        for name in ("complexity", "uncertainty", "blast_radius", "reversibility"):
            self._require_score(name, getattr(self, name))
        self._require_bool("reasoning_centric", self.reasoning_centric)
        self._require_int("prior_failures", self.prior_failures, minimum=0)
        self._require_choice("runtime", self.runtime, sorted(policy.runtimes))
        if self.worker_seat is not None:
            self._require_choice("worker_seat", self.worker_seat,
                                 list(WORKER_SEAT_KINDS))
        self._require_str_list("flags", self.flags, policy.known_flags, "flag")
        self._require_str_list("unavailable_roles", self.unavailable_roles,
                               frozenset(policy.roles), "role")
        self._require_str_list("unavailable_models", self.unavailable_models,
                               policy.model_ids, "model id")
        # An alias is accepted by validation and refused by `history_gap`, so
        # the caller gets an actionable terminal naming the flag rather than a
        # usage error. What it must never do is reach resolution.
        self._require_str_list("prior_models", self.prior_models,
                               frozenset(policy.roles) | policy.model_ids, "role or model id")
        if self.isolation_available is not None:
            self._require_bool("isolation_available", self.isolation_available)
        # This field is the only input that can reach `enforced`, so it gets
        # the same strictness as everything else. It previously skipped the
        # shared validator, and a list of integers was enough to report an
        # independence the router had no basis for.
        if isinstance(self.isolation_evidence, str) or not isinstance(self.isolation_evidence, (list, tuple)):
            raise ValidationError("isolation_evidence: expected a list of session identifiers")
        for item in self.isolation_evidence:
            if not isinstance(item, str) or not item.strip():
                raise ValidationError(
                    f"isolation_evidence: expected non-empty strings, got {item!r}"
                )
        # `local_policy` VALUES, not only its key names. The request parser
        # checked the names; nothing checked what they held, and the two halves
        # failed in opposite directions: an unknown effort token was dropped by
        # a membership test while `effective_policy` still reported it as
        # applied — the "recorded a change that did not happen" defect this
        # module's docstring forbids — and a non-numeric tier reached `int()`
        # inside `route()` and crashed to exit 5, the status reserved for
        # outcomes that are never routing outcomes. Validated here rather than
        # in `task_from_request_v1` for two reasons: this is the only place
        # holding a Policy to check the vocabulary against, and it also covers
        # a `Task` constructed directly.
        self._validate_local_policy(policy)
        self._validate_host_seat(policy)
        self._validate_review_context(policy)
        self._validate_attempt_outcomes(policy)
        self._validate_implementer(policy)
        if self._checks_available is not None:
            self._require_bool("availability_snapshot.checks_available", self._checks_available)
        if self._family_quota is not None:
            quota = self._family_quota
            if not isinstance(quota, dict):
                raise ValidationError("availability_snapshot.family_quota must be an object")
            for family, level in quota.items():
                if family not in policy.families:
                    raise ValidationError(f"family_quota: unknown model family {family!r}")
                if level not in FAMILY_QUOTA_LEVELS:
                    raise ValidationError(
                        f"family_quota.{family}: {level!r} is not one of {list(FAMILY_QUOTA_LEVELS)}")
            self._family_quota = dict(sorted(quota.items()))
        self._policy = policy

    def _validate_implementer(self, policy: Policy) -> None:
        """Completed WRITE work only. REVIEW names its source's author in
        `review_context` already, and a read-only class's worker produces a
        judgement rather than work a review could be planned against. Any
        registry id is accepted, history rows included: the work may have
        finished before the registry moved on."""
        decl = self._implementer
        if decl is None:
            return
        if not isinstance(decl, dict) or set(decl) != IMPLEMENTER_KEYS:
            raise ValidationError('implementer must be exactly {"model_id": <registry model id>}')
        model = decl["model_id"]
        if not isinstance(model, str) or model not in policy.model_ids:
            raise ValidationError(f"implementer.model_id: unknown model id {model!r}")
        if self.task_class == "REVIEW":
            raise ValidationError(
                "implementer does not apply to REVIEW: declare the source's author in "
                "review_context.author_model_ids")
        if policy.worker_seat_kind(self.task_class) != "write":
            raise ValidationError(
                f"implementer declares completed write work; {self.task_class} is a "
                f"read-only class")
        self._implementer = {"model_id": model}

    def _validate_attempt_outcomes(self, policy: Policy) -> None:
        history = self._attempt_outcomes
        if history is None:
            return
        if self.prior_failures or self.prior_models:
            raise ValidationError("attempt_outcomes cannot be mixed with legacy failure history")
        if not isinstance(history, list):
            raise ValidationError("attempt_outcomes must be an array or null")
        try:
            ensure_json_value(history)
        except ValueError as exc:
            raise ValidationError(f"attempt_outcomes: {exc}") from None
        required = {"attempt_id", "model_id", "kind", "evidence_sha256"}
        # `effort` and `retry_evidence_sha256` (1.17.0, design 2026-09-25
        # DD-B6): the conceptual effort the attempt actually ran at, and fresh
        # evidence for a same-model retry. Optional, and omitted from the
        # normalised record when absent, so a 1.16 history hashes as it did.
        optional = {"recovery_sha256", "effort", "retry_evidence_sha256"}
        seen, normalized = set(), []
        for item in history:
            if (not isinstance(item, dict) or not required <= set(item)
                    or set(item) - (required | optional)):
                raise ValidationError("attempt_outcomes entry has missing or unknown fields")
            attempt_id = item["attempt_id"]
            if (not isinstance(attempt_id, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", attempt_id) is None
                    or attempt_id in seen):
                raise ValidationError("attempt_outcomes requires unique safe attempt IDs")
            seen.add(attempt_id)
            self._require_choice("attempt_outcomes.model_id", item["model_id"], sorted(policy.model_ids))
            kind = item["kind"]
            self._require_choice("attempt_outcomes.kind", kind, sorted(ATTEMPT_OUTCOME_KINDS))
            for key in ("evidence_sha256", "recovery_sha256"):
                value = item.get(key)
                if key == "recovery_sha256" and value is None:
                    continue
                if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    raise ValidationError(f"attempt_outcomes.{key} must be lowercase hex64")
            if kind not in OPERATIONAL_OUTCOMES and item.get("recovery_sha256") is not None:
                raise ValidationError("recovery evidence cannot bypass capability failure or unconfirmed termination")
            if item.get("recovery_sha256") == item["evidence_sha256"]:
                raise ValidationError("recovery evidence must differ from the failed attempt evidence")
            if "effort" in item and item["effort"] not in policy.efforts:
                raise ValidationError(
                    f"attempt_outcomes.effort: {item['effort']!r} is not one of {policy.efforts}")
            if "retry_evidence_sha256" in item:
                value = item["retry_evidence_sha256"]
                if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    raise ValidationError("attempt_outcomes.retry_evidence_sha256 must be lowercase hex64")
                if kind != "capability_failure":
                    raise ValidationError(
                        "retry_evidence_sha256 is evidence for a same-model retry after a "
                        "capability_failure; it has no meaning on any other kind")
            normalized.append({**item, "recovery_sha256": item.get("recovery_sha256")})
        # Fresh evidence has to be fresh: a retry hash that repeats any hash in
        # the history — its own attempt's evidence included — proves nothing new.
        for i, row in enumerate(normalized):
            fresh = row.get("retry_evidence_sha256")
            if fresh is None:
                continue
            others = {h for j, other in enumerate(normalized)
                      for k, h in other.items() if k.endswith("sha256") and h is not None
                      and not (j == i and k == "retry_evidence_sha256")}
            if fresh in others:
                raise ValidationError(
                    "attempt_outcomes.retry_evidence_sha256 repeats a hash already in the history")
        self._attempt_outcomes = normalized

    def _validate_review_context(self, policy: Policy) -> None:
        ctx = self._review_context
        if ctx is None:
            return
        if (self.task_class != "REVIEW"
                or (self.worker_seat or policy.worker_seat_kind(self.task_class)) != "read_only"):
            raise ValidationError("review_context requires a read-only REVIEW task")
        if not isinstance(ctx, dict):
            raise ValidationError("review_context must be an object or null")
        try:
            ensure_json_value(ctx)
        except ValueError as exc:
            raise ValidationError(f"review_context: {exc}") from None
        if set(ctx) - REVIEW_CONTEXT_KEYS:
            raise ValidationError("review_context has unknown fields")
        digest = ctx.get("target_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError("review_context.target_sha256 must be lowercase hex64")
        ids = ctx.get("author_model_ids", [])
        families = ctx.get("author_families", [])
        self._require_str_list("review_context.author_model_ids", ids, policy.model_ids, "model id")
        self._require_str_list("review_context.author_families", families, policy.families, "family")
        if not ids and not families:
            raise ValidationError("review_context must declare author models or families")
        self._review_context = {"target_sha256": digest,
                                "author_model_ids": sorted(set(ids)),
                                "author_families": sorted(set(families))}

    def _validate_host_seat(self, policy: Policy) -> None:
        hs = self._host_seat
        if hs is None:
            return
        if not isinstance(hs, dict):
            raise ValidationError(
                f"host_seat: expected an object or null, got {type(hs).__name__}")
        if (unknown := set(hs) - HOST_SEAT_KEYS):
            raise ValidationError(
                f"host_seat has unknown field(s): {', '.join(sorted(unknown))}")
        model = hs.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValidationError("host_seat.model: expected a non-empty model id")
        model = model.strip()
        effort = hs.get("effort")
        if effort is not None and effort not in policy.efforts:
            raise ValidationError(
                f"host_seat.effort: {effort!r} is not one of {list(policy.efforts)}")
        if model in policy.tier_of:
            family = policy.family_of[model]
            local = policy.local_family[self.runtime]
            if family != local:
                raise ValidationError(
                    f"host_seat.model {model!r} is family {family!r}, but a "
                    f"{self.runtime} host runs family {local!r}")
            ceiling = policy.ceiling_of.get(model)
            if (effort is not None and ceiling is not None
                    and policy.efforts.index(effort) > policy.efforts.index(ceiling)):
                raise ValidationError(
                    f"host_seat.effort {effort!r} exceeds {model!r}'s ceiling "
                    f"{ceiling!r} — that host state cannot exist")
        self._host_seat = {"model": model, "effort": effort}

    def _validate_local_policy(self, policy: Policy) -> None:
        lp = self._local_policy
        if lp is None:
            return
        if not isinstance(lp, dict):
            raise ValidationError(
                f"local_policy: expected an object, got {type(lp).__name__}")
        if (unknown := set(lp) - LOCAL_POLICY_KEYS):
            raise ValidationError(
                f"local_policy has unknown field(s): {', '.join(sorted(unknown))}")
        if (effort := lp.get("minimum_effort")) is not None and effort not in policy.efforts:
            # Listed in policy order, not sorted: `_require_choice` alphabetises,
            # which is right for a set of names and wrong for a floor — a caller
            # told the levels are [HIGH, LOW, MAX, MEDIUM, MINIMAL, VERY_HIGH]
            # cannot see which one is a higher floor than the value they typed.
            raise ValidationError(
                f"local_policy.minimum_effort: {effort!r} is not one of "
                f"{policy.efforts} (weakest to strongest)")
        # Zero is meaningful for all three (an explicit "no floor"), and a
        # negative floor is not a weaker ask — it is a value no comparison in
        # `route()` can act on.
        for name in ("minimum_capability_tier", "minimum_reviewers",
                     "minimum_provider_families"):
            if (value := lp.get(name)) is not None:
                self._require_int(f"local_policy.{name}", value, minimum=0)
        # An empty list is legal and deliberately load-bearing: it is the one
        # value that makes the policy unsatisfiable by construction, which
        # `route()` reports as a terminal rather than as an error.
        if (fams := lp.get("allowed_families")) is not None:
            self._require_str_list("local_policy.allowed_families", fams,
                                   policy.families, "model family")

    # -- validators ------------------------------------------------------

    @staticmethod
    def _require_choice(name, value, allowed):
        if value not in allowed:
            raise ValidationError(f"{name}: {value!r} is not one of {sorted(allowed)}")

    @staticmethod
    def _require_int(name, value, minimum=None):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{name}: expected an integer, got {type(value).__name__}")
        if minimum is not None and value < minimum:
            raise ValidationError(f"{name}: must be >= {minimum}, got {value}")

    @classmethod
    def _require_score(cls, name, value):
        cls._require_int(name, value)
        if not 0 <= value <= MAX_DIMENSION_SCORE:
            raise ValidationError(
                f"{name}: must be between 0 and {MAX_DIMENSION_SCORE}, got {value}")

    @staticmethod
    def _require_bool(name, value):
        if not isinstance(value, bool):
            raise ValidationError(f"{name}: expected a boolean, got {type(value).__name__} {value!r}")

    @staticmethod
    def _require_str_list(name, value, allowed, label):
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValidationError(f"{name}: expected a list, got {type(value).__name__}")
        for item in value:
            if not isinstance(item, str):
                raise ValidationError(f"{name}: expected strings, got {type(item).__name__}")
            if item not in allowed:
                raise ValidationError(f"{name}: unknown {label} {item!r}")

    # -- convenience -----------------------------------------------------

    def has(self, flag: str) -> bool:
        return flag in self.flags

    def critical_flags(self, policy: Policy) -> list[str]:
        return [f for f in policy.critical_domain_flags if f in self.flags]

    def failed_models(self, policy: Policy) -> set[str]:
        """The models that already failed, as concrete ids.

        Round 13 removed the alias half of this. `prior_models` only reaches
        resolution when `history_gap` passed, and that requires every entry to
        be a concrete id — so the branch that resolved an alias to "the model it
        probably held", along with the `excluded_as_ambiguous_alias` field it
        fed, became unreachable the moment the router stopped inferring history.
        Leaving them would have been a permanently-empty field and a dead
        inference, which is the shape this artifact keeps having to delete.
        """
        return {m for m in self.prior_models if m in policy.model_ids}


# --------------------------------------------------------------------------
# Stage 2 — score
# --------------------------------------------------------------------------

def score(task: Task, cfg: dict) -> int:
    w = cfg["router"]["score_weights"]
    return (task.complexity * w["complexity"] + task.uncertainty * w["uncertainty"]
            + task.blast_radius * w["blast_radius"] + task.reversibility * w["reversibility"])


def band_from_score(value: int, policy: Policy) -> str:
    """Load-time validation covers 0..max_risk_score exactly, so the table is
    total over the reachable scores; the former lookup-time ConfigError could
    no longer fire (design DD-1, [R2-opus-F10])."""
    return policy.band_of(value)


def execution_score(task: Task, cfg: dict) -> int:
    ex = cfg["execution"]
    w, fw = ex["score_weights"], ex["flag_weights"]
    return (task.complexity * w["complexity"] + task.uncertainty * w["uncertainty"]
            + sum(weight for flag, weight in fw.items() if task.has(flag)))


# --------------------------------------------------------------------------
# Stage 3 — overrides, evaluated from config
# --------------------------------------------------------------------------

KNOWN_EFFECT_KEYS = frozenset({"band_at_least", "band_exactly", "route"})


def _predicate(node: Any, task: Task, cfg: dict) -> bool:
    if not isinstance(node, dict) or len(node) != 1:
        raise ConfigError(f"malformed override predicate: {node!r}")
    (op, arg), = node.items()
    if op == "flag":
        return task.has(arg)
    if op == "any_flag_in":
        return any(task.has(f) for f in cfg["flags"][arg])
    if op == "dimension_at_least":
        (dim, threshold), = arg.items()
        return getattr(task, dim) >= threshold
    if op == "all":
        return all(_predicate(sub, task, cfg) for sub in arg)
    if op == "any":
        return any(_predicate(sub, task, cfg) for sub in arg)
    raise ConfigError(f"unknown override operator: {op!r}")


def apply_overrides(task: Task, band: str, policy: Policy) -> tuple[str, list[str], list[str], str | None]:
    """Returns (band, fired_overrides, redundant_overrides, route_path).

    An unknown effect key raises rather than being skipped. The asymmetry the
    other way round — strict about operators, lax about effects — let a single
    typo turn a safety rule into decoration that still reported itself as
    applied, and no test could tell the difference.

    `redundant` means the override fired and asked for a band the task had
    already reached by another rule. That is expected and healthy: several
    rules independently agreeing on CRITICAL is redundancy by design, not a
    rule that failed to work. It is reported separately only so the rationale
    can show which rule actually moved the number.
    """
    cfg = policy.cfg
    applied: list[str] = []
    redundant: list[str] = []
    route_path: str | None = None

    for entry in cfg["overrides"]:
        effect = entry["effect"]
        unknown = set(effect) - KNOWN_EFFECT_KEYS
        if unknown:
            raise ConfigError(
                f"override {entry['name']!r} has unknown effect key(s) {sorted(unknown)}; "
                f"known keys are {sorted(KNOWN_EFFECT_KEYS)}"
            )
        if not _predicate(entry["when"], task, cfg):
            continue

        changed = False
        if "band_at_least" in effect:
            new = policy.band_max(band, effect["band_at_least"])
            changed |= new != band
            band = new
        if "band_exactly" in effect:
            changed |= band != effect["band_exactly"]
            band = effect["band_exactly"]
        if "route" in effect:
            route_path = effect["route"]
            changed = True

        applied.append(entry["name"])
        if not changed:
            redundant.append(entry["name"])

    return band, applied, redundant, route_path


# --------------------------------------------------------------------------
# Stage 4 — worker
# --------------------------------------------------------------------------

def history_gap(task: Task, policy: Policy) -> str | None:
    """Why this task's retry history cannot be used, or `None` if it can.

    Checked BEFORE anything resolves. Round 13: the check sat inside
    `select_worker`, which runs after `Resolver.__init__` has already read
    `prior_models` — so an alias still produced inferred `excluded_prior_failures`
    on a route whose whole point was that aliases identify nothing, a history
    long enough to exhaust every candidate raised `ValidationError` (exit 2,
    "invalid input") before the terminal could be reported, and a configurable
    terminal could fire first and hide the real reason.

    `prior_failures == 0` is checked too: naming models that failed while
    declaring no failures is a contradiction, and it was silently accepted —
    the models were excluded from resolution and the route emitted at exit 0.
    """
    named, count = list(task.prior_models), task.prior_failures
    concrete = [m for m in named if m in policy.model_ids]
    if len(concrete) == len(named) == count:
        return None
    detail = ""
    if len(concrete) != len(named):
        detail = " (role aliases do not identify which model held that seat)"
    return (f"retry history required: {count} prior failure(s) but {len(concrete)} "
            f"concrete model id(s) supplied{detail} — pass --prior-models with one "
            f"model id per failure")


def _promote_above(floor: int, policy: Policy, resolver: "Resolver",
                   *, write: bool = False) -> str | None:
    """The weakest role whose RESOLVED model outranks `floor`.

    `None` when no such role exists — a real exhaustion, not a clamp.

    Round 10 briefly grew an `avoid` set here, holding the models a
    reconstruction believed earlier attempts had run, so a retry could not
    re-dispatch one. It was inert and provably so: the walk sets `floor` to the
    tier of each model it adds, so every believed-run model sits at a tier at
    or below `floor`, while this function only ever returns roles strictly
    above it. The intersection is empty by construction, and a probe over the
    reachable space found 0 inputs where removing it changed anything. It is
    gone rather than kept as insurance — this module has spent three rounds
    removing policy that changes nothing while reading as protective, and
    adding some in the same breath would be worse than the defect.

    """
    def tier(role):
        model = resolver.peek(role, write=write)
        return policy.tier_of[model] if model else -1

    stronger = [r for r in policy.roles if tier(r) > floor]
    return min(stronger, key=lambda r: (tier(r), policy.roles.index(r)), default=None)


@dataclass(frozen=True)
class WorkerChoice:
    """One resolved worker selection: the role, the notes that selected it,
    and whether the retry ladder ran out on the way (design DD-2, S4/S5)."""
    role: str
    notes: tuple[str, ...]
    ceiling_exhausted: bool


def _resolve_cell(cell: str, task: Task, notes: list[str]) -> str:
    if cell == "by_reasoning_centric":
        role = "reasoning_specialist" if task.reasoning_centric else "senior_engineer"
        notes.append(f"reasoning_centric={task.reasoning_centric} selected {role}")
        return role
    return cell


def _legacy_pre(task: Task, band: str, policy: Policy) -> tuple[str, list[str]]:
    """S1: the 1.12.1 table cell plus its class promotions, unchanged."""
    cfg = policy.cfg
    notes: list[str] = []
    worker = _resolve_cell(cfg["worker_selection"][task.task_class][band], task, notes)

    if task.task_class == "ARCHITECTURE" and (task.uncertainty == 3 or task.has("long_horizon")):
        if worker != "principal_architect":
            worker = "principal_architect"
            notes.append("architecture promotion: uncertainty==3 or long_horizon")

    if task.task_class == "DEBUGGING" and task.has("unknown_root_cause") and task.prior_failures >= 2:
        target = "reasoning_specialist" if task.reasoning_centric else "senior_engineer"
        if policy.roles.index(target) > policy.roles.index(worker):
            worker = target
            notes.append("debugging promotion: unknown root cause after 2+ failures")

    if task.task_class == "INVESTIGATION" and task.has("unknown_root_cause"):
        if policy.roles.index("worker_balanced") > policy.roles.index(worker):
            worker = "worker_balanced"
            notes.append("investigation promotion: unknown root cause")

    return worker, notes


def _floor_and_ladder(task: Task, worker: str, notes: list[str], policy: Policy,
                      resolver: "Resolver") -> WorkerChoice:
    """S4/S5: the critical-domain floor and the retry ladder, unchanged code."""
    cfg = policy.cfg
    if task.critical_flags(policy):
        # The floor is written in the config as a role, but what it means is a
        # minimum CAPABILITY — "not the cheapest model" — so it is enforced on
        # the resolved tier. Enforcing the label instead breaks in both
        # directions under scarcity: it can demand a promotion that lands on a
        # weaker model, and it reads a `worker_fast` seat holding the frontier
        # model as a floor violation. The retry ladder above can legitimately
        # leave the worker on a low-ordinal role holding a strong model.
        named = cfg["router"]["floors"]["critical_domain_worker"]
        floor_tier = policy.tier_of[cfg["models"][resolver.binding[named]]["id"]]
        current = resolver.peek(worker, write=True)
        if current is None or policy.tier_of[current] < floor_tier:
            promoted = _promote_above(floor_tier - 1, policy, resolver, write=True)
            # Recorded only when a promotion was found. The tier precondition
            # on the branch above is what suppresses the spurious notes — an
            # earlier comment here credited a `peek(promoted) != current` test
            # that could never be false, since `promoted` is drawn from a
            # strictly higher tier than `current` by construction.
            #
            # Measured over 570,240 paired
            # critical-domain routes this floor never changes the dispatched
            # model or the terminal state — `worker_selection` already places
            # every class at `worker_balanced` or above once a critical-domain
            # flag forces the band to HIGH. It fired a note on 2,145 of them
            # anyway, which is this module's own forbidden shape: a recorded
            # change that changed nothing. It stays as a guard against a future
            # edit to that table; it does not get to claim credit meanwhile.
            if promoted is not None:
                notes.append(f"critical-domain floor raised worker to {promoted} "
                             f"(capability tier {floor_tier} or better)")
                worker = promoted
            # `None` means no reachable model meets the floor at all. The worker
            # stays below it and nothing is recorded here — a latent fail-open,
            # unreachable today because every binding holds a tier-1 model. The
            # thing that would notice is
            # `test_a_critical_domain_flag_always_reaches_high_review_and_worker`,
            # which asserts the dispatched tier rather than this note.

    ceiling_exhausted = False
    # `history_gap` is the same predicate `route()` checked before resolving. It
    # is asked again here rather than passed in, so this function cannot be
    # called into the retry branch with a history it must not act on.
    if task.prior_failures >= 1 and history_gap(task, policy) is None:
        # The router does not reconstruct its own history. It asks for it.
        #
        # Rounds 8 through 12 each produced a Critical here, each in a DIFFERENT
        # reading of the same unknowable — `peek` (returns the replacement), the
        # nominal binding (misses a fallback), the candidate ladder (missed a
        # withholding channel), a base captured by position (missed two
        # promotions), and then the consumer of the floor rather than the floor
        # itself (a retry weaker than the first attempt, at exit 0). Five
        # readings, five defects, one radius. All three reviewers of round 12
        # independently recommended deleting the mechanism instead of reading it
        # a sixth way, and the decisive argument is internal: this module
        # already concedes that attempt history belongs to the caller —
        # `retry.same_model_same_effort` and its siblings are documented as
        # "budget for the CALLING agent's loop; one route() call cannot count
        # attempts". Reconstructing WHICH MODELS those uncounted attempts ran is
        # the same claim it declined to make, one field over.
        #
        # Disclosing the guess and gating it on a human was the previous answer.
        # It asked a person to validate a reconstruction they have no better
        # information about than the router did, which is this module's own
        # definition of disclosure standing in for a control.
        #
        # So: one concrete model id per prior failure, or no route. The caller
        # always has them — it just dispatched them, and `selected_model` is in
        # every route this script emits.
        # The structure was checked before this function ran (see
        # `history_gap`), so reaching here means the history is exact.
        floor = max(policy.tier_of[m] for m in task.prior_models)
        current = resolver.peek(worker, write=True)
        if current is not None and policy.tier_of[current] > floor:
            # Already stronger than everything that failed. `_promote_above`
            # returns the WEAKEST role above the floor, so taking it here
            # could only move the route down — which round 12 caught doing
            # exactly that: a task whose table selection was tier 2 came
            # back at tier 1 after one tier-0 failure, called an escalation,
            # at exit 0. Evidence of difficulty must never weaken a route.
            notes.append(f"retry keeps {worker}: already above the failed "
                         f"capability tier {floor}")
        elif (promoted := _promote_above(floor, policy, resolver,
                                         write=True)) is not None:
            notes.append(f"escalated above capability tier {floor}")
            worker = promoted
        else:
            ceiling_exhausted = True
            notes.append(f"retry ladder exhausted: no usable model is stronger "
                         f"than capability tier {floor}")


    return WorkerChoice(worker, tuple(notes), ceiling_exhausted)


def _tier(policy: Policy, resolver: "Resolver", role: str) -> int:
    model = resolver.peek(role, write=True)
    return policy.tier_of[model] if model else -1


def select_worker(task: Task, band: str, execution_band: str, policy: Policy,
                  resolver: "Resolver") -> tuple[WorkerChoice, WorkerChoice]:
    """Returns (candidate, legacy). `candidate is legacy` when the execution
    cell did not win (design DD-2 S1-S5)."""
    legacy_role, legacy_notes = _legacy_pre(task, band, policy)
    legacy = _floor_and_ladder(task, legacy_role, list(legacy_notes), policy, resolver)

    exec_notes: list[str] = []
    exec_role = _resolve_cell(policy.execution_selection[task.task_class][execution_band],
                              task, exec_notes)
    if _tier(policy, resolver, exec_role) <= _tier(policy, resolver, legacy_role):
        return legacy, legacy                                   # S3: not strictly stronger
    candidate = _floor_and_ladder(task, exec_role, exec_notes, policy, resolver)
    if _tier(policy, resolver, candidate.role) <= _tier(policy, resolver, legacy.role):
        return legacy, legacy                                   # S5: converged to the same tier
    return candidate, legacy


# --------------------------------------------------------------------------
# Stage 5 — effort
# --------------------------------------------------------------------------

def select_effort(task: Task, band: str, execution_band: str, policy: Policy) -> tuple[str, list[str]]:
    cfg = policy.cfg
    notes: list[str] = []
    table = cfg["effort_by_work"]

    if task.task_class == "MECHANICAL":
        effort = table["formatting_rename"]
    elif task.task_class == "DOCUMENTATION":
        effort = table["boilerplate"]
    elif task.task_class in ("DEBUGGING", "INVESTIGATION"):
        effort = table["unknown_root_cause"] if task.has("unknown_root_cause") else table["debugging"]
    elif task.task_class == "REFACTORING":
        effort = table["multi_system_refactoring"] if task.has("cross_service_change") else table["refactoring"]
    elif task.task_class in ("ARCHITECTURE", "MIGRATION"):
        effort = table["complex_architecture"] if band == "CRITICAL" else table["architecture"]
    elif task.task_class == "REVIEW":
        effort = table["adversarial_review"] if band == "CRITICAL" else table["standard_review"]
    else:
        effort = table["multi_file_feature"] if task.complexity >= 2 else table["straightforward_impl"]

    # DD-B3 (C2): a LOW-risk task does not get the class table's HIGH effort.
    # Before the floors, so an execution floor, a local minimum and a
    # compensation still win; not after any prior attempt (a recovered
    # operational one included — that is failure history too, review i1) or
    # on an unknown root cause, where the effort is doing the diagnosing.
    cap = policy.effort_caps.get(band)
    capped_from = None
    if (cap is not None and not task.has("unknown_root_cause") and task.total_prior_attempts == 0
            and policy.efforts.index(effort) > policy.efforts.index(cap)):
        capped_from, effort = effort, cap
    floors = cfg["effort_floors"]
    for condition, floor, why in (
        (band == "HIGH", floors["band_HIGH"], "band HIGH"),
        (band == "CRITICAL", floors["band_CRITICAL"], "band CRITICAL"),
        (bool(task.critical_flags(policy)), floors["any_critical_domain"], "critical-domain flag"),
        (execution_band == "HARD", floors["execution_HARD"], "execution band HARD"),
        (execution_band == "VERY_HARD", floors["execution_VERY_HARD"], "execution band VERY_HARD"),
    ):
        if condition:
            raised = policy.effort_max(effort, floor)
            if raised != effort:
                notes.append(f"{why} floored effort at {floor}")
                effort = raised
    if capped_from is not None and effort == cap:
        notes.append(f"{EFFORT_CAP_NOTE} band {band} capped effort {capped_from} at {cap}")
    return effort, notes


EFFORT_CAP_NOTE = "effort cap:"


# --------------------------------------------------------------------------
# Stage 6 — review, by band alone
# --------------------------------------------------------------------------

def select_review(band: str, worker: str, policy: Policy, resolver: "Resolver") -> dict:
    """Review depth follows the band. Which concrete reviewer fills a MEDIUM
    slot additionally considers availability and family, because picking a
    reviewer that then falls back to the implementer's own family throws away
    the diversity that is the point of the slot."""
    cfg = policy.cfg
    spec = dict(cfg["review"][band])

    if band == "MEDIUM":
        # One seat, and cross-family first. Both were config keys until the
        # 2026-08-18 audit found them inert: this band has always seated
        # exactly one reviewer and has always preferred a different family, so
        # the keys read as policy while changing nothing. The constants live
        # here now, with the config carrying the reasons.
        spec = {
            "reviewers": [_seat_medium_reviewer(spec, worker, policy, resolver)],
            "effort": spec["effort"],
            "independent": spec["independent"],
        }

    spec.setdefault("required_checks", [])
    spec["band"] = band

    # An implementer must never be one of its own independent reviewers. At
    # HIGH and CRITICAL the reviewer pair is fixed, so any task whose worker is
    # already `senior_engineer` or `reasoning_specialist` was being reviewed by
    # itself — the exact arrangement dual review exists to prevent, in the most
    # common high-risk routes. Substitute the colliding slot, preferring a
    # replacement from a family the other reviewer does not already cover.
    if spec["independent"]:
        spec = _deconflict(spec, worker, policy, resolver)
    return spec


def _medium_reviewer_floor(policy: Policy, band: str, worker_model: str | None) -> int | None:
    """What a MEDIUM reviewer has to reach (design 2026-09-25 DD-B5, C4): the
    band's floor, and never less than the implementer — the one reviewer is
    the only check on that implementer's work. Other bands keep their floor;
    their two independent seats are already at or above the frontier floor."""
    floor = policy.band_reviewer_floor[band]
    if floor is None:
        return None                     # the deterministic LOW band: no reviewer seat to floor
    if band == "MEDIUM" and worker_model is not None:
        floor = max(floor, policy.tier_of[worker_model])
    return floor


def _seat_medium_reviewer(spec: dict, worker: str, policy: Policy, resolver: "Resolver") -> str:
    """The MEDIUM reviewer role (DD-B5, C4): the LOWEST-tier candidate that
    still meets `_medium_reviewer_floor`, cross-family first, ties in list
    order. Until 1.17.0 a per-implementer preference seated the frontier sol
    seat behind every grok worker, one tier above what the band asks.

    A candidate resolving to the implementer's own model is not a candidate:
    de-confliction would replace it by its own rule. With no cross-family
    candidate at the requirement, a same-family one at it is taken and
    `cross_family_review` reports the loss. When no LISTED candidate reaches
    it, every role is searched the same way — the list is a preference, and
    de-confliction always searched every role for an adequate replacement, so
    stopping at the list would trade an adequate seat for a shortfall gate.
    With none anywhere, the strongest listed candidate is seated and the
    shortfall gate discloses it.
    """
    worker_model = resolver.peek(worker, write=True)
    worker_family = policy.family_of[worker_model] if worker_model else None
    need = _medium_reviewer_floor(policy, "MEDIUM", worker_model)
    ranked = list(dict.fromkeys(spec["candidates"]))

    def fell_back(role, model):
        # Two roles can reach one model, one by its binding and one through a
        # fallback. Seating the second records an outage that did not happen
        # to this seat and charges its confidence penalty.
        key = resolver._primary(role)
        return key is None or policy.cfg["models"][key]["id"] != model

    def usable(roles):
        return [(i, role, model) for i, role in enumerate(roles)
                if (model := resolver.peek(role)) and model != worker_model]

    seats = usable(ranked)
    cross = [x for x in seats if policy.family_of[x[2]] != worker_family]
    wider = usable(ranked + [r for r in policy.roles if r not in ranked and r != worker])
    wider_cross = [x for x in wider if policy.family_of[x[2]] != worker_family]
    for pool in (cross, seats, wider_cross, wider):
        fits = [x for x in pool if policy.tier_of[x[2]] >= need]
        if fits:
            return min(fits, key=lambda x: (policy.tier_of[x[2]], fell_back(x[1], x[2]), x[0]))[1]
    pool = cross or seats
    if pool:
        return max(pool, key=lambda x: (policy.tier_of[x[2]], not fell_back(x[1], x[2]), -x[0]))[1]
    return ranked[0]


def _is_source_review(task: Task, policy: Policy) -> bool:
    """Does this route seat its worker as the lead REVIEWER — reviewer-1, one
    of the band's reviewers — rather than as a worker the review checks?
    Always for a REVIEW task since 1.17.0 (design 2026-09-25 DD-B7, U-8);
    before, only when it declared a `review_context`."""
    return task.task_class == "REVIEW" and (task._review_context is not None
                                            or policy.review_lead_counts)


def _review_seat_supply(policy: Policy, task: Task, band: str) -> tuple[int, int]:
    """(reviewer seats, seats that can hold distinct families) the class x band
    seat matrix gives this band (design 2026-09-25 DD-B7). Outside REVIEW: the
    worker plus the band's reviewers, and a judge at the top band. A REVIEW
    task's lead IS a reviewer: max(1, the band's reviewers) seats, plus the
    judge — the lead alone at LOW and MEDIUM, two at HIGH and CRITICAL."""
    spec = policy.cfg["review"][band]
    reviewers = len(spec["reviewers"]) if "reviewers" in spec else 1
    judge = 1 if band == policy.bands[-1] else 0
    if _is_source_review(task, policy):
        return max(1, reviewers), max(1, reviewers) + judge
    return reviewers, 1 + reviewers + judge


def _low_review_escape(task: Task, policy: Policy, band: str, route_path: str | None,
                       lp: dict) -> tuple[str, list[str]] | None:
    """Where a LOW route that deterministic checks cannot carry goes instead
    (design 2026-09-25 DD-B4). Raise only, once, before the fixed point: a
    dispute needs a model review; so does a repository whose checks cannot
    run; and a caller floor on reviewers or provider families takes the lowest
    band whose seat matrix can meet it. None when LOW stands — or when no band
    can meet the floor, which then ends where it always did, at
    UNSATISFIABLE_LOCAL_POLICY, without a detour through the bands."""
    low = policy.bands[0]
    here = policy.bands.index(band)
    deterministic = band == low and policy.band_reviewer_floor[low] is None
    # A REVIEW task's lead counts as its reviewer, so its MEDIUM seats one
    # model where it used to seat two: a caller floor there leaves the band
    # too (DD-B7 — a two-family REVIEW does not stay at one seat).
    lead = _is_source_review(task, policy) and policy.review_lead_counts
    if not deterministic and not lead:
        return None
    target, reasons = here, []
    if deterministic and route_path == "disagreement":
        target, reasons = here + 1, ["review_disagreement"]
    if deterministic and task._checks_available is False:
        target = max(target, here + 1)
        reasons.append("checks_unavailable")
    need_reviewers = lp.get("minimum_reviewers") or 0
    need_families = lp.get("minimum_provider_families") or 0
    supply = [_review_seat_supply(policy, task, b) for b in policy.bands]
    if need_reviewers > supply[here][0] or need_families > supply[here][1]:
        fits = [i for i, (seats, families) in enumerate(supply)
                if i > here and seats >= need_reviewers and families >= need_families]
        if fits:
            target = max(target, fits[0])
            if need_reviewers > supply[here][0]:
                reasons.append("minimum_reviewers")
            if need_families > supply[here][1]:
                reasons.append("minimum_provider_families")
    return (policy.bands[target], reasons) if target > here else None


def _deconflict(spec: dict, worker: str, policy: Policy, resolver: "Resolver") -> dict:
    """Ensure no reviewer resolves to the implementer's model, or to another
    reviewer's.

    The collision test is on the **resolved model**, not the role label. Roles
    are not distinct models: a degraded single-provider binding maps several
    roles onto one id, so a role-level check reported a substitution while the
    same model kept every seat — implementer, both "independent" reviewers, and
    the judge. Recording an avoidance that avoided nothing is the same defect
    this module removed from the fallback and escalation paths, and it is worse
    here because it clears a safety gate.

    When no substitution can break the collision, the caller is told
    (`independence_compromised`) rather than being handed a route that looks
    independent.
    """
    worker_model = resolver.peek(worker, write=True)
    # Round 7. The ladder position was standing in for capability here too,
    # while the config declares `capability_tier` the single axis for
    # substitution-vs-replaced. Latent rather than live in today's registry —
    # the fallback ladder happens to keep the two orderings agreeing — but one
    # added entry makes it wrong, and nothing would notice.
    worker_tier = policy.tier_of[worker_model] if worker_model else -1
    reviewers = list(spec["reviewers"])
    substitutions: list[dict] = []
    taken = {worker_model} if worker_model else set()
    compromised = False

    for index, role in enumerate(reviewers):
        model = resolver.peek(role)
        if model is not None and model not in taken:
            taken.add(model)
            continue

        other_models = {resolver.peek(x) for i, x in enumerate(reviewers) if i != index}
        pool = []
        for candidate in policy.roles:
            if candidate in reviewers:
                continue
            candidate_model = resolver.peek(candidate)
            if candidate_model is None or candidate_model in taken:
                continue
            pool.append((candidate, candidate_model))

        # Strength first, then a family the other reviewer does not cover. A
        # reviewer below the implementer's tier cannot supply the check the
        # implementer could not perform on itself; a lost family difference is
        # a real but lesser cost, and cross_family_review discloses it.
        other_family = next((policy.family_of[m] for m in other_models if m), None)
        pick = max(
            pool,
            key=lambda pair: (policy.tier_of[pair[1]] >= worker_tier,
                              policy.family_of[pair[1]] != other_family,
                              policy.tier_of[pair[1]]),
            default=None,
        )
        if pick is None:
            compromised = True
            continue
        substitutions.append({"replaced": role, "with": pick[0],
                              "reason": "would have shared a model with the implementer"
                                        if model == worker_model else
                                        "would have duplicated another reviewer"})
        reviewers[index] = pick[0]
        taken.add(pick[1])

    if substitutions or compromised:
        spec = dict(spec)
        spec["reviewers"] = reviewers
        spec["self_review_avoided"] = substitutions
        spec["independence_compromised"] = compromised
    return spec


def _seat_judge(review: dict, worker: str, judge_role: str, policy: "Policy",
                resolver: "Resolver") -> tuple[dict, str | None]:
    """Give the judge a model no other seat holds, no weaker than any model it
    will adjudicate.

    Every comparison here is on the resolved model's `capability_tier`, not on
    the role's position in the ladder. Round 6 found why that distinction is
    not pedantry: under scarcity `worker_fast` can resolve to the frontier
    model and `worker_balanced` to a mid one, and ranking those by role ordinal
    seats the weaker model as the judge of the stronger — an adjudicator that
    the parties outrank, reported as a clean route.

    Allocation is greedy — worker, then reviewers, then judge — and the
    reviewer step maximises tier, so the judge can be told no adequate model
    was free while one sits unused behind a reviewer that did not need it.
    When that happens one reviewer is re-seated lower and the judge is tried
    again, but never below what the *band* asks of a reviewer. The floor used
    to be the implementer's tier, which is not a review requirement at all:
    with a `worker_fast` implementer it let a HIGH band be reviewed two tiers
    below its own policy, silently, to buy a judge seat. Review depth is not
    currency. If the judge cannot be seated without spending it, the judge is
    unavailable and a human is asked — a shortage the caller can act on.
    """
    def tier(role: str) -> int:
        model = resolver.peek(role)
        return policy.tier_of[model] if model else -1

    def taken(reviewers):
        return {resolver.peek(worker, write=True)} | {resolver.peek(x) for x in reviewers}

    def pick(reviewers):
        # No party may outrank its adjudicator — including the implementer,
        # who is a party to any dispute about its own work.
        floor = max((tier(x) for x in [worker, *reviewers]), default=0)
        used = taken(reviewers)
        pool = [x for x in policy.roles
                if resolver.peek(x) and resolver.peek(x) not in used and tier(x) >= floor]
        return max(pool, key=tier, default=None)

    reviewers = list(review["reviewers"])
    if resolver.peek(judge_role) not in taken(reviewers) and tier(judge_role) >= max(
            (tier(x) for x in [worker, *reviewers]), default=0):
        return review, judge_role

    if (found := pick(reviewers)):
        review = dict(review)
        return review, found

    # Retry once, freeing the strongest reviewer seat if another model that
    # still satisfies the band can take its place.
    floor = policy.band_reviewer_floor[review["band"]]
    if floor is None:
        raise RouterInvariantError(
            f"a judge is being seated on the {review['band']} band, which seats no reviewer")
    # The implementer's model is off-limits to a REPLACEMENT reviewer at every
    # band, `independent` or not.
    #
    # Round 6 relaxed this for LOW on the reasoning that LOW permits the
    # implementer to review itself. That conflates two different things, and
    # round 7 showed what the conflation costs. LOW's exemption is about the
    # reviewer the BAND CONFIGURED resolving onto the implementer; it is not a
    # licence for the router to MOVE the reviewer there. With the exemption in
    # place a LOW route traded a distinct, stronger reviewer for the
    # implementer itself in order to free a model for the judge, recorded
    # nothing (LOW's depth floor is 0, so the shortfall gate cannot fire
    # either), and turned `requires_human_confirmation` from true to false on
    # the same input. That is a control being removed, not weakened.
    #
    # Refusing the trade means some routes report `judge_unavailable` where a
    # judge looked reachable. It only looked reachable: with two models and
    # three roles you cannot have both an independent reviewer and an
    # independent adjudicator, and saying so is the honest answer.
    highest = max(range(len(reviewers)), key=lambda i: tier(reviewers[i]), default=None)
    if highest is not None:
        used = taken(reviewers) - {resolver.peek(reviewers[highest])} | {
            resolver.peek(worker, write=True)} | {
            resolver.peek(x) for i, x in enumerate(reviewers) if i != highest}
        alternatives = [x for x in policy.roles
                        if x not in reviewers and resolver.peek(x)
                        and resolver.peek(x) not in used and tier(x) >= floor]
        for alt in sorted(alternatives, key=tier):
            trial = list(reviewers)
            trial[highest] = alt
            # `pick` already excludes every model in `taken(trial)`, so a second
            # test of the same thing here could never fail — the exact shape this
            # module keeps finding elsewhere, sitting in the function that hunts
            # it. One check, in one place.
            if (found := pick(trial)):
                review = dict(review)
                review["reviewers"] = trial
                # The seat that a substitution record pointed at has just been
                # re-seated. Leaving the record as written makes the rationale
                # name a reviewer who is not there — the module's own rule is
                # that a recorded change must be a real change, and it applies
                # to the record as much as to the change.
                review["self_review_avoided"] = _restate(
                    review.get("self_review_avoided"), reviewers[highest], alt)
                return review, found

    review = dict(review)
    review["judge_unavailable"] = True
    return review, None


def _restate(records: list[dict] | None, old: str, new: str) -> list[dict]:
    """Re-point substitution records whose landing seat was re-seated."""
    out = []
    for record in records or []:
        if record.get("with") != old:
            out.append(record)
        elif record.get("replaced") != new:
            out.append({**record, "with": new})
        # else: the displaced role is back in the seat, so nothing was
        # substituted after all and the record describes an event that did
        # not survive. It is dropped rather than corrected.
    return out


def _extra_reviewer(review: dict, worker: str, policy: "Policy", resolver: "Resolver") -> str | None:
    """A reviewer whose model is not already in use by the worker or a peer."""
    taken = {resolver.peek(worker, write=True)} | {resolver.peek(x) for x in review["reviewers"]}
    pool = [x for x in policy.roles
            if x not in review["reviewers"] and x != worker
            and resolver.peek(x) and resolver.peek(x) not in taken]
    # Strongest by resolved model, not by ladder position — the config says
    # every strength comparison reads `capability_tier`, and this one was left
    # on role ordinals when the rest were converted.
    return max(pool, key=lambda x: policy.tier_of[resolver.peek(x)], default=None)


# --------------------------------------------------------------------------
# Stage 7 — resolution
# --------------------------------------------------------------------------

def _joint_seats(review: dict, worker: str, judge: str | None,
                 policy: "Policy", resolver: "Resolver") -> tuple[dict, str | None]:
    """Search eligible candidate IDs, retaining a conservative slate on failure.

    An ordinary worker is fixed. A declared source review jointly seats its
    lead reviewer at or above the selected worker tier and final review floor. Only successful complete assignments are installed.
    """
    if any(row.get("with") not in review["reviewers"] for row in review.get("self_review_avoided", [])):
        raise RouterInvariantError("stale reviewer substitution before joint allocation")
    source_review = resolver.source_review
    def unavailable():
        # Compromised independence is a fact about a band that asks for it: a
        # REVIEW task's lone lead at LOW that cannot be seated is a shortage,
        # which the final resolution reports as one.
        compromised = source_review and review["independent"]
        return ({**review, "independence_compromised": True} if compromised else review), judge
    worker_model = resolver.peek(worker, write=True)
    # What the band asks of a reviewer — at MEDIUM including the implementer's
    # tier (DD-B5), so a MEDIUM slate below its implementer is deficient here
    # too and the search can reach an adequate model no role is bound to.
    floor = _medium_reviewer_floor(policy, review["band"], worker_model)
    if floor is None:
        # The deterministic LOW band seats no reviewer: its only possible seats
        # are a REVIEW task's lead (which keeps its own floor, the worker's
        # tier) and a compensation's extra reviewer, which no band floor asked
        # for. Anything more is a plan this band cannot have produced.
        if len(review["reviewers"]) > review.get("compensating_reviewers", 0) + int(source_review):
            raise RouterInvariantError(
                f"{review['band']} seats no reviewer but the plan names {review['reviewers']}")
        floor = 0
    if worker_model is None and not source_review:
        return unavailable()
    need_judge = judge is not None or bool(review.get("judge_unavailable"))
    current = [resolver.peek(r) for r in review["reviewers"]]
    deficient = (review.get("independence_compromised") or review.get("judge_unavailable")
                 or any(m is None or policy.tier_of[m] < floor for m in current))
    if not source_review and not deficient:
        return unavailable()

    count = len(review["reviewers"])
    if source_review:
        count = max(count, 1)           # the lead is a seat even where the band has no reviewer
    # Aliases carry one model each; reserve a unique alias for every real seat.
    aliases = list(dict.fromkeys([r for r in review["reviewers"] if r != worker]
                                + [r for r in policy.roles if r != worker]))
    reviewer_roles = ([worker] if source_review else []) + aliases[:count - int(source_review)]
    if len(reviewer_roles) != count:
        return unavailable()
    free_aliases = [r for r in policy.roles if r != worker and r not in reviewer_roles]
    if need_judge and not free_aliases:
        return unavailable()
    judge_role = (judge if judge in free_aliases else free_aliases[-1]) if need_judge else None
    variable_roles = [r for r in reviewer_roles if source_review or r != worker] + ([judge_role] if judge_role else [])
    offered = list(dict.fromkeys(key for r in policy.roles for key in resolver._candidates(r)))
    pools = {}
    for role in variable_roles:
        pools[role] = list(dict.fromkeys(
            policy.cfg["models"][key]["id"] for key in list(dict.fromkeys(resolver._candidates(role) + offered))
            if policy.cfg["models"][key]["id"] not in resolver.unusable
            and policy.tier_of[policy.cfg["models"][key]["id"]] >= floor
            and (review["effort"] is None
                 or _clamp(policy, review["effort"], policy.cfg["models"][key]["id"]) == review["effort"])))
    if source_review:
        lead_floor = (policy.tier_of[worker_model] if worker_model else
                      policy.cfg["models"][policy.cfg["role_bindings"]["default"][worker]]["capability_tier"])
        lp = resolver.task._local_policy or {}
        lead_floor = max(lead_floor, (lp.get("minimum_capability_tier") or 0))
        minimum_effort = (lp.get("minimum_effort") or review["effort"])
        pools[worker] = [m for m in pools[worker] if policy.tier_of[m] >= lead_floor
                         and (minimum_effort is None
                              or _clamp(policy, minimum_effort, m) == minimum_effort)]
    best, best_rank = None, None

    def search(index, assigned, used, preference):
        nonlocal best, best_rank
        if index == len(variable_roles):
            reviewers = [assigned[r] for r in reviewer_roles]
            if judge_role and policy.tier_of[assigned[judge_role]] < max(
                    policy.tier_of[m] for m in [assigned[worker], *reviewers]):
                return
            families = {policy.family_of[m] for m in reviewers}
            source_families = {policy.family_of[m] for m in resolver.author_excluded}
            comparison_families = source_families if source_review else {policy.family_of[assigned[worker]]}
            cross = len(families) > 1 or (len(reviewers) == 1 and
                    policy.family_of[reviewers[0]] not in comparison_families)
            family_floor = ((resolver.task._local_policy or {}).get("minimum_provider_families") or 0)
            all_families = {policy.family_of[m] for m in assigned.values()}
            rank = (len(all_families) >= family_floor, int(cross), len(families), -preference)
            if best_rank is None or rank > best_rank:
                best, best_rank = dict(assigned), rank
            return
        role = variable_roles[index]
        for order, model in enumerate(pools[role]):
            if model not in used:
                assigned[role] = model
                search(index + 1, assigned, used | {model}, preference + order)
                del assigned[role]

    initial = {} if source_review else {worker: worker_model}
    used = set() if source_review else {worker_model}
    search(0, dict(initial), used, 0)
    if best is None and judge_role:
        variable_roles.remove(judge_role)
        judge_role = None
        search(0, dict(initial), used, 0)
    if best is None:
        return unavailable()
    resolver.assignments = best
    result = dict(review)
    result["reviewers"] = reviewer_roles
    result["independence_compromised"] = False
    result["judge_unavailable"] = need_judge and judge_role is None
    result["self_review_avoided"] = []
    return result, judge_role


class Resolver:
    """Turns role aliases into concrete models, honouring every constraint the
    caller supplied and the provider boundary implied by the runtime state."""

    def __init__(self, task: Task, policy: Policy):
        self.task = task
        self.policy = policy
        cfg = policy.cfg

        self.bridge_down = task.has("bridge_down")
        self.binding_name = "default"
        self.notes: list[str] = []
        if self.bridge_down:
            self.binding_name = policy.degraded_binding[task.runtime]
            self.notes.append(f"binding degraded to {self.binding_name} (cross-provider bridge down)")
        self.binding = cfg["role_bindings"][self.binding_name]

        # When the bridge is down the opposite family is unreachable by
        # definition — a fallback that crosses it names a model that cannot be
        # invoked, which is the one thing a route must never do.
        allowed: set[str] | None = None
        if self.bridge_down:
            allowed = {policy.local_family[task.runtime]}
        lp = task._local_policy or {}
        if lp.get("allowed_families") is not None:
            asked = set(lp["allowed_families"])
            allowed = asked if allowed is None else allowed & asked
        self.allowed_families = allowed

        context = task._review_context or {}
        author_families = set(context.get("author_families", []))
        self.author_excluded = set(context.get("author_model_ids", [])) | {
            m["id"] for m in cfg["models"].values() if m["family"] in author_families}

        blocked = set(task.unavailable_models)
        for role in task.unavailable_roles:
            for b in (cfg["role_bindings"]["default"], self.binding):
                key = b.get(role)
                if key:
                    blocked.add(cfg["models"][key]["id"])
        self.blocked = blocked

        # A tier that already failed must not be re-emitted under a new label.
        # Role-level escalation alone is not enough: in a degraded binding the
        # top roles collapse onto one model, so "escalating" changed nothing.
        #
        # `blocked` is finished before this line on purpose: resolving what an
        # alias HELD needs the caller's withholding, and nothing else. Round 10
        # found the two consumers of that same question spelled differently —
        # the retry floor walked the candidate ladder while this read the
        # nominal binding — so the floor believed one model had failed and the
        # exclusion set removed another, and the model that actually ran came
        # straight back out of `peek`. One function answers it now.
        self.failed = task.failed_models(policy)
        if task._same_model_retry is not None:
            # DD-B6: the one failed model this retry goes back to, at a higher
            # effort. Every other seat still excludes it.
            self.failed.discard(task._same_model_retry["model"])
        # Kept apart from `blocked`: that set is the CALLER's withholding and is
        # echoed on every route, terminal ones included. A local revocation is
        # not caller input, so it filters seats without being echoed.
        self.local_blocked = set(policy.local_blocked_ids)
        # DD-B8 (C7). `exhausted`: the family's models are withheld like the
        # caller's `unavailable_models` (reason: quota) — not echoed there, it
        # is not that list. `low`: consulted only for the worker seat.
        quota = task._family_quota or {}
        self.quota_exhausted = {m["id"] for m in cfg["models"].values()
                                if quota.get(m["family"]) == "exhausted"}
        self.quota_low = frozenset(f for f, level in quota.items() if level == "low")
        self.quota_swaps: dict[str, str] = {}
        self.unusable = self.blocked | self.failed | self.local_blocked | self.quota_exhausted

        # Does THIS route's worker seat have to write? Set by `route()`.
        #
        # The requirement belongs to the SEAT, which is why every lookup below
        # takes `write=` rather than the resolver deciding from a role name.
        # Round 1 of this tranche scoped it by role and `test_d10` caught the
        # consequence immediately: `worker_balanced` is the only role that
        # binds the xai seat, so making that ROLE write-only made the model
        # invisible to review seating too — a route substituted a tier-0
        # reviewer while the tier-1 xai seat sat free. Losing the verified
        # read-only reviewer along with the unshipped maker is the one outcome
        # the 2026-08-25 gate decided must not happen.
        self.worker_writes: bool = False
        # The role the worker seat ended up on, once `select_worker` has
        # decided it. `resolved` is keyed by role, so a role holds exactly ONE
        # model per route — the write requirement therefore has to reach every
        # reader of that role, or review seating reasons about a model
        # `resolve` is not going to hand it. Round 2 of this tranche tried to
        # scope the requirement to the seat instead, so that the xai model
        # could be skipped as the worker and still seated as a reviewer under
        # the same role; the role-keyed map cannot express that, and the route
        # came out INDEPENDENCE_UNAVAILABLE because the one entry resolved to
        # the worker's model and the reviewer had nothing left.
        self.write_seat_role: str | None = None
        self.assignments: dict[str, str] = {}
        # (worker role, declared implementer id) when the worker seat already
        # executed (DD-B1). The role answers with that model for every reader
        # and no availability, family or write filter applies to it: those
        # constrain seats still to be dispatched, and this one has run.
        self.pinned_worker: tuple[str, str] | None = None
        # The worker is the lead reviewer (DD-B7): its seat is searched with
        # the reviewers' instead of fixed ahead of them.
        self.source_review: bool = _is_source_review(task, policy)

    def _primary(self, role: str) -> str | None:
        """The registry key this role binds to FOR THIS TASK.

        `worker_balanced_selection` moves the first escalation to its alt seat
        when the task carries any configured flag: the primary seat's provider
        re-bills the entire request at a doubled rate past a context threshold
        and its window ends earlier, so above that line the cheaper seat is the
        more expensive one; separately, a quality-tied latency measurement can
        make the alt the faster same-tier seat. The threshold is a token count
        the router never sees; each flag is the caller's statement about which
        side of that comparison this route is on.

        It is a binding decision and not a fallback, which is why it lives here
        rather than in the candidate ladder alone: `resolve` compares what was
        chosen against what this role *should* have bound to, and a swap the
        policy made on purpose must not be reported as a model going missing.
        """
        sel = self.policy.cfg.get("worker_balanced_selection") or {}
        flags = sel.get("prefer_alt_when_flags") or []
        if (role == sel.get("prefer")
                and isinstance(flags, list)
                and any(self.task.has(f) for f in flags)):
            # A degraded binding names no alt seat. Nothing to prefer, so the
            # rule is a no-op there rather than an invented substitution.
            if (alt := self.binding.get(sel.get("alt"))):
                return alt
        return self.binding.get(role)

    def _candidates(self, role: str, *, write: bool = False) -> list[str]:
        cfg = self.policy.cfg
        ordered: list[str] = []
        if (primary := self._primary(role)):
            ordered.append(primary)
        # The role's own binding stays on the ladder behind the preferred seat:
        # preferring the alt must not delete the seat it was preferred over.
        if (bound := self.binding.get(role)):
            ordered.append(bound)
        ordered.extend(cfg["fallbacks"].get(self.task.runtime, {}).get(role, []))
        degraded_name = self.policy.degraded_binding[self.task.runtime]
        if (d := cfg["role_bindings"][degraded_name].get(role)):
            ordered.append(d)
        # A binding-only role (`worker_balanced_alt`) has no rung on the
        # ladder: it resolves to its own binding and nothing else, so a MEDIUM
        # candidate list can name it without inheriting another role's seats.
        if role in self.policy.roles:
            index = self.policy.roles.index(role)
            for other in self.policy.roles[index + 1:] + self.policy.roles[:index][::-1]:
                if (k := self.binding.get(other)):
                    ordered.append(k)
        seen: set[str] = set()
        out = []
        for k in ordered:
            if k in seen:
                continue
            seen.add(k)
            if cfg["models"][k]["id"] in self.author_excluded:
                continue
            if self.allowed_families is not None and cfg["models"][k]["family"] not in self.allowed_families:
                continue
            if cfg["models"][k].get("dispatchable", True) is False:
                continue
            # A seat with no write-capable recipe in THIS host direction
            # cannot fill a seat that has to write. The router still only
            # names models; what it stops naming is one it knows no
            # conforming dispatcher can execute for this work. `write` is the
            # caller's statement about the SEAT — the same role read by the
            # review seating is unfiltered, which is what keeps the verified
            # read-only reviewer available on a write route.
            if (self.worker_writes and (write or role == self.write_seat_role)
                    and not self.policy.write_capable(
                        self.task.runtime, cfg["models"][k]["family"])):
                continue
            out.append(k)
        # A REVIEW task's lead is a review seat (DD-B7), and `low` moves the
        # worker seat only (review i1).
        if self.quota_low and not self.source_review and (write or role == self.write_seat_role):
            out = self._quota_low_order(role, out)
        return out

    def _quota_low_order(self, role: str, keys: list[str]) -> list[str]:
        """DD-B8 `low`, the worker seat only: when the seat would land on a
        model of a low-quota family, the first same-tier model of another
        family goes first. A binding choice with no confidence penalty — not a
        fallback — and nothing moves without a same-tier seat to move to."""
        models = self.policy.cfg["models"]
        usable = [k for k in keys if models[k]["id"] not in self.unusable]
        if not usable or models[usable[0]]["family"] not in self.quota_low:
            return keys
        tier = self.policy.tier_of[models[usable[0]]["id"]]
        swap = next((k for k in usable
                     if self.policy.tier_of[models[k]["id"]] == tier
                     and models[k]["family"] not in self.quota_low), None)
        if swap is None:
            return keys
        self.quota_swaps[role] = models[swap]["id"]
        return [swap] + [k for k in keys if k != swap]

    def peek(self, role: str, *, write: bool = False) -> str | None:
        """The model this role would resolve to, or None if nothing is usable.

        `write=True` asks the question for a seat that has to write. Every
        caller that is asking about the WORKER passes it; reviewer and judge
        seating does not, because those seats read.
        """
        if self.pinned_worker is not None and role == self.pinned_worker[0]:
            return self.pinned_worker[1]
        if role in self.assignments:
            return self.assignments[role]
        cfg = self.policy.cfg
        for key in self._candidates(role, write=write):
            model_id = cfg["models"][key]["id"]
            if model_id not in self.unusable:
                return model_id
        return None

    def family_for_role(self, role: str, *, write: bool = False) -> str | None:
        model = self.peek(role, write=write)
        return self.policy.family_of[model] if model else None

    def resolve(self, roles: list[str], *, write_role: str | None = None
                ) -> tuple[dict[str, str], list[str], list[str]]:
        """Returns (role -> model id, fallback notes, compensation notes).

        `write_role` names the one seat in `roles` that has to write. It is a
        single role and not a set because a route has one worker; everything
        else in `roles` is a reviewer or the judge.
        """
        cfg = self.policy.cfg
        resolved: dict[str, str] = {}
        fallbacks = list(self.notes)
        compensations: list[str] = []
        comp_cfg = cfg.get("fallback_compensations", {})

        for role in roles:
            if self.pinned_worker is not None and role == self.pinned_worker[0]:
                # Already executed: no binding was resolved, so none fell back.
                resolved[role] = self.pinned_worker[1]
                continue
            write = role == write_role
            primary_key = self._primary(role)
            primary_id = cfg["models"][primary_key]["id"] if primary_key else None
            if primary_id in self.author_excluded:
                # Intentional author exclusion is eligibility, not an outage.
                # Keep the first eligible candidate as the baseline BEFORE
                # applying availability, so a real outage still gets recorded.
                offered = self._candidates(role, write=write)
                primary_id = cfg["models"][offered[0]]["id"] if offered else None
            # A seat the policy never offered for THIS kind of work is not a
            # model that went missing, and must not be billed as one. Same rule
            # `_primary` already follows for the alt-seat preference: a swap the
            # policy made on purpose is a binding decision, not a fallback.
            #
            # It is not cosmetic. `fallbacks_applied` feeds `routing_confidence`,
            # which can promote the review band — so recording this as scarcity
            # promoted the review of EVERY cross-family write route on the two
            # hosts that bridge to xai, permanently, with no change in the risk
            # that review is supposed to answer to. `test_s3` caught it as a
            # band moving from HIGH to CRITICAL.
            #
            # The baseline MOVES to the first seat the policy does offer; it is
            # never dropped. Round 1 of review dropped it, and that bought the
            # first defect by committing its mirror image: the next candidate's
            # genuine outage then had nothing to be compared against, so a real
            # scarcity fallback, its 0.10 confidence penalty and the review
            # promotion that follows all disappeared. Not recording a change
            # that DID happen is the same failure as recording one that did not.
            if (primary_id is not None and self.worker_writes
                    and (write or role == self.write_seat_role)
                    and not self.policy.write_capable(
                        self.task.runtime, self.policy.family_of[primary_id])):
                offered = self._candidates(role, write=write)
                primary_id = cfg["models"][offered[0]]["id"] if offered else None
            chosen_id = self.peek(role, write=write)
            if (chosen_id is not None and chosen_id == self.quota_swaps.get(role)
                    and primary_id is not None and primary_id not in self.unusable):
                # A low-quota family moved this seat on purpose (DD-B8): a
                # binding decision, not a model that went missing.
                primary_id = chosen_id
            if chosen_id is None:
                # The fourth cause is new and is often the only true one: the
                # candidate exists, is available, has not failed and the bridge
                # is up — it simply has no write-capable recipe for this seat.
                # Enumerating three causes that did not happen is the shape
                # this module removes everywhere else.
                because = ("every candidate is unavailable, already failed, or on the "
                           "unreachable side of a downed bridge")
                if write and self.worker_writes:
                    because += (f", or has no write-capable recipe for this seat on "
                                f"{self.task.runtime}")
                raise SupplyExhausted(f"no usable model for role {role!r}: {because}")
            resolved[role] = chosen_id
            # Deliberate seat allocation is not model unavailability. Preserve
            # an actual outage behind the original role baseline.
            if role in self.assignments and primary_id not in self.unusable:
                primary_id = chosen_id
            if primary_id is not None and chosen_id != primary_id:
                fallbacks.append(f"{role}: {primary_id} unavailable -> {chosen_id}")
                # Joint seats already meet their explicit tier/effort floors.
                # A role label reused for a peer is not an architect downgrade.
                if role not in self.assignments:
                    compensations.extend(self._compensations(role, primary_id, chosen_id, comp_cfg))
        return resolved, fallbacks, compensations

    def _compensations(self, role, primary_id, chosen_id, comp_cfg) -> list[str]:
        """Configured compensations for a downgrade. Declared and never applied,
        these were policy that existed only as a comment."""
        out = []
        fam = self.policy.family_of
        if (role == "principal_architect" and self.policy.tier_of[chosen_id] < self.policy.tier_of[primary_id]
                and (rule := comp_cfg.get("principal_architect_to_senior"))):
            out.append(rule)
        if role == "reasoning_specialist" and fam[chosen_id] == fam.get(
                self.peek("senior_engineer") or chosen_id):
            if (rule := comp_cfg.get("reasoning_specialist_to_same_family")):
                out.append(rule)
        return out


# --------------------------------------------------------------------------
# Independence
# --------------------------------------------------------------------------

def independence(review: dict, task: Task) -> str:
    """Separate what the policy asks for from what was actually established.

    Three states, not two. `unavailable` is positive evidence that isolation
    cannot be achieved; `degraded` is the absence of evidence either way.
    Collapsing them makes a confirmed gap indistinguishable from an unchecked
    one, and the config's own ledger records a case of exactly that.

    `enforced` requires post-dispatch proof — one distinct session per
    reviewer. A caller's capability attestation alone yields `planned`, because
    a route computed before any reviewer runs cannot know what happened.
    """
    if not review["independent"]:
        return "not_applicable"
    # A reviewer set the router could not de-conflict is not independent, no
    # matter what the caller attests.
    if review.get("independence_compromised"):
        return "unavailable"
    if task.isolation_available is False:
        return "unavailable"
    distinct = len({e.strip() for e in task.isolation_evidence if e.strip()})
    # One identifier per reviewer — exactly. `>=` accepted surplus ids, so a
    # stale or foreign identifier could ride along and still flip the
    # strongest reported state; a count mismatch in either direction is not
    # evidence about THIS route's reviewers. The old `and distinct >= 2`
    # failure — a single-reviewer MEDIUM band unable to reach `enforced` —
    # stays fixed: one reviewer, one id, equality holds.
    if review["reviewers"] and distinct == len(review["reviewers"]):
        return "enforced"
    if task.isolation_available is True:
        return "planned"
    return "degraded"


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def routing_confidence(task: Task, fallbacks: list[str], cfg: dict, *,
                       skip_uncertainty: bool = False) -> float:
    """The number `router.confidence.escalate_below` is compared against.

    The thresholds lived in the config and the penalties that produce the value
    lived here, so moving a threshold meant guessing at numbers in another file.
    One policy, one place.

    `skip_uncertainty` leaves the uncertainty penalty out. Only the review
    promotion asks for that, and only when uncertainty already raised the band
    (DD-B2); the reported value always carries it.
    """
    conf = cfg["router"]["confidence"]
    penalty = conf["penalties"]
    c = conf["base"]
    if skip_uncertainty:
        pass
    elif task.uncertainty == 3:
        c -= penalty["uncertainty_3"]
    elif task.uncertainty == 2:
        c -= penalty["uncertainty_2"]
    if task.prior_failures >= 2:
        c -= penalty["prior_failures_2_or_more"]
    elif task.prior_failures == 1:
        c -= penalty["prior_failures_1"]
    if fallbacks:
        c -= penalty["any_fallback"]
    if task.has("unknown_root_cause"):
        c -= penalty["unknown_root_cause"]
    return round(max(0.0, min(1.0, c)), 2)


def orchestrator_ask(task: Task, policy: Policy, cfg: dict, band: str,
                     confidence: float) -> dict:
    """What the policy asks of the orchestrator seat for this route.

    The ask always starts from the canonical default binding's nominal tier;
    scarcity and degraded bindings never lower that bar.
    """
    router = cfg["router"]
    nominal = {
        role: policy.tier_of[cfg["models"][key]["id"]]
        for role, key in cfg["role_bindings"]["default"].items()
    }
    critical = bool(task.critical_flags(policy))
    tier_rules = (
        ("orchestrator_uncertainty_3", task.uncertainty == 3, "worker_balanced"),
        ("orchestrator_critical_u2", critical and task.uncertainty >= 2,
         "worker_balanced"),
        ("orchestrator_architecture_high",
         task.task_class == "ARCHITECTURE" and band in ("HIGH", "CRITICAL"),
         "senior_engineer"),
        ("orchestrator_architecture_ambiguity",
         task.task_class == "ARCHITECTURE" and task.uncertainty == 3,
         "principal_architect"),
    )
    tier = nominal[router["default_orchestrator"]]
    effort = router["default_orchestrator_effort"]
    raised: list[str] = []
    for code, fires, role in tier_rules:
        if fires:
            raised.append(code)
            tier = max(tier, nominal[role])
    if confidence < cfg["router"]["confidence"]["escalate_below"]:
        raised.append("orchestrator_low_confidence")
        effort = "MAX"
    if task.blast_radius >= 2:
        raised.append("orchestrator_blast_high")
        effort = "MAX"
    return {"tier": tier, "effort": effort, "raised_by": raised}


def host_seat_comparisons(declared: dict | None, ask: dict,
                          policy: Policy) -> tuple[str, str, str]:
    if declared is None:
        return "undeclared", "undeclared", "none"
    tier = policy.tier_of.get(declared["model"])
    if tier is None:
        model_cmp = "unrecognized"
    else:
        model_cmp = ("below" if tier < ask["tier"]
                     else "at" if tier == ask["tier"] else "above")
    effort = declared.get("effort")
    if effort is None:
        effort_cmp = "undeclared"
    else:
        d, a = policy.efforts.index(effort), policy.efforts.index(ask["effort"])
        effort_cmp = "below" if d < a else "at" if d == a else "above"
    advisory = ("upgrade_recommended"
                if "below" in (model_cmp, effort_cmp) else "none")
    return model_cmp, effort_cmp, advisory


# --------------------------------------------------------------------------
# Stage 8 — emit
# --------------------------------------------------------------------------

def _worker_effort_floor(task: Task, band: str, execution_band: str, policy: Policy) -> tuple[str, str] | None:
    """The strongest floor `select_effort` applied to the worker, as
    (rule name, level), or None when no floor applied.

    The risk band, not the promoted review band: `select_effort` is called with
    the risk band, and comparing against a band it never saw would invent a
    floor the code did not apply. A level that came from `effort_by_work` alone
    is a preference, not a requirement, so it is not a floor.
    """
    floors = policy.cfg["effort_floors"]
    applied = [(f"effort_floors.{name}", floors[name]) for condition, name in (
        (band == "HIGH", "band_HIGH"),
        (band == "CRITICAL", "band_CRITICAL"),
        (bool(task.critical_flags(policy)), "any_critical_domain"),
        (execution_band == "HARD", "execution_HARD"),
        (execution_band == "VERY_HARD", "execution_VERY_HARD"),
    ) if condition]
    return max(applied, key=lambda pair: policy.efforts.index(pair[1]), default=None)


def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def request_sha256_of(task: Task) -> str:
    """The caller's request in canonical form, hashed — computed from the
    ORIGINAL validated input, before route() blanks prior_models on a
    history gap. Normalisation mirrors exactly how route() consumes each
    field: set-semantics lists dedup+sort, isolation_evidence additionally
    strips (route reads it as a stripped distinct set), prior_models keeps
    multiplicity (two failures of one model are two entries), and
    local_policy follows bool(lp): falsy (absent or {}) becomes null.
    """
    lp = task._local_policy
    lp_norm = None
    if lp:
        lp_norm = {k: lp.get(k) for k in sorted(LOCAL_POLICY_KEYS)}
        if lp_norm.get("allowed_families") is not None:
            lp_norm["allowed_families"] = sorted(set(lp_norm["allowed_families"]))
    canonical = {
        "task_class": task.task_class,
        "complexity": task.complexity,
        "uncertainty": task.uncertainty,
        "blast_radius": task.blast_radius,
        "reversibility": task.reversibility,
        "reasoning_centric": task.reasoning_centric,
        "flags": sorted(set(task.flags)),
        "prior_failures": task.prior_failures,
        "prior_models": sorted(task.prior_models),
        "runtime": task.runtime,
        "unavailable_roles": sorted(set(task.unavailable_roles)),
        "unavailable_models": sorted(set(task.unavailable_models)),
        "isolation_available": task.isolation_available,
        "isolation_evidence": sorted({e.strip() for e in task.isolation_evidence
                                      if e.strip()}),
        "local_policy": lp_norm,
    }
    # Key omission, not a null: an undeclared seat must hash exactly as it did
    # before this field existed, or every stored fingerprint from an earlier
    # release stops matching a request that did not change. Same rule
    # `host_seat` follows, and `test_undeclared_preserves_legacy_hash_by_key_omission`
    # is what holds it.
    if task.worker_seat is not None:
        canonical["worker_seat"] = task.worker_seat
    if task._host_seat is not None:
        canonical["host_seat"] = {
            "model": task._host_seat["model"],
            "effort": task._host_seat.get("effort"),
        }
    if task._review_context is not None:
        canonical["review_context"] = task._review_context
    if task._attempt_outcomes is not None:
        canonical["attempt_outcomes"] = task._attempt_outcomes
    if task._implementer is not None:
        canonical["implementer"] = task._implementer
    if task._checks_available is not None:
        canonical["checks_available"] = task._checks_available
    if task._family_quota is not None:
        canonical["family_quota"] = task._family_quota
    return hashlib.sha256(_canonical_json(canonical).encode()).hexdigest()


def decision_fingerprint_of(request_sha: str, policy_sha: str,
                            version: str) -> str:
    """Same request x same policy x same router version -> same decision.
    Identifies the DECISION equivalence class, not the task instance —
    instance linkage lives in the receipt's prompt_sha256 (see design §8).
    """
    return hashlib.sha256(_canonical_json({
        "request_sha256": request_sha,
        "policy_sha256": policy_sha,
        "router_plugin_version": version,
    }).encode()).hexdigest()


def _clamp(policy: Policy, effort: str, model: str | None) -> str:
    """The highest level at or below `effort` that `model` will accept."""
    ceiling = policy.ceiling_of.get(model) if model else None
    if ceiling is None:
        return effort
    return policy.efforts[min(policy.efforts.index(effort),
                              policy.efforts.index(ceiling))]


_INDEPENDENCE_ORDER = {"unavailable": 0, "degraded": 1, "planned": 2, "enforced": 3}


def _seat_effort(policy: Policy, plan: dict, role: str, base: str) -> str:
    rec = next((x for x in plan["effort_ceiling_applied"] if x["role"] == role), None)
    return rec["capped_at"] if rec else base


def _contract_violation(policy: Policy, cand: dict, legacy: dict) -> str | None:
    """First row of design DD-2 S6's table that `cand` fails against `legacy`,
    or None when adopting the candidate leaves the review/control contract no
    worse. Row order is evaluation order; the name is what the yield note
    reports. Both arguments are `_plan()` results (or the same shape)."""
    ei = policy.efforts.index
    cr, lr = cand["review"], legacy["review"]
    if legacy["terminal"] and not cand["terminal"]:
        return None                                              # row 1: unlocking is allowed
    if cand["terminal"] and cand["terminal"] != legacy["terminal"]:
        return "terminal"
    if not set(cand["human_control_causes"]) <= set(legacy["human_control_causes"]):
        return "human_control_causes"
    if cr["band"] != lr["band"]:
        return "review.band"
    if (len(cr["reviewers"]), cr["effort"], cr["required_checks"], cr["independence_required"]) != \
       (len(lr["reviewers"]), lr["effort"], lr["required_checks"], lr["independence_required"]):
        return "review.shape"
    ci, li = cr["review_independence"], lr["review_independence"]
    # The `not_applicable` mismatch disjunct cannot fire on a pair of real plans
    # today: `independence()` returns `not_applicable` exactly when
    # `review["independent"]` is false, and the row above already compared
    # `independence_required` — which IS that flag — and returned. It is kept as
    # depth against a future band whose `independent` flag stops tracking
    # `review_independence`, because the ordering below has no answer for
    # `not_applicable` and would raise a KeyError instead of yielding
    # [impl-R1-opus-F5].
    if (ci == "not_applicable") != (li == "not_applicable") or (
            ci != "not_applicable" and _INDEPENDENCE_ORDER[ci] < _INDEPENDENCE_ORDER[li]):
        return "review_independence"
    for flag in ("independence_compromised", "band_floor_unsatisfiable", "judge_unavailable"):
        if cr[flag] and not lr[flag]:
            return "review.flags"
    if cr.get("judge_model") and lr.get("judge_model") and \
            policy.tier_of[cr["judge_model"]] < policy.tier_of[lr["judge_model"]]:
        return "judge_tier"
    if legacy["cross_family_review"] and not cand["cross_family_review"]:
        return "cross_family_review"
    ct = sorted((policy.tier_of[m] for m in cr["reviewer_models"] if m), reverse=True)
    lt = sorted((policy.tier_of[m] for m in lr["reviewer_models"] if m), reverse=True)
    if len(ct) != len(lt) or any(c < l for c, l in zip(ct, lt)):
        return "reviewer_tiers"
    # A seat with no review effort (the lead of a REVIEW task on the band that
    # seats no reviewer, DD-B4) carries the worker's effort, compared above.
    ce = sorted((ei(e) for role in cr["reviewers"]
                 if (e := _seat_effort(policy, cand, role, cr["effort"])) is not None), reverse=True)
    le = sorted((ei(e) for role in lr["reviewers"]
                 if (e := _seat_effort(policy, legacy, role, lr["effort"])) is not None), reverse=True)
    if any(c < l for c, l in zip(ce, le)):
        return "reviewer_efforts"
    if len(cr["review_depth_reduced"]) > len(lr["review_depth_reduced"]):
        return "review_depth_reduced"
    cw = next((x for x in cand["effort_ceiling_applied"] if x["role"] == cand["selected_role"] and x["floor_broken"]), None)
    if cw:
        lw = next((x for x in legacy["effort_ceiling_applied"] if x["role"] == legacy["selected_role"] and x["floor_broken"]), None)
        if lw is None or ei(cw["floor_requires"]) > ei(lw["floor_requires"]) or ei(cw["capped_at"]) < ei(lw["capped_at"]):
            return "worker_floor_broken"
    return None


@dataclass(frozen=True)
class _Prelude:
    """Everything `route()` decides before the worker, handed to `_plan()`
    read-only. Tuples, not lists: `_plan` may be called twice per route
    (design DD-2 S6) and a shared list would be appended to twice."""
    request_sha: str
    policy_hash: str
    lp: dict
    local_unsat: bool
    history_note: str | None
    budget_spent: bool
    seat_kind: str
    seat_source: str
    seat_downgraded: bool
    risk_score: int
    band: str
    overrides: tuple[str, ...]
    redundant_overrides: tuple[str, ...]
    route_path: str | None
    execution_score: int
    execution_band: str
    # DD-B1: the tier of the worker the policy would have seated, which a
    # declared implementer is compared against. None when nothing is declared.
    implementer_ref_tier: int | None = None
    # DD-B2 (C1-iii): the double uncertainty weight alone lifted the band, and
    # the policy says not to charge it again when deciding a review promotion.
    uncertainty_counted_in_band: bool = False


def _without_low_quota(task: Task) -> Task:
    """`task` with its `low` quota readings dropped (DD-B8) — the same object
    when it has none. `exhausted` stays: a withheld family is withheld from
    every plan."""
    quota = task._family_quota
    if not quota or "low" not in quota.values():
        return task
    kept = {family: level for family, level in quota.items() if level != "low"}
    return replace(task, _family_quota=kept or None)


def _dispatch_seats(result: dict, policy: Policy, *, lead: bool) -> list[dict]:
    """The seats a caller dispatches, once each: the reviewers and the judge.

    `lead` — the worker seat IS reviewer-1 (a REVIEW task's lead), so that
    seat also carries the worker's effort. A terminal route dispatches nothing.
    """
    seats: list[dict] = []
    if result["terminal"] is not None:
        return seats
    rv = result["review"]
    for i, (role, model) in enumerate(zip(rv["reviewers"], rv["reviewer_models"])):
        effort = rv["effort"]
        if lead and role == result["selected_role"]:
            effort = (result["selected_effort_effective"] if effort is None else
                      max((effort, result["selected_effort_effective"]), key=policy.efforts.index))
        seats.append(dict(seat=f"reviewer-{i+1}", role=role, model_id=model, effort=effort,
                          effort_native=policy.native_effort(model, effort)))
    if rv["judge_model"]:
        seats.append(dict(seat="judge", role=rv["judge"], model_id=rv["judge_model"],
                          effort=rv["effort"],
                          effort_native=policy.native_effort(rv["judge_model"], rv["effort"])))
    return seats


def _same_model_retry(task: Task, policy: Policy, cfg: dict, history_note: str | None,
                      budget_spent: bool) -> tuple[str, str, str] | None:
    """(role, model, effort) when a capability failure may be retried on the
    SAME model at a higher effort (design 2026-09-25 DD-B6, U-7), else None
    and the ladder climbs a tier as before. All of:

    1. The history is typed and usable, nothing else stops the route (spent
       budget, unconfirmed termination, unrecovered operational outcomes), no
       implementer is declared, and every capability failure is on one model
       — a stronger model having failed too is no case for the weaker again.
    2. That model's capability failures number at most
       `retry.same_model_higher_effort`: the integer is the real budget.
    3. Its most recent capability failure declares the `effort` it ran at and,
       while `retry.require_new_evidence_on_same_tier` holds, fresh
       `retry_evidence_sha256` (validation refuses a repeated hash).
    4. The failure-free plan seats that model, and one level above the higher
       of every effort any of its records ran at and the effort that plan
       dispatches it at exists and is within its ceiling — computed, never
       clamped: a model that failed at its ceiling is not sent back at it.

    The caller then keeps the retry only if the settled plan still seats that
    model (a REVIEW lead is searched with its reviewers, and the failure's own
    confidence penalty can move the band and the search).

    Caller-declared, like every attempt record: the router reads no receipt.
    """
    history = task._attempt_outcomes
    if (history is None or history_note or budget_spent or task._implementer is not None
            or task.has("termination_unconfirmed")
            or any(row["kind"] in OPERATIONAL_OUTCOMES and row["recovery_sha256"] is None
                   for row in history)):
        return None
    failures = [row for row in history if row["kind"] == "capability_failure"]
    models = {row["model_id"] for row in failures}
    if len(models) != 1:
        return None
    (model,) = models
    retry_cfg = cfg["retry"]
    if len(failures) > retry_cfg["same_model_higher_effort"]:
        return None
    last = failures[-1]
    if "effort" not in last:
        return None
    if retry_cfg["require_new_evidence_on_same_tier"] and "retry_evidence_sha256" not in last:
        return None
    free = route(replace(task, prior_failures=0, prior_models=[], _attempt_outcomes=None,
                         flags=[f for f in task.flags if f != "termination_unconfirmed"],
                         _policy_pin=None, _policy=None), cfg)
    if free["terminal"] is not None or free["selected_model"] != model:
        return None
    # Every record of this model, operational ones included: a recovered
    # attempt that ran at MAX already had MAX (review i1). And what the
    # failure-free plan gives it is what it would be DISPATCHED at — a REVIEW
    # lead runs at the higher of its own effort and the review's.
    ran = [policy.efforts.index(row["effort"]) for row in history
           if row["model_id"] == model and "effort" in row]
    assigned = policy.efforts.index(free["selected_effort_effective"])
    for seat in free.get("dispatch_seats") or []:
        if seat["model_id"] == model and seat["effort"] is not None:
            assigned = max(assigned, policy.efforts.index(seat["effort"]))
    above = max(ran + [assigned]) + 1
    ceiling = policy.ceiling_of.get(model)
    if above >= len(policy.efforts) or (
            ceiling is not None and above > policy.efforts.index(ceiling)):
        return None
    return free["selected_role"], model, policy.efforts[above]


def route(task: Task, cfg: dict | None = None, *,
          env: Mapping[str, str] | None = None, home: Path | None = None) -> dict:
    """Route `task`. With no `cfg`, on the effective policy (base + committed
    local model state, read from `env`/`home`, default the process's); an
    explicit `cfg` is used as given and never reads state."""
    overlay = None
    if task._policy_pin is not None:
        _check_pin(task._policy_pin)
        if cfg is not None:
            # Dropping the pin would emit a route whose fingerprint is not the
            # pinned one at exit 0 (DD-A2).
            raise ValidationError(
                "policy_pin needs the router's own effective policy; an explicit "
                "cfg cannot honour it")
    if cfg is None:
        eff = resolve_effective_policy(os.environ if env is None else env,
                                       Path.home() if home is None else Path(home),
                                       pin=task._policy_pin)
        if eff.config is None:
            return _state_unavailable_route(eff.provenance)
        cfg, overlay = eff.config, eff.provenance
    policy = Policy.of(cfg)
    task.validate(policy)
    request_sha = request_sha256_of(task)

    if task._attempt_outcomes is not None:
        failures = [row["model_id"] for row in task._attempt_outcomes
                    if row["kind"] == "capability_failure"]
        flags = list(task.flags)
        if (any(row["kind"] == "termination_unconfirmed" for row in task._attempt_outcomes)
                and "termination_unconfirmed" not in flags):
            flags.append("termination_unconfirmed")
        # Project onto the legacy capability ladder without mutating the input
        # Task, so rerouting the same validated request is repeatable.
        task = replace(task, prior_failures=len(failures), prior_models=failures, flags=flags)

    # The digest of the policy ACTUALLY IN USE — the same computation that
    # keyed this Policy, not a second dump of the same object. A non-dict
    # Mapping (test instrumentation such as test_d14's recorder) has no
    # content digest and falls back to the on-disk one, so the digest walk
    # cannot mark every config path as "read" and hollow out the consumer
    # guard. Precondition, and the reason this reads a single value: `cfg`
    # must not change during a `route()` call. The router never mutates it;
    # a caller that does (from another thread, or a self-mutating Mapping)
    # gets a decision this digest does not describe.
    policy_hash = policy.content_sha or policy_sha256(CONFIG_PATH)

    lp = task._local_policy or {}
    local_unsat = False
    if isinstance(lp.get("allowed_families"), list) and len(lp["allowed_families"]) == 0:
        local_unsat = True

    # Before anything resolves: a route whose retry history cannot be used will
    # not run, so nothing may be inferred from that history on the way there.
    history_note = history_gap(task, policy)
    budget_spent = task.total_prior_attempts >= cfg["retry"]["max_total_implementation_attempts"]
    if history_note:
        if budget_spent:
            history_note = ("retry budget spent; no further attempt is available, so "
                            "the prior-model history is moot — surface this to a human")
        task = replace(task, prior_models=[])

    # Resolved before the first candidate is looked at: this decides which
    # models can fill the worker seat at all, so it cannot be computed after
    # something has already been chosen without them.
    seat_kind = task.worker_seat or policy.worker_seat_kind(task.task_class)
    seat_source = "declared" if task.worker_seat else "task_class"
    # A caller declaration that WEAKENS a class default is the one input that
    # can hand write work back to a seat with no write-capable recipe. The
    # opposite direction is disclosed, so this one is too — `isolation_available`
    # sets the precedent that a caller's claim is announced, not absorbed.
    seat_downgraded = (seat_source == "declared"
                       and seat_kind == "read_only"
                       and policy.worker_seat_kind(task.task_class) == "write")

    resolver = Resolver(task, policy)
    resolver.worker_writes = seat_kind == "write"

    risk_score = score(task, cfg)
    band = band_from_score(risk_score, policy)
    band, overrides, redundant_overrides, route_path = apply_overrides(task, band, policy)
    exec_score = execution_score(task, cfg)
    exec_band = policy.execution_band_of(exec_score)
    counted = False
    if policy.skip_uncertainty_penalty and task.uncertainty:
        # The band this task would reach with uncertainty weighted once,
        # overrides included: an override that sets the band anyway means the
        # double weight did not raise it.
        once = risk_score - task.uncertainty * (cfg["router"]["score_weights"]["uncertainty"] - 1)
        band_once = apply_overrides(task, band_from_score(once, policy), policy)[0]
        counted = policy.bands.index(band) > policy.bands.index(band_once)

    pre = _Prelude(request_sha=request_sha, policy_hash=policy_hash, lp=lp,
                   local_unsat=local_unsat, history_note=history_note,
                   budget_spent=budget_spent, seat_kind=seat_kind,
                   seat_source=seat_source, seat_downgraded=seat_downgraded,
                   risk_score=risk_score, band=band, overrides=tuple(overrides),
                   redundant_overrides=tuple(redundant_overrides), route_path=route_path,
                   execution_score=exec_score, execution_band=exec_band,
                   uncertainty_counted_in_band=counted)
    retry = _same_model_retry(task, policy, cfg, history_note, budget_spent)
    result = None
    if retry is not None:
        # DD-B6 (C5): the failed model again, one effort above every effort it
        # ran at, in the role the failure-free plan seats it in. One plan:
        # there is no ladder step to weigh an execution cell against. Kept only
        # if it is executable on that model — otherwise C5 does not hold and
        # the ladder climbs, as it always did (review i1).
        role, model, effort = retry
        retried = replace(task, _same_model_retry={"model": model, "effort": effort})
        planned = _plan(retried, policy, cfg, pre, resolver, WorkerChoice(role, (), False))
        if planned["terminal"] is None and planned["selected_model"] == model:
            task, result = retried, planned
    if result is None:
        candidate, legacy = select_worker(task, band, exec_band, policy, resolver)
        if task._implementer is not None:
            # DD-B1: the worker seat already executed. One plan, for the REVIEW of
            # that work: the role is the one the policy would have seated after the
            # execution cell (the ladder and fallback code need a role), the model
            # is the declared one, and the review is sized against the stronger of
            # the two — a weaker implementer is gated, not silently accepted.
            ref = resolver.peek(candidate.role, write=True)
            pre = replace(pre, implementer_ref_tier=policy.tier_of[ref] if ref else None)
            result = _plan(task, policy, cfg, pre, resolver, candidate)
        elif candidate is legacy:
            result = _plan(task, policy, cfg, pre, resolver, legacy)
        else:
            # DD-B2's guard. The two plans are weighed with the review promotion
            # 1.16.1 made — uncertainty charged in full — and only the adopted
            # plan is planned again without the double-counted penalty. Weighing
            # them without it let a promotion the table plan no longer takes, while
            # the execution-cell plan still took one on its own fallback, make the
            # bands differ and yield the stronger worker: counting uncertainty once
            # is a review rule, and must never cost the worker a tier.
            #
            # DD-B8 likewise: a `low` quota reading is a binding preference for
            # the worker the policy adopts, never a reason to adopt a weaker
            # one, so the plans are weighed without it and it is applied to the
            # adopted plan only.
            weigh = replace(pre, uncertainty_counted_in_band=False)
            weigh_task = _without_low_quota(task)
            with_cell = _plan(weigh_task, policy, cfg, weigh, resolver, candidate)
            without = _plan(weigh_task, policy, cfg, weigh, resolver, legacy)
            if with_cell["terminal"] and without["terminal"]:
                adopted, note = legacy, None                          # both terminal: nothing to gain
            else:
                row = _contract_violation(policy, with_cell, without)
                if (row is None and _is_source_review(task, policy) and policy.review_lead_counts
                        and without["terminal"] is None
                        and policy.tier_of[with_cell["selected_model"]]
                        <= policy.tier_of[without["selected_model"]]):
                    # A REVIEW task's lead is searched with its reviewers (DD-B7):
                    # the stronger cell only raises the lead's FLOOR, and the
                    # search can land on the same model or an equal or weaker
                    # one than the table cell's lead. That is no raise, so the
                    # table plan stands and nothing is recorded.
                    adopted, note = legacy, None
                elif row is None:
                    adopted, note = candidate, "raised"
                else:
                    adopted, note = legacy, f"execution band {exec_band} yielded {candidate.role}: {row}"
            if pre.uncertainty_counted_in_band or weigh_task is not task:
                result = _plan(task, policy, cfg, pre, resolver, adopted)
            else:
                result = with_cell if adopted is candidate else without
            if note == "raised":
                capped = (f" at effective effort {result['selected_effort_effective']} (ceiling)"
                          if result["selected_effort_effective"] != result["selected_effort"] else "")
                result["notes"].append(
                    f"execution band {exec_band} raised worker from {legacy.role} to {candidate.role}{capped}")
            elif note is not None:
                result["notes"].append(note)
    if task._review_context is not None:
        result["review_context"] = {k: list(v) if isinstance(v, list) else v
                                    for k, v in task._review_context.items()}
        result["notes"].append("review_context excludes declared source authors from every REVIEW-task seat; target identity is caller-declared")
    if _is_source_review(task, policy):
        result["dispatch_seats"] = _dispatch_seats(result, policy, lead=True)
        result["notes"].append("REVIEW task: dispatch dispatch_seats exactly once each; selected_* aliases the lead reviewer (reviewer-1), not an extra worker")
    if task._family_quota:
        exhausted = sorted(f for f, level in task._family_quota.items() if level == "exhausted")
        low = sorted(f for f, level in task._family_quota.items() if level == "low")
        if exhausted:
            result["notes"].append(
                f"family_quota: {', '.join(exhausted)} exhausted — every model of the family "
                f"is withheld (reason: quota)")
        if low:
            result["notes"].append(
                f"family_quota: {', '.join(low)} low — the worker seat prefers a same-tier "
                f"model of another family; review seats are unaffected")
    if task._implementer is not None:
        result["dispatch_seats"] = _dispatch_seats(result, policy, lead=False)
        result["notes"].append(
            "implementer is caller-declared: the worker seat already executed, so dispatch "
            "only dispatch_seats; review independence relies on this declaration, which the "
            "router does not authenticate")
    if task._attempt_outcomes is not None:
        history = task._attempt_outcomes
        result["attempt_outcomes"] = [dict(row) for row in history]
        result["attempt_outcome_summary"] = {
            "classification": "caller_declared", "total": len(history),
            "capability_failures": task.prior_failures,
            "operational_outcomes": sum(row["kind"] in OPERATIONAL_OUTCOMES for row in history),
            "unconfirmed_terminations": sum(row["kind"] == "termination_unconfirmed" for row in history),
        }
        result["notes"].append("typed attempt history separates capability escalation from operational recovery; evidence hashes are caller declarations")
    result["model_overlay"] = overlay.to_json() if overlay is not None else None
    if overlay is not None and overlay.applied and result["terminal"] is None:
        seated = {result["selected_model"], result["review"]["judge_model"],
                  *result["review"]["reviewer_models"],
                  *(seat["model_id"] for seat in result.get("dispatch_seats", []))}
        keys = {policy.id_to_key[m] for m in seated if m} & set(overlay.applied)
        result["notes"].extend(OVERLAY_SEAT_NOTE.format(key=k) for k in sorted(keys))
    result["rationale"] = explain(task, result, policy)
    return result


def _plan(task: Task, policy: Policy, cfg: dict, pre: _Prelude,
          resolver: "Resolver", choice: WorkerChoice) -> dict:
    """Everything after the worker is chosen, as a pure function of its inputs.

    Pure means: every value the old body mutated is rebuilt here from `pre`
    and `choice`; a fresh resolver owns per-pass seat assignments, so a second
    candidate plan cannot inherit the first plan's bindings.
    """
    # Each candidate plan owns its assignments; no state reaches a sibling plan.
    resolver = Resolver(task, policy)
    resolver.worker_writes = pre.seat_kind == "write"
    worker = choice.role
    if task._implementer is not None:
        resolver.pinned_worker = (worker, task._implementer["model_id"])
    worker_notes = list(choice.notes)
    ceiling_exhausted = choice.ceiling_exhausted
    overrides = list(pre.overrides)
    redundant_overrides = list(pre.redundant_overrides)
    lp, local_unsat = pre.lp, pre.local_unsat
    history_note, budget_spent = pre.history_note, pre.budget_spent
    seat_kind, seat_source, seat_downgraded = pre.seat_kind, pre.seat_source, pre.seat_downgraded
    risk_score, band, route_path = pre.risk_score, pre.band, pre.route_path
    request_sha, policy_hash = pre.request_sha, pre.policy_hash
    resolver.write_seat_role = None
    try:
        # Disclosed as a policy decision, in `notes`, not as scarcity in
        # `fallbacks_applied` — and computed here, while `write_seat_role` is still
        # unset, so the unfiltered peek still answers what the role BINDS to.
        if seat_downgraded:
            # Id-free, like the skip note: a terminal route withholds bindings.
            worker_notes.append(
                f"worker seat declared read_only against the {task.task_class} default; "
                f"a seat with no write-capable recipe on {task.runtime} may be named")
        if seat_kind == "write" and task._implementer is None:
            nominal, seated = resolver.peek(worker), resolver.peek(worker, write=True)
            if nominal is not None and nominal != seated:
                # Families, not model ids. A terminal route must withhold every
                # execution binding, and a note is part of the route — the
                # host-seat advisory's id-free note is the same rule. The id of
                # what WAS seated is `selected_model`, which a terminal route
                # already nulls.
                got = (f"seated the {policy.family_of[seated]} one"
                       if seated else "no seat left")
                worker_notes.append(
                    f"{worker}: no write-capable {policy.family_of[nominal]} seat "
                    f"on {task.runtime}; {got}")
            elif seated and policy.family_of.get(seated) == "xai" \
                    and policy.local_family.get(task.runtime) != "xai":
                worker_notes.append(
                    f"{worker}: xai write seat on {task.runtime} requires "
                    "dispatch_agent --seat-profile grok-maker-v1")
        # From here on every reader of the worker's role — review seating, judge
        # seating, the final resolve — must see the seat the worker actually got.
        resolver.write_seat_role = worker
        if history_note:
            worker_notes.append(history_note)
        effort, effort_notes = select_effort(task, band, pre.execution_band, policy)
        if task._same_model_retry is not None:
            retry_effort = task._same_model_retry["effort"]
            if policy.efforts.index(retry_effort) > policy.efforts.index(effort):
                effort = retry_effort
            effort_notes.append(
                f"same-model retry: {policy.id_to_key[task._same_model_retry['model']]} again at "
                f"{retry_effort}, above every effort it failed at "
                f"(retry.same_model_higher_effort)")
        if lp.get("minimum_effort") is not None:
            asked = lp["minimum_effort"]
            if policy.efforts.index(asked) > policy.efforts.index(effort):
                # A cap a local minimum overrode did not survive; its note
                # would describe an effort the route does not use.
                effort_notes = [n for n in effort_notes if not n.startswith(EFFORT_CAP_NOTE)]
                effort_notes.append(f"local_policy raised effort to {asked}")
                effort = asked
        disagreement = cfg["review"]["disagreement"]

        def roles_for(rev):
            needed = [worker] + list(rev["reviewers"])
            # The judge follows the REVIEW band, not the risk band — a review
            # promoted by low confidence needs adjudication just as much.
            if rev["band"] == "CRITICAL" or route_path == "disagreement":
                needed.append(disagreement["default_judge"])
            return list(dict.fromkeys(needed))

        # Bounded fixed point. Confidence depends on the fallbacks, the fallbacks
        # depend on which roles are needed, and which roles are needed depends on
        # the review band — which confidence can raise. Computing confidence once
        # from a preliminary role set let a route whose *final* fallbacks pushed it
        # below the escalation floor still emit as executable.
        review_band = band
        escape = _low_review_escape(task, policy, band, route_path, lp)
        if escape is not None:
            review_band, reasons = escape
            prefix = "low_band" if band == policy.bands[0] else "review_lead"
            overrides.extend(f"{prefix}_{why}_raised_review_to_{review_band}" for why in reasons)
        promoted_once = False
        promotion_confidence = None
        supply_exhausted: str | None = None
        # Everything the loop body mutates has to be restored at the top of each
        # pass, or the body is not idempotent and the "fixed point" is a fold.
        # Round 19: moving the plan inside the loop (round 18's fix) broke the
        # inherited assumption that a compensation runs at most once per route.
        # `review`, `applied_compensations`, `judge_role` and `supply_exhausted`
        # were already rebuilt per pass; `effort` and its notes were not, so a
        # promoted route raised effort TWICE for one compensation — 16,268 routes
        # shipped with the notes, the record and the effort all disagreeing, one of
        # them reporting no compensation at all.
        base_effort, base_effort_notes = effort, list(effort_notes)
        ceiling_records: list[dict] = []
        for _ in range(MAX_PROMOTION_PASSES):
            effort, effort_notes = base_effort, list(base_effort_notes)
            # Rebuilt with everything else the body mutates. A fixed point whose
            # body is not idempotent is a fold, and a ceiling record accumulated
            # across passes would report a cap the emitted plan never applied.
            ceiling_records = []
            resolver.assignments = {}
            review = select_review(review_band, worker, policy, resolver)
            source_judge = None
            if resolver.source_review:
                source_judge = disagreement["default_judge"] if (
                    review["band"] == "CRITICAL" or route_path == "disagreement") else None
                review, source_judge = _joint_seats(review, worker, source_judge, policy, resolver)
                preliminary_roles = list(dict.fromkeys([worker] + review["reviewers"]
                    + ([source_judge] if source_judge else [])))
            else:
                preliminary_roles = roles_for(review)
            try:
                resolved, fallbacks, compensations = resolver.resolve(preliminary_roles, write_role=worker)
            except SupplyExhausted as exc:
                resolved, fallbacks, compensations = {}, [], []
                supply_exhausted = str(exc)
            applied_compensations: list[str] = []
            for note in compensations:
                if note == "raise_effort_one_level":
                    effort = policy.effort_up(effort)
                    effort_notes.append("compensation: fallback lost family diversity, effort +1")
                    applied_compensations.append(note)
                elif note == "raise_effort_to_MAX_and_add_second_review":
                    effort = policy.efforts[-1]
                    # The note is written after the outcome is known. Round 19: it
                    # was written here, before, so a compensation that could not be
                    # completed still had "effort raised to MAX" in the notes while
                    # `fallback_compensations_applied` stayed empty — the notes
                    # claiming a compensation the record denied.
                    # The name promises two things. Recording it while doing one is the
                    # same false report this module exists to avoid, so the extra
                    # reviewer is actually added — and if none can be resolved, the
                    # compensation is not claimed.
                    extra = _extra_reviewer(review, worker, policy, resolver)
                    if extra:
                        review = dict(review)
                        review["reviewers"] = list(review["reviewers"]) + [extra]
                        if review["effort"] is None:
                            # A model seat on the band that seats none (DD-B4):
                            # reviewed at the next band's effort, the first band
                            # that asks for a model reviewer at all.
                            review["effort"] = cfg["review"][policy.bands[1]]["effort"]
                        # `independent` stays the BAND's answer. Round 4 added the flip
                        # so the extra seat would be de-conflicted; round 10 showed what
                        # it actually bought — a bonus reviewer upgrading the band's own
                        # requirement, so that a LOW route whose *compensating* review
                        # could not be isolated terminated the whole task, and the
                        # independence invariants started applying to a band that never
                        # asked. Seat allocation at the emit boundary is unconditional
                        # and works on resolved models, so the extra seat is checked
                        # either way; that is what makes this safe to drop.
                        # A count, not a name. Recording the role invited exactly the
                        # staleness `self_review_avoided` had to be rescued from: seat
                        # allocation can re-seat that role afterwards, and then the
                        # record names a reviewer who is not there. What the
                        # compensation promises is a SEAT, so the seat count is what it
                        # records.
                        review["compensating_reviewers"] = review.get("compensating_reviewers", 0) + 1
                        effort_notes.append(
                            "compensation: architect downgraded, effort raised to MAX")
                        # No re-resolve here. Round 19: this block read as "reflect
                        # the extra seat in the plan" and was a dead store — every
                        # one of its outputs is overwritten unconditionally by the
                        # final resolve at the end of this pass, and nothing between
                        # reads them (`_deconflict` and `_seat_judge` work through
                        # `resolver.peek`). Eleventh instance of the class, and a
                        # fossil besides: its `supply_exhausted or str(exc)` was the
                        # sticky-shortage policy round 15 removed, preserved here
                        # where it could not be seen.
                        applied_compensations.append(note)
                        effort_notes.append(f"compensation: added a second independent review ({extra})")
                    else:
                        effort_notes.append(
                            "compensation NOT fully applied: no additional reviewer could be resolved"
                        )
                else:
                    raise UnknownCompensationError(
                        f"fallback_compensations declares the effect {note!r}, which no branch "
                        f"implements; it would be reported as applied while doing nothing"
                    )
            compensations = applied_compensations

            # The worker's effective effort, capped to what its model can receive.
            # Here rather than after the loop because the compensation above is what
            # raises the requested value, and here rather than before it for the
            # same reason. The requested value and its notes are untouched: the
            # compensation really did raise what was asked for, and `selected_effort`
            # is what was asked for.
            #
            # `peek`, not the provisional `resolved` map. `resolve()` is
            # all-or-nothing: a shortage on any role empties the map for the whole
            # pass. `_seat_judge` can then drop the judge so the FINAL resolve
            # succeeds, and a clamp that already ran against `{}` ships the
            # uncapped effort. `peek` is pure, depends on no other role, and
            # equals the final `resolved[worker]` whenever resolution succeeds.
            # Reviewer seating still moves below, so a reviewer clamp here would
            # still read a roster that does not ship.
            # Final de-confliction, at the emit boundary rather than mid-pipeline.
            #
            # This invariant has now been broken four times, each in a different place,
            # because it was being enforced at one point that later code could route
            # around: the compensation path above appends a reviewer and flips
            # `independent` to true, so a LOW-band review that never went through
            # de-confliction was promoted to "independent" with the worker's own model
            # sitting in a reviewer slot. Checking in the middle protects only the
            # paths that existed when the check was written. Checking here protects
            # every path, including ones added later, because nothing runs after it.
            judge_role = disagreement["default_judge"] if (
                review["band"] == "CRITICAL" or route_path == "disagreement") else None

            # Seat allocation. This block is unconditional on purpose.
            #
            # The previous version wrapped it in `if review["independent"]:`, which is
            # how the same defect survived a fifth round: a check moved to the boundary
            # but placed behind a condition is not a boundary, it is a mid-pipeline
            # check in a new location. The disagreement path sets a judge at ANY band,
            # and LOW declares `independent: false`, so LOW + disagreement skipped seat
            # allocation entirely and the implementer adjudicated its own work.
            #
            # Reviewer de-confliction is still gated on `independent` — LOW's
            # worker-reviews-itself is documented design. The judge is not covered by
            # that exemption: an adjudicator brought in to settle a dispute must not be
            # one of the parties, whatever the band.
            if resolver.source_review:
                judge_role = source_judge
            else:
                if review["independent"]:
                    review = _deconflict(review, worker, policy, resolver)
                if judge_role:
                    review, judge_role = _seat_judge(review, worker, judge_role, policy, resolver)

            review, judge_role = _joint_seats(review, worker, judge_role, policy, resolver)

            worker_model = resolver.peek(worker, write=True)
            worker_effective = _clamp(policy, effort, worker_model)
            if worker_effective != effort:
                floor = _worker_effort_floor(task, band, pre.execution_band, policy)
                broken = floor and policy.efforts.index(worker_effective) < policy.efforts.index(floor[1])
                ceiling_records.append({
                    "role": worker, "model": worker_model,
                    "requested": effort, "capped_at": worker_effective,
                    "floor_broken": floor[0] if broken else None,
                    "floor_requires": floor[1] if broken else None,
                })

            try:
                resolved, fallbacks, _ = resolver.resolve(
                    list(dict.fromkeys([worker] + list(review["reviewers"])
                                       + ([judge_role] if judge_role else []))),
                    write_role=worker)
                # The final seat plan resolved, so any shortage seen while exploring a
                # preliminary one is not a fact about this route. Round 15: it was
                # sticky, and a LOW disagreement route whose provisional
                # `principal_architect` could not resolve stayed terminal even though
                # `_seat_judge` had found a complete assignment.
                supply_exhausted = None
            except SupplyExhausted as exc:
                resolved, fallbacks = {}, []
                supply_exhausted = str(exc)
            # From the FINAL fallbacks: the plan above is the one that ships, so
            # the number the promotion decision reads is the number the route
            # reports. Rounds 16-18 each moved this and each moved it wrong — into
            # the loop reading a preliminary resolve, then after the loop where the
            # post-conditions could not see the promotion. The plan and the decision
            # belong in the same iteration.
            confidence = routing_confidence(task, fallbacks, cfg)
            # DD-B2: the promotion reads the confidence without the uncertainty
            # penalty when that uncertainty already raised the band. Only this
            # decision — the reported value and ESCALATE_ROUTING keep it.
            promotion_input = (routing_confidence(task, fallbacks, cfg, skip_uncertainty=True)
                               if pre.uncertainty_counted_in_band else confidence)
            threshold = cfg["router"]["confidence"]["extra_review_below"]
            # The policy is "raise the review band ONE level" — the loop exists so
            # the terminal decision sees the final confidence, not to change how
            # far the promotion goes. Confidence is very nearly invariant in the
            # review band, so a loop that re-promotes on every pass walks to
            # CRITICAL every time; that regression put a CRITICAL human gate on
            # routine documentation work, and a gate that fires on everything
            # trains people to wave it through.
            #
            # Round 18: this settles the BAND only. Round 17 re-ran the promotion
            # after the emit-boundary post-conditions had already passed, so a
            # promoted route shipped without the depth, family and de-confliction
            # checks — a change placed where the checks could not see it, which is
            # the same shape as a check placed where later code routes around it.
            # Everything the post-conditions inspect is now built after this loop,
            # from the band it settled on.
            if promotion_input < threshold and review_band != "CRITICAL" and not promoted_once:
                promoted = policy.bands[policy.bands.index(review_band) + 1]
                overrides.append(f"low_routing_confidence_raised_review_to_{promoted}")
                review_band = promoted
                promoted_once = True
                # The number the DECISION read. The one the route reports is the
                # promoted plan's, and the two can differ (see the note below).
                promotion_confidence = promotion_input
                continue
            break
        else:  # pragma: no cover - the band ladder is shorter than the pass budget
            raise ConfigError("review band promotion failed to reach a fixed point")


        if resolver.assignments:
            worker_notes.append("jointly allocated eligible models across the review and judge seats")
        # A cap a later compensation overrode did not survive either (review
        # i1): the note describes the effort the route runs at, or nothing.
        cap = policy.effort_caps.get(band)
        if cap is not None and effort != cap:
            effort_notes = [n for n in effort_notes if not n.startswith(EFFORT_CAP_NOTE)]

        # Post-condition, asserted rather than assumed. Reviewer duplication is
        # only a defect where independence was requested; a judge sharing any seat
        # is a defect always.
        seat_models = {
            **({"worker": resolved.get(worker)} if not resolver.source_review else {}),
            **{f"reviewer_{i}": resolved.get(x) for i, x in enumerate(review["reviewers"])},
        }
        if review["independent"]:
            filled = [m for m in seat_models.values() if m]
            if len(filled) != len(set(filled)):
                review = dict(review)
                review["independence_compromised"] = True
        if judge_role:
            judge_model = resolved.get(judge_role)
            parties = [m for m in seat_models.values() if m]
            outranked = judge_model and parties and (
                policy.tier_of[judge_model] < max(policy.tier_of[m] for m in parties))
            if judge_model and (judge_model in parties or outranked):
                review = dict(review)
                review["judge_unavailable"] = True
                judge_role = None

        # Post-conditions on the emitted review, both unconditional.
        #
        # The first asks whether the reviewers who ended up in the seats still meet
        # the depth the band asked for. Fallbacks and de-confliction both re-seat
        # reviewers under scarcity, and neither consults the band while doing it,
        # so a HIGH review can be staffed at tier 0. That may be the best available
        # assignment — it is not one to emit as if the band were satisfied.
        #
        # The second asks whether every recorded substitution still describes the
        # final roster. A record that names a reviewer who is not there is worse
        # than no record: it is the rationale asserting a fact about the route that
        # the route contradicts.
        floor = _medium_reviewer_floor(policy, review["band"], resolved.get(worker))
        # None on the deterministic LOW band: its only possible seat is a
        # compensation's extra reviewer, which no band floor asked for.
        shortfall = [] if floor is None else [
            {"reviewer": role, "model": resolved[role],
             "capability_tier": policy.tier_of[resolved[role]], "band_requires": floor}
            for role in review["reviewers"] if resolved.get(role)
            and policy.tier_of[resolved[role]] < floor
        ]
        stale = [s for s in (review.get("self_review_avoided") or [])
                 if s.get("with") not in review["reviewers"]]
        if stale:
            # Silently dropping the record would satisfy every downstream check
            # while destroying a disclosure the human was owed — and round 7 found
            # that a corrective boundary also makes the assertions guarding it
            # incapable of failing, because the emitted value then satisfies them
            # by construction. Nothing upstream may produce this state; if one
            # does, that is a defect in the pipeline and it says so out loud.
            raise RouterInvariantError(
                f"substitution record outlived the seat it describes: {stale}; "
                f"seats hold {review['reviewers']}"
            )
        # Scarcity and binding capacity produce the same shortfall and need
        # different answers. Scarcity is recoverable: the human can wait for the
        # model to come back. A binding that structurally cannot supply the tier is
        # not — `openai_only` holds exactly one model at tier 2, so every HIGH
        # route under a downed bridge on that side gates, permanently, and "proceed"
        # is the only possible answer. This module's own reasoning is that a gate
        # firing on everything trains people to wave it through, so the two are
        # told apart in the output rather than presented identically.
        # The implementer occupies a distinct model only where independence is
        # required, and it counts against the floor-tier supply only if it is
        # itself at or above the floor — a `worker_balanced` implementer does not
        # consume a tier-2 model. Counting it unconditionally over-stated the
        # requirement by one and reported an ordinary, recoverable shortage as
        # permanent, pushing the operator toward "proceed at reduced depth" on a
        # gate that restoring one model would have cleared.
        worker_model = resolved.get(worker)
        seats = len(review["reviewers"]) + (
            1 if not resolver.source_review and review["independent"] and worker_model
            and policy.tier_of[worker_model] >= floor else 0)
        # Every id the binding can reach, including roles outside `role_tiers`
        # (`worker_balanced_alt`) that the fallback ladder can still seat.
        supply = {cfg["models"][key]["id"] for key in resolver.binding.values()}
        if resolver.source_review:
            supply = {cfg["models"][key]["id"] for role in policy.roles
                      for key in resolver._candidates(role)}
        unsatisfiable = bool(shortfall) and sum(
            1 for m in supply if policy.tier_of[m] >= floor) < seats
        if shortfall:
            review = dict(review)
            review["review_depth_reduced"] = shortfall
            review["band_floor_unsatisfiable"] = unsatisfiable

        fams = {r: policy.family_of[m] for r, m in resolved.items()}
        reviewer_families = {fams[r] for r in review["reviewers"] if r in fams}
        cross_family = len(reviewer_families) > 1 or (
            len(review["reviewers"]) == 1 and review["reviewers"][0] in fams and worker in fams
            and fams[review["reviewers"][0]] != fams[worker]
        )
        if resolver.source_review and len(review["reviewers"]) == 1:
            # One seat reviews the source: cross-family means cross to its
            # declared authors. With no authors declared there is nothing to
            # be cross to, and one family is not two.
            source_families = {policy.family_of[m] for m in resolver.author_excluded}
            cross_family = bool(source_families) and bool(reviewer_families - source_families)


        if resolver.allowed_families is not None and len(resolver.allowed_families) == 0:
            local_unsat = True
        if not local_unsat and lp:
            if lp.get("minimum_capability_tier") is not None and resolved.get(worker):
                if policy.tier_of[resolved[worker]] < int(lp["minimum_capability_tier"]):
                    local_unsat = True
            if lp.get("minimum_reviewers") is not None:
                if len(review.get("reviewers") or []) < int(lp["minimum_reviewers"]):
                    local_unsat = True
            if lp.get("minimum_provider_families") is not None:
                seated = {policy.family_of[m] for m in resolved.values()}
                if len(seated) < int(lp["minimum_provider_families"]):
                    local_unsat = True
            if lp.get("minimum_effort") is not None and resolved.get(worker):
                ceiling = policy.ceiling_of.get(resolved[worker])
                if ceiling is not None and policy.efforts.index(ceiling) < policy.efforts.index(lp["minimum_effort"]):
                    local_unsat = True

        # The band is settled and the plan above was built from it, so the
        # confidence that ships is the confidence of what ships. Round 16 found the
        # two disagreeing; round 17's fix put the correction after the
        # post-conditions and round 18 moved the whole plan below the loop instead.
        confidence = routing_confidence(task, fallbacks, cfg)
        ask = orchestrator_ask(task, policy, cfg, band, confidence)
        declared = task._host_seat
        model_cmp, effort_cmp, advisory = host_seat_comparisons(declared, ask, policy)
        host_seat_advisory = {
            "declared": dict(declared) if declared else None,
            "policy_ask": ask,
            "model_comparison": model_cmp,
            "effort_comparison": effort_cmp,
            "advisory": advisory,
        }

        review_independence = independence(review, task)
        supplied = len({e.strip() for e in task.isolation_evidence if e.strip()})
        if supplied and review["independent"] and supplied != len(review["reviewers"]):
            # The caller typed evidence and it was NOT counted — say so, or the
            # refusal is invisible and the next caller pads the list further.
            effort_notes.append(
                f"isolation evidence not counted: {supplied} id(s) for "
                f"{len(review['reviewers'])} reviewer seat(s); independence stays "
                f"{review_independence!r}")
        judge = judge_role

        band_requires_independence = bool(
            cfg["review"][review["band"]].get("independent", False))

        # One dispatcher over the configured actions, instead of five hand-written
        # comparisons. Round 12 found those comparisons validated against a UNION of
        # the vocabulary while each consumer implemented one word of it, so the
        # strictest-sounding value silently removed the control — and a key with
        # exactly one implemented value is not configuration at all, it is a
        # constant with a config file in front of it. Every action is implemented
        # here, so every key genuinely selects behaviour and a test can prove it.
        #
        # `band_requires_independence` is the BAND's spec, not `review`'s flag: the
        # architect compensation sets that flag at any band, and keying off it let a
        # *bonus* reviewer's isolation gap terminate a LOW route.
        # Each control carries a machine-readable CAUSE alongside its prose reason,
        # and the cause is emitted. Round 15's reviewers converged on this after the
        # seventh instance of the class that has cost this loop six rounds: an edit
        # whose comment claims one thing while the predicate does another. A comment
        # cannot be checked; a cause code can, and
        # `test_d19_every_control_fires_exactly_on_its_declared_cause` asserts that
        # each control's predicate partitions the sweep exactly as its cause says.
        # Reviewer and judge seats, now that the roster is final. Their floor is
        # the review band's own effort — the promoted band, because that is the
        # review that will run. Unlike the worker there is no table/floor split
        # here: what the band names IS the requirement.
        review_floor = review["effort"]
        for role in (list(review["reviewers"]) + ([judge] if judge else [])
                     if review_floor is not None else []):
            model = resolved.get(role)
            capped = _clamp(policy, review_floor, model)
            if capped != review_floor:
                record = {
                    "role": role, "model": model,
                    "requested": review_floor, "capped_at": capped,
                    "floor_broken": f"review.{review['band']}.effort",
                    "floor_requires": review_floor,
                }
                # Same seat, once. One role can hold two seats two different ways:
                # `_deconflict` may fail to substitute and leave the worker among
                # the reviewers (terminal, since that also compromises
                # independence), and LOW seats `worker_fast` as its own reviewer by
                # design (`independent: false`, so `_deconflict` never runs and the
                # route stays executable). Both reach here.
                existing = next((r for r in ceiling_records if r["role"] == role), None)
                if existing is None:
                    ceiling_records.append(record)
                    continue
                # One row, and it must read coherently. The seat was asked for two
                # different levels — its own and the review band's — so the row
                # reports the HIGHER ask, which is the one any named floor is
                # measured against. Keeping the worker's lower `requested` beside a
                # review floor produced rows saying "requested LOW" next to
                # "requires MEDIUM", which is not a fact about anything.
                if policy.efforts.index(record["requested"]) > policy.efforts.index(existing["requested"]):
                    existing["requested"] = record["requested"]
                # A floor that broke is never erased, and when BOTH broke the
                # stricter one is what the human has to satisfy — reporting the
                # weaker would understate what the seat owes.
                if record["floor_broken"] and (
                        not existing["floor_broken"]
                        or policy.efforts.index(record["floor_requires"])
                        > policy.efforts.index(existing["floor_requires"])):
                    existing["floor_broken"] = record["floor_broken"]
                    existing["floor_requires"] = record["floor_requires"]

        hitl = cfg["human_in_the_loop"]
        controls = [
            Control("on_independence_unachievable", "caller_declared_isolation_gap",
                    band_requires_independence and task.isolation_available is False,
                    "INDEPENDENCE_UNAVAILABLE"),
            Control("on_any_critical_review", "critical_review_band",
                    review["band"] == "CRITICAL",
                    "HUMAN_REQUIRED"),
            Control("on_judge_unavailable", "no_adjudicator",
                    bool(review.get("judge_unavailable")),
                    "HUMAN_REQUIRED"),
            Control("on_review_depth_reduced", "review_below_band",
                    bool(review.get("review_depth_reduced")),
                    "HUMAN_REQUIRED"),
            Control("on_effort_below_floor", "effort_below_floor",
                    any(r["floor_broken"] for r in ceiling_records),
                    "HUMAN_REQUIRED"),
        ]

        terminal = None
        requires_human = False
        notified: list[tuple[str, str]] = []
        fired_causes: list[str] = []
        # First, and outside the configurable set: a route whose history cannot be
        # used is not a policy choice, and it must not be masked by a control that
        # happens to fire on the same input. Round 13 found `prior_failures=4` with
        # no models reported as `HUMAN_REQUIRED`, and a missing history alongside an
        # isolation gap reported as `INDEPENDENCE_UNAVAILABLE` — both true, neither
        # the reason the caller has to act on.
        if history_note:
            # Unconditional, and that matters twice over. Round 14 suppressed this
            # branch when the budget was spent — to stop sending the caller after a
            # history it could not use — and round 15 found that had moved an
            # unconditional gate under a configurable control, so
            # `on_retry_exhaustion: notify_human` routed a task with no history at
            # all, at exit 0. The gate stays; what changes is which terminal it
            # names, because with the budget gone the actionable fact is the budget.
            terminal = "HUMAN_REQUIRED" if budget_spent else "RETRY_HISTORY_REQUIRED"

        # Likewise not configurable: a review whose seats could not be given
        # distinct models is the implementer reviewing itself. It was an
        # unconditional gate before this dispatcher existed, and round 13 caught the
        # move making it optional — `notify_human` emitted a route with two
        # identical reviewers, `independence_required: true`, at exit 0. Making a
        # key a real choice must not include the choice to delete a protection that
        # was never optional.
        # Before the derived gates. When nothing resolves, the seats cannot be given
        # distinct models either — so `independence_compromised` is true, and round
        # 15 found it claiming the terminal while the actual cause sat in a note.
        # A report that names a symptom sends the operator to the wrong problem.
        if budget_spent:
            # Ahead of the shortage, for the reason the shortage was put ahead of
            # the seat collision: name the fact the caller has to act on. Round 17
            # found the mirror of the case round 16 fixed — a spent budget with a
            # complete history reported as `SUPPLY_EXHAUSTED`, which is true and is
            # not what stops the next attempt.
            #
            # Unconditional, with no `on_retry_exhaustion` key above it: round 17
            # measured all three of that key's actions producing the same route, so
            # a key offering to vary a safety cap was a constant wearing a config
            # file. No config value may dispatch an attempt past the cap.
            #
            # And it says so. Round 18: deleting the control left this terminal
            # ANONYMOUS — `HUMAN_REQUIRED` with no cause, no note and nothing in the
            # rationale, so the one fact the caller had to act on was the one thing
            # the route did not state. Removing a control must not remove its
            # disclosure.
            terminal = terminal or "HUMAN_REQUIRED"
            requires_human = True
            worker_notes.append(
                f"retry budget spent: {task.total_prior_attempts} attempt(s) against a cap of "
                f"{cfg['retry']['max_total_implementation_attempts']} — stop retrying and "
                f"surface what was tried to a human")

        if task.has("termination_unconfirmed"):
            terminal = terminal or "TERMINATION_UNCONFIRMED"
            requires_human = True
            fired_causes.append("unconfirmed_prior_termination")
            worker_notes.append("terminal/termination_unconfirmed: " + CAUSE_REASONS["unconfirmed_prior_termination"])
        elif task._attempt_outcomes is not None:
            if any(row["kind"] in OPERATIONAL_OUTCOMES and row["recovery_sha256"] is None
                     for row in task._attempt_outcomes):
                terminal = terminal or "OPERATIONAL_RECOVERY_REQUIRED"
                requires_human = True
                worker_notes.append("operational outcomes lack recovery evidence; recover before retrying")

        if local_unsat:
            worker_notes.append("local_policy cannot be satisfied")
            terminal = terminal or "UNSATISFIABLE_LOCAL_POLICY"

        if supply_exhausted:
            worker_notes.append(f"supply exhausted: {supply_exhausted}")
            # `terminal or`, not `=`. The stated design is that this outranks the
            # states it PRODUCES — `independence_compromised` is one, because with
            # nothing resolved the seats cannot be given distinct models. A missing
            # retry history and a spent budget are not produced by it, and round 16
            # found the plain assignment burying "pass --prior-models with one model
            # id per failure" under a shortage the caller cannot fix. Placing this
            # ahead of the `terminal or` gate below is all the precedence the
            # reasoning ever asked for.
            terminal = terminal or "SUPPLY_EXHAUSTED"

        if review.get("independence_compromised"):
            # No inner `if band_requires_independence`: it cannot be false here.
            # `independence_compromised` is only ever set behind `review["independent"]`,
            # which since round 13 is the band's own spec — so the flag implies the
            # band asked. A condition that cannot be false reads as a safeguard and
            # guards nothing, which is the shape this artifact keeps removing.
            requires_human = True
            terminal = terminal or "INDEPENDENCE_UNAVAILABLE"

        for control in controls:
            if not control.fired:
                continue
            key, action = control.key, hitl[control.key]
            terminal_name, why = control.terminal, control.reason
            fired_causes.append(control.cause)
            if action == "terminal":
                terminal = terminal or terminal_name
                effort_notes.append(f"terminal/{key}: {why}")
            elif action == "require_human_confirmation":
                requires_human = True
                effort_notes.append(f"confirm/{key}: {why}")
            elif action == "notify_human":
                # Deferred: whether the route proceeds is not known until every
                # control and every non-configurable terminal has been evaluated,
                # and round 14 found this note asserting "proceeding without a
                # gate" on a route that was terminal at exit 1.
                notified.append((key, why))
            else:
                # No `else: treat it as the weakest action`. `Policy` validates
                # this vocabulary at build time, and since 1.2.0 a dict config
                # edited between routes is rebuilt (the content digest moved), so
                # that path now raises in `Policy.__init__` and never reaches
                # here. This stays as depth: the remaining way in is a non-dict
                # Mapping, whose Policy is still cached on identity alone — and
                # defaulting an unknown word to "notify" is a control failing
                # OPEN, which is the one direction it must never fail.
                raise ConfigError(
                    f"human_in_the_loop.{key} = {action!r} is not an implemented action")

        # DD-B1. Not a configurable control: a caller who declares weaker work
        # than the policy would have dispatched is asking for a review sized for
        # the wrong worker, and only a person can accept that.
        if (pre.implementer_ref_tier is not None
                and policy.tier_of[task._implementer["model_id"]] < pre.implementer_ref_tier):
            requires_human = True
            fired_causes.append("implementer_below_worker_tier")
            effort_notes.append("confirm/implementer_below_worker_tier: "
                                + CAUSE_REASONS["implementer_below_worker_tier"])

        # Outcomes the config does not govern: these are properties of the route,
        # not policy choices.
        if terminal is None:
            if ceiling_exhausted:
                terminal = "HUMAN_REQUIRED"
            elif confidence < cfg["router"]["confidence"]["escalate_below"]:
                terminal = "ESCALATE_ROUTING"

        # A terminal outcome always needs a person, whatever the controls above
        # decided — including the ones the config set to `notify_human`.
        requires_human = bool(terminal) or requires_human

        # Deferral, decided last so it can see every gate that fired. Round 20:
        # what moves is WHEN the human is asked, and only where the review itself
        # can be trusted. `independence_compromised` and `review_depth_reduced` say
        # it cannot be — the reviewers are the implementer under another label, or
        # there are fewer of them than the band requires — and an incident does not
        # make an untrustworthy review acceptable. Those keep blocking, as does any
        # terminal.
        #
        # `judge_unavailable` deliberately does NOT block deferral. An adjudicator
        # is needed only if the two reviewers disagree, which is an event AFTER the
        # review runs, and the deferred confirmation is where that lands anyway.
        # Excluding it looked prudent and was measured to be wrong: the canonical
        # incident — a CRITICAL hotfix whose frontier models are all sitting in
        # reviewer seats — has no free adjudicator almost by construction, so the
        # exclusion turned the deferral off exactly where it was written for.
        deferred = False
        if (task.has("production_hotfix") and requires_human and not terminal
                and hitl["on_production_hotfix"] == "defer_human_confirmation"
                and not review.get("independence_compromised")
                and not review.get("review_depth_reduced")
                and not any(r["floor_broken"] for r in ceiling_records)
                # A prior write-capable attempt whose process tree could not be
                # confirmed dead is not made acceptable by an incident — it is
                # made MORE dangerous: hotfix pressure is exactly when a second
                # writer racing the first is likeliest. This gate is a hold,
                # not a disclosure a later confirmation can absorb, so
                # production_hotfix's deferral does not reach it either.
                and not task.has("termination_unconfirmed")
                # A review sized for a stronger worker than the one that ran is
                # not a review a later confirmation can absorb either (DD-B1).
                and "implementer_below_worker_tier" not in fired_causes):
            deferred = True
            requires_human = False
            effort_notes.append(
                "production hotfix: the review runs at full depth and the human "
                "confirmation is owed AFTER the fix ships, not before it")

        for key, why in notified:
            if terminal:
                outcome = "recorded; the route is terminal for another reason"
            elif requires_human:
                # Round 15: this said "proceeding without a gate" whenever the route
                # was not terminal, which is false when a DIFFERENT control gated it.
                outcome = "recorded; another control requires confirmation"
            else:
                outcome = "proceeding without a gate, per policy"
            effort_notes.append(f"notify_human/{key}: {why} — {outcome}")

        # The router cannot verify where an isolation receipt came from: it is a
        # caller-supplied string bound to no dispatch, so `enforced` reports what
        # the caller claims and unlocks nothing. That is why the CRITICAL control
        # above keys on the band and never on the receipt — making the strongest
        # gate in the policy openable by typing is the failure this skill is about.

        # A terminal route emits no execution bindings at all. Nulling only the
        # worker left a consumer able to dispatch the reviewers from a route the
        # rationale said must not be executed.
        executable = terminal is None
        if executable:
            # Served-model caveat disclosure (design §4 B5): per MODEL once,
            # registry key not model id (terminal withholding scans ids), the
            # matched flags joined so multiple caveats stay one line.
            disclosed: set[str] = set()
            for role in dict.fromkeys([worker] + list(review["reviewers"])
                                      + ([judge] if judge else [])):
                model = resolved.get(role)
                if not model or model in disclosed:
                    continue
                disclosed.add(model)
                key = policy.id_to_key[model]
                caveats = cfg["models"][key].get("served_model_caveats") or []
                matched = sorted(set(caveats) & set(task.flags))
                if matched:
                    # The vendor comes from the SEATED model's family, never a
                    # literal: `served_model_caveats` is accepted on any registry
                    # row, so hard-coding one vendor is a false disclosure one
                    # config edit away (round-1 review F3). The registry key still
                    # does the identifying — model ids stay out of notes.
                    worker_notes.append(
                        f"provider may substitute another {policy.family_of[model]} "
                        f"model for {', '.join(matched)} content; the requested "
                        f"identity of {key} is declared_only")
        all_notes = worker_notes + effort_notes
        # A promotion is decided on the plan that existed BEFORE it, and the plan it
        # produces is what ships: promoting the review band reseats reviewers, which
        # can retire the very fallback whose penalty triggered the promotion. The
        # emitted confidence is the promoted plan's, so a route could carry
        # `low_routing_confidence_raised_review_to_X` beside a confidence at or above
        # the threshold — a recorded reason the same route disproves. Un-promoting
        # would oscillate (the un-promoted plan is low again), so the conservative
        # band stands and the recovery is disclosed instead of hidden.
        extra_review_below = cfg["router"]["confidence"]["extra_review_below"]
        if promoted_once and confidence >= extra_review_below:
            all_notes.append(
                f"review band promoted at pre-promotion confidence "
                f"{promotion_confidence:.2f}; the promoted plan resolves at "
                f"{confidence:.2f} (>= {extra_review_below:.2f}) — the promotion "
                f"stands and the reported confidence is the promoted plan's")
        if advisory == "upgrade_recommended":
            clauses = []
            if model_cmp == "below":
                clauses.append(
                    f"model tier {policy.tier_of[declared['model']]} < {ask['tier']}")
            if effort_cmp == "below":
                clauses.append(f"effort {declared['effort']} < {ask['effort']}")
            suffix = f" [{', '.join(ask['raised_by'])}]" if ask["raised_by"] else ""
            all_notes.append("host seat below orchestrator ask: "
                             + "; ".join(clauses) + suffix)
        if model_cmp == "unrecognized":
            all_notes.append(
                "host seat model is not in the registry (model_comparison=unrecognized): "
                "the registry may be stale - bump the id and re-probe")
        effective_policy = {
            "minimum_capability_tier": lp.get("minimum_capability_tier"),
            "minimum_effort": lp.get("minimum_effort"),
            "minimum_reviewers": lp.get("minimum_reviewers"),
            "minimum_provider_families": lp.get("minimum_provider_families"),
            "allowed_families": lp.get("allowed_families"),
        }
        worker_model = resolved.get(worker) if executable else None
        result = {
            "route_schema_version": ROUTE_SCHEMA_VERSION,
            "router_plugin_version": plugin_manifest_version(),
            "policy_sha256": policy_hash,
            "request_sha256": request_sha,
            "decision_fingerprint": decision_fingerprint_of(
                request_sha, policy_hash, plugin_manifest_version()),
            "effective_policy": effective_policy,
            "host_seat_advisory": host_seat_advisory,
            "worker_seat": {
                "kind": seat_kind,
                "source": seat_source,
                "overrode_class_default": seat_downgraded,
                # What this host can dispatch write work to, as the transport
                # table stands. Recorded on every route, including `read_only`
                # ones where it filtered nothing: the point of the metrics is to
                # let someone reconstruct what was AVAILABLE at decision time, and
                # a field that appears only when it bit cannot do that.
                "write_capable_families": sorted(
                    f for f in set(policy.family_of.values())
                    if policy.write_capable(task.runtime, f)),
            },
            "selected_capability_tier": (
                policy.tier_of[worker_model] if worker_model else None),
            # DD-B1: whether the worker seat is still to be dispatched or has
            # already executed under a caller-declared implementer.
            "worker_seat_state": "already_executed" if task._implementer else "to_dispatch",
            "implementer_declared": task._implementer is not None,
            "implementer_source": "caller_declared" if task._implementer else None,
            "selected_families": sorted({
                policy.family_of[m] for m in resolved.values()
            }) if executable else [],
            "local_policy_applied": bool(lp),
            "task_class": task.task_class,
            "complexity": task.complexity,
            "uncertainty": task.uncertainty,
            "blast_radius": task.blast_radius,
            "reversibility": task.reversibility,
            "reasoning_centric": task.reasoning_centric,
            "risk_score": risk_score,
            "risk_band": band,
            "execution_score": pre.execution_score,
            "execution_band": pre.execution_band,
            "band_overrides_applied": overrides,
            "band_overrides_redundant": redundant_overrides,
            "critical_flags": task.critical_flags(policy),
            "route_path": route_path,
            "terminal": terminal,
            "selected_role": worker if executable else None,
            "selected_model": resolved.get(worker) if executable else None,
            "selected_effort": effort if executable else None,
            # What the worker's model will actually receive. Equal to
            # `selected_effort` when it has no ceiling. The pair is deliberately not
            # collapsed: the first is what the policy asked for, the second is what
            # runs, and reporting the ask as the outcome is how a control becomes a
            # false assurance.
            "selected_effort_effective": worker_effective if executable else None,
            "selected_effort_native": (
                policy.native_effort(resolved[worker], worker_effective)
                if executable else None),
            "review": {
                "band": review["band"],
                "reviewers": review["reviewers"],
                "reviewer_models": [resolved.get(r) for r in review["reviewers"]] if executable else [],
                "effort": review["effort"] if executable else None,
                "independence_required": review["independent"],
                "review_independence": review_independence,
                "required_checks": review.get("required_checks", []),
                # DD-B4: `deterministic_checks` when the settled band seats no
                # model reviewer — the checks ARE the review, and exit 0 means
                # dispatchable, not that they passed.
                "mode": "model_review" if review["reviewers"] else "deterministic_checks",
                "judge": judge,
                "judge_model": (resolved.get(judge) if judge else None) if executable else None,
                "self_review_avoided": review.get("self_review_avoided") or [],
                "independence_compromised": bool(review.get("independence_compromised")),
                "judge_unavailable": bool(review.get("judge_unavailable")),
                # A terminal route emits no concrete model anywhere, this field
                # included. The shortfall is still reported — the human needs to
                # know the review was thin — but with the binding withheld, the
                # same way `fallbacks_applied` scrubs ids two fields below.
                "review_depth_reduced": [
                    s if executable else {**s, "model": None}
                    for s in (review.get("review_depth_reduced") or [])
                ],
                "band_floor_unsatisfiable": bool(review.get("band_floor_unsatisfiable")),
                # Not gated on `executable`: that rule exists to withhold concrete
                # MODELS from a terminal route, and a count is not a model. Zeroing
                # it produced a terminal route reporting the compensation applied,
                # two reviewers seated, and zero compensating reviewers.
                "compensating_reviewers": review.get("compensating_reviewers", 0),
            },
            "cross_family_review": cross_family,
            "fallbacks_applied": (
                fallbacks if executable
                else [f.split(":")[0] + ": binding withheld (terminal route)"
                      if ":" in f and "->" in f else f for f in fallbacks]
            ),
            # Scrubbed the way `review_depth_reduced` is: the cap is still
            # disclosed on a terminal route — a human deciding what went wrong
            # needs it — but with the binding withheld.
            "effort_ceiling_applied": [
                r if executable else {**r, "model": None} for r in ceiling_records
            ],
            "fallback_compensations_applied": compensations,
            "unavailable_models": sorted(resolver.blocked),
            "excluded_prior_failures": sorted(resolver.failed),
            "escalation_count": task.prior_failures,
            "retry_count": task.total_prior_attempts,
            "routing_confidence": confidence,
            # A heuristic gate score (base minus penalties), NOT a calibrated
            # success probability. The kind is emitted so no consumer has to
            # guess which one it is reading.
            "routing_confidence_kind": "heuristic_policy_score",
            "requires_human_confirmation": requires_human,
            # Which configurable controls fired, by cause rather than by prose. The
            # reason strings are for people; these are what a test can hold the
            # predicate to.
            "human_control_causes": sorted(fired_causes),
            # Dispatchable now, and a confirmation is owed once it has shipped. Not
            # folded into `requires_human_confirmation`: a caller that blocks on
            # that boolean would block on this too, which is the whole thing the
            # deferral exists to avoid.
            "human_confirmation_deferred": deferred,
            "notes": all_notes,
        }
        return result
    finally:
        resolver.write_seat_role = None
        resolver.assignments = {}


def explain(task: Task, r: dict, policy: Policy) -> str:
    # The ceiling is the weights at their maximum, not the literal 18 that was
    # typed here — a weight change used to leave every rationale quoting a
    # denominator the scorer could no longer reach.
    parts = [f"{task.task_class} scored {r['risk_score']}/{policy.max_risk_score} "
             f"(c={task.complexity} u={task.uncertainty} b={task.blast_radius} r={task.reversibility}) "
             f"-> band {r['risk_band']}; execution {r['execution_score']}/{policy.max_execution_score} "
             f"-> {r['execution_band']}."]
    if r["band_overrides_applied"]:
        parts.append(f"Overrides applied: {', '.join(r['band_overrides_applied'])}.")
    if r["band_overrides_redundant"]:
        parts.append(f"Overrides that fired but were already satisfied: "
                     f"{', '.join(r['band_overrides_redundant'])}.")
    if r["critical_flags"]:
        parts.append(f"Critical-domain flags: {', '.join(r['critical_flags'])}.")
    if r["terminal"]:
        history_text = (f"{task.prior_failures} prior failure(s)" if task._attempt_outcomes is None
                        else f"{task.total_prior_attempts} prior attempt(s), {task.prior_failures} capability failure(s)")
        parts.append(
            f"TERMINAL: {r['terminal']} — no executable bindings emitted; routing confidence "
            f"{r['routing_confidence']} after {history_text}. Surface to a "
            "human with what was tried, what evidence accumulated, and the blocking uncertainty."
        )
    elif r["worker_seat_state"] == "already_executed":
        parts.append(f"Worker seat {r['selected_role']} already executed by the "
                     f"caller-declared implementer; this route plans its review.")
    else:
        effective = r["selected_effort_effective"]
        if effective != r["selected_effort"]:
            parts.append(
                f"Worker {r['selected_role']} at {effective} effort "
                f"({r['selected_effort']} was requested; the model's ceiling is lower).")
        else:
            parts.append(f"Worker {r['selected_role']} at {r['selected_effort']} effort.")
    rv = r["review"]
    if rv["mode"] == "deterministic_checks":
        parts.append(f"Review band {rv['band']}: no model reviewer — deterministic checks "
                     f"({', '.join(rv['required_checks'])}) must pass before the work is accepted.")
    else:
        parts.append(f"Review band {rv['band']}: {', '.join(rv['reviewers'])}, "
                     f"independence_required={rv['independence_required']}, "
                     f"review_independence={rv['review_independence']}.")
    for sub in (rv.get("self_review_avoided") or []):
        parts.append(f"Reviewer slot substituted: {sub['replaced']} -> {sub['with']} "
                     f"({sub['reason']}).")
    if rv.get("independence_compromised"):
        parts.append("Independence could not be established: no distinct model was available "
                     "for every seat.")
    if rv.get("judge_unavailable"):
        parts.append("No independent adjudicator is available at or above every party's tier "
                     "(the implementer included); a human must resolve any disagreement.")
    if rv.get("band_floor_unsatisfiable"):
        parts.append("The binding in force cannot supply this band's reviewer tier at all, "
                     "so the shortfall will not clear by retrying; proceeding at reduced "
                     "depth or restoring the cross-provider bridge are the only options.")
    for short in (rv.get("review_depth_reduced") or []):
        where = f" resolves to {short['model']}" if short["model"] else ""
        parts.append(f"Review depth reduced: {short['reviewer']}{where} "
                     f"(tier {short['capability_tier']}), below the tier "
                     f"{short['band_requires']} this band asks of a reviewer.")
    if rv["judge"]:
        parts.append(f"Judge: {rv['judge']}.")
    if rv["required_checks"] and rv["mode"] != "deterministic_checks":
        parts.append(f"Required checks: {', '.join(rv['required_checks'])}.")
    if r["fallbacks_applied"]:
        parts.append(f"Fallbacks: {'; '.join(r['fallbacks_applied'])}.")
    else:
        parts.append("No fallbacks applied.")
    if r["fallback_compensations_applied"]:
        parts.append(f"Compensations: {', '.join(r['fallback_compensations_applied'])}.")
    if r["excluded_prior_failures"]:
        parts.append(f"Excluded as already-failed: {', '.join(r['excluded_prior_failures'])}.")
    if not r["cross_family_review"] and rv["independence_required"]:
        parts.append("cross_family_review=false — reviewers share a family; weigh the second verdict accordingly.")
    # The rationale is what a person reads, so it names why a person is
    # involved. Round 16: the causes were emitted as codes and the reasons sat
    # in `notes`, so the prose a human sees never said which control fired —
    # and the guard that checks prose against cause had nothing to check.
    for note in r["notes"]:
        if note.split("/", 1)[0] in ("terminal", "confirm", "notify_human"):
            # Verbatim, not prettified: the declared wording is what the
            # equivalence guard matches, and a capitalised copy is a different
            # string that silently defeats it.
            parts.append(f"Human control: {note.split(': ', 1)[1]}.")
    if r["requires_human_confirmation"]:
        parts.append("Requires human confirmation before proceeding.")
    return " ".join(parts)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _split(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def build_parser(policy: Policy) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", help="full task as a JSON object; overrides the flags below")
    p.add_argument("--request-json", dest="request_json",
                   help="RouteRequestV1 JSON file; wins over --json and flags")
    p.add_argument("--class", dest="task_class", choices=policy.task_classes)
    p.add_argument("--complexity", type=int)
    p.add_argument("--uncertainty", type=int)
    p.add_argument("--blast-radius", type=int)
    p.add_argument("--reversibility", type=int)
    p.add_argument("--reasoning-centric", action="store_true")
    p.add_argument("--flags", default="",
                   help=f"comma-separated; known: {', '.join(sorted(policy.known_flags))}")
    p.add_argument("--prior-failures", type=int, default=0)
    p.add_argument("--prior-models", default="",
                   help="comma-separated concrete model ids that already failed — one "
                        "per --prior-failures; a role alias does not identify what ran")
    p.add_argument("--runtime", default="claude_code", choices=sorted(policy.runtimes))
    p.add_argument("--unavailable", default="", help="comma-separated unavailable roles")
    p.add_argument("--unavailable-models", default="", help="comma-separated unavailable model ids")
    p.add_argument("--isolation", choices=["available", "unavailable"],
                   help="whether reviewer context isolation can be achieved this session")
    p.add_argument("--isolation-evidence", default="",
                   help="comma-separated distinct session ids, one per dispatched reviewer")
    p.add_argument("--worker-seat", dest="worker_seat", default=None,
                   choices=list(WORKER_SEAT_KINDS),
                   help="override this route's class default for whether the "
                        "worker needs a write-capable dispatch recipe")
    p.add_argument("--checks-unavailable", action="store_true",
                   help="this repository cannot run the deterministic checks a LOW "
                        "review consists of; LOW review then seats a model reviewer")
    p.add_argument("--family-quota", default="",
                   help="comma-separated family=ok|low|exhausted — the caller's reading of "
                        "each provider's remaining quota (exhausted withholds the family; "
                        "low moves only the worker to a same-tier model of another family)")
    p.add_argument("--host-model", default=None)
    p.add_argument("--host-effort", default=None)
    p.add_argument("--policy-pin", dest="policy_pin", default=None,
                   help="policy_sha256 of an earlier route to reproduce from the "
                        "committed local model state (64 lowercase hex)")
    p.add_argument("--format", default="text", choices=["text", "json"])
    return p


def _parse_family_quota(value: str) -> dict | None:
    pairs = _split(value)
    if not pairs:
        return None
    quota = {}
    for pair in pairs:
        family, sep, level = pair.partition("=")
        if not sep or not family.strip() or family.strip() in quota:
            raise ValidationError(f"--family-quota: expected family=level pairs, got {pair!r}")
        quota[family.strip()] = level.strip()
    return quota


REQUIRED_JSON_FIELDS = ("task_class", "complexity", "uncertainty", "blast_radius", "reversibility")
PUBLIC_FIELDS = {f.name for f in fields(Task) if not f.name.startswith("_")}


def task_from_request_v1(payload: dict) -> Task:
    if not isinstance(payload, dict):
        raise ValidationError("--request-json must be a JSON object")
    try:
        ensure_json_value(payload)
    except ValueError as exc:
        raise ValidationError(f"--request-json: {exc}") from None
    if (unknown := set(payload) - REQUEST_V1_KEYS):
        raise ValidationError(
            f"--request-json has unknown field(s): {', '.join(sorted(unknown))}")
    version = payload.get("route_schema_version")
    if type(version) is not int or version != ROUTE_SCHEMA_VERSION:
        raise ValidationError(
            f"unsupported route_schema_version {version!r} (want {ROUTE_SCHEMA_VERSION})")
    if (missing := [f for f in REQUIRED_JSON_FIELDS if f not in payload]):
        raise ValidationError(
            f"--request-json is missing required field(s): {', '.join(missing)}")

    prior = payload.get("prior_failures", [])
    if prior is None:
        prior = []
    if isinstance(prior, int):
        raise ValidationError("prior_failures must be a list of model ids")
    if not isinstance(prior, list) or not all(isinstance(x, str) for x in prior):
        raise ValidationError("prior_failures must be a list of model ids")

    snap = payload.get("availability_snapshot")
    if snap is None:
        snap = {}
    if not isinstance(snap, dict):
        raise ValidationError("availability_snapshot must be an object")
    if snap:
        if (unknown := set(snap) - AVAIL_KEYS):
            raise ValidationError(
                f"availability_snapshot has unknown field(s): {', '.join(sorted(unknown))}")
        if snap.get("isolation_evidence") and "isolation" not in snap:
            raise ValidationError("isolation_evidence requires isolation")

    # Values are validated in `Task.validate`, which has the Policy this
    # vocabulary is defined by; duplicating the checks here would be a second
    # source of truth for the same contract.
    lp = payload.get("local_policy")
    if lp is not None and not isinstance(lp, dict):
        raise ValidationError("local_policy must be an object")

    hs = payload.get("host_seat")
    if hs is not None:
        if not isinstance(hs, dict):
            raise ValidationError("host_seat must be an object or null")
        if (unknown := set(hs) - HOST_SEAT_KEYS):
            raise ValidationError(
                f"host_seat has unknown field(s): {', '.join(sorted(unknown))}")

    isolation = snap.get("isolation") if snap else None
    if isolation is not None and isolation not in ("available", "unavailable"):
        raise ValidationError("availability_snapshot.isolation must be available, unavailable, or null")
    def string_list(value, label):
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValidationError(f"{label} must be a list of strings or null")
        return list(value)

    flags = payload.get("flags")
    if isinstance(flags, str):
        flags = _split(flags)
    flags = string_list(flags, "flags")
    pin = payload.get("policy_pin")
    if pin is not None:
        _check_pin(pin)
    implementer = payload.get("implementer")
    if implementer is not None and not isinstance(implementer, dict):
        raise ValidationError("implementer must be an object or null")
    checks = snap.get("checks_available") if snap else None
    if checks is not None and not isinstance(checks, bool):
        raise ValidationError("availability_snapshot.checks_available must be true, false or null")
    quota = snap.get("family_quota") if snap else None
    if quota is not None and not isinstance(quota, dict):
        raise ValidationError("availability_snapshot.family_quota must be an object or null")
    return Task(
        task_class=payload["task_class"],
        complexity=payload["complexity"],
        uncertainty=payload["uncertainty"],
        blast_radius=payload["blast_radius"],
        reversibility=payload["reversibility"],
        reasoning_centric=payload.get("reasoning_centric", False),
        flags=flags,
        prior_failures=len(prior),
        prior_models=list(prior),
        runtime=payload.get("runtime", "claude_code"),
        worker_seat=payload.get("worker_seat"),
        unavailable_roles=string_list(snap.get("unavailable_roles"), "unavailable_roles"),
        unavailable_models=string_list(snap.get("unavailable_models"), "unavailable_models"),
        isolation_available=None if isolation is None else isolation == "available",
        isolation_evidence=string_list(snap.get("isolation_evidence"), "isolation_evidence"),
        _local_policy=lp,
        _host_seat=hs,
        _review_context=payload.get("review_context"),
        _attempt_outcomes=payload.get("attempt_outcomes"),
        _policy_pin=pin,
        _implementer=implementer,
        _checks_available=checks,
        _family_quota=quota,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        policy = _default_policy()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — a crash must not borrow 1 or 2
        traceback.print_exc()
        print(f"internal error before routing: {exc}", file=sys.stderr)
        return 5

    p = build_parser(policy)
    args = p.parse_args(argv)

    try:
        if args.request_json is not None:
            try:
                payload = strict_json_loads(Path(args.request_json).read_bytes())
            except OSError as exc:
                raise ValidationError(f"--request-json cannot be read: {exc}") from None
            except ValueError as exc:
                raise ValidationError(f"--request-json is not valid JSON: {exc}") from None
            task = task_from_request_v1(payload)
        elif args.json is not None:
            try:
                payload = strict_json_loads(args.json)
            except ValueError as exc:
                raise ValidationError(f"--json is not valid JSON: {exc}") from None
            if not isinstance(payload, dict):
                raise ValidationError("--json must be a JSON object")
            if (unknown := set(payload) - PUBLIC_FIELDS):
                raise ValidationError(f"--json has unknown field(s): {', '.join(sorted(unknown))}")
            if (missing := [f for f in REQUIRED_JSON_FIELDS if f not in payload]):
                raise ValidationError(f"--json is missing required field(s): {', '.join(missing)}")
            try:
                task = Task(**payload)
            except TypeError as exc:
                raise ValidationError(f"--json field types are invalid: {exc}") from None
        else:
            if (missing := [n for n in REQUIRED_JSON_FIELDS if getattr(args, n) is None]):
                p.error("missing required arguments: "
                        + ", ".join("--" + m.replace("_", "-") for m in missing))
            task = Task(
                task_class=args.task_class, complexity=args.complexity,
                uncertainty=args.uncertainty, blast_radius=args.blast_radius,
                reversibility=args.reversibility, reasoning_centric=args.reasoning_centric,
                flags=_split(args.flags), prior_failures=args.prior_failures,
                prior_models=_split(args.prior_models), runtime=args.runtime,
                unavailable_roles=_split(args.unavailable),
                unavailable_models=_split(args.unavailable_models),
                isolation_available=None if args.isolation is None else args.isolation == "available",
                isolation_evidence=_split(args.isolation_evidence),
                worker_seat=args.worker_seat,
                _checks_available=False if args.checks_unavailable else None,
                _family_quota=_parse_family_quota(args.family_quota),
            )
            if args.host_effort and not args.host_model:
                raise ValidationError("--host-effort requires --host-model")
            if args.host_model:
                task._host_seat = {
                    "model": args.host_model,
                    "effort": args.host_effort,
                }
        if args.policy_pin is not None:
            _check_pin(args.policy_pin)
            task._policy_pin = args.policy_pin
        result = route(task)

        if args.format == "json":
            print(json.dumps(result, indent=2))
        else:
            _print_text(result)
        # Exit status is the only part of this contract a shell can act on. A
        # route that needs a human but exits 0 is a gate that any caller treating
        # success as authorisation walks straight through — which is the shape
        # this module rejects everywhere else ("disclosure is not a control"). So
        # every human-gated outcome is nonzero, terminal or not. 1 is terminal;
        # 3 is executable-after-approval, distinct so a caller can tell them apart.
        if result["terminal"]:
            return 1
        if result["requires_human_confirmation"]:
            return policy.human_gate_exit_status
        # 4: run it, then get the confirmation. A shell that treats 0 as "nothing
        # further is required" would drop the obligation, and an obligation nobody
        # can read from the exit status is the disclosure-instead-of-control shape
        # this module rejects everywhere else.
        return 4 if result["human_confirmation_deferred"] else 0
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — a crash must not borrow 1 or 2
        traceback.print_exc()
        print(f"internal error: {exc}", file=sys.stderr)
        return 5


def _print_text(r: dict) -> None:
    if r["terminal"] == "MODEL_STATE_UNAVAILABLE":
        print(f"TERMINAL:    {r['terminal']}  — no executable bindings emitted")
        print(f"reason:      {r['model_overlay']['state_reason']}")
        for note in r["notes"]:
            print(f"  note: {note}")
        return
    print(f"risk_score:  {r['risk_score']}")
    print(f"risk_band:   {r['risk_band']}")
    print(f"exec_score:  {r['execution_score']}")
    print(f"exec_band:   {r['execution_band']}")
    print(f"overrides:   {r['band_overrides_applied'] or '(none)'}")
    if r["band_overrides_redundant"]:
        print(f"  already satisfied by another rule: {r['band_overrides_redundant']}")
    if r["terminal"]:
        print(f"TERMINAL:    {r['terminal']}  — no executable bindings emitted")
    elif r["worker_seat_state"] == "already_executed":
        print(f"worker:      {r['selected_role']}  ->  {r['selected_model']}  "
              f"(already executed — caller-declared implementer)")
    else:
        print(f"worker:      {r['selected_role']}  ->  {r['selected_model']}")
        effective = r["selected_effort_effective"]
        capped = f" -> {effective}" if effective != r["selected_effort"] else ""
        print(f"effort:      {r['selected_effort']}{capped}  "
              f"(native: {r['selected_effort_native']})")
    rv = r["review"]
    label = "review (policy only — not dispatchable)" if r["terminal"] else "review"
    print(f"{label}:")
    print(f"  band:            {rv['band']}")
    print(f"  reviewers:       {', '.join(rv['reviewers']) or '(none — deterministic checks)'}")
    if not r["terminal"] and rv["reviewers"]:
        print(f"  models:          {', '.join(m for m in rv['reviewer_models'] if m)}")
        print(f"  effort:          {rv['effort']}")
    print(f"  required:        independent={rv['independence_required']}")
    print(f"  actual:          {rv['review_independence']}")
    if rv["required_checks"]:
        print(f"  checks:          {', '.join(rv['required_checks'])}")
    if rv["judge"]:
        print(f"  judge:           {rv['judge']}" + (f" -> {rv['judge_model']}" if rv["judge_model"] else ""))
    elif rv["judge_unavailable"]:
        print("  judge:           UNAVAILABLE — a human settles any disagreement")
    for short in rv["review_depth_reduced"]:
        print(f"  depth reduced:   {short['reviewer']}"
              + (f" -> {short['model']}" if short["model"] else "")
              + f" (tier {short['capability_tier']} < {short['band_requires']} required by band)")
    print(f"cross_family_review: {r['cross_family_review']}")
    print(f"fallbacks:   {r['fallbacks_applied'] or '(none)'}")
    if r["fallback_compensations_applied"]:
        print(f"compensations: {r['fallback_compensations_applied']}")
    if r["excluded_prior_failures"]:
        print(f"excluded:    {r['excluded_prior_failures']} (already failed)")
    print(f"confidence:  {r['routing_confidence']}")
    adv = r["host_seat_advisory"]
    if adv["advisory"] == "upgrade_recommended":
        detail = next((n for n in r["notes"]
                       if n.startswith("host seat below")), "")
        print(f"host-seat advisory: upgrade recommended — {detail}")

    if r["requires_human_confirmation"]:
        print("human:       CONFIRMATION REQUIRED")
    elif r["human_confirmation_deferred"]:
        print("human:       CONFIRMATION OWED AFTER THE FIX SHIPS (production hotfix)")
    if r["notes"]:
        print("notes:")
        for n in r["notes"]:
            print(f"  - {n}")
    print()
    print(r["rationale"])


if __name__ == "__main__":
    raise SystemExit(main())
