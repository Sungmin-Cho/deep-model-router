"""Regression tests for defects found in round 1 of adversarial review.

Every test here failed before the corresponding fix. They exist because the
original suite passed 36/36 while five of ten fallback paths silently emitted
a model that had just been declared unavailable — green tests that never
entered the defective branch.

The lesson encoded here: an invariant test must exercise the condition the
invariant is about. `test_i4` asserted "routes name only available models"
without ever marking a model unavailable, so it could not have failed.

Run:  python3 -m pytest skills/model-router/tests/ -q
"""

import copy
import itertools
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    BANDS,
    TERMINAL_STATES,
    ROLES,
    TASK_CLASSES,
    Policy,
    Resolver,
    Task,
    ValidationError,
    load_config,
    route,
)

CFG = load_config()


def _leave_only(*keep):
    """Everything but the one model this probe is about. Enumerating five ids
    meant the oracle went stale the moment the registry grew."""
    return sorted({m["id"] for m in CFG["models"].values()} - set(keep))


def _task(**kw):
    kw.setdefault("complexity", 0)
    kw.setdefault("uncertainty", 0)
    kw.setdefault("blast_radius", 0)
    kw.setdefault("reversibility", 0)
    return Task(**kw)


def r(**kw):
    return route(_task(**kw), CFG)


def cli(*args):
    proc = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "route_task.py"), *args],
        capture_output=True, text=True,
    )
    return proc


# ---------------------------------------------------------------------------
# D1 — fallback must actually fall back  (both reviewers, independently)
# ---------------------------------------------------------------------------

def emitted_models(out):
    """Every concrete model the route hands a consumer."""
    return {m for m in [
        out["selected_model"], out["review"]["judge_model"],
        *out["review"]["reviewer_models"],
    ] if m}


# Routes chosen so that between them every role actually reaches the resolver.
# The earlier version used one fixed route where worker_fast and
# principal_architect were never resolved, so four of ten (role x runtime)
# combinations were asserted about without ever being exercised.
ROLE_FORCING_ROUTES = {
    "worker_fast": dict(task_class="MECHANICAL"),
    "worker_balanced": dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=1),
    "senior_engineer": dict(task_class="IMPLEMENTATION", complexity=3, uncertainty=1,
                            blast_radius=3, reversibility=2),
    "reasoning_specialist": dict(task_class="INVESTIGATION", complexity=2, uncertainty=2,
                                 blast_radius=2, reasoning_centric=True),
    "principal_architect": dict(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
                                blast_radius=3, reversibility=2, flags=["financial_sensitive"]),
}


def test_d1_every_role_is_actually_resolved_by_its_forcing_route():
    """Guards the guard: if a route stops reaching its role, the fallback tests
    below would pass vacuously without anyone noticing."""
    for role, probe in ROLE_FORCING_ROUTES.items():
        out = r(**probe)
        touched = {out["selected_role"], *out["review"]["reviewers"], out["review"]["judge"]}
        assert role in touched, f"{role} is never resolved by its probe route"


def test_d1_no_fallback_returns_the_model_it_just_declared_unavailable():
    """The original defect: the degraded binding for a role often names the same
    model as the default binding, so the 'fallback' was a no-op that still
    recorded a downgrade in fallbacks_applied."""
    for runtime, (role, probe) in itertools.product(sorted(CFG["runtimes"]),
                                                    ROLE_FORCING_ROUTES.items()):
        out = r(**probe, runtime=runtime, unavailable_roles=[role])
        blocked = set(out["unavailable_models"])
        leak = emitted_models(out) & blocked
        assert not leak, (
            f"{runtime}/{role}: emitted an unavailable model ({leak}); "
            f"fallbacks={out['fallbacks_applied']}"
        )


def test_d1_fallback_note_is_only_recorded_when_the_model_actually_changed():
    """A recorded fallback that changed nothing is worse than no record — it
    reads as a managed degradation when none happened."""
    saw_a_real_fallback = False
    for runtime, (role, probe) in itertools.product(sorted(CFG["runtimes"]),
                                                    ROLE_FORCING_ROUTES.items()):
        out = r(**probe, runtime=runtime, unavailable_roles=[role])
        for note in out["fallbacks_applied"]:
            if "->" not in note:
                continue
            saw_a_real_fallback = True
            before, after = (s.strip() for s in note.split("->", 1))
            assert before.split(":")[-1].strip() != after, f"no-op fallback recorded: {note}"
    assert saw_a_real_fallback, "the probe set never triggered a fallback — assertion was vacuous"


# Hardcoded on purpose: this is the contract a runtime's degraded binding
# must satisfy, not a restatement of it. Deriving it from `degraded_binding`
# made the oracle agree with any binding the config named, including a
# binding on the far side of the very bridge that is down.
NATIVE_FAMILY = {"claude_code": "claude", "codex": "openai", "grok": "xai"}


def test_d1_bridge_down_never_crosses_the_provider_boundary():
    """When the bridge is down the other family is unreachable by definition,
    so naming a model from it produces a route that cannot be executed."""
    assert set(NATIVE_FAMILY) == set(CFG["runtimes"]), (
        f"NATIVE_FAMILY covers {sorted(NATIVE_FAMILY)} but runtimes are "
        f"{sorted(CFG['runtimes'])}; a new runtime must be pinned here")
    families = {m["id"]: m["family"] for m in CFG["models"].values()}
    for runtime, expected_family in NATIVE_FAMILY.items():
        binding = CFG["runtimes"][runtime]["degraded_binding"]
        bound = {CFG["models"][key]["family"]
                 for key in CFG["role_bindings"][binding].values()}
        assert bound == {expected_family}, (
            f"{runtime}'s degraded binding {binding!r} resolves to "
            f"{sorted(bound)}, not {expected_family}"
        )
    local = NATIVE_FAMILY
    for runtime, (role, probe) in itertools.product(sorted(CFG["runtimes"]),
                                                    ROLE_FORCING_ROUTES.items()):
        for unavailable in ([], [role]):
            kw = dict(probe)
            kw["flags"] = list(kw.get("flags", [])) + ["bridge_down"]
            out = r(**kw, runtime=runtime, unavailable_roles=unavailable)
            foreign = {m for m in emitted_models(out) if families[m] != local[runtime]}
            assert not foreign, (
                f"{runtime}/bridge_down/{role} unavailable={unavailable}: "
                f"emitted unreachable-family model(s) {sorted(foreign)}"
            )


def test_d1_a_failed_model_is_never_re_emitted_under_a_new_role():
    """Role-tier escalation alone is not enough: in a degraded binding the top
    roles collapse onto one model, so 'escalating' re-ran what just failed
    while recording a promotion that never happened."""
    # Round 13: this loop used role aliases, and once an alias history became
    # terminal every one of its six iterations hit `if out["terminal"]: continue`
    # — a regression test for a shipped Critical executing zero assertions,
    # green. Concrete ids now, and the escape hatch has to prove it is honest.
    checked = 0
    # One concrete prior from every configured family. Two Claude ids stay
    # because the original probe used them; grok-4.6 is the family that was
    # missing. A family added to the registry without a prior here is a
    # retry path nobody exercises.
    priors = ("claude-sonnet-5", "claude-opus-5", "gpt-5.6-sol", "grok-4.6")
    fam = {m["id"]: m["family"] for m in CFG["models"].values()}
    dispatchable_fams = {
        m["family"] for m in CFG["models"].values()
        if m.get("dispatchable", True)
    }
    assert {fam[p] for p in priors} == dispatchable_fams, (
        f"priors {priors} miss a dispatchable family "
        f"{sorted(dispatchable_fams - {fam[p] for p in priors})}")
    for runtime in sorted(CFG["runtimes"]):
        for prior in priors:
            out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, blast_radius=1,
                    runtime=runtime, flags=["bridge_down"],
                    prior_failures=1, prior_models=[prior])
            if out["terminal"]:
                assert out["terminal"] != "RETRY_HISTORY_REQUIRED", (
                    f"{runtime}/{prior}: a complete concrete history was rejected")
                continue          # failing closed is an acceptable outcome
            checked += 1
            failed = set(out["excluded_prior_failures"])
            assert failed, f"{runtime}/{prior}: the named failure was not excluded"
            leak = emitted_models(out) & failed
            assert not leak, f"{runtime}/{prior}: re-emitted the failed model {leak}"
    assert checked, "every iteration was terminal — the assertions never ran"


def test_d1_unavailable_accepts_model_ids_too():
    ids = {m["id"] for m in CFG["models"].values()}
    for model_id in ids:
        out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2,
                blast_radius=2, unavailable_models=[model_id])
        assert model_id not in emitted_models(out)


def test_d1_multiple_simultaneous_unavailabilities():
    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=3,
            reversibility=2, flags=["financial_sensitive"],
            unavailable_roles=["principal_architect", "reasoning_specialist"])
    assert not (emitted_models(out) & set(out["unavailable_models"]))
    assert out["fallbacks_applied"]


# ---------------------------------------------------------------------------
# D2 — terminal control-loop gates must actually stop
# ---------------------------------------------------------------------------

def test_d2_retry_exhaustion_is_terminal_not_a_route():
    cap = CFG["retry"]["max_total_implementation_attempts"]
    # Round 12: a retry needs one concrete model id per failure, so the history
    # is spelled out. `worker_fast` used to stand in for it, and five rounds of
    # inferring which model that alias held produced five different defects.
    out = r(task_class="DEBUGGING", complexity=2, uncertainty=1, blast_radius=1,
            prior_failures=cap, prior_models=["gpt-5.6-luna"] * cap)
    assert out["terminal"] == "HUMAN_REQUIRED"
    assert out["selected_model"] is None, "an exhausted task must not get an executable route"


def test_d2_low_routing_confidence_is_terminal_not_a_route():
    # Round 11 moved this probe. The original (`DEBUGGING 3/3/1`, two failures,
    # one named alias) now hits the retry ceiling FIRST — the reconstruction
    # correctly places attempt 2 at the top tier, so `HUMAN_REQUIRED` fires
    # before confidence is consulted. That is the right answer for that input
    # and not what this test is about, so the probe was moved to one where
    # confidence is the binding constraint. The subject is unchanged: a route
    # below the escalation floor is terminal, not an executable route.
    out = r(task_class="MECHANICAL", complexity=0, uncertainty=2, blast_radius=0,
            flags=["unknown_root_cause", "bridge_down"], prior_failures=2,
            prior_models=["gpt-5.6-luna", "claude-sonnet-5"])
    assert out["routing_confidence"] < CFG["router"]["confidence"]["escalate_below"]
    assert out["terminal"] == "ESCALATE_ROUTING"
    assert out["selected_model"] is None


def test_d2_cli_exits_nonzero_on_a_terminal_state():
    cap = str(CFG["retry"]["max_total_implementation_attempts"])
    p = cli("--class", "DEBUGGING", "--complexity", "2", "--uncertainty", "1",
            "--blast-radius", "1", "--reversibility", "0",
            "--prior-failures", cap,
            "--prior-models", ",".join(["gpt-5.6-luna"] * int(cap)))
    assert p.returncode != 0, "a terminal state must not exit 0"
    assert "HUMAN_REQUIRED" in p.stdout + p.stderr

    # And the round-12 terminal: an incomplete history is well-formed input
    # with insufficient evidence, so it is a routing outcome (exit 1) rather
    # than a usage error, and the note says what to supply.
    q = cli("--class", "DEBUGGING", "--complexity", "2", "--uncertainty", "1",
            "--blast-radius", "1", "--reversibility", "0", "--prior-failures", "1")
    assert q.returncode == 1
    assert "RETRY_HISTORY_REQUIRED" in q.stdout
    assert "one model id per failure" in q.stdout


# ---------------------------------------------------------------------------
# D3 — judge follows the review band, not the risk band
# ---------------------------------------------------------------------------

def test_d3_confidence_promoted_critical_review_gets_a_judge():
    out = r(task_class="DEBUGGING", complexity=2, uncertainty=2, blast_radius=1,
            flags=["auth_sensitive", "unknown_root_cause"])
    assert out["review"]["band"] == "CRITICAL"
    assert out["risk_band"] != "CRITICAL", "this is the promoted case, not a natural CRITICAL"
    assert out["review"]["judge"] is not None, "a CRITICAL review without a judge is half a control"
    assert out["review"]["judge_model"] is not None, "the judge must also be resolved for availability"


def test_d3_every_critical_review_either_seats_a_judge_or_says_it_cannot():
    """A judge must be able to adjudicate the reviewers it sits above. When the
    implementer is already the architect there is no higher authority in the
    ladder, so the honest outcome is to say so and hand adjudication to a
    human — not to seat one of the disputing parties as its own judge."""
    known = {m["id"] for m in CFG["models"].values()}
    for task_class in TASK_CLASSES:
        out = r(task_class=task_class, complexity=3, uncertainty=3,
                blast_radius=3, reversibility=3)
        rv = out["review"]
        if rv["band"] != "CRITICAL" or out["terminal"]:
            continue
        if rv["judge_unavailable"]:
            assert rv["judge"] is None
            assert out["requires_human_confirmation"] is True
            assert "human must resolve" in out["rationale"]
        else:
            assert rv["judge_model"] in known


def test_d11_no_seat_is_held_twice():
    """Worker, every reviewer, and the judge are distinct models. The judge was
    allocated after de-confliction ran and never checked against it, so an
    adjudicator could be the same model as a reviewer whose disagreement it was
    brought in to settle."""
    checked = 0
    for out in _sweep():
        rv = out["review"]
        if out["terminal"] or not rv["independence_required"]:
            continue
        seats = [out["selected_model"], *rv["reviewer_models"], rv["judge_model"]]
        filled = [x for x in seats if x]
        checked += 1
        assert len(filled) == len(set(filled)), (
            f"{out['task_class']}/{rv['band']}: a model holds more than one seat: {seats}"
        )
    assert checked, "the sweep produced no dispatchable independent route"


def test_d11_judge_is_never_weaker_than_any_party_it_adjudicates():
    for out in _sweep():
        rv = out["review"]
        if out["terminal"] or not rv["judge"]:
            continue
        # Round 7. This was the pre-round-6 spelling, left asserting the very
        # property round 6 disproved: role ordinals are not capability. It gave
        # false assurance (a reader sees judge strength covered twice) and it
        # opposed the corrected invariant — the moment scarcity produced an
        # ordinal/tier inversion, a CORRECT seating would have failed here.
        tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
        parties = [m for m in [out["selected_model"], *rv["reviewer_models"]] if m]
        assert tier[rv["judge_model"]] >= max(tier[m] for m in parties), (
            f"judge {rv['judge_model']} is outranked by a party it adjudicates: {parties}"
        )


# ---------------------------------------------------------------------------
# D4 — independence: policy requirement vs observed enforcement
# ---------------------------------------------------------------------------

def test_d4_route_emits_the_review_independence_enum():
    out = r(task_class="IMPLEMENTATION", complexity=1)
    assert out["review"]["review_independence"] in {"enforced", "degraded", "not_applicable"}


def test_d4_unknown_isolation_is_never_reported_as_enforced():
    """The most damaging thing this skill can produce is a claim of independence
    it did not structurally get. Absent evidence, it must not claim it."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=None)
    assert out["review"]["independence_required"] is True
    assert out["review"]["review_independence"] == "degraded"


def test_d4_capability_attestation_alone_is_only_planned_not_enforced():
    """A route is computed before any reviewer runs, so the caller saying
    isolation is *possible* cannot establish that it *happened*. Treating the
    attestation as proof let the flag alone clear the CRITICAL human gate."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=True)
    assert out["review"]["review_independence"] == "planned"


def test_d4_only_post_dispatch_evidence_yields_enforced():
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=True,
            isolation_evidence=["session-a1b2", "session-c3d4"])
    assert out["review"]["review_independence"] == "enforced"


def test_d4_evidence_must_be_distinct_per_reviewer():
    """Two reviewers in the same session is precisely the leak the requirement
    exists to prevent, so a repeated identifier must not count."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=True, isolation_evidence=["same", "same"])
    assert out["review"]["review_independence"] != "enforced"


def test_d4_confirmed_absence_is_distinguished_from_no_evidence():
    """`unavailable` is positive evidence that isolation cannot be achieved;
    `degraded` is the absence of evidence either way. Collapsing them makes a
    confirmed gap indistinguishable from an unchecked one."""
    unknown = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2)
    confirmed_absent = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2,
                         blast_radius=2, isolation_available=False)
    assert unknown["review"]["review_independence"] == "degraded"
    assert confirmed_absent["review"]["review_independence"] == "unavailable"


@pytest.mark.parametrize("isolation,evidence", [
    (None, []), (False, []), (True, []), (True, ["only-one"]),
])
def test_d4_critical_without_enforced_independence_requires_a_human(isolation, evidence):
    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=3,
            reversibility=2, flags=["financial_sensitive"],
            isolation_available=isolation, isolation_evidence=evidence)
    assert out["review"]["band"] == "CRITICAL"
    assert out["review"]["review_independence"] != "enforced"
    assert out["requires_human_confirmation"] is True


def test_d4_low_band_review_is_not_applicable_rather_than_degraded():
    out = r(task_class="MECHANICAL")
    assert out["review"]["independence_required"] is False
    assert out["review"]["review_independence"] == "not_applicable"


# ---------------------------------------------------------------------------
# D5 — prior-failure escalation must not silently no-op
# ---------------------------------------------------------------------------

def test_d5_prior_models_accepts_concrete_model_ids():
    """selected_model is emitted as a model id, so feeding it back must work."""
    out = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=1, blast_radius=1,
            prior_failures=1, prior_models=["gpt-5.6-luna"])
    assert ROLES.index(out["selected_role"]) >= ROLES.index("worker_balanced")
    assert any("escalated above" in n for n in out["notes"])


def test_d5_unknown_prior_model_is_rejected_not_ignored():
    with pytest.raises(ValidationError):
        r(task_class="IMPLEMENTATION", complexity=1, prior_failures=1,
          prior_models=["not-a-real-model"])


# ---------------------------------------------------------------------------
# D6 — config is the single source of truth
# ---------------------------------------------------------------------------

def test_d6_taxonomy_is_derived_from_config_not_duplicated():
    assert BANDS == sorted(CFG["router"]["bands"], key=lambda b: CFG["router"]["bands"][b]["ordinal"])
    assert ROLES == CFG["role_tiers"]
    assert set(TASK_CLASSES) == set(CFG["worker_selection"])


def test_d6_every_configured_override_has_a_consumer():
    """A rule declared in config and consumed nowhere is a promise the system
    does not keep."""
    exercised = set()
    for entry in CFG["overrides"]:
        name = entry["name"]
        probe = _probe_for_override(entry)
        if probe is None:
            continue
        out = r(**probe)
        if name in out["band_overrides_applied"] or out.get("route_path") == entry.get("effect", {}).get("route"):
            exercised.add(name)
    declared = {e["name"] for e in CFG["overrides"]}
    assert declared - exercised == set(), f"overrides with no observable effect: {declared - exercised}"


def _probe_for_override(entry):
    """Build a minimal task that should trigger exactly this override."""
    flags = _flags_in(entry["when"])
    if not flags:
        return None
    task = dict(task_class="MECHANICAL", complexity=0, uncertainty=0,
                blast_radius=0, reversibility=0, flags=sorted(flags))
    if "reversibility" in json.dumps(entry["when"]):
        task["reversibility"] = 3
    return task


def _flags_in(node):
    out = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "flag":
                out.add(value)
            elif key == "any_flag_in":
                out |= set(CFG["flags"][value])
            else:
                out |= _flags_in(value)
    elif isinstance(node, list):
        for item in node:
            out |= _flags_in(item)
    return out


def test_d6_review_disagreement_routes_to_the_disagreement_path():
    out = r(task_class="MECHANICAL", flags=["review_disagreement"])
    assert out["route_path"] == "disagreement"
    assert out["review"]["judge"] is not None


def test_d6_every_declared_flag_has_an_effect_or_a_documented_role():
    """Elevating flags must elevate something.

    The probe starts from the lowest possible task so a band floor is
    observable — measuring against an already-MEDIUM baseline would hide a
    MEDIUM floor and report a working rule as inert. A flag whose effect is
    genuinely conditional must say so in `conditional_flags`, which is what
    separates a deliberate combination rule from one nobody wired up.
    """
    conditional = CFG.get("conditional_flags", {})
    inert = []
    base = r(task_class="MECHANICAL")
    for flag in CFG["flags"]["elevating"]:
        probe = [flag] + list(conditional.get(flag, {}).get("requires", []))
        with_flag = r(task_class="MECHANICAL", flags=probe)
        changed = (
            flag in with_flag["band_overrides_applied"]
            or with_flag["risk_band"] != base["risk_band"]
            or with_flag["selected_role"] != base["selected_role"]
            or with_flag["selected_effort"] != base["selected_effort"]
            or with_flag["review"]["band"] != base["review"]["band"]
            or with_flag["routing_confidence"] != base["routing_confidence"]
            or with_flag.get("route_path") != base.get("route_path")
        )
        if not changed:
            inert.append(flag)
    assert inert == [], f"elevating flags with no observable effect: {inert}"


def test_d6_conditional_flags_actually_fire_in_combination():
    for flag, spec in CFG.get("conditional_flags", {}).items():
        alone = r(task_class="MECHANICAL", flags=[flag])
        combined = r(task_class="MECHANICAL", flags=[flag, *spec["requires"]])
        assert combined["risk_band"] != alone["risk_band"] or combined["band_overrides_applied"], (
            f"{flag} declares a conditional effect that does not fire with {spec['requires']}"
        )


# ---------------------------------------------------------------------------
# D7 — input validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "reasoning_centric": "false"},
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "runtime": "not-a-runtime"},
    {"task_class": "IMPLEMENTATION", "complexity": True, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0},
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "flags": ["not_a_known_flag"]},
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "unavailable_roles": ["not_a_role"]},
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "flags": "auth_sensitive"},
])
def test_d7_invalid_input_is_rejected(payload):
    with pytest.raises(ValidationError):
        route(Task(**payload), CFG)


def test_d7_cli_rejects_bad_json_cleanly_without_a_traceback():
    p = cli("--json", '{"task_class":"IMPLEMENTATION","complexity":1,"uncertainty":1,'
                      '"blast_radius":1,"reversibility":0,"runtime":"not-a-runtime"}')
    assert p.returncode != 0
    assert "Traceback" not in p.stderr, "a bad input should not surface a raw traceback"
    assert "runtime" in (p.stdout + p.stderr)


def test_d7_bridge_down_is_a_declared_flag_not_a_magic_string():
    known = set().union(*(set(v) for v in CFG["flags"].values()))
    assert "bridge_down" in known, "the only correct way to select a degraded binding must be discoverable"


# ---------------------------------------------------------------------------
# D8 — documentation must not drift from the registry
# ---------------------------------------------------------------------------

def test_d8_model_ids_appear_only_in_the_registry():
    """The contract says identifiers live in config and nowhere else. Copies in
    prose are guaranteed to drift after the next registry change."""
    ids = {m["id"] for m in CFG["models"].values()}
    offenders = {}
    for path in sorted((SKILL / "references").glob("*.md")):
        text = path.read_text()
        # Host-seat examples intentionally retain one concrete --host-model
        # id; registry-key substitution applies only to seat bindings.
        hits = sorted(i for i in ids if i in text and not (
            path.name == "examples.md" and i == "claude-haiku-4-5-20251001"))
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"model ids duplicated outside the registry: {offenders}"


def test_d8_skill_md_does_not_hardcode_model_ids():
    ids = {m["id"] for m in CFG["models"].values()}
    text = (SKILL / "SKILL.md").read_text()
    assert not [i for i in ids if i in text]


# ---------------------------------------------------------------------------
# D9 — no tautological assertions
# ---------------------------------------------------------------------------

def test_d9_no_test_file_contains_a_tautological_assert():
    """`assert X or True` can never fail. It reads as coverage and is not."""
    for path in sorted((SKILL / "tests").glob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("assert ") and stripped.endswith(" or True"):
                pytest.fail(f"{path.name}:{lineno} tautological assertion: {stripped}")


def test_d9_parity_holds_including_the_native_effort_spelling():
    """The original parity test ended in `or True`, so the native-effort claim
    in its own docstring was never checked."""
    kw = dict(task_class="DEBUGGING", complexity=2, uncertainty=3,
              blast_radius=2, reversibility=1, flags=["auth_sensitive"])
    outs = [r(runtime=rt, **kw) for rt in sorted(CFG["runtimes"])]
    first = outs[0]
    for other in outs[1:]:
        assert other["selected_role"] == first["selected_role"]
        assert other["selected_effort"] == first["selected_effort"]
        assert other["review"]["band"] == first["review"]["band"]
    # Keyed by the family of the model that runs, not by the host runtime.
    # Native is map[family][effective], not map[family][requested].
    fam = {m["id"]: m["family"] for m in CFG["models"].values()}
    for out in outs:
        expected = CFG["effort_map"][fam[out["selected_model"]]][
            out["selected_effort_effective"]]
        assert out["selected_effort_native"] == expected

    # The route above happens to emit equal requested and effective, so the
    # wrong key cannot fail. This one splits them: requested MAX, effective
    # VERY_HIGH (the worker's ceiling).
    #
    # On the grok host, because the only seat with a ceiling below MAX is the
    # xai one and a cross-family WRITE route can no longer reach it — the maker
    # recipe for `to_xai` did not pass its shipping gate (issue #19). The grok
    # host seats it natively, which is what this test needs and which the
    # write-seat coupling deliberately leaves alone.
    split = r(task_class="DEBUGGING", complexity=2, uncertainty=2,
              blast_radius=1, reversibility=0, runtime="grok",
              flags=["auth_sensitive", "unknown_root_cause"])
    assert split["selected_effort"] == "MAX"
    assert split["selected_effort_effective"] == "VERY_HIGH"
    assert split["selected_effort"] != split["selected_effort_effective"]
    expected = CFG["effort_map"][fam[split["selected_model"]]][
        split["selected_effort_effective"]]
    assert split["selected_effort_native"] == expected


# ---------------------------------------------------------------------------
# D10 — an implementer must never review its own MODEL
# ---------------------------------------------------------------------------
#
# The first version of this check compared role labels, and the sweep never set
# runtime or flags — so it only ever ran the default binding, where the role
# check already held. Under a degraded single-provider binding several roles
# resolve to one model, and there the worker, both "independent" reviewers and
# the judge were all the same model while the route reported a substitution and
# cleared the human gate.
#
# Roles are labels. Models are what actually runs. The invariant is about
# models, so the test is too — and it sweeps the bindings where roles collapse.

DIMENSION_SWEEP = [(0, 0, 0, 0), (2, 2, 2, 0), (3, 2, 3, 1), (3, 3, 3, 3), (2, 2, 2, 2)]
BINDING_SWEEP = [
    dict(runtime=rt, flags=list(f))
    for rt in sorted(CFG["runtimes"])
    for f in ([], ["bridge_down"])
]

# Scarcity is what forces roles onto shared models, and the previous sweep
# never created any: it varied runtime and bridge_down but never marked a model
# unavailable, so every collision the code could actually produce lived outside
# the sweep. Withholding models is the cheapest way to reach that state.
_MODEL_IDS = sorted(m["id"] for m in CFG["models"].values())
SCARCITY_SWEEP = [
    [],
    [_MODEL_IDS[0]],
    _MODEL_IDS[:2],
    _MODEL_IDS[:3],
    _MODEL_IDS[1:4],
    _MODEL_IDS[2:5],
    sorted(set(_MODEL_IDS) - {"claude-opus-5", "gpt-5.6-sol", "grok-4.6"}),
    sorted(set(_MODEL_IDS) - {"grok-4.6"}),
]


def _sweep(with_task=False):
    for task_class in TASK_CLASSES:
        for c, u, b, rev in DIMENSION_SWEEP:
            for reasoning in (False, True):
                for binding in BINDING_SWEEP:
                    for scarce in SCARCITY_SWEEP:
                        try:
                            kwargs = dict(task_class=task_class, complexity=c, uncertainty=u,
                                          blast_radius=b, reversibility=rev,
                                          reasoning_centric=reasoning,
                                          unavailable_models=list(scarce), **binding)
                            out = r(**kwargs)
                            yield (_task(**kwargs), out) if with_task else out
                        except ValidationError:
                            # Every candidate withheld — failing closed is a
                            # correct outcome, not a route to inspect.
                            continue


def test_d10_sweep_actually_reaches_the_conditions_it_asserts_about():
    """Guards the guard, and it has to guard three separate things.

    Each round of review found the same failure: an invariant test that never
    entered the branch where the invariant could break. A sweep that produces
    no substitutions, no compromised routes, and no compensations proves
    nothing about any of them.
    """
    substituted = compromised = compensated = 0
    for out in _sweep():
        if out["review"].get("self_review_avoided"):
            substituted += 1
        if out["review"].get("independence_compromised"):
            compromised += 1
        if out["fallback_compensations_applied"]:
            compensated += 1
    assert substituted, "the sweep never needed a reviewer substitution"
    assert compromised, "the sweep never reached an unresolvable collision"
    assert compensated, "the sweep never exercised a fallback compensation"


def test_d10_worker_model_is_never_one_of_its_own_reviewers():
    offenders = []
    for out in _sweep():
        rv = out["review"]
        if not rv["independence_required"] or out["terminal"]:
            continue
        if out["selected_model"] and out["selected_model"] in rv["reviewer_models"]:
            offenders.append((out["task_class"], rv["band"], out["selected_model"]))
    assert offenders == [], f"worker model also reviews its own output in: {sorted(set(offenders))[:5]}"


def test_d10_two_reviewers_are_never_the_same_model():
    offenders = []
    for out in _sweep():
        rv = out["review"]
        if not rv["independence_required"] or out["terminal"]:
            continue
        models = [m for m in rv["reviewer_models"] if m]
        if len(models) != len(set(models)):
            offenders.append((out["task_class"], rv["band"], tuple(models)))
    assert offenders == [], f"duplicate reviewer models in: {sorted(set(offenders))[:5]}"


def test_d10_unresolvable_collision_gates_rather_than_merely_disclosing():
    """Disclosure is not a control. A route whose reviewers could not be given
    distinct models must stop, not ship with a boolean set and hope the
    consumer reads it."""
    seen = 0
    for out in _sweep():
        rv = out["review"]
        if not rv.get("independence_compromised"):
            continue
        seen += 1
        assert rv["review_independence"] == "unavailable"
        assert out["requires_human_confirmation"] is True
        assert "could not be established" in out["rationale"]
    assert seen, "no compromised route in the sweep — the assertion was vacuous"


def test_d10_compensation_path_cannot_bypass_deconfliction():
    """The second-review compensation appends a reviewer and flips
    `independent` to true after the band's own de-confliction has run. Checking
    the invariant mid-pipeline protected only the paths that existed when the
    check was written; this asserts the post-condition at the emit boundary."""
    checked = 0
    for out in _sweep():
        rv = out["review"]
        if not out["fallback_compensations_applied"] or not rv["independence_required"]:
            continue
        if out["terminal"] or rv.get("independence_compromised"):
            continue
        checked += 1
        models = [m for m in rv["reviewer_models"] if m]
        assert out["selected_model"] not in models
        assert len(models) == len(set(models))
    assert checked, "no compensated independent route in the sweep"


def test_d10_every_recorded_substitution_actually_changed_the_model():
    """The defect this whole file exists to prevent, applied to the newest
    code: a recorded avoidance that avoided nothing."""
    seen = 0
    # Both sweeps. `_sweep()` never sets a flag, so it never takes the
    # disagreement route, so it never seats a judge — and the judge retry is
    # exactly where round 6 found a substitution record outliving the seat it
    # described. A regression test that cannot reach the path is the failure
    # mode this whole file is named for.
    for out in itertools.chain(_sweep(), _disagreement_sweep()):
        rv = out["review"]
        for sub in (rv.get("self_review_avoided") or []):
            seen += 1
            assert sub["with"] in rv["reviewers"], (
                f"record names reviewer {sub['with']}, seats hold {rv['reviewers']}")
            assert sub["replaced"] != sub["with"]
            assert sub["with"] in ROLES
            assert sub["reason"]
            # Role labels are not models. Under a degraded binding several
            # roles collapse onto one id, so a substitution between two
            # aliases can rename the seat without changing who sits in it —
            # which is the exact defect this file is named after. The seat the
            # substitute now holds must differ from the implementer's.
            substitute_model = _peek_model(out, sub["with"])
            if substitute_model is not None and out["selected_model"] is not None:
                assert substitute_model != out["selected_model"], (
                    f"{out['task_class']}/{rv['band']}: substituted "
                    f"{sub['replaced']} -> {sub['with']} but both resolve to "
                    f"{substitute_model}"
                )
    assert seen, "no substitution in the sweep — the assertion was vacuous"


def test_d10_a_below_tier_substitute_is_only_taken_when_nothing_better_is_free():
    """Scarcity can leave no reviewer at or above the implementer's tier, and a
    weaker distinct reviewer still beats the implementer reviewing itself. What
    must not happen is taking a weaker one while a stronger one sits unused.

    The previous version of this test asserted `cross_family_review is not
    None` (always true, it is a bool) and that the rationale contains a string
    `explain()` emits unconditionally on that branch — the same shape as the
    `or True` tautology `test_d9` forbids, so the invariant in its own name was
    never checked.

    Round 6 found the replacement still could not fail. It asked "was a
    stronger role free?" through `_peek_model`, which only knows roles that
    appear in the emitted route — so an *unseated* candidate, the only kind
    that could have been free, returned `None` and was skipped. 1,829 of the
    checks were skipped that way and the 1,512 that ran merely rediscovered
    models already in `used`. The question is about roles the route did not
    take, so it has to be asked of the resolver, not of the answer.
    """
    checked = free_seen = 0
    for task, out in _sweep(with_task=True):
        rv = out["review"]
        if out["terminal"] or not out["selected_role"]:
            continue
        # Only substituted seats are in scope. A band that configures a
        # cheaper reviewer than the implementer (LOW does, by design) is not a
        # substitution and asserting over it tests the policy, not the code.
        seated = dict(zip(rv["reviewers"], rv["reviewer_models"]))
        tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
        resolver = Resolver(task, Policy.of(CFG))
        # Ask the question the ROUTER asked. On a write route the worker's
        # role is restricted to seats this host has a write-capable recipe
        # for, so a model that cannot fill that role was never free to be
        # seated — `resolved` is keyed by role, and a role holds one model.
        resolver.worker_writes = out["worker_seat"]["kind"] == "write"
        resolver.write_seat_role = out["selected_role"]
        worker_tier = tier[out["selected_model"]]
        weak = [seated[sub["with"]] for sub in rv["self_review_avoided"]
                if seated.get(sub["with"]) and tier[seated[sub["with"]]] < worker_tier]
        if not weak:
            continue
        checked += 1
        used = {out["selected_model"], *(m for m in rv["reviewer_models"] if m),
                rv["judge_model"]} - {None}
        for role in ROLES:
            if role in rv["reviewers"]:
                continue
            model = resolver.peek(role)          # asks the resolver, not the route
            if model is None or model in used:
                # A candidate whose model is already held could not have been
                # taken anyway. Counting it was counting the rediscoveries this
                # test was rewritten to stop counting.
                continue
            free_seen += 1
            assert model in used or tier[model] < worker_tier, (
                f"{out['task_class']}/{rv['band']}: reviewed at {weak} while "
                f"{role} -> {model} (tier {tier[model]}) sat unused"
            )
    assert checked, "no below-tier substitution in the sweep — the assertion was vacuous"
    assert free_seen, "no unseated candidate was ever resolved — the check was skipped"


# ---------------------------------------------------------------------------
# D13 — round 7: what the round-6 fixes broke, and what verified nothing
# ---------------------------------------------------------------------------

def test_d16_skill_md_stays_small_without_being_hollowed_out():
    """Round 12 replaced a hard 500-LINE cap with a byte budget plus content
    assertions, because the line cap was measurably the wrong constraint.

    What both runtimes pay on every invocation is bytes, not newlines, and this
    file is mostly tables and fenced blocks whose density varies wildly. Worse,
    the cap was actively harmful: satisfying it is what made me strip the
    `references/` prefix from all six links in round 11, breaking every path the
    file tells an agent to open. A budget met by damaging the content is a
    budget that costs more than it saves.

    So: bytes, with headroom, plus the contracts that must survive any future
    shave. Deleting the retry rule to fit is now a test failure, not a saving.
    """
    text = (SKILL / "SKILL.md").read_text()
    size = len(text.encode())
    assert size <= 30_000, (
        f"SKILL.md is {size} bytes against a 30,000-byte budget. Move explanation "
        f"into references/ — do not compress the contracts below out of existence")

    required = {
        "the risk score formula": "complexity + 2",
        "the four terminal states": "RETRY_HISTORY_REQUIRED",
        "the exit-status contract": "human_gate_exit_status",
        "the retry rule": "one concrete model id per failure",
        "the independence states": "review_independence",
        "the capability-tier rule": "capability_tier",
        "how to reach the scorer": "route_task.py",
    }
    missing = [name for name, needle in required.items() if needle not in text]
    assert not missing, (
        f"SKILL.md no longer states {missing}. These are the load-bearing "
        f"contracts; a size budget must never be met by dropping one")

    # Round 13: the guard checked for content that must be PRESENT and missed a
    # field the code had deleted — `retry_history_inferred` survived in the
    # schema for a round, promising a key no route emits. Documentation drifts
    # in both directions.
    from route_task import Task, route as _route
    sample = _route(_task(task_class="MECHANICAL"), CFG)
    emitted = set(sample)
    start = text.index("task_class:  complexity:")
    schema_block = text[start:text.index("```", start)]
    # Round 14: this read the FIRST key on each line and skipped the whole
    # nested `review` block, so 16 of 44 declared fields were checked. Several
    # lines declare three fields; the schema's densest part was invisible.
    top, nested = set(sample), set(sample["review"])
    for raw in schema_block.splitlines():
        indented = raw.startswith(" ")
        line = raw.split("#")[0]
        for token in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", line):
            if token in ("true", "false", "null"):
                continue
            pool = nested if indented else top
            assert token in pool, (
                f"SKILL.md's output schema names "
                f"{'review.' if indented else ''}{token!r}, which no route emits")
    # And the other direction, for the nested block the guard used to skip
    # entirely: a field a route emits but the schema never mentions is drift too.
    for key in sorted(emitted):
        assert key in schema_block, f"the schema never mentions the emitted field {key!r}"

    # Terminal states must be enumerated, not summarised.
    from route_task import TERMINAL_STATES
    for name in TERMINAL_STATES:
        assert name in text, f"SKILL.md does not name the {name} terminal"

def test_d13_the_judge_retry_never_seats_the_implementer_as_its_own_reviewer():
    """Round 6 relaxed the retry at LOW on the reasoning that LOW permits
    self-review. LOW's exemption is about the reviewer the BAND CONFIGURED
    landing on the implementer, not a licence for the router to move it there
    to free a model for the judge. With the relaxation in place this exact
    route traded a distinct, stronger reviewer for the implementer itself,
    recorded nothing (LOW's depth floor is 0, so the shortfall gate cannot fire
    either), and flipped requires_human_confirmation from true to false."""
    keep = ("claude-opus-5", "gpt-5.6-terra")
    out = r(task_class="MIGRATION", flags=["review_disagreement"], runtime="claude_code",
            unavailable_models=_leave_only(*keep))
    rv = out["review"]
    remaining = sorted({m["id"] for m in CFG["models"].values()} - set(out["unavailable_models"]))
    seats = [out["selected_role"], *rv["reviewers"], rv["judge"]]
    assert remaining == sorted(keep), (
        f"precondition 'two models' drifted: remaining={remaining}")
    assert len(seats) == 3, (
        f"precondition 'three seats' drifted: seats={seats}")
    assert rv["band"] == "LOW" and out["route_path"] == "disagreement", "probe drifted"
    assert out["selected_model"] not in rv["reviewer_models"], (
        f"implementer {out['selected_model']} was re-seated as its own reviewer "
        f"to free a judge: reviewers={rv['reviewer_models']} judge={rv['judge_model']}")
    assert rv["judge_unavailable"] and out["requires_human_confirmation"], (
        "with two models and three seats there is no independent judge; saying "
        "otherwise buys the seat with the review")


def test_d13_a_substitution_record_that_outlives_its_seat_is_raised_not_dropped():
    """Round 7. The emit boundary used to filter records down to those matching
    the final roster, which made every assertion about that match true by
    construction — the round-6 defect shape, reintroduced by the round-6 fix.
    Two reviewers proved it independently: `_restate` -> `[]` left 114 green.

    So `_restate` is tested directly, and the boundary raises instead of
    correcting."""
    from route_task import _restate
    rec = [{"replaced": "reasoning_specialist", "with": "principal_architect", "reason": "x"}]
    # untouched: the record does not point at the re-seated role
    assert _restate(rec, "senior_engineer", "worker_balanced") == rec
    # re-pointed: the seat it named now holds someone else
    assert _restate(rec, "principal_architect", "worker_balanced") == [
        {"replaced": "reasoning_specialist", "with": "worker_balanced", "reason": "x"}]
    # dropped: the displaced role came back, so nothing was substituted
    assert _restate(rec, "principal_architect", "reasoning_specialist") == []
    assert _restate(None, "a", "b") == []


# Config entries the code deliberately does not read, each with the reason.
# Anything NOT listed here must have a consumer: round 9 found eight keys that
# had none, two of which were a second copy of a number the router reads from
# somewhere else — the drift the config header forbids.
DOCUMENTED_BUT_UNREAD = {
    "version": "schema marker for humans and diffs",
    "conditional_flags": "documents which flag pairs are deliberate, for the "
                         "inert-flag test; the effect itself lives in `overrides`",
    "worker_promotions": "prose description of promotions implemented in "
                         "select_worker; kept as the human-readable statement",
    "worker_balanced_selection": "documents that worker_balanced_alt is the "
                                 "first-escalation fallback; the alt is now Claude "
                                 "while worker_fast is OpenAI, so it is no longer "
                                 "a same-family fallback. `fallbacks` implements it",
    "verification_ledger": "provenance of the identifiers, read by people",
    "router.default_worker": "states the policy's starting point; the table in "
                             "worker_selection is what executes",
    # Round 10, from the instrumented reader below. Each of these is read by
    # nobody across 34,848 routes; the reason is what separates "documented on
    # purpose" from "forgotten".
    "retry.same_model_same_effort": "budget for the CALLING agent's loop; one "
                                    "route() call cannot count attempts",
    "retry.same_model_higher_effort": "same",
    "retry.stronger_model": "same",
    "retry.max_review_rounds": "the caller runs the review loop, not the router",
    "retry.max_judge_invocations": "same",
    "retry.require_new_evidence_on_same_tier": "states the rule the router "
                                               "enforces by refusing same-tier retries",
    "review.disagreement.resolution": "verdict-combination table the caller "
                                      "applies after reviews return",
    "review.disagreement.code_local_dispute_judge": "REAL GAP, recorded not "
        "excused: the router always seats `default_judge` and never inspects "
        "the dispute's kind, so these two never bind. Wiring them needs a "
        "dispute-kind input the schema does not have yet.",
    "review.disagreement.formal_reasoning_dispute_judge": "see above",
    "review.disagreement.formal_reasoning_dispute_effort": "see above",
    "review.MEDIUM.preferred_by_implementer.reasoning_specialist": "read for "
        "the implementers that reach MEDIUM; these two do not in any probe",
    "review.MEDIUM.preferred_by_implementer.worker_balanced_alt": "see above",
    "effort_map.claude.MINIMAL": "vocabulary completeness — no band or "
                                 "floor selects MINIMAL",
    "effort_map.openai.MINIMAL": "same",
    "effort_map.xai.MINIMAL": "same",
    "effort_map.xai.MAX": "the clamp runs before the native lookup and the "
                          "only xai model's ceiling is VERY_HIGH, so this leaf "
                          "is unreachable rather than merely unvisited",
    "effort_map.xai.LOW": "the probes below never pair the only xai seat with "
                          "LOW effort — reachable via a prior-failure promotion "
                          "on MECHANICAL/LOW, which this probe set does not build",
    "effort_map.gemini": "unbound family: registered for suite ID spelling, "
                         "dispatchable:false, never seated so effort_map.gemini "
                         "leaves are completeness-only",
    "transports.claude_code.native": "how the CALLER invokes the host's own "
        "seat; there is no recipe to check because there is no bridge",
    "transports.codex.native": "see above",
    "transports.grok.native": "see above",
    "transports.claude_code.to_openai.mechanism": "the argv the CALLER runs; "
        "the router reads only whether a write-capable key is PRESENT",
    "transports.claude_code.to_openai.isolation": "dispatch property, not a "
        "routing input",
    "transports.claude_code.to_xai.mechanism_reviewer": "see mechanism above",
    "transports.claude_code.to_xai.isolation": "see isolation above",
    "transports.codex.to_claude.mechanism": "see mechanism above",
    "transports.codex.to_claude.isolation": "see isolation above",
    "transports.codex.to_xai.mechanism_reviewer": "see mechanism above",
    "transports.codex.to_xai.isolation": "see isolation above",
    "transports.grok.to_claude.mechanism": "see mechanism above",
    "transports.grok.to_claude.isolation": "see isolation above",
    "transports.grok.to_openai.mechanism": "see mechanism above",
    "transports.grok.to_openai.isolation": "see isolation above",
}

# Per model and per leaf, not as a blanket `models` exemption. The audit of
# 2026-08-18 found the blanket form giving `capability_tier` — which the router
# reads on every strength comparison — the same excuse as `price_per_mtok`,
# which it never reads. Naming the leaves means adding an unread one to the
# registry requires saying so.
DOCUMENTED_BUT_UNREAD.update({
    f"models.{key}.{leaf}": reason
    for key, model in CFG["models"].items()
    for leaf, reason in (
        ("price_per_mtok", "cost documentation for people; grep-verified to have "
                           "no code consumer, so staleness misleads a reader "
                           "rather than misrouting a task"),
        ("verified", "provenance documentation; verification_ledger is the record"),
        ("context_window", "documentation of the seat's limit; the router counts "
                           "no tokens, so it cannot compare against it. The one "
                           "decision this number drives is expressed as the "
                           "`large_context` / `latency_sensitive` rule in "
                           "worker_balanced_selection"),
    )
    if leaf in model
})

# A model no role binding and no fallback list names is never offered as a
# candidate, so the one switch that would refuse it is never reached. Derived
# rather than spelled with a model key: binding the model is what should make
# this leaf required reading, and that must not depend on editing a test.
_BOUND_MODEL_KEYS = (
    {key for binding in CFG["role_bindings"].values() for key in binding.values()}
    | {key for runtime in CFG["fallbacks"].values()
       for candidates in runtime.values() for key in candidates}
)
DOCUMENTED_BUT_UNREAD.update({
    f"models.{key}.dispatchable":
        "the second lock on a model nothing binds: `_candidates` reads this for "
        "every model it offers and never offers this one, since no role binding "
        "and no fallback list names it. It becomes required reading the moment "
        "something does bind it, which is exactly when it matters"
    for key, model in CFG["models"].items()
    if key not in _BOUND_MODEL_KEYS and "dispatchable" in model
})


def test_orchestrator_defaults_are_no_longer_excused():
    """T1이 두 키에 소비자(Policy 검증)를 붙였다. 읽히는 키가 unread
    allowlist에 남는 것은 excuse의 거짓말이므로 명시적으로 금지한다."""
    assert "router.default_orchestrator" not in DOCUMENTED_BUT_UNREAD
    assert "router.default_orchestrator_effort" not in DOCUMENTED_BUT_UNREAD


class _Recording(Mapping):
    """A config that remembers which paths were actually read.

    A string scan of the source cannot answer this question: `cfg["effort_map"]
    [task.runtime][effort]` reads a leaf whose name appears nowhere, and
    `cfg["flags"].values()` reads every child of a key without naming one. Round
    10 tried the scan both ways — any-segment matching excused whole subtrees,
    leaf matching invented orphans — so the reads are instrumented instead.

    A `Mapping` rather than a `dict` subclass on purpose: `dict(spec)` copies a
    dict subclass's storage directly without going through `__getitem__`, so the
    router's ordinary `spec = dict(spec)` would have slipped every key in that
    subtree past the recorder.
    """

    def __init__(self, data, path=(), seen=None):
        self._data, self._path = data, path
        self._seen = seen if seen is not None else set()

    def __getitem__(self, key):
        value = self._data[key]
        self._seen.add(self._path + (str(key),))
        if isinstance(value, dict):
            return _Recording(value, self._path + (str(key),), self._seen)
        return value

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def get(self, key, default=None):
        if key in self._data:
            return self[key]
        self._seen.add(self._path + (str(key),))
        return default


def test_d14_every_config_rule_has_a_consumer_or_a_recorded_reason():
    """The config's own rule: "a rule declared and consumed nowhere is a promise
    the system does not keep." Round 9 found eight keys breaking it. Round 10
    found the test that was supposed to hold the line matching ANY segment of a
    path, so `"router"` appearing once in the source excused every key beneath
    it — including the two removed in round 9, which could be re-added with the
    test still green. It now runs the router over a spread of inputs and asserts
    on what was actually read."""
    seen: set = set()
    cfg = _Recording(load_config(), (), seen)
    # Every task class and every band, because a key read only on
    # `MIGRATION/CRITICAL` is not inert — it is uncovered, and conflating the
    # two would turn this test into a coverage complaint. The retry budget is
    # exercised past its own ceiling for the same reason.
    probes = [
        dict(task_class=c, complexity=x, uncertainty=u, blast_radius=b, reversibility=r,
             flags=list(f), runtime=rt, prior_failures=pf,
             prior_models=list(pm), unavailable_models=list(um), isolation_available=iso)
        for c in TASK_CLASSES
        for x, u, b, r in ((0, 0, 0, 0), (1, 1, 1, 1), (2, 2, 2, 0), (3, 3, 3, 3))
        for f in ([], ["auth_sensitive"], ["review_disagreement"], ["bridge_down"],
                  ["long_horizon"], ["migration", "data_integrity_sensitive"],
                  ["unknown_root_cause"], ["production_hotfix"], ["public_api_change"],
                  ["concurrency_sensitive"], ["auth_sensitive", "review_disagreement"],
                  # Context flags reach effort and binding leaves nothing else
                  # does: `cross_service_change` is the only route to
                  # `effort_by_work.multi_system_refactoring`, and
                  # `large_context` / `latency_sensitive` are the routes to
                  # the alt-seat rule. Both were invisible while the subtrees
                  # holding them were excused wholesale.
                  ["cross_service_change"], ["large_context"],
                  ["latency_sensitive"])
        for rt in sorted(CFG["runtimes"])
        for pf, pm in ((0, []), (1, ["senior_engineer"]), (2, []), (5, []))
        for um in ([], ["claude-fable-5"], ["claude-opus-5", "gpt-5.6-sol"])
        for iso in (None, True, False)
    ]
    for kw in probes:
        try:
            route(Task(**kw), cfg)
        except (ValidationError, Exception):
            continue

    def paths(node, prefix=()):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from paths(v, prefix + (str(k),))
        else:
            yield prefix

    def excused(path):
        return any(".".join(path[:n]) in DOCUMENTED_BUT_UNREAD
                   for n in range(1, len(path) + 1))

    def unread(snapshot):
        """Leaves the router never read. One predicate, used by the assertion
        and by the canary below, so weakening it fails the canary."""
        return {".".join(path) for path in paths(snapshot)
                if not excused(path) and path not in seen}

    orphans = sorted(unread(load_config()))
    assert not orphans, (
        f"the router never read {orphans} across {len(probes)} routes; either wire "
        f"them up or add them to DOCUMENTED_BUT_UNREAD with a reason")

    # A canary, because the guard could not otherwise detect its own weakening:
    # with the allowlist complete the orphan set is empty under a correct rule
    # AND under the any-segment rule this replaced, so nothing distinguished
    # them. An unread key planted under an ancestor that IS read must surface —
    # that ancestor is precisely what the old spelling used as an excuse.
    planted = dict(load_config())
    planted["router"] = {**planted["router"],
                         "floors": {**planted["router"]["floors"], "__canary__": "unread"}}
    canary = unread(planted)
    assert canary == {"router.floors.__canary__"}, (
        f"the guard did not surface a planted unread key under a read ancestor; "
        f"it reported {sorted(canary)}")


# Review-band values whose perturbation is absorbed by design, each with the
# reason. `DOCUMENTED_BUT_UNREAD` answers a different question — "was this key
# ever looked at" — and B2 was two keys that WERE looked at and changed nothing.
PERTURBATION_EXEMPT = {
    "review.MEDIUM.preferred_by_implementer.reasoning_specialist":
        "no probe puts this implementer in a MEDIUM review: the classes that "
        "seat it are HIGH/CRITICAL, and a MEDIUM promotion reaching it would "
        "have to come from a failed principal_architect, which is already the "
        "ceiling. Uncovered, not inert.",
    "review.MEDIUM.preferred_by_implementer.worker_balanced_alt":
        "the alt is a binding target, not a role select_worker can return, so "
        "it is never the implementer a MEDIUM review is picked against.",
    "review.disagreement.code_local_dispute_judge":
        "the recorded REAL GAP: the router always seats `default_judge` and "
        "never inspects the dispute's kind. Wiring these needs a dispute-kind "
        "input the request schema does not have.",
    "review.disagreement.formal_reasoning_dispute_judge": "see above",
    "review.disagreement.formal_reasoning_dispute_effort": "see above",
}

# Probes chosen so that every `preferred_by_implementer` key a route can reach
# is actually reached — the four implementers that can hold a MEDIUM review,
# including the two that only arrive there by promotion after a failure.
_PERTURBATION_PROBES = [
    dict(task_class="MECHANICAL", complexity=1, uncertainty=1, blast_radius=1, reversibility=1),
    dict(task_class="MECHANICAL", complexity=0, uncertainty=0, blast_radius=0, reversibility=0),
    dict(task_class="REFACTORING", complexity=2, uncertainty=1, blast_radius=1, reversibility=0),
    dict(task_class="IMPLEMENTATION", complexity=3, uncertainty=3, blast_radius=3, reversibility=3),
    dict(task_class="DEBUGGING", complexity=2, uncertainty=2, blast_radius=2, reversibility=0),
    dict(task_class="OPERATIONS", complexity=2, uncertainty=2, blast_radius=1,
         reversibility=1, flags=["auth_sensitive"]),
    dict(task_class="TESTING", complexity=1, uncertainty=1, blast_radius=1,
         reversibility=0, flags=["review_disagreement"]),
    dict(task_class="REFACTORING", complexity=2, uncertainty=1, blast_radius=1,
         reversibility=0, flags=["review_disagreement"]),
    dict(task_class="REVIEW", complexity=2, uncertainty=1, blast_radius=1,
         reversibility=0, prior_failures=1, prior_models=["grok-4.6"]),
    dict(task_class="MECHANICAL", complexity=2, uncertainty=1, blast_radius=1,
         reversibility=0, prior_failures=1, prior_models=["claude-opus-5"]),
    dict(task_class="MECHANICAL", complexity=2, uncertainty=1, blast_radius=1,
         reversibility=0, prior_failures=1, prior_models=["claude-fable-5"]),
]


# Config-identity digests, not routing outcomes. Since design §4 B1 made
# `policy_sha256` a CONTENT digest of the cfg actually in use, every legal
# perturbation moves it — and `decision_fingerprint` with it. Comparing them
# here would make every review key look effective and leave this test unable
# to find an inert one ever again (its own canary catches exactly that). The
# question this test asks is whether a value changes a routing DECISION, so
# the decision is what gets compared.
_PERTURBATION_IGNORED_KEYS = ("policy_sha256", "decision_fingerprint")


def _routes_under(cfg):
    out = []
    for kw in _PERTURBATION_PROBES:
        try:
            decision = route(Task(**kw), cfg)
            out.append({k: v for k, v in decision.items()
                        if k not in _PERTURBATION_IGNORED_KEYS})
        except Exception as exc:  # noqa: BLE001 — a raise is itself an outcome
            out.append({"raised": f"{type(exc).__name__}: {exc}"})
    return out


def _scalar_leaves(node, prefix=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _scalar_leaves(value, prefix + (str(key),))
    elif not isinstance(node, list):
        yield prefix, node


def _alternatives(value, policy):
    """Other values this leaf could legally hold. Every one of them, not one
    neighbour: `default_judge` names the strongest role, so the single
    next-role-along probe wrapped to the weakest and was corrected straight
    back by the judge-seating rule — an effective key measured as inert."""
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, int):
        return [value + 1, max(0, value - 1)]
    if isinstance(value, str):
        if value in policy.efforts:
            return [e for e in policy.efforts if e != value]
        if value in policy.roles:
            return [r for r in policy.roles if r != value]
    return []


def _inert_review_leaves(cfg, baseline, policy):
    """Leaves no legal perturbation of which moves any probe route."""
    inert = set()
    for path, value in _scalar_leaves(cfg["review"]):
        name = "review." + ".".join(path)
        if name in PERTURBATION_EXEMPT:
            continue
        alternatives = _alternatives(value, policy)
        if not alternatives:
            # Outside the perturbation vocabulary (a verdict-combination action,
            # say). Not claimed to have an effect and not claimed to lack one.
            continue
        for alternative in alternatives:
            probe_cfg = copy.deepcopy(cfg)
            node = probe_cfg["review"]
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = alternative
            if _routes_under(probe_cfg) != baseline:
                break
        else:
            inert.add(name)
    return inert


def test_d28_every_review_band_value_changes_a_route_when_perturbed():
    """B2 (audit 2026-08-18). `test_d14` proves a key was READ, and reading is
    not having an effect: `select_review` starts with `dict(cfg["review"][band])`,
    which touches every key of the band at once, so `reviewer_count: 3` and
    `prefer_cross_family: false` both passed d14 while producing byte-identical
    routes. Only perturbation can tell the two apart — change the value, and if
    nothing downstream moves, the key is decoration.

    Scoped to `review` because that is where the defect was found and where the
    "read the whole band spec" shape lives; widening it to the whole config is
    a bigger job than this audit item, not a different one."""
    baseline = _routes_under(CFG)
    policy = Policy.of(CFG)
    inert = _inert_review_leaves(CFG, baseline, policy)
    assert inert == set(), (
        f"review policy values no route depends on: {sorted(inert)}. Either wire "
        f"them up, delete them, or record them in PERTURBATION_EXEMPT with the "
        f"reason their perturbation is absorbed.")

    # The same canary shape test_d14 uses, for the same reason: with the config
    # clean the inert set is empty under a working check AND under one that
    # silently skips everything. A planted key nothing can read must surface.
    planted = copy.deepcopy(CFG)
    planted["review"]["MEDIUM"]["__canary__"] = True
    assert _inert_review_leaves(planted, _routes_under(planted), Policy.of(planted)) == {
        "review.MEDIUM.__canary__"}


def test_d14_every_declared_compensation_can_actually_be_emitted():
    """Round 9. `fallback_compensations.cross_family_reviewer_lost` was declared
    and `_compensations()` had no branch that could emit it, so a compensation
    name that is misspelled — or added to the config and never wired — goes
    inert in silence while reading as active policy."""
    src = (SKILL / "scripts" / "route_task.py").read_text()
    declared = set(CFG["fallback_compensations"])
    emittable = {name for name in declared if f'"{name}"' in src or f"'{name}'" in src}
    assert declared == emittable, (
        f"declared but unreachable compensations: {sorted(declared - emittable)}")

    # Round 10: the check above reads the KEYS, and the router dispatches on the
    # VALUES. Renaming an effect left `_compensations()` emitting it, `route()`
    # matching no branch, the compensation reported as applied, and 130 tests
    # green — an inert compensation reading as active, which is the exact defect
    # the config comment above this block claims the test prevents.
    from route_task import UnknownCompensationError
    effects = set(CFG["fallback_compensations"].values())
    unimplemented = {e for e in effects if f'"{e}"' not in src}
    assert not unimplemented, f"declared effects nothing implements: {sorted(unimplemented)}"

    altered = {**CFG, "fallback_compensations": {
        **CFG["fallback_compensations"], "principal_architect_to_senior": "typo_effect"}}
    with pytest.raises(UnknownCompensationError):
        route(_task(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=3,
                    reversibility=2, unavailable_roles=["principal_architect"]), altered)


def test_d16_a_retry_needs_one_concrete_model_id_per_failure():
    """Round 12, unanimous across three reviewers: delete the reconstruction.

    Rounds 8-12 each produced a Critical in a different reading of the same
    unknowable — what a previous attempt ran. The router cannot see its own
    history, and this module had already conceded as much one field over
    (`retry.*` budgets are documented as the caller's, because "one route()
    call cannot count attempts"). So it asks instead of guessing.
    """
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    first = r(task_class="MECHANICAL")
    ran = first["selected_model"]

    # Exact concrete history: routes, ungated, strictly stronger.
    ok = r(task_class="MECHANICAL", prior_failures=1, prior_models=[ran])
    assert ok["terminal"] is None and ok["selected_model"]
    assert tier[ok["selected_model"]] > tier[ran]

    # Everything else is terminal, with no bindings and an actionable note.
    for label, kw in (
        ("nothing named", dict(prior_failures=1)),
        ("a role alias", dict(prior_failures=1, prior_models=["worker_fast"])),
        ("too few", dict(prior_failures=2, prior_models=[ran])),
        ("too many", dict(prior_failures=1, prior_models=[ran, "claude-fable-5"])),
        ("mixed alias and id", dict(prior_failures=2, prior_models=[ran, "worker_fast"])),
    ):
        out = r(task_class="MECHANICAL", **kw)
        assert out["terminal"] == "RETRY_HISTORY_REQUIRED", f"{label}: {out['terminal']}"
        assert out["selected_model"] is None and out["review"]["reviewer_models"] == []
        assert any("retry history required" in n for n in out["notes"]), label
        assert out["requires_human_confirmation"]

    # A model that legitimately failed twice is named twice — the same-model
    # retry budgets in `retry.*` make that a truthful history, not a duplicate.
    twice = r(task_class="MECHANICAL", prior_failures=2, prior_models=[ran, ran])
    assert twice["terminal"] is None and tier[twice["selected_model"]] > tier[ran]


def test_d16_prior_failures_never_make_a_route_weaker():
    """Round 12's Critical, and the one assertion that subsumes the round-9,
    round-11 and round-12 defects at once: evidence of difficulty must never
    produce a weaker route than the same task with no failures. It did — a task
    whose table selection was tier 2 came back at tier 1 after one tier-0
    failure, called an escalation, at exit 0."""
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    ids = sorted(tier)
    checked = 0
    for task_class in TASK_CLASSES:
        for dims in ((0, 1, 3, 0), (0, 0, 0, 0), (2, 2, 2, 0), (3, 3, 3, 3)):
            kw = dict(task_class=task_class, complexity=dims[0], uncertainty=dims[1],
                      blast_radius=dims[2], reversibility=dims[3])
            base = r(**kw)
            if base["terminal"] or not base["selected_model"]:
                continue
            for failed in ids:
                out = r(**kw, prior_failures=1, prior_models=[failed])
                if out["terminal"]:
                    continue
                assert tier[out["selected_model"]] >= tier[base["selected_model"]], (
                    f"{task_class}/{dims}: no failures -> {base['selected_model']} "
                    f"(tier {tier[base['selected_model']]}), one failure of {failed} -> "
                    f"{out['selected_model']} (tier {tier[out['selected_model']]}) — "
                    f"{out['notes']}")
                assert tier[out["selected_model"]] > tier[failed], "did not clear the failure"
                checked += 1
    assert checked > 200, f"only {checked} pairs reached the assertion"


def test_d15_every_human_control_action_is_validated_and_load_bearing():
    """Round 11. The five action keys were compared against string literals with
    no vocabulary check, so a one-character typo deleted the control silently.
    Each is checked twice here: an unknown value must fail at construction, and
    a DIFFERENT valid value must actually change behaviour — otherwise the key
    is read but inert, which is the shape that has cost three rounds."""
    from route_task import Policy, ConfigError
    # Per key, against the actions that key's consumer implements. Round 12:
    # validating against the UNION let the strictest-sounding word disable four
    # of the five controls — `on_any_critical_review: terminal` passed and
    # removed the CRITICAL gate outright.
    # `on_retry_exhaustion` is gone: round 17 measured all three of its actions
    # producing the same route, because the cap is a safety limit rather than a
    # policy choice. A key whose values cannot differ is not configuration.
    keys = ("on_independence_unachievable", "on_any_critical_review",
            "on_judge_unavailable", "on_review_depth_reduced",
            "on_effort_below_floor")
    for key in keys:
        for bad in ("require_human_confirmaton", "", None, 1, "TERMINAL", "gate"):
            altered = {**CFG, "human_in_the_loop": {**CFG["human_in_the_loop"], key: bad}}
            with pytest.raises(ConfigError):
                Policy(altered)

    # Load-bearing, for EVERY key and EVERY action. Round 12: the previous
    # version proved it for one key while its docstring claimed five, and the
    # validator meanwhile accepted words no consumer implemented. A key with a
    # single legal value is a constant with a config file in front of it, so
    # each action has to be observable.
    probes = {
        "on_any_critical_review": dict(task_class="MECHANICAL", complexity=0,
                                       uncertainty=3, blast_radius=0, reversibility=0,
                                       flags=["auth_sensitive"]),
        "on_judge_unavailable": dict(task_class="MECHANICAL", complexity=0, uncertainty=3,
                                     blast_radius=0, reversibility=0,
                                     flags=["review_disagreement"],
                                     unavailable_models=["claude-fable-5", "gpt-5.6-sol"]),
        "on_review_depth_reduced": dict(task_class="IMPLEMENTATION", complexity=0,
                                        uncertainty=0, blast_radius=0, reversibility=0,
                                        flags=["auth_sensitive", "bridge_down"],
                                        runtime="codex"),
        "on_independence_unachievable": dict(task_class="IMPLEMENTATION", complexity=2,
                                             uncertainty=2, blast_radius=2,
                                             reversibility=0, isolation_available=False),
    }
    for key, probe in probes.items():
        seen = {}
        for action in ("terminal", "require_human_confirmation", "notify_human"):
            cfg = {**CFG, "human_in_the_loop": {**CFG["human_in_the_loop"], key: action}}
            out = route(_task(**probe), cfg)
            seen[action] = (out["terminal"] is not None,
                            out["requires_human_confirmation"])
        assert seen["terminal"][0], f"{key}: 'terminal' produced no terminal state"
        assert seen["require_human_confirmation"][1], f"{key}: no gate"
        assert len(set(seen.values())) >= 2, (
            f"{key}: every action produces the same outcome {seen} — read, but inert")

    # `on_effort_below_floor` needs a model that cannot reach the band's review
    # effort. Injected here rather than assumed from the registry, so the probe
    # states its own precondition.
    capped_cfg = {**CFG, "models": {**CFG["models"], "claude_senior": {
        **CFG["models"]["claude_senior"], "effort_ceiling": "HIGH"}}}
    seen = {}
    probe = dict(task_class="IMPLEMENTATION", complexity=3, uncertainty=3,
                 blast_radius=3, reversibility=2, flags=["auth_sensitive"])
    for action in ("terminal", "require_human_confirmation", "notify_human"):
        cfg = {**capped_cfg, "human_in_the_loop": {
            **capped_cfg["human_in_the_loop"], "on_effort_below_floor": action}}
        out = route(_task(**probe), cfg)
        seen[action] = (out["terminal"] is not None,
                        out["requires_human_confirmation"])
    assert seen["terminal"][0], "on_effort_below_floor: 'terminal' produced no terminal state"
    assert seen["require_human_confirmation"][1], "on_effort_below_floor: no gate"
    assert len(set(seen.values())) >= 2, (
        f"on_effort_below_floor: every action produces the same outcome {seen}")

    for bad in ("require_human_confirmaton", "", None, 1, "TERMINAL", "gate"):
        altered = {**CFG, "human_in_the_loop": {
            **CFG["human_in_the_loop"], "on_effort_below_floor": bad}}
        with pytest.raises(ConfigError):
            Policy(altered)


def test_d15_every_reference_skill_md_names_actually_exists():
    """Round 11, and self-inflicted: chasing the 500-line budget I stripped the
    `references/` prefix from all six links in SKILL.md, so every path an agent
    is told to open resolved to nothing. A line budget is not worth a broken
    artifact, and nothing was checking."""
    text = (SKILL / "SKILL.md").read_text()
    # EVERY backticked .md token, not just the ones already carrying the
    # prefix: the defect was a prefix being dropped, so a pattern that only
    # matches prefixed paths cannot see it.
    named = set(re.findall(r"`([A-Za-z0-9_./-]+\.md)`", text))
    assert len(named) >= 6, f"SKILL.md names only {sorted(named)}"
    broken = sorted(n for n in named if n != "SKILL.md" and not (SKILL / n).is_file())
    assert not broken, (
        f"SKILL.md tells an agent to open {broken}, which do not resolve relative "
        f"to it; the files live under references/")
    for doc in sorted((SKILL / "references").glob("*.md")):
        assert f"references/{doc.name}" in text, f"{doc.name} is never pointed at"

    # Fences must balance, or a section renders as literal code — this round's
    # new control-loop.md text was swallowed by an unclosed ```yaml block.
    for doc in sorted((SKILL / "references").glob("*.md")) + [SKILL / "SKILL.md"]:
        fences = sum(1 for line in doc.read_text().splitlines() if line.startswith("```"))
        assert fences % 2 == 0, f"{doc.name} has {fences} code fences — one is unclosed"


def test_d18_terminal_precedence_is_pinned_where_two_reasons_are_true():
    """Round 14. The ordering of the two unconditional gates carried six lines
    of justification and no test: swapping them, or deleting the
    `RETRY_HISTORY_REQUIRED` promotion entirely, failed nothing. The rule this
    artifact holds itself to is that every fix comes with a regression test
    that can fail, and the most-argued code in the round was outside it."""
    cap = CFG["retry"]["max_total_implementation_attempts"]

    # Budget spent AND no history: the budget is what the caller must act on,
    # because there is no next attempt for a history to place.
    spent = r(task_class="MECHANICAL", prior_failures=cap)
    assert spent["terminal"] == "HUMAN_REQUIRED", spent["notes"]
    # And the gate does not depend on the config. Round 15: suppressing the
    # history branch when the budget was spent put an unconditional gate under
    # `on_retry_exhaustion`, so `notify_human` routed a task with no history at
    # all, at exit 0. The shipped value hid it; only a different value shows it.
    # Round 18: this used to set `on_retry_exhaustion`, a key deleted in round
    # 17 — an unknown key in a config the validator does not reject, so the loop
    # was three identical iterations proving nothing. The gate is unconditional
    # now, so what there is to assert is that it holds, once.
    out = route(_task(task_class="MECHANICAL", prior_failures=cap), CFG)
    assert out["terminal"] and out["selected_model"] is None
    assert any("retry budget spent" in n for n in out["notes"]), (
        "the terminal does not name the fact the caller must act on")
    assert not any("retry history required" in n for n in spent["notes"]), (
        "told to collect a history that cannot be used")

    # Below the cap, the history is the actionable reason even when other
    # conditions are also true.
    incomplete = r(task_class="MECHANICAL", prior_failures=1)
    assert incomplete["terminal"] == "RETRY_HISTORY_REQUIRED"

    # Missing history AND a compromised review: the caller can act on the
    # history; it cannot act on the seat shortage.
    both = None
    for scarce in itertools.combinations(sorted(m["id"] for m in CFG["models"].values()), 4):
        out = r(task_class="MECHANICAL", flags=["auth_sensitive"],
                prior_failures=1, unavailable_models=list(scarce))
        if out["review"]["independence_compromised"]:
            both = out
            break
    assert both is not None, "no route reaches both conditions — assertion vacuous"
    assert both["terminal"] == "RETRY_HISTORY_REQUIRED", (
        f"the seat shortage masked the reason the caller can act on: {both['terminal']}")


def test_d18_a_notified_control_never_describes_a_cause_that_did_not_occur():
    """Round 14, found only by escalating the reviewer a capability tier.

    `on_independence_unachievable` was narrowed to `review_independence ==
    "unavailable"` under a comment claiming it now covered only the
    caller-declared gap. The narrowing was logically INERT — `independence()`
    returns exactly that whenever the router's own seats collide — so the
    control still fired on both causes while its new reason string described
    only one. With `notify_human` the result was a note on a TERMINAL route
    reading "the caller reported that isolation cannot be achieved here —
    proceeding without a gate", where the caller had reported nothing and the
    route did not proceed. Both clauses false, in the artifact whose second
    load-bearing property is that a recorded change must be a real change.
    """
    relaxed = {**CFG, "human_in_the_loop": {**CFG["human_in_the_loop"],
                                            "on_independence_unachievable": "notify_human"}}
    seen = 0
    for scarce in itertools.combinations(sorted(m["id"] for m in CFG["models"].values()), 4):
        task = _task(task_class="MECHANICAL", flags=["auth_sensitive"],
                     unavailable_models=list(scarce))
        out = route(task, relaxed)
        if not out["review"]["independence_compromised"]:
            continue
        seen += 1
        assert task.isolation_available is None, "probe drifted"
        for note in out["notes"]:
            assert "the caller reported" not in note, (
                f"the caller reported nothing, yet: {note}")
            if out["terminal"]:
                assert "proceeding without a gate" not in note, (
                    f"terminal route {out['terminal']} claims it is proceeding: {note}")
    assert seen, "no compromised route reached the assertion"

    # And the control must still work for the cause it names.
    declared = route(_task(task_class="IMPLEMENTATION", complexity=2, uncertainty=2,
                           blast_radius=2, isolation_available=False), relaxed)
    assert declared["review"]["review_independence"] == "unavailable"
    assert any("the caller reported" in n for n in declared["notes"]), (
        "the control no longer fires for the gap the caller did declare")


def test_d18_an_operational_shortage_is_a_terminal_not_invalid_input():
    """Round 14. `bridge_down` plus four concrete prior failures empties the
    local family, and `resolve()` raised `ValidationError` — which the CLI
    reports as exit 2, "invalid input", for a command that obeyed every
    documented contract. A scheduler that tells "call a human" (1) from "fix
    your input and retry" (2) was handed the wrong one, and the retry-exhaustion
    gate that should have answered never ran."""
    proc = cli("--class", "IMPLEMENTATION", "--complexity", "1", "--uncertainty", "1",
               "--blast-radius", "1", "--reversibility", "0", "--flags", "bridge_down",
               "--prior-failures", "4", "--prior-models",
               "claude-haiku-4-5-20251001,claude-sonnet-5,claude-opus-5,claude-fable-5")
    assert proc.returncode == 1, (
        f"an operational shortage exited {proc.returncode}; 2 means the caller's "
        f"input was wrong, and it was not")
    assert "TERMINAL" in proc.stdout
    assert "supply exhausted" in proc.stdout, "the reason is not reported"

    out = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=1, blast_radius=1,
            flags=["bridge_down"], prior_failures=4,
            prior_models=["claude-haiku-4-5-20251001", "claude-sonnet-5",
                          "claude-opus-5", "claude-fable-5"])
    assert out["terminal"] in TERMINAL_STATES
    assert out["selected_model"] is None and out["requires_human_confirmation"]


def test_d22_a_production_hotfix_dispatches_now_and_owes_the_confirmation():
    """Round 20, and the first finding that came from USING the skill rather
    than reviewing it.

    `production_hotfix` raises the band, so a live incident got the frontier
    model, near-max effort, dual independent review AND a pre-dispatch human
    gate — the slowest, most-blocked path the policy can produce, for the
    situation with the least time. The four dimensions score what a change IS
    and what it might COST; none of them represents the cost of DELAY.

    The review is not the problem and does not move. What moves is when the
    human is asked."""
    probe = dict(task_class="DEBUGGING", complexity=1, uncertainty=2,
                 blast_radius=3, reversibility=0, flags=["production_hotfix"])
    out = r(**probe)
    rv = out["review"]
    assert rv["band"] == "CRITICAL" and len(rv["reviewers"]) == 2
    assert rv["independence_required"], "the deferral cost the review its independence"
    assert not out["requires_human_confirmation"], "a live incident is still blocked"
    assert out["human_confirmation_deferred"]
    assert any("owed AFTER" in n for n in out["notes"])

    # A shell must be able to tell "run it" from "run it and then confirm".
    proc = cli("--class", "DEBUGGING", "--complexity", "1", "--uncertainty", "2",
               "--blast-radius", "3", "--reversibility", "0",
               "--flags", "production_hotfix")
    assert proc.returncode == 4, (
        f"a deferred confirmation exited {proc.returncode}; 0 means nothing "
        f"further is required, and something is")
    assert "CONFIRMATION OWED" in proc.stdout

    # It is a policy, and the other value has to work.
    blocking = {**CFG, "human_in_the_loop": {**CFG["human_in_the_loop"],
                                             "on_production_hotfix": "require_human_confirmation"}}
    strict = route(_task(**probe), blocking)
    assert strict["requires_human_confirmation"] and not strict["human_confirmation_deferred"], (
        "on_production_hotfix is read but changes nothing — an inert control")

    # And a review that cannot be trusted still blocks, incident or not.
    degraded = r(task_class="DEBUGGING", complexity=1, uncertainty=2, blast_radius=3,
                 reversibility=0, flags=["production_hotfix", "auth_sensitive"],
                 unavailable_models=["claude-opus-5", "gpt-5.6-sol"])
    assert degraded["review"]["review_depth_reduced"], "probe drifted"
    assert degraded["requires_human_confirmation"]
    assert not degraded["human_confirmation_deferred"]


def test_d20_a_shortage_never_buries_a_reason_the_caller_can_act_on():
    """Round 16. Round 15 gave `SUPPLY_EXHAUSTED` precedence so a symptom would
    stop masking the cause, and used a plain assignment — which overshot the
    stated reasoning. It outranks the states it PRODUCES; a missing retry
    history is not one of them, and burying "pass --prior-models with one model
    id per failure" under a shortage the caller cannot fix trades one wrong
    report for its mirror image."""
    everything = sorted(m["id"] for m in CFG["models"].values())
    out = r(task_class="MECHANICAL", prior_failures=1, unavailable_models=everything)
    assert out["terminal"] == "RETRY_HISTORY_REQUIRED", (
        f"the actionable reason was replaced by {out['terminal']}")
    assert any("one model id per failure" in n for n in out["notes"])

    # And it still outranks what it does produce. Round 17: this was wrapped in
    # `if produced["terminal"] == "SUPPLY_EXHAUSTED"`, so deleting the very
    # precedence it names skipped the assertion instead of failing it — the
    # subject of a test must never be its own precondition. It is also the only
    # positive coverage `SUPPLY_EXHAUSTED` has, so it asserts the terminal.
    produced = r(task_class="MECHANICAL", flags=["auth_sensitive"],
                 unavailable_models=_leave_only("claude-haiku-4-5-20251001"))
    remaining = sorted({m["id"] for m in CFG["models"].values()}
                       - set(produced["unavailable_models"]))
    assert remaining == ["claude-haiku-4-5-20251001"], (
        f"precondition 'one model left' drifted: remaining={remaining}")
    assert produced["terminal"] == "SUPPLY_EXHAUSTED", (
        f"a route with one model left emitted {produced['terminal']}")
    assert produced["review"]["independence_compromised"], (
        "the symptom this outranks is not present — the probe proves nothing")
    assert produced["selected_model"] is None and produced["requires_human_confirmation"]
    assert any("supply exhausted" in n for n in produced["notes"])


def test_d21_the_promotion_decision_sees_the_confidence_that_ships():
    """Round 17. Round 16 recomputed confidence from the emitted fallbacks so
    the two would stop contradicting each other — and left the decision that
    CONSUMES it standing on the discarded plan's value. A route shipping 0.75
    kept the LOW review band that 0.95 had justified, so the policy "0.60-0.79
    raises the review band one level" quietly did not apply. The fixed point
    exists precisely so the promotion sees the final number."""
    threshold = CFG["router"]["confidence"]["extra_review_below"]
    # Same remaining pair the five-id withhold used to leave: haiku + terra.
    # The promotion fires because that pair is thin, not because five names
    # were listed.
    keep = ("claude-haiku-4-5-20251001", "gpt-5.6-terra")
    out = r(task_class="MECHANICAL", flags=["review_disagreement", "unknown_root_cause"],
            unavailable_models=_leave_only(*keep))
    remaining = sorted({m["id"] for m in CFG["models"].values()} - set(out["unavailable_models"]))
    assert remaining == sorted(keep), (
        f"precondition 'thin remaining pair' drifted: remaining={remaining}")
    assert out["routing_confidence"] < threshold, "probe drifted"
    assert any("low_routing_confidence" in o for o in out["band_overrides_applied"]), (
        f"confidence {out['routing_confidence']} is below {threshold} and the review "
        f"band was never promoted: {out['band_overrides_applied']}")
    assert out["review"]["band"] != "LOW"

    # And the invariant over the sweep, both directions.
    checked = 0
    for task_class in ("MECHANICAL", "IMPLEMENTATION", "DEBUGGING"):
        for flags in ([], ["review_disagreement"], ["review_disagreement", "unknown_root_cause"]):
            for scarce in ([], ["claude-fable-5"],
                           _leave_only("claude-haiku-4-5-20251001", "gpt-5.6-terra")):
                o = r(task_class=task_class, flags=list(flags), unavailable_models=list(scarce))
                if o["terminal"]:
                    continue
                promoted = any("low_routing_confidence" in x
                               for x in o["band_overrides_applied"])
                if o["routing_confidence"] < threshold and o["review"]["band"] != "CRITICAL":
                    assert promoted, (
                        f"{task_class}/{flags}/{scarce}: {o['routing_confidence']} < "
                        f"{threshold} without promotion")
                checked += 1
    assert checked > 15


def test_d20_the_emitted_confidence_matches_the_emitted_fallbacks():
    """Round 16. The fixed point computes confidence per candidate plan, and a
    route that recovered from a failed pass shipped the discarded plan's number:
    `routing_confidence` 0.95 beside two recorded fallbacks whose own formula
    gives 0.85. Two emitted facts contradicting each other is the shape this
    module's second load-bearing property forbids, and it is checkable in one
    line because the formula is a function of what is emitted."""
    from route_task import routing_confidence
    checked = 0
    for task_class in ("MECHANICAL", "IMPLEMENTATION", "ARCHITECTURE"):
        for scarce in ([], ["claude-fable-5"], ["claude-fable-5", "claude-opus-5"],
                       ["claude-fable-5", "claude-opus-5", "claude-sonnet-5",
                        "gpt-5.6-luna", "gpt-5.6-sol"]):
            for flags in ([], ["review_disagreement"], ["auth_sensitive"]):
                task = _task(task_class=task_class, flags=list(flags),
                             unavailable_models=list(scarce))
                out = route(task, CFG)
                assert out["routing_confidence"] == routing_confidence(
                    task, out["fallbacks_applied"], CFG), (
                    f"{task_class}/{scarce}/{flags}: confidence "
                    f"{out['routing_confidence']} does not follow from the "
                    f"fallbacks it reports ({out['fallbacks_applied']})")
                checked += 1
    assert checked > 30


def test_d20_the_retry_cap_is_not_a_configurable_suggestion():
    """Round 16. `on_retry_exhaustion` selects how loudly the cap is announced,
    never whether it holds — with a complete history and `notify_human` the
    cap+1th attempt shipped at exit 0. A budget any config value can opt out of
    is not a budget."""
    cap = CFG["retry"]["max_total_implementation_attempts"]
    history = ["gpt-5.6-luna"] * cap
    out = route(_task(task_class="MECHANICAL", prior_failures=cap,
                      prior_models=list(history)), CFG)
    assert out["requires_human_confirmation"], f"attempt {cap + 1} was dispatchable"
    assert out["terminal"] == "HUMAN_REQUIRED"
    # Round 18: the key that used to gate this is gone, and a deleted key must
    # not linger in a test as a setting nobody reads — three iterations over a
    # value with no consumer is the inert shape one layer up.
    assert "on_retry_exhaustion" not in CFG["human_in_the_loop"]
    assert any("retry budget spent" in n for n in out["notes"])


def test_d19_a_recovered_seat_plan_is_not_reported_as_exhausted():
    """Round 15. `supply_exhausted` was set while exploring a PRELIMINARY seat
    set and never cleared, so a route whose final assignment resolved
    completely was still reported terminal. The fixed point exists to try
    several plans; a shortage seen in one it discarded is not a fact about the
    one it emitted."""
    out = r(task_class="MECHANICAL", flags=["review_disagreement"],
            unavailable_models=["claude-fable-5", "claude-opus-5", "claude-sonnet-5",
                                "gpt-5.6-luna", "gpt-5.6-sol"])
    assert out["terminal"] is None, (
        f"a complete seat plan was reported as {out['terminal']}: {out['notes']}")
    assert out["selected_model"] == "claude-haiku-4-5-20251001"
    assert not any("supply exhausted" in n for n in out["notes"])


def test_d19_every_control_fires_exactly_on_its_declared_cause():
    """The answer round 15's reviewers converged on for the class that has cost
    this loop seven rounds.

    Six of those seven were the same shape: a comment claiming a predicate does
    one thing while it does another. Prose cannot be checked. A CAUSE CODE
    paired with the predicate can be — each control now declares one, the route
    emits which fired, and this asserts that the set of routes where a control
    fired is exactly the set where its cause holds, computed independently from
    the inputs. Round 14's Critical — a predicate narrowed to something
    logically equivalent while its reason string claimed a narrower cause —
    fails this immediately.
    """
    # An INDEPENDENT copy of the wording. Round 17: this looked the phrases up
    # in the same `CAUSE_REASONS` the router uses to write them, so a reason
    # that was wrong but consistently wrong passed — the "oracle computed by the
    # formula under test" shape this suite has removed twice. Changing a reason
    # now requires changing it here too, which is the point: the wording is a
    # contract with the operator, not an implementation detail.
    EXPECTED_WORDING = {
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
    }
    from route_task import CAUSE_REASONS
    assert CAUSE_REASONS == EXPECTED_WORDING, (
        f"the router's wording drifted from the contract: "
        f"{set(CAUSE_REASONS.items()) ^ set(EXPECTED_WORDING.items())}")
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    cap = CFG["retry"]["max_total_implementation_attempts"]
    ids = sorted(tier)

    # Causes computed from the INPUTS and the emitted facts, never from the
    # control table those inputs feed.
    def expected(task, out):
        rv = out["review"]
        return {
            "caller_declared_isolation_gap":
                bool(CFG["review"][rv["band"]].get("independent"))
                and task.isolation_available is False,
            "critical_review_band": rv["band"] == "CRITICAL",
            "no_adjudicator": bool(rv["judge_unavailable"]),
            "review_below_band": bool(rv["review_depth_reduced"]),
            "effort_below_floor": any(
                r["floor_broken"] for r in out["effort_ceiling_applied"]),
            "unconfirmed_prior_termination":
                "termination_unconfirmed" in task.flags,
        }

    # The oracle must cover exactly the causes the router declares. A control
    # added with a new cause and not added here would be swept past in silence:
    # `got == want` holds trivially for a cause neither side knows about. This
    # is the drift that matters, and unlike weakening the count it is
    # detectable — a weakened threshold only shows up once something is
    # already missing, which is too late to be a guard.
    probe_out = route(_task(task_class="MECHANICAL"), CFG)
    assert set(expected(_task(task_class="MECHANICAL"), probe_out)) == set(EXPECTED_WORDING), (
        f"the oracle covers {sorted(set(expected(_task(task_class='MECHANICAL'), probe_out)))} "
        f"but the contract declares {sorted(EXPECTED_WORDING)}")

    checked = 0
    seen_causes: set = set()
    for task_class in TASK_CLASSES:
        for dims in ((0, 0, 0, 0), (2, 2, 2, 0), (3, 3, 3, 3)):
            for flags in ([], ["auth_sensitive"], ["review_disagreement"],
                          ["auth_sensitive", "bridge_down"],
                          ["termination_unconfirmed"]):
                for iso in (None, True, False):
                    for pf, pm in ((0, []), (1, [ids[4]]), (cap, [ids[4]] * cap)):
                        for scarce in ([], [ids[0]], ids[:3]):
                            task = _task(task_class=task_class, complexity=dims[0],
                                         uncertainty=dims[1], blast_radius=dims[2],
                                         reversibility=dims[3], flags=list(flags),
                                         isolation_available=iso, prior_failures=pf,
                                         prior_models=list(pm),
                                         unavailable_models=list(scarce))
                            out = route(task, CFG)
                            want = {c for c, holds in expected(task, out).items() if holds}
                            got = set(out["human_control_causes"])
                            assert got == want, (
                                f"{task_class}/{dims}/{flags}/iso={iso}/pf={pf}: "
                                f"controls fired for {sorted(got)}, the inputs say "
                                f"{sorted(want)}")
                            # The other half of the gap. Round 16: pairing a
                            # cause with a predicate caught the predicate
                            # drifting; nothing observed the PROSE, which is
                            # what round 14's Critical actually falsified. The
                            # rationale must carry each fired cause's declared
                            # wording, and must not carry any other cause's.
                            for cause in EXPECTED_WORDING:
                                present = EXPECTED_WORDING[cause] in out["rationale"]
                                assert present == (cause in want), (
                                    f"{task_class}/{dims}: rationale "
                                    f"{'claims' if present else 'omits'} "
                                    f"{cause!r} but the inputs say otherwise")
                            seen_causes |= want
                            checked += 1
    assert checked > 500, f"only {checked} routes reached the assertion"
    # Every cause, not most of them. Round 16: `>= 4` of five let a
    # task-class-dependent predicate drift out of the sweep unnoticed, and the
    # docstring meanwhile claimed whole-input-space equivalence over three
    # classes. It sweeps every class now and demands every cause.
    assert seen_causes == set(EXPECTED_WORDING), (
        f"never exercised: {sorted(set(EXPECTED_WORDING) - seen_causes)}; a cause no "
        f"route triggers is a control this test cannot hold to anything")


def test_d18_every_terminal_the_router_assigns_is_declared():
    """Round 14. `TERMINAL_STATES` was a hand-maintained tuple beside the code
    that assigns terminals — a second source of truth, which is what the config
    header forbids and what this artifact has removed twice already. Adding a
    fifth terminal without listing it would leave the documentation guard
    passing while SKILL.md omitted it."""
    # AST, not regexes. Round 15: the previous version ran its second pattern
    # against the literal empty string — the seventh instance of the class this
    # very guard exists to end — and its first pattern saw only the first branch
    # of a ternary, so a terminal reachable only through the control table or
    # the false arm of a conditional was invisible.
    import ast
    src = (SKILL / "scripts" / "route_task.py").read_text()
    tree = ast.parse(src)
    assigned: set = set()

    def literals(node):
        """Every string constant a value expression can evaluate to."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return literals(node.body) | literals(node.orelse)
        if isinstance(node, ast.BoolOp):
            return set().union(*(literals(v) for v in node.values))
        return set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "terminal":
                    assigned |= literals(node.value)
        # `Control(key, cause, fired, TERMINAL, reason)` — the table is the other
        # place a terminal name enters the router.
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Control":
            if len(node.args) >= 4:
                assigned |= literals(node.args[3])
    assigned.discard("")
    from route_task import TERMINAL_STATES
    assert len(assigned) >= 4, f"terminal extraction found only {sorted(assigned)}"
    assert assigned <= set(TERMINAL_STATES), (
        f"route() assigns {sorted(assigned - set(TERMINAL_STATES))}, which "
        f"TERMINAL_STATES does not declare")
    assert set(TERMINAL_STATES) <= assigned, (
        f"TERMINAL_STATES declares {sorted(set(TERMINAL_STATES) - assigned)}, "
        f"which route() never assigns")


def test_d17_a_self_reviewing_route_is_gated_whatever_the_config_says():
    """Round 13. `independence_compromised` — the router could not give the
    seats distinct models, so the implementer reviews itself — was an
    unconditional gate until round 12 folded it into a configurable control.
    With `on_independence_unachievable: notify_human` it then emitted a route
    with two identical reviewers, `independence_required: true`, at exit 0.

    Round 12's own Critical was "a validated word silently removes the control
    it names"; this is the same defect with the opposite word. Making a key a
    real choice must not include the choice to delete a protection that was
    never optional — so the gate is outside the dispatcher, and this test looks
    at it through the config value that would hide it."""
    relaxed = {**CFG, "human_in_the_loop": {**CFG["human_in_the_loop"],
                                            "on_independence_unachievable": "notify_human"}}
    seen = 0
    for scarce in itertools.combinations(sorted(m["id"] for m in CFG["models"].values()), 4):
        for task_class in ("MECHANICAL", "IMPLEMENTATION"):
            task = _task(task_class=task_class, flags=["auth_sensitive"],
                         unavailable_models=list(scarce))
            try:
                out = route(task, relaxed)
            except ValidationError:
                continue
            if not out["review"]["independence_compromised"]:
                continue
            seen += 1
            models = [m for m in out["review"]["reviewer_models"] if m]
            assert out["requires_human_confirmation"], (
                f"{task_class}/{scarce}: implementer {out['selected_model']} with "
                f"reviewers {models} emitted without a gate")
            # Round 14: checking only the gate let the terminal assignment be
            # deleted with the test still green, leaving a dispatchable route
            # whose reviewers are the implementer under another label.
            assert out["terminal"] == "INDEPENDENCE_UNAVAILABLE", (
                f"{task_class}/{scarce}: emitted {out['terminal']!r} with no "
                f"distinct models for the seats")
            assert out["selected_model"] is None
    assert seen, "no route reached independence_compromised — the assertion was vacuous"


def test_d17_an_unimplemented_action_fails_closed_even_on_a_cached_config():
    """Round 13. `Policy.of` cached on config identity and the config is a
    mutable dict, so a value changed after the first route reached the
    dispatcher unvalidated. The dispatcher treated anything that was not
    `terminal` or `require_human_confirmation` as `notify_human` — a control
    failing OPEN, which is the one direction it must never fail.

    1.2.0 moved WHERE this closes for a dict config: the content digest in the
    cache key rebuilds the Policy, so the unknown word is now rejected at build
    time and never reaches the dispatch block. Both are asserted separately —
    a round-2 reviewer pointed out that keeping only the end-to-end assertion
    would leave this named regression test green while no longer exercising the
    guard it is named after.
    """
    from route_task import ConfigError, Policy
    cfg = load_config()
    probe = _task(task_class="MECHANICAL", complexity=0, uncertainty=3,
                  blast_radius=0, reversibility=0, flags=["auth_sensitive"])
    assert route(probe, cfg)["requires_human_confirmation"], "probe drifted"

    # Mutate the SAME object the Policy cache is keyed on, as a caller sharing
    # a config dict across calls would. Still fails closed, end to end.
    cfg["human_in_the_loop"]["on_any_critical_review"] = "notify_the_human"
    with pytest.raises(ConfigError):
        route(probe, cfg)

    # Where it closes now: rebuild-on-content-change, at Policy build time.
    with pytest.raises(ConfigError, match="this router implements"):
        Policy.of(cfg)

    # And the dispatch-time guard itself, which stays as depth for the one
    # path that is still identity-cached — a non-dict Mapping. Reached
    # directly, since no cfg mutation can route through it any more.
    good = load_config()
    policy = Policy.of(good)
    good["human_in_the_loop"]["on_any_critical_review"] = "notify_the_human"
    with pytest.raises(ConfigError):
        route(probe, _StaleMapping(good, policy))


class _StaleMapping(dict):
    """A config whose Policy is already built — the shape a non-dict Mapping
    presents to `Policy.of`: cached on identity, so a later content change is
    not seen at build time and only the dispatch-time guard is left."""

    def __init__(self, data, policy):
        super().__init__(data)
        Policy._cache[id(self)] = (None, policy)


def test_d17_an_alias_in_prior_models_identifies_nothing_and_routes_nothing():
    """Round 13. Deleting the reconstruction left the alias inference alive one
    layer down: `Resolver` still read `prior_models` and turned an alias into
    "the model it probably held", so a route whose whole premise was that
    aliases identify nothing still excluded models on the strength of one — and
    at `prior_failures=0` it did so on a route that was emitted at exit 0.

    The history is now checked before anything resolves, so an alias reaches no
    inference at all, and the field that reported the guess
    (`excluded_as_ambiguous_alias`) is gone rather than left permanently empty.
    """
    for kw in (dict(prior_failures=1, prior_models=["senior_engineer"]),
               dict(prior_failures=0, prior_models=["senior_engineer"]),
               dict(prior_failures=2, prior_models=["senior_engineer", "gpt-5.6-sol"])):
        out = r(task_class="REFACTORING", complexity=2, **kw)
        assert out["terminal"] == "RETRY_HISTORY_REQUIRED", kw
        assert out["excluded_prior_failures"] == [], (
            f"an alias still produced an exclusion: {out['excluded_prior_failures']}")
        assert "excluded_as_ambiguous_alias" not in out, "the guess field survived"
        assert any("aliases do not identify" in n for n in out["notes"])

    # Naming models while declaring no failures is a contradiction, not a hint.
    contradiction = r(task_class="REFACTORING", complexity=2, prior_failures=0,
                      prior_models=["gpt-5.6-luna"])
    assert contradiction["terminal"] == "RETRY_HISTORY_REQUIRED"
    assert contradiction["selected_model"] is None

def test_d14_a_bonus_review_that_cannot_be_isolated_does_not_kill_the_task():
    """Round 9. The isolation terminal keyed off `review["independent"]`, which
    the architect-downgrade compensation sets at ANY band. A LOW route whose
    *bonus* second review could not be isolated emitted nothing at all — a
    compensation punishing the caller for its own best effort. The terminal
    belongs to the band's own requirement."""
    out = r(task_class="ARCHITECTURE", flags=["long_horizon"],
            unavailable_models=["claude-fable-5"], isolation_available=False)
    assert out["review"]["band"] == "LOW", "probe drifted"
    assert not CFG["review"]["LOW"].get("independent"), "LOW now asks for independence"
    assert out["fallback_compensations_applied"], "probe no longer reaches the compensation"
    assert out["terminal"] is None, (
        "a bonus review's isolation gap terminated a band that never asked for it")
    assert out["selected_model"]


def test_d14_the_human_gate_exit_status_must_survive_posix_truncation():
    """Round 9. The guard rejected 0/1/2 and nothing else, so 256 passed it and
    exited 0 — a human-gated route reporting success, the single hazard the
    value exists to remove. It also raised after the route had been printed,
    outside main()'s handler, landing on exit 1 for a route it had just emitted
    as executable."""
    from route_task import Policy, ConfigError
    cfg = load_config()
    for bad in (256, 512, -256, 0, 1, 2, True, "3", 3.0):
        altered = {**cfg, "human_in_the_loop": {**cfg["human_in_the_loop"],
                                                "human_gate_exit_status": bad}}
        with pytest.raises(ConfigError):
            Policy(altered)
    for good in (3, 7, 255):
        altered = {**cfg, "human_in_the_loop": {**cfg["human_in_the_loop"],
                                                "human_gate_exit_status": good}}
        assert Policy(altered).human_gate_exit_status == good


def test_d13_a_confirmed_isolation_gap_is_terminal_where_the_band_requires_it():
    """Round 8. The policy declares `on_independence_unachievable: terminal`,
    and the five-state contract goes to the trouble of separating `unavailable`
    (a confirmed gap) from `degraded` (an unchecked one). The router answered
    both the same way — an ordinary route at exit 0 — which also falsified the
    exhaustiveness claim the exit contract had just made."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=False)
    assert out["review"]["independence_required"], "probe drifted"
    assert out["review"]["review_independence"] == "unavailable"
    assert out["terminal"] == "INDEPENDENCE_UNAVAILABLE"
    assert out["requires_human_confirmation"]
    # And the band that does not ask for independence is untouched by this.
    low = r(task_class="MECHANICAL", isolation_available=False)
    assert not low["review"]["independence_required"] and not low["terminal"]


def test_d13_the_human_gate_exit_status_comes_from_the_config():
    """Round 8. The key was declared in `human_in_the_loop` and read nowhere;
    the CLI hard-coded 3. The config's own rule is that a rule consumed nowhere
    is a promise the system does not keep."""
    import tempfile, yaml, os
    cfg = load_config()
    assert cfg["human_in_the_loop"]["human_gate_exit_status"] == 3
    probe = ["--class", "IMPLEMENTATION", "--complexity", "0", "--uncertainty", "0",
             "--blast-radius", "0", "--reversibility", "0",
             "--flags", "auth_sensitive,bridge_down", "--runtime", "codex"]
    assert cli(*probe).returncode == 3
    # Change the policy and the CLI must follow it, or the key is decoration.
    altered = dict(cfg)
    altered["human_in_the_loop"] = {**cfg["human_in_the_loop"], "human_gate_exit_status": 7}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "model-routing.yaml"
        path.write_text(yaml.safe_dump(altered))
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys;sys.path.insert(0,%r);import route_task as rt;"
             "from pathlib import Path;"
             "rt._DEFAULT_CFG=rt.load_config(Path(%r));sys.exit(rt.main(sys.argv[1:]))"
             % (str(SKILL / "scripts"), str(path)), *probe],
            capture_output=True, text=True)
    assert proc.returncode == 7, (
        f"config said 7, CLI returned {proc.returncode}: the key is not read")


def _peek_model(out, role):
    """The model a role holds *as a reviewer* in this route.

    Round 6: the previous version fell through to the judge seat, so when the
    judge retry re-seated a reviewer this returned the displaced role's model
    from the judge and the stale substitution record looked consistent. A
    substitution record is about a reviewer slot; reading it out of the judge
    seat answers a different question than the one being asked.
    """
    rv = out["review"]
    for name, model in zip(rv["reviewers"], rv["reviewer_models"]):
        if name == role:
            return model
    return None


# ---------------------------------------------------------------------------
# D12 — the disagreement path is a seat allocator too
# ---------------------------------------------------------------------------
#
# `route_path == "disagreement"` binds a judge at ANY band, but seat allocation
# used to sit behind `if review["independent"]`, and LOW declares
# `independent: false`. The code's blind spot and the test suite's blind spot
# were the same one: the sweep never set `review_disagreement` either.

DISAGREEMENT_SWEEP = [
    ["review_disagreement"],
    ["review_disagreement", "bridge_down"],
    ["review_disagreement", "long_horizon"],
    ["review_disagreement", "auth_sensitive"],
]


def _disagreement_sweep():
    for task_class in TASK_CLASSES:
        for c, u, b, rev in DIMENSION_SWEEP:
            for flags in DISAGREEMENT_SWEEP:
                for runtime in sorted(CFG["runtimes"]):
                    for scarce in SCARCITY_SWEEP:
                        try:
                            yield r(task_class=task_class, complexity=c, uncertainty=u,
                                    blast_radius=b, reversibility=rev, flags=list(flags),
                                    runtime=runtime, unavailable_models=list(scarce))
                        except ValidationError:
                            continue


def test_d12_sweep_reaches_low_band_disagreement_routes():
    low = [o for o in _disagreement_sweep()
           if not o["terminal"] and o["review"]["band"] == "LOW" and o["review"]["judge"]]
    assert low, "the sweep never produced a LOW-band route with a judge"


def test_d12_the_judge_is_never_a_party_at_any_band():
    """An adjudicator brought in to settle a dispute must not be one of the
    parties. LOW's worker-reviews-itself is documented design; the judge seat
    is not covered by that exemption."""
    checked = 0
    for out in _disagreement_sweep():
        rv = out["review"]
        if out["terminal"] or not rv["judge_model"]:
            continue
        checked += 1
        parties = {out["selected_model"], *(m for m in rv["reviewer_models"] if m)}
        assert rv["judge_model"] not in parties, (
            f"{out['task_class']}/{rv['band']}: judge {rv['judge_model']} is also a party"
        )
    assert checked, "no seated judge in the disagreement sweep"


def test_d12_judge_unavailable_gates_and_explains():
    seen = 0
    for out in _disagreement_sweep():
        rv = out["review"]
        if out["terminal"] or not rv["judge_unavailable"]:
            continue
        seen += 1
        assert rv["judge"] is None and rv["judge_model"] is None
        assert out["requires_human_confirmation"] is True
        assert "human must resolve" in out["rationale"]
    assert seen, "no judge_unavailable route in the sweep — the assertion was vacuous"


def test_d12_judge_reclaims_a_tier_a_reviewer_did_not_need():
    """Allocation is greedy — worker, then reviewers, then judge — and the
    reviewer step maximises tier, so the judge could be told nothing adequate
    was free while a model sat unused behind a reviewer that did not need it.
    This is the exact route that exhibited it; a stop nobody can act on is as
    unhelpful as a missing one."""
    out = r(task_class="MECHANICAL", flags=["review_disagreement"],
            unavailable_models=["claude-fable-5"])
    rv = out["review"]
    assert rv["judge_unavailable"] is False
    assert rv["judge_model"] is not None
    parties = {out["selected_model"], *(m for m in rv["reviewer_models"] if m)}
    assert rv["judge_model"] not in parties


def test_d23_an_effort_ceiling_must_name_a_real_effort_level():
    """A ceiling is compared against conceptual effort levels. A typo would
    either raise deep inside `efforts.index()` or be silently skipped — and a
    ceiling that is silently skipped is a route promising an effort the model
    refuses.

    `None` is deliberately absent from the bad list: a model with no ceiling and
    a model whose ceiling is explicitly null are the same thing, and `.get()`
    cannot tell them apart anyway.
    """
    from route_task import Policy, ConfigError
    key = next(iter(CFG["models"]))
    for bad in ("XHIGH", "very_high", "", 3, "MAXIMUM"):
        altered = {**CFG, "models": {**CFG["models"],
                                     key: {**CFG["models"][key], "effort_ceiling": bad}}}
        with pytest.raises(ConfigError):
            Policy(altered)

    # And a real level is accepted, with the ceiling readable per model id.
    ok = {**CFG, "models": {**CFG["models"],
                            key: {**CFG["models"][key], "effort_ceiling": "HIGH"}}}
    policy = Policy(ok)
    assert policy.ceiling_of[CFG["models"][key]["id"]] == "HIGH"
    other = next(k for k in CFG["models"] if k != key)
    assert policy.ceiling_of[CFG["models"][other]["id"]] is None


# Independent of `effort_map`, on purpose. Probed against each CLI on
# 2026-08-14; see the config's verification_ledger. If a CLI's vocabulary
# changes, this table and the map both have to move, and that is the point:
# an oracle that reads the map cannot tell you the map is wrong.
# The whole mapping, by hand, independent of `effort_map` — not just the set of
# tokens each CLI will take. A membership check catches a token a CLI rejects; it
# cannot catch a token the CLI ACCEPTS at the wrong level, and "quietly
# under-delivers" is half of what this test claims to guard. Probed against each
# CLI on 2026-08-14; the config's verification_ledger records the round-trips.
#
# Yes, this duplicates `effort_map`. That is the point, and it is the same
# bargain `EXPECTED_WORDING` makes: an oracle that reads the table under test
# can only tell you the router is self-consistent, never that the table is
# right. Changing a spelling now takes two edits, and the second one is a
# decision rather than a consequence.
EXPECTED_NATIVE = {
    "claude": {"MINIMAL": "low",  "LOW": "low", "MEDIUM": "medium",
               "HIGH": "high", "VERY_HIGH": "xhigh", "MAX": "max"},
    "openai": {"MINIMAL": "none", "LOW": "low", "MEDIUM": "medium",
               "HIGH": "high", "VERY_HIGH": "xhigh", "MAX": "max"},
    # `max` and `none` are rejected outright; MAX has nowhere to go but xhigh,
    # and the clamp means no route reaches that entry anyway.
    "xai":    {"MINIMAL": "low",  "LOW": "low", "MEDIUM": "medium",
               "HIGH": "high", "VERY_HIGH": "xhigh", "MAX": "xhigh"},
    "gemini": {"MINIMAL": "low",  "LOW": "low", "MEDIUM": "medium",
               "HIGH": "high", "VERY_HIGH": "high", "MAX": "high"},
}
ACCEPTED_CLI_TOKENS = {f: set(m.values()) for f, m in EXPECTED_NATIVE.items()}


def test_d23_the_native_spelling_is_always_a_token_that_model_accepts():
    """Both directions. A route that names an effort the model's CLI rejects is
    not executable, and one that quietly under-delivers is the same defect
    pointed the other way.

    The second direction needs `EXPECTED_NATIVE`, not a token set: setting
    `effort_map.openai.HIGH` to `low` emits a token Codex happily accepts, at an
    effort level below the one the band asked for, and a membership check waves
    it through."""
    from route_task import Policy
    policy = Policy(CFG)
    fam = {m["id"]: m["family"] for m in CFG["models"].values()}
    assert set(ACCEPTED_CLI_TOKENS) == set(fam.values()), (
        f"ACCEPTED_CLI_TOKENS covers {sorted(ACCEPTED_CLI_TOKENS)} but "
        f"the registry holds {sorted(set(fam.values()))}")
    levels = CFG["effort_levels"]
    seen = 0
    for out in _sweep():
        if out["terminal"]:
            assert out["selected_effort_effective"] is None
            continue
        model = out["selected_model"]
        effective, requested = out["selected_effort_effective"], out["selected_effort"]
        ceiling = policy.ceiling_of[model]
        if ceiling is None:
            assert effective == requested, (
                f"{model} has no ceiling but effort was reduced "
                f"{requested} -> {effective}")
        else:
            assert levels.index(effective) <= levels.index(ceiling), (
                f"{model} cannot receive {effective} (ceiling {ceiling})")
            assert levels.index(effective) <= levels.index(requested)
        native = out["selected_effort_native"]
        # Against the hand table, not against the map the router just read.
        assert native == EXPECTED_NATIVE[fam[model]][effective], (
            f"{model} ({fam[model]}) at {effective} emitted native {native!r}; "
            f"that family's CLI spells it "
            f"{EXPECTED_NATIVE[fam[model]][effective]!r}"
        )
        assert native in ACCEPTED_CLI_TOKENS[fam[model]], (
            f"{model} ({fam[model]}) emitted native {native!r}, which that "
            f"family's CLI does not accept; accepted={sorted(ACCEPTED_CLI_TOKENS[fam[model]])}"
        )
        seen += 1
    assert seen, "no executable route in the sweep — the assertion was vacuous"


def test_d23_native_is_looked_up_from_the_effort_that_ships():
    """Since the request/effective split, native is `map[family][effective]`.
    Nothing on the shipped registry can prove that: the only capped family maps
    MAX and VERY_HIGH to the same token, so indexing the requested level instead
    emits the identical string and every assertion stays green.

    So the discriminating case is built rather than found. A Claude model capped
    at HIGH separates the two lookups — `claude.MAX` is `max` and `claude.HIGH`
    is `high` — and a route that requests MAX must then emit `high`. Reverting
    the index to `selected_effort` fails here and nowhere else, which is the
    only reason this test exists.
    """
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["models"]["claude_senior"]["effort_ceiling"] = "HIGH"
    m = cfg["effort_map"]["claude"]
    assert m["MAX"] != m["HIGH"], (
        "the probe rests on these two spellings differing; if the map changed, "
        "this test no longer discriminates and must be rebuilt")

    out = route(_task(task_class="IMPLEMENTATION", complexity=3, uncertainty=3,
                      blast_radius=3, reversibility=2, flags=["auth_sensitive"],
                      unavailable_models=["claude-fable-5"]), cfg)
    assert out["selected_model"] == "claude-opus-5", "probe drifted"
    assert out["selected_effort"] == "MAX", "probe drifted"
    assert out["selected_effort_effective"] == "HIGH", (
        f"the cap did not apply: {out['selected_effort_effective']}")
    assert out["selected_effort_native"] == m["HIGH"], (
        f"native is {out['selected_effort_native']!r}; it must come from the "
        f"effort that ships ({m['HIGH']!r}), not the one that was asked for "
        f"({m['MAX']!r})")


def test_d23_a_merged_ceiling_row_reads_coherently():
    """One role can hold two seats, and the single row it gets has to be true of
    both asks.

    Two ways in, and the first review of this merge found only the second:

    - LOW seats `worker_fast` as its own reviewer BY DESIGN (`independent:
      false`, so `_deconflict` never runs). The route stays EXECUTABLE. Three
      separate probes of mine missed this because they all swept dimensions that
      land above LOW, and each reported "no problems" from a path it never
      entered.
    - `_deconflict` failing to substitute leaves the worker among the reviewers.
      That also compromises independence, so those routes are terminal.

    The row reports the HIGHER of the two asks, because that is the level any
    named floor is measured against. Before this, a LOW route shipped
    `requested: LOW` beside `floor_requires: MEDIUM` — two numbers that cannot
    both describe one seat.
    """
    import copy

    # (a) executable, by design, no independence failure anywhere near it.
    cfg = copy.deepcopy(CFG)
    cfg["models"]["openai_worker_fast"]["effort_ceiling"] = "MINIMAL"
    out = route(_task(task_class="MECHANICAL"), cfg)
    assert out["terminal"] is None, "the executable merge path went terminal"
    assert out["review"]["reviewers"] == [out["selected_role"]], (
        "probe drifted: this case exists because the worker reviews itself")
    assert not out["review"]["independence_required"]
    rows = out["effort_ceiling_applied"]
    assert len(rows) == 1, f"one seat, one row: {rows}"
    row = rows[0]
    assert row["requested"] == row["floor_requires"], (
        f"the row asks for {row['requested']} while naming a floor that "
        f"requires {row['floor_requires']}: {row}")
    assert row["floor_broken"] == "review.LOW.effort"

    # (b) both floors broken on one seat: the stricter one is what ships.
    cfg = copy.deepcopy(CFG)
    cfg["models"]["xai_frontier"]["effort_ceiling"] = "HIGH"
    ids = sorted(m["id"] for m in cfg["models"].values())
    out = route(_task(task_class="MECHANICAL", complexity=3, uncertainty=3,
                      blast_radius=3, reversibility=3,
                      unavailable_models=sorted(set(ids) - {"grok-4.6"})), cfg)
    assert out["review"]["independence_compromised"], "probe drifted"
    shared = [r for r in out["effort_ceiling_applied"]
              if r["floor_broken"] == "review.CRITICAL.effort"]
    assert len(shared) == len(out["effort_ceiling_applied"]), (
        f"a seat broke the review floor and reported a weaker one: "
        f"{out['effort_ceiling_applied']}")
    assert all(r["floor_requires"] == "MAX" for r in shared), (
        "the stricter of two broken floors is what the human has to satisfy")


def test_d23_a_ceiling_record_appears_exactly_when_a_seat_was_capped():
    """The record is the disclosure. Emitting it when nothing was capped makes
    a managed decision out of a non-event; omitting it when something was makes
    the route claim an effort it does not deliver."""
    for out in _sweep():
        records = out["effort_ceiling_applied"]
        for rec in records:
            assert rec["capped_at"] != rec["requested"], (
                f"a record with nothing capped: {rec}")
        roles = [rec["role"] for rec in records]
        assert len(roles) == len(set(roles)), f"same seat twice: {records}"
        if not out["terminal"] and out["selected_effort_effective"] != out["selected_effort"]:
            assert any(r["role"] == out["selected_role"] for r in records), (
                f"worker effort moved {out['selected_effort']} -> "
                f"{out['selected_effort_effective']} with no record: {records}")


def test_d23_a_capped_reviewer_seat_gates_on_a_live_route():
    """The path the ceiling control actually fires on.

    Not `xai_only` + CRITICAL: one model fills every seat there, so
    de-confliction fails and the route is INDEPENDENCE_UNAVAILABLE before any
    ceiling matters. The live path is the default binding under scarcity —
    withholding the architect makes `_deconflict` seat `worker_balanced` in a
    CRITICAL reviewer slot, where the band asks for MAX and the model tops out
    one level below.

    DOCUMENTATION/CRITICAL looks like the obvious probe and is not: its worker
    IS `worker_balanced`, but the worker's ceiling equals `band_CRITICAL`'s
    floor, so nothing breaks.
    """
    out = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=3,
            blast_radius=3, reversibility=2, flags=["auth_sensitive"],
            unavailable_models=["claude-fable-5"])
    assert out["terminal"] is None, f"probe went terminal: {out['terminal']}"
    assert out["review"]["band"] == "CRITICAL"
    assert "grok-4.6" in out["review"]["reviewer_models"], (
        f"probe drifted; reviewers are {out['review']['reviewer_models']}")

    capped = [rec for rec in out["effort_ceiling_applied"] if rec["floor_broken"]]
    assert capped, f"no floor broken: {out['effort_ceiling_applied']}"
    assert all(rec["floor_broken"] == "review.CRITICAL.effort" for rec in capped)
    assert all(rec["capped_at"] == "VERY_HIGH" and rec["requested"] == "MAX"
               for rec in capped)

    # Inclusion, not equality. The same withhold also drops the architect, so
    # the judge cannot be seated and the compensation adds a third reviewer.
    assert "effort_below_floor" in out["human_control_causes"]
    assert out["requires_human_confirmation"]

    # And the counter-case: a route with no capped seat never claims the cause.
    clean = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=3,
              blast_radius=3, reversibility=2, flags=["auth_sensitive"])
    assert not [rec for rec in clean["effort_ceiling_applied"] if rec["floor_broken"]]
    assert "effort_below_floor" not in clean["human_control_causes"]


def test_d23_a_terminal_route_keeps_the_ceiling_record_without_the_model():
    """`review_depth_reduced`'s rule, applied to the new record: the human still
    needs to know a seat was capped, and a terminal route still names no model.
    Emptying the list instead would make the d19 oracle disagree with the
    dispatcher, which emitted the cause before emit ran."""
    out = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=3,
            blast_radius=3, reversibility=2, flags=["auth_sensitive"],
            unavailable_models=["claude-fable-5"], isolation_available=False)
    assert out["terminal"] == "INDEPENDENCE_UNAVAILABLE", "probe drifted"
    assert out["effort_ceiling_applied"], "the disclosure was dropped on terminal"
    assert all(rec["model"] is None for rec in out["effort_ceiling_applied"])
    assert out["selected_effort_effective"] is None


def test_d23_worker_clamp_reads_peek_not_the_provisional_resolved_map():
    """`resolve()` is all-or-nothing: a judge shortage empties `resolved` for
    the whole pass. `_seat_judge` can then drop the judge so the FINAL
    resolution succeeds, but a clamp that read `resolved.get` already ran
    against `{}` and shipped an effort the worker cannot receive."""
    cfg = {**CFG, "models": {**CFG["models"], "claude_worker_balanced": {
        **CFG["models"]["claude_worker_balanced"], "effort_ceiling": "LOW"}}}
    out = route(_task(task_class="MIGRATION", flags=["review_disagreement"],
                      unavailable_models=_leave_only("claude-sonnet-5")), cfg)
    assert out["terminal"] is None, f"probe went terminal: {out['terminal']}"
    assert out["selected_model"] == "claude-sonnet-5"
    assert out["selected_effort"] == "HIGH"
    assert out["selected_effort_effective"] == "LOW", (
        f"worker clamp missed a LOW ceiling: effective={out['selected_effort_effective']} "
        f"records={out['effort_ceiling_applied']}")
    assert out["selected_effort_native"] == "low"
    worker_recs = [rec for rec in out["effort_ceiling_applied"]
                   if rec["role"] == out["selected_role"]]
    assert worker_recs, f"worker clamp wrote nothing: {out['effort_ceiling_applied']}"
    assert worker_recs[0]["model"] == "claude-sonnet-5"
    assert worker_recs[0]["requested"] == "HIGH"
    assert worker_recs[0]["capped_at"] == "LOW"


def test_d23_the_same_role_writes_one_ceiling_row():
    """When `_deconflict` cannot substitute, the worker stays in
    `review["reviewers"]` and both clamps would write it. Design §4.3.3:
    the same seat appears once. The broken floor must survive the merge."""
    # On the grok host: an xai-only supply is that host's own degraded
    # binding, and the seat is native there. Off it, MECHANICAL is a write
    # class and `to_xai` ships no maker recipe (issue #19), so an xai-only
    # supply has no worker at all and never reaches the ceiling merge this
    # test is about.
    out = r(task_class="MECHANICAL", complexity=3, uncertainty=3,
            blast_radius=3, reversibility=3, runtime="grok",
            unavailable_models=_leave_only("grok-4.6"))
    roles = [rec["role"] for rec in out["effort_ceiling_applied"]]
    assert len(roles) == len(set(roles)), (
        f"same seat twice: {out['effort_ceiling_applied']}")
    senior = [rec for rec in out["effort_ceiling_applied"]
              if rec["role"] == "senior_engineer"]
    assert len(senior) == 1, f"expected one senior_engineer row: {senior}"
    assert senior[0]["floor_broken"] == "review.CRITICAL.effort"
    assert senior[0]["floor_requires"] == "MAX"


# ---------------------------------------------------------------------------
# D24/D25 — a crash must not borrow a contracted exit status
# (docs/design/2026-08-15-dispatch-layer-design.md, DD-2)
# ---------------------------------------------------------------------------

def test_d24_yaml_parse_error_is_a_config_error_not_a_crash(tmp_path):
    """A config that exists but does not parse must exit 2 with an actionable
    message — not escape as a traceback on the interpreter's exit 1, which
    the contract reserves for "terminal (no route to execute)"."""
    import shutil
    clone = tmp_path / "skill"
    shutil.copytree(SKILL, clone, ignore=shutil.ignore_patterns(
        "__pycache__", ".pytest_cache", "tests"))
    (clone / "config" / "model-routing.yaml").write_text(
        "bands: [\n  broken: yaml: : :\n")
    proc = subprocess.run(
        [sys.executable, str(clone / "scripts" / "route_task.py"),
         "--class", "MECHANICAL", "--complexity", "0", "--uncertainty", "0",
         "--blast-radius", "0", "--reversibility", "0", "--format", "json"],
        capture_output=True, text=True)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "not valid YAML" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_d24_an_unexpected_crash_exits_5_not_a_route_outcome(monkeypatch):
    """1 means "terminal", 2 means "invalid input". A crash is neither, so it
    must have its own status — a wrapper branching on the exit code must
    never be told a story about the route that did not happen."""
    import route_task
    def boom(task, cfg=None):
        raise RuntimeError("synthetic crash")
    monkeypatch.setattr(route_task, "route", boom)
    rc = route_task.main(["--class", "MECHANICAL", "--complexity", "0",
                          "--uncertainty", "0", "--blast-radius", "0",
                          "--reversibility", "0"])
    assert rc == 5


def test_d25_help_states_every_exit_the_cli_can_return():
    """main() returns 4 (deferred confirmation) and now 5 (crash); --help is
    built from the module docstring, so the docstring must state both."""
    proc = cli("--help")
    flat = proc.stdout.replace("\n", " ")
    for token in ("0 dispatchable", "1 terminal", "2 invalid",
                  "3 executable only after", "4 dispatchable", "5 internal"):
        assert token in flat, token


def test_d24_a_post_route_crash_also_exits_5(monkeypatch):
    """The guard must cover the whole post-parse body, not just the
    route(task) call — a crash while turning a route into output
    (serialization, text formatting, the exit-status mapping) is still a
    crash, not a route outcome, and must not escape as the interpreter's
    exit 1 either."""
    import route_task

    class Unserializable:
        pass

    def route_returns_bad_json(task, cfg=None):
        return {"unserializable": Unserializable()}

    monkeypatch.setattr(route_task, "route", route_returns_bad_json)
    rc = route_task.main(["--class", "MECHANICAL", "--complexity", "0",
                          "--uncertainty", "0", "--blast-radius", "0",
                          "--reversibility", "0", "--format", "json"])
    assert rc == 5


# ---------------------------------------------------------------------------
# D26 — surplus isolation evidence must not count (DD-7)
# ---------------------------------------------------------------------------

def test_d26_surplus_evidence_is_not_enforced():
    """`>=` accepted stale, foreign, or extra ids as proof, so over-supplying
    evidence could flip the strongest reported state. A count mismatch in
    either direction is not evidence about THIS route's reviewers."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=True,
            isolation_evidence=["s1", "s2", "stale-from-another-route"])
    assert out["review"]["review_independence"] == "planned"
    assert any("isolation evidence not counted" in n for n in out["notes"]), \
        out["notes"]


def test_d26_exact_evidence_still_reaches_enforced():
    """Regression guard: the fix must not resurrect the old `>= 2` failure —
    one reviewer, one id, equality holds."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=True,
            isolation_evidence=["session-a1b2", "session-c3d4"])
    assert out["review"]["review_independence"] == "enforced"
    assert not any("isolation evidence not counted" in n for n in out["notes"])


# ---------------------------------------------------------------------------
# D27 — an unconfirmed termination must hold the route (DD-10)
# ---------------------------------------------------------------------------

def test_d27_unconfirmed_termination_holds_the_route_for_a_human():
    """A timed-out write-capable attempt whose process tree could not be
    confirmed dead may still be writing. Dispatching a retry behind it is
    two concurrent writers on the same files."""
    out = r(task_class="IMPLEMENTATION", flags=["termination_unconfirmed"])
    assert out["requires_human_confirmation"] is True
    assert "unconfirmed_prior_termination" in out["human_control_causes"]


def test_d27_the_gate_reaches_the_shell():
    proc = cli("--class", "IMPLEMENTATION", "--complexity", "0",
               "--uncertainty", "0", "--blast-radius", "0",
               "--reversibility", "0", "--flags", "termination_unconfirmed")
    assert proc.returncode == 3


def test_d27_production_hotfix_does_not_defer_an_unconfirmed_termination():
    """The deferral branch excludes independence_compromised,
    review_depth_reduced, and floor-broken already — termination_unconfirmed
    must join them. A possibly-live writer is not made acceptable by an
    incident; hotfix pressure is exactly when a second writer racing the
    first is likeliest, so this hold must survive combination with
    production_hotfix rather than being deferred to after the fix ships."""
    out = r(task_class="IMPLEMENTATION",
            flags=["production_hotfix", "termination_unconfirmed"])
    assert out["requires_human_confirmation"] is True
    assert out["human_confirmation_deferred"] is False


def test_d27_production_hotfix_combined_with_termination_unconfirmed_exits_3():
    proc = cli("--class", "IMPLEMENTATION", "--complexity", "0",
               "--uncertainty", "0", "--blast-radius", "0",
               "--reversibility", "0",
               "--flags", "production_hotfix,termination_unconfirmed")
    assert proc.returncode == 3



def test_d29_a_router_post_condition_failure_never_borrows_a_route_status(monkeypatch):
    """`RouterInvariantError` is raised in `route()` and no test ever provoked
    it (audit 2026-08-18 §5), so the branch that distinguishes "this code
    produced a state it promises never to produce" from "the caller's input is
    wrong" was itself unexercised. The distinction is the point: exit 2 would
    blame the caller for a defect here, and exit 1 would report a terminal
    route that was never computed."""
    import route_task

    real_deconflict = route_task._deconflict

    def leaves_a_stale_record(spec, worker, policy, resolver):
        out = dict(real_deconflict(spec, worker, policy, resolver))
        # A substitution record naming a seat the final roster does not hold —
        # the exact state the post-condition exists to refuse.
        out["self_review_avoided"] = [
            {"replaced": "senior_engineer", "with": "a_role_no_seat_holds",
             "reason": "planted by the test"}]
        return out

    monkeypatch.setattr(route_task, "_deconflict", leaves_a_stale_record)
    hard = dict(task_class="IMPLEMENTATION", complexity=3, uncertainty=3,
                blast_radius=3, reversibility=0)
    with pytest.raises(route_task.RouterInvariantError):
        route(_task(**hard), CFG)
    assert route_task.main([
        "--class", "IMPLEMENTATION", "--complexity", "3", "--uncertainty", "3",
        "--blast-radius", "3", "--reversibility", "0", "--format", "json",
    ]) == 5
