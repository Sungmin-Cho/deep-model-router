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
FULL = os.environ.get("DMR_FULL_BASELINE") == "1"

# The closed rule vocabulary, in replay order (design DD-B11 ②). A rule not
# named here cannot explain anything.
ORDER = ("implementer_declared", "c1iii", "c4", "c2", "c3", "c5", "review_lead", "c7")

# Keys that are not decisions: provenance, prose, and the explicit-cfg overlay.
NOT_DECISIONS = frozenset({"notes", "rationale", "policy_sha256", "request_sha256",
                           "decision_fingerprint", "router_plugin_version", "model_overlay"})


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


# Filled by the rule tasks (B1-B7), in ORDER.
RULES: dict[str, Rule] = {}

# Output keys Part B adds, with the value a route carries when no rule moved it.
# Stripped before the snapshot comparison, after asserting the default.
NEW_KEY_DEFAULTS: dict[str, object] = {}
NEW_REVIEW_KEY_DEFAULTS: dict[str, object] = {}


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
CONFIG_SWITCHES: dict[str, Callable[[dict], None]] = {}

# rule -> function(ExitStack) that installs the pre-rule behaviour.
PATCH_SWITCHES: dict[str, Callable[[ExitStack], None]] = {}


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


def weaker(prev: dict, cur: dict) -> str | None:
    """Design DD-B11's "weaker": every `_contract_violation` row, plus the
    worker tier. Not narrowed — a rule that weakens a route has to be counted
    and listed in the CHANGELOG."""
    if "error" in prev or "error" in cur:
        return None
    if prev["terminal"] and not cur["terminal"]:
        return None
    if cur["terminal"] and cur["terminal"] != prev["terminal"]:
        return "terminal"
    if prev["terminal"]:
        return None
    row = rt._contract_violation(POLICY, cur, prev)
    if row is not None:
        return row
    if TIER_OF[cur["selected_model"]] < TIER_OF[prev["selected_model"]]:
        return "worker_tier"
    return None


# --------------------------------------------------------------------------
# Grid
# --------------------------------------------------------------------------

RUNTIMES = sorted(CFG["runtimes"])
CLASSES = list(CFG["worker_selection"])
DIMS = list(itertools.product(range(4), repeat=4))
WRITE_CLASSES = [c for c in CLASSES if CFG["task_write_seat"][c] == "write"]
# A fixed stratified sample for the default run: the band corners and the
# uncertainty/blast points the rules key on, plus a seeded spread.
SAMPLE_DIMS = sorted(set([(0, 0, 0, 0), (1, 1, 0, 0), (0, 3, 0, 0), (1, 1, 1, 0), (2, 1, 1, 1),
                          (3, 2, 0, 0), (1, 3, 1, 0), (2, 2, 1, 1), (3, 3, 0, 1), (2, 2, 2, 2),
                          (3, 3, 3, 3)]
                         + random.Random(20260928).sample(DIMS, 13)))
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
    extra_dims = EXTRA_DIMS if full else SAMPLE_DIMS
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

    def fail(self, name, text):
        if len(self.problems) < 40:
            self.problems.append(f"{name}: {text}")
        else:
            self.problems.append("...")


def replay(full: bool) -> Ledger:
    ledger = Ledger(weakened={r: Counter() for r in ORDER})
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
    assert led.inputs > (80_000 if FULL else 4_000), led.inputs


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
    print("| rule | admitted | changed | weakened (by _contract_violation row / worker tier) |")
    print("|---|---|---|---|")
    for name in ORDER:
        if name in RULES:
            weak = ", ".join(f"{k} {v}" for k, v in sorted(led.weakened[name].items())) or "0"
            print(f"| `{name}` | {led.admitted[name]} | {led.changed[name]} | {weak} |")


if __name__ == "__main__":
    _print("--full" in sys.argv)
