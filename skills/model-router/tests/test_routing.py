"""Validation scenarios and structural invariants for the deterministic router.

The four cases marked REGRESSION are the ones the previous policy version got
wrong: it dispatched on task class with early returns and only checked
critical-domain flags afterwards, so debugging an auth bug and designing a
payments architecture silently skipped mandatory dual review. Those two cases
plus the exact-boundary and band-ambiguity cases are the reason this file
exists — if a future edit reintroduces class-dispatch-before-override, they
fail loudly.

Note on scenario dimension scores: the spec's scenario table names expected
routes without fixing the four dimension scores, so each test picks scores a
careful classifier would plausibly assign for that description. Where the
resulting review band differs from the spec's loose parenthetical, the test
says so rather than bending the policy to match.

Run:  python3 -m pytest skills/model-router/tests/ -q
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from route_task import (  # noqa: E402
    BANDS,
    ROLES,
    TASK_CLASSES,
    Task,
    load_config,
    route,
)

CFG = load_config()


def r(**kw):
    """Route a task, defaulting every dimension to 0."""
    kw.setdefault("complexity", 0)
    kw.setdefault("uncertainty", 0)
    kw.setdefault("blast_radius", 0)
    kw.setdefault("reversibility", 0)
    return route(Task(**kw), CFG)


def at_least(role, floor):
    return ROLES.index(role) >= ROLES.index(floor)


def band_at_least(band, floor):
    return BANDS.index(band) >= BANDS.index(floor)


# ---------------------------------------------------------------------------
# Scenario 1-6, 12-13: ordinary routing
# ---------------------------------------------------------------------------

def test_s1_mechanical_rename_stays_cheap():
    """A rename across 12 files is a large workload with no uncertainty."""
    out = r(task_class="MECHANICAL")
    assert out["risk_band"] == "LOW"
    assert out["selected_role"] == "worker_fast"
    assert out["selected_effort"] == "LOW"
    assert out["review"]["band"] == "LOW"
    assert out["review"]["independence_required"] is False


def test_s2_ordinary_feature_with_clear_spec():
    out = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=1, blast_radius=1)
    assert out["risk_band"] == "MEDIUM"
    assert out["selected_role"] == "worker_fast"
    assert out["selected_effort"] == "MEDIUM"
    assert out["review"]["band"] == "MEDIUM"
    assert len(out["review"]["reviewers"]) == 1


def test_s3_complex_multi_file_feature():
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=1)
    assert out["risk_band"] == "HIGH"
    assert out["selected_role"] == "worker_balanced"
    assert out["selected_effort"] == "HIGH"
    # Review is HIGH, not MEDIUM: review depth follows the band alone, and this
    # scores 8. That independence is what makes the invariants below checkable.
    assert out["review"]["band"] == "HIGH"


def test_s4_failed_worker_escalates_rather_than_retrying():
    # Round 12: `--prior-models` takes concrete model ids now. A role alias does
    # not say which model held that seat on the attempt that failed, and five
    # rounds of trying to infer it produced five different defects, so the
    # router asks instead. The scenario is unchanged — a failed worker escalates.
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    failed = CFG["models"][CFG["role_bindings"]["default"]["worker_fast"]]["id"]
    out = r(
        task_class="IMPLEMENTATION",
        complexity=1, uncertainty=1, blast_radius=1,
        prior_failures=1,
        prior_models=[failed],
    )
    # On the model, not the label. Round 8: excluding the failed model already
    # moves `worker_fast` onto `claude-sonnet-5` — the very model
    # `worker_balanced` would have supplied — so the role-label assertion failed
    # on a route that satisfies this test's own intent exactly. What §S4
    # requires is that the retry does not re-run what failed and is stronger.
    assert out["selected_model"] != failed
    assert tier[out["selected_model"]] > tier[failed]
    assert any("capability tier" in n for n in out["notes"]), out["notes"]


def test_s5_hard_logical_verification_routes_to_reasoning():
    out = r(
        task_class="INVESTIGATION",
        complexity=2, uncertainty=2, blast_radius=2, reversibility=0,
        reasoning_centric=True,
    )
    assert out["selected_role"] == "reasoning_specialist"
    assert band_at_least(out["review"]["band"], "MEDIUM")


def test_s6_difficult_code_centric_debugging():
    out = r(
        task_class="DEBUGGING",
        complexity=2, uncertainty=2, blast_radius=1, reversibility=0,
        reasoning_centric=False,
    )
    assert at_least(out["selected_role"], "worker_balanced")
    assert out["selected_role"] != "reasoning_specialist"


def test_s12_large_repetitive_generation_is_not_frontier_work():
    """Volume is not difficulty. 20 similar components stay on the cheap worker."""
    out = r(task_class="IMPLEMENTATION", complexity=1)
    assert out["risk_band"] == "LOW"
    assert out["selected_role"] == "worker_fast"
    assert out["review"]["band"] == "LOW"


def test_s13_high_uncertainty_tiny_diff_still_escalates():
    """Five lines, but nobody knows which five. Size is not the signal."""
    out = r(task_class="DEBUGGING", complexity=0, uncertainty=3, blast_radius=1)
    assert out["risk_score"] == 8
    assert out["risk_band"] == "HIGH"
    assert at_least(out["selected_role"], "worker_balanced")


# ---------------------------------------------------------------------------
# REGRESSION cases — the ones the previous policy got wrong
# ---------------------------------------------------------------------------

def test_s7_regression_auth_change_forces_dual_independent_review():
    """An auth change is small and well-understood, and still must not ship
    on a single lightweight review."""
    out = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=0,
            blast_radius=1, reversibility=0, flags=["auth_sensitive"])
    assert band_at_least(out["risk_band"], "HIGH")
    assert at_least(out["selected_role"], "worker_balanced")
    assert band_at_least(out["review"]["band"], "HIGH")
    assert len(out["review"]["reviewers"]) == 2
    assert out["review"]["independence_required"] is True


def test_s8_regression_auth_bug_unknown_root_cause():
    """The exact case the previous version dropped: DEBUGGING dispatched early
    and never reached the critical-flag override."""
    out = r(task_class="DEBUGGING", complexity=2, uncertainty=2,
            blast_radius=1, reversibility=0,
            flags=["auth_sensitive", "unknown_root_cause"])
    assert band_at_least(out["risk_band"], "HIGH")
    assert at_least(out["selected_role"], "worker_balanced")
    assert band_at_least(out["review"]["band"], "HIGH")
    assert len(out["review"]["reviewers"]) == 2
    assert out["review"]["independence_required"] is True
    assert "critical_domain" in out["band_overrides_applied"]


def test_s14_regression_score_exactly_10_is_unambiguously_high():
    """The previous version compared `>= 10` in one place and `high_risk_max: 10`
    in another, so a score of exactly 10 banded differently depending on which
    branch you arrived through."""
    seen = set()
    for task_class in TASK_CLASSES:
        out = r(task_class=task_class, complexity=2, uncertainty=2,
                blast_radius=2, reversibility=0)
        assert out["risk_score"] == 10
        seen.add(out["risk_band"])
    assert seen == {"HIGH"}, f"score 10 banded inconsistently across classes: {seen}"


def test_s15_regression_payments_architecture():
    """ARCHITECTURE also dispatched early in the previous version, so a payments
    design skipped the critical-domain override entirely."""
    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
            blast_radius=3, reversibility=2,
            flags=["financial_sensitive"])
    assert out["risk_band"] == "CRITICAL"
    assert out["selected_role"] == "principal_architect"
    assert out["review"]["band"] == "CRITICAL"
    assert len(out["review"]["reviewers"]) == 2
    assert out["review"]["independence_required"] is True
    assert set(out["review"]["required_checks"]) == {
        "security", "edge_cases", "rollback", "test_adequacy", "specification_compliance",
    }


# ---------------------------------------------------------------------------
# Scenario 9-11, 16-18
# ---------------------------------------------------------------------------

def test_s9_migration_touching_user_save_data():
    out = r(task_class="MIGRATION", complexity=3, uncertainty=2,
            blast_radius=3, reversibility=3,
            flags=["migration", "data_integrity_sensitive"])
    assert out["risk_band"] == "CRITICAL"
    assert out["selected_role"] == "principal_architect"
    assert "rollback" in out["review"]["required_checks"]
    assert "test_adequacy" in out["review"]["required_checks"]
    assert "migration_data_integrity" in out["band_overrides_applied"]


def test_s10_new_subsystem_with_maximum_uncertainty():
    out = r(task_class="ARCHITECTURE", complexity=2, uncertainty=3,
            blast_radius=1, reversibility=1)
    assert out["selected_role"] == "principal_architect"
    assert band_at_least(out["review"]["band"], "HIGH")


def test_s11_disagreement_judge_is_configured_and_bound():
    """Judge selection is policy, not runtime — assert the binding exists and
    that a CRITICAL route actually names it."""
    d = CFG["review"]["disagreement"]
    assert d["default_judge"] == "principal_architect"
    assert d["resolution"]["PASS+FAIL"] == "judge"
    assert d["resolution"]["FAIL+PASS"] == "judge"
    assert d["resolution"]["FAIL+FAIL"] == "reimplement"

    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
            blast_radius=3, reversibility=2, flags=["financial_sensitive"])
    # The architect is the implementer here, so it cannot also judge its own
    # work; with no higher tier free, adjudication goes to a human.
    assert out["review"]["judge_unavailable"] is True
    assert out["requires_human_confirmation"] is True


def test_s16_isolation_unavailable_is_a_disclosed_degradation():
    """The router must never claim independence it did not structurally get.
    Degradation is a runtime observation, so the policy's job is to make the
    degraded state representable and to require human confirmation."""
    assert CFG["human_in_the_loop"]["on_any_critical_review"] == "require_human_confirmation"
    assert CFG["human_in_the_loop"]["on_independence_unachievable"] == "terminal"
    out = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=3,
            blast_radius=3, reversibility=2, flags=["security_sensitive"])
    assert out["review"]["independence_required"] is True  # what the policy asks for
    # ...and the runtime must downgrade the *claim*, not the requirement.


def test_s17_unresolvable_model_degrades_instead_of_failing():
    out = r(task_class="INVESTIGATION", complexity=2, uncertainty=2,
            blast_radius=2, unavailable_roles=["reasoning_specialist"])
    assert out["selected_model"], "a route was still emitted"
    assert out["fallbacks_applied"], "the fallback was recorded"
    assert any("reasoning_specialist" in f for f in out["fallbacks_applied"])


def test_s18_both_runtimes_agree_on_role_effort_and_review_band():
    """Parity: given identical inputs and capability, only the resolved model
    ids may differ between runtimes."""
    kw = dict(task_class="DEBUGGING", complexity=2, uncertainty=3,
              blast_radius=2, reversibility=1, flags=["auth_sensitive"])
    outs = [r(runtime=rt, **kw) for rt in sorted(CFG["runtimes"])]
    first = outs[0]
    for other in outs[1:]:
        assert other["selected_role"] == first["selected_role"]
        assert other["selected_effort"] == first["selected_effort"]
        assert other["review"]["band"] == first["review"]["band"]
        assert other["review"]["reviewers"] == first["review"]["reviewers"]
        assert other["risk_band"] == first["risk_band"]
        # Round 21: the native spelling used to be the one field allowed to differ
        # between runtimes. It is now keyed by the family of the model that runs,
        # and every runtime resolves the same worker under the shared binding —
        # so it is equal too, and asserting that is what keeps this honest.
        assert other["selected_effort_native"] == first["selected_effort_native"]


# ---------------------------------------------------------------------------
# Structural invariants — asserted over the whole input space, not one example
# ---------------------------------------------------------------------------

ALL_DIMS = [(c, u, b, rev)
            for c in (0, 3) for u in (0, 3) for b in (0, 3) for rev in (0, 3)]


def test_i1_every_task_reaches_review_selection():
    for task_class in TASK_CLASSES:
        for c, u, b, rev in ALL_DIMS:
            out = r(task_class=task_class, complexity=c, uncertainty=u,
                    blast_radius=b, reversibility=rev)
            assert out["review"]["reviewers"], f"{task_class} {c}{u}{b}{rev} got no reviewer"
            assert out["review"]["band"] in BANDS


@pytest.mark.parametrize("flag", [
    "security_sensitive", "auth_sensitive",
    "financial_sensitive", "data_integrity_sensitive",
])
def test_i2_critical_flag_implies_review_band_at_least_high(flag):
    """Verified for every task class, including the two the previous version
    let slip through."""
    for task_class in TASK_CLASSES:
        out = r(task_class=task_class, flags=[flag])
        assert band_at_least(out["review"]["band"], "HIGH"), (
            f"{task_class} with {flag} produced review band {out['review']['band']}"
        )
        assert out["review"]["independence_required"] is True


@pytest.mark.parametrize("flag", [
    "security_sensitive", "auth_sensitive",
    "financial_sensitive", "data_integrity_sensitive",
])
def test_i3_critical_flag_implies_worker_at_least_balanced(flag):
    for task_class in TASK_CLASSES:
        out = r(task_class=task_class, flags=[flag])
        assert at_least(out["selected_role"], "worker_balanced"), (
            f"{task_class} with {flag} routed to {out['selected_role']}"
        )


def test_i4_emitted_routes_name_only_known_models():
    known = {m["id"] for m in CFG["models"].values()}
    for task_class in TASK_CLASSES:
        for c, u, b, rev in ALL_DIMS:
            out = r(task_class=task_class, complexity=c, uncertainty=u,
                    blast_radius=b, reversibility=rev)
            assert out["selected_model"] in known
            for m in out["review"]["reviewer_models"]:
                assert m in known


def test_i5_rationale_names_band_flags_and_fallbacks():
    out = r(task_class="MIGRATION", complexity=3, uncertainty=2,
            blast_radius=3, reversibility=3,
            flags=["migration", "data_integrity_sensitive"],
            unavailable_roles=["principal_architect"])
    text = out["rationale"]
    assert out["risk_band"] in text
    assert "data_integrity_sensitive" in text
    assert "Fallbacks:" in text
    assert "migration_data_integrity" in text


# ---------------------------------------------------------------------------
# Policy consistency — the config must not contradict itself
# ---------------------------------------------------------------------------

def test_bands_are_contiguous_and_exhaustive():
    ranges = [CFG["router"]["bands"][b] for b in BANDS]
    assert ranges[0]["min"] == 0
    assert ranges[-1]["max"] == 18
    for lower, upper in zip(ranges, ranges[1:]):
        assert upper["min"] == lower["max"] + 1, "bands must be contiguous, no gap or overlap"


def test_every_task_class_has_a_row_for_every_band():
    for task_class in TASK_CLASSES:
        row = CFG["worker_selection"][task_class]
        assert set(row) == set(BANDS), f"{task_class} is missing a band"


def test_every_role_resolves_in_every_binding():
    for name, binding in CFG["role_bindings"].items():
        for role in ROLES:
            assert role in binding, f"binding {name} has no entry for {role}"
            assert binding[role] in CFG["models"], f"{name}.{role} names an unknown model"


def test_effort_maps_cover_every_conceptual_level():
    families = {m["family"] for m in CFG["models"].values()}
    assert set(CFG["effort_map"]) == families, (
        "effort_map is keyed by model family; every family needs a spelling")
    for family, mapping in CFG["effort_map"].items():
        assert set(mapping) == set(CFG["effort_levels"]), (
            f"{family} does not spell every conceptual level")


def test_review_policy_exists_for_every_band():
    for band in BANDS:
        assert band in CFG["review"], f"no review policy for band {band}"


def test_high_and_critical_reviews_are_cross_family_by_construction():
    """Dual review is only worth its cost if the two reviewers fail differently.
    Bind both to one family and the second review is near-redundant."""
    binding = CFG["role_bindings"]["default"]
    fams = {
        role: CFG["models"][binding[role]]["family"]
        for role in ("senior_engineer", "reasoning_specialist")
    }
    assert len(set(fams.values())) == 2, f"HIGH/CRITICAL reviewers share a family: {fams}"


def test_ledger_does_not_overclaim():
    """Any entry marked plainly `verified` must carry evidence, the never-probed
    isolation item must not be marked verified, and the worker binding entries
    must not claim a quality *win*: the 2026-08-18 head-to-head measured haiku
    marginally ahead of luna and a tie at ceiling for the balanced pair, so
    both bindings still rest on price, with quality probed rather than proven."""
    entries = {e["item"]: e for e in CFG["verification_ledger"]["entries"]}
    for item, e in entries.items():
        assert e.get("evidence"), f"{item} claims a status with no evidence"
    codex_isolation = next(k for k in entries if "Codex native subagent" in k)
    assert entries[codex_isolation]["status"] != "verified"
    grok_isolation = next(k for k in entries if "grok native subagent" in k)
    assert entries[grok_isolation]["status"] != "verified"
    for binding in ("worker_fast binding", "worker_balanced binding"):
        key = next(k for k in entries if binding in k)
        assert entries[key]["status"] == "price_verified_quality_probed"


def test_grok_hosted_bridge_facts():
    """Issue #16: the grok-hosted bridged directions are verified, to the
    same ledger standard as the Claude Code -> xai seat; the native surface
    and the codex-hosted direction are not, and must not ride along."""
    t = CFG["transports"]
    assert t["grok"]["to_claude"]["verified"] is True
    assert t["grok"]["to_openai"]["verified"] is True
    assert t["grok"]["to_claude"]["isolation"] == "separate_process"
    assert t["grok"]["to_openai"]["isolation"] == "separate_process"
    assert t["grok"]["native"]["isolation"] == "unverified"
    assert t["claude_code"]["to_xai"]["verified"] is True
    assert t["codex"]["to_xai"]["verified"] is False
    entries = {e["item"]: e for e in CFG["verification_ledger"]["entries"]}
    for item in ("grok -> Claude transport", "grok -> OpenAI transport"):
        e = entries[item]
        assert e["status"] == "verified"
        assert "darwin" in e["evidence"]
        assert re.search(r"grok \d+\.\d+\.\d+", e["evidence"])
        assert str(e["probed_on"]) >= "2026-08-29"
        assert re.search(r"g16-r\d+-", e["evidence"]), item
    assert entries["grok native subagent isolation"]["status"] == (
        "flag_verified_semantics_unverified")
    assert entries[
        "Grok reviewer seat recipe (transports.*.to_xai.mechanism_reviewer)"
    ]["status"] == "verified"


# ---------------------------------------------------------------------------
# Large context — the one deterministic fact that moves the worker_balanced
# binding (audit 2026-08-18 §2.2)
#
# xAI bills the whole request at the long-context tier once the prompt reaches
# 200K tokens, so the seat that is cheaper below that line is more expensive on
# both axes above it — and its window ends at 500K where the alt seat's runs to
# 1M. `large_context` was declared in `flags.context` and read by nothing.
# ---------------------------------------------------------------------------

def _balanced_task(**kw):
    """A route whose worker is `worker_balanced` under the default binding.

    Declared `read_only` so the seat under test is reachable at all. This
    section is about `worker_balanced_selection` — which of the two balanced
    seats a flag prefers — and on a WRITE route the xai primary is not a
    candidate on this host in the first place (issue #19: `to_xai` ships no
    maker recipe), so the comparison would have nothing to compare. The rule
    stays live for read-only seats and, natively, on the grok host.
    """
    kw.setdefault("task_class", "REFACTORING")
    kw.setdefault("complexity", 2)
    kw.setdefault("uncertainty", 1)
    kw.setdefault("blast_radius", 1)
    kw.setdefault("worker_seat", "read_only")
    return r(**kw)


SEL = CFG["worker_balanced_selection"]
PRIMARY_ID = CFG["models"][CFG["role_bindings"]["default"][SEL["prefer"]]]["id"]
ALT_ID = CFG["models"][CFG["role_bindings"]["default"][SEL["alt"]]]["id"]


def test_worker_balanced_defaults_to_the_primary_seat():
    out = _balanced_task()
    assert out["selected_role"] == "worker_balanced"
    assert out["selected_model"] == PRIMARY_ID


def test_large_context_moves_worker_balanced_to_the_alt_seat():
    out = _balanced_task(flags=["large_context"])
    assert out["selected_role"] == "worker_balanced"
    assert out["selected_model"] == ALT_ID


def test_large_context_swap_is_not_recorded_as_a_fallback():
    """Nothing became unavailable — recording scarcity that did not happen is
    the defect this module's docstring names first."""
    out = _balanced_task(flags=["large_context"])
    assert not [f for f in out["fallbacks_applied"] if "worker_balanced:" in f]
    assert out["fallback_compensations_applied"] == []


def test_large_context_falls_back_to_the_primary_when_the_alt_is_gone():
    """With the alt unavailable the primary is the answer again — and THAT is a
    real fallback, so it is recorded."""
    out = _balanced_task(flags=["large_context"], unavailable_models=[ALT_ID])
    assert out["selected_model"] == PRIMARY_ID
    assert any("worker_balanced:" in f for f in out["fallbacks_applied"])


def test_large_context_is_a_no_op_under_a_degraded_binding():
    """`claude_only` names no alt seat, so there is nothing to prefer; the rule
    must not invent one."""
    a = _balanced_task(flags=["bridge_down"])
    b = _balanced_task(flags=["bridge_down", "large_context"])
    assert a["selected_model"] == b["selected_model"]


def test_large_context_rule_is_actually_consumed():
    """A perturbation, not a read: delete the rule and the route must move
    back. `test_d14` only proves a key was looked at."""
    without = {**CFG, "worker_balanced_selection": {
        k: v for k, v in SEL.items() if k != "prefer_alt_when_flags"}}
    out = route(Task(task_class="REFACTORING", complexity=2, uncertainty=1,
                     blast_radius=1, reversibility=0, flags=["large_context"],
                     worker_seat="read_only"),
                without)
    assert out["selected_model"] == PRIMARY_ID


def test_prefer_alt_when_flags_is_a_list_and_the_singular_key_is_gone():
    """Two flags, one list. Keeping the old singular key beside it would be
    two places holding one policy — the drift this config's header forbids."""
    assert "prefer_alt_when_flag" not in SEL
    assert isinstance(SEL["prefer_alt_when_flags"], list)
    assert SEL["prefer_alt_when_flags"] == ["large_context", "latency_sensitive"]


def test_latency_sensitive_moves_worker_balanced_to_the_alt_seat():
    out = _balanced_task(flags=["latency_sensitive"])
    assert out["selected_role"] == "worker_balanced"
    assert out["selected_model"] == ALT_ID


def test_latency_sensitive_swap_is_not_recorded_as_a_fallback():
    """Nothing became unavailable — recording scarcity that did not happen is
    the defect this module's docstring names first."""
    out = _balanced_task(flags=["latency_sensitive"])
    assert not [f for f in out["fallbacks_applied"] if "worker_balanced:" in f]
    assert out["fallback_compensations_applied"] == []


def test_latency_sensitive_falls_back_to_the_primary_when_the_alt_is_gone():
    """With the alt unavailable the primary is the answer again — and THAT is a
    real fallback, so it is recorded."""
    out = _balanced_task(flags=["latency_sensitive"], unavailable_models=[ALT_ID])
    assert out["selected_model"] == PRIMARY_ID
    assert any("worker_balanced:" in f for f in out["fallbacks_applied"])


def test_latency_sensitive_is_a_no_op_under_a_degraded_binding():
    """`claude_only` names no alt seat, so there is nothing to prefer; the rule
    must not invent one."""
    a = _balanced_task(flags=["bridge_down"])
    b = _balanced_task(flags=["bridge_down", "latency_sensitive"])
    assert a["selected_model"] == b["selected_model"]


def test_latency_sensitive_rule_is_actually_consumed():
    """A perturbation, not a read: delete the list and the route must move
    back. `test_d14` only proves a key was looked at."""
    without = {**CFG, "worker_balanced_selection": {
        k: v for k, v in SEL.items() if k != "prefer_alt_when_flags"}}
    out = route(Task(task_class="REFACTORING", complexity=2, uncertainty=1,
                     blast_radius=1, reversibility=0, flags=["latency_sensitive"],
                     worker_seat="read_only"),
                without)
    assert out["selected_model"] == PRIMARY_ID


def test_both_prefer_alt_flags_select_the_same_alt_seat():
    """The two flags name the same swap. Carrying both is composition, not a
    conflict, and still must not be recorded as scarcity."""
    a = _balanced_task(flags=["large_context"])
    b = _balanced_task(flags=["latency_sensitive"])
    both = _balanced_task(flags=["large_context", "latency_sensitive"])
    assert a["selected_model"] == ALT_ID
    assert b["selected_model"] == ALT_ID
    assert both["selected_model"] == ALT_ID
    assert not [f for f in both["fallbacks_applied"] if "worker_balanced:" in f]


def test_prefer_alt_flags_rebind_the_role_including_a_reviewer_seat():
    """The flags rebind the `worker_balanced` ROLE, not a review-policy path.
    A MEDIUM `worker_fast` implementer keeps its worker; the reviewer that
    holds `worker_balanced` moves to the alt, and both flags do the same
    swap. Reviewer-only latency preference would be a different rule."""
    kw = dict(task_class="MECHANICAL", complexity=0, uncertainty=0,
              blast_radius=1, reversibility=2)
    base = r(**kw)
    latency = r(**kw, flags=["latency_sensitive"])
    large = r(**kw, flags=["large_context"])
    assert base["selected_role"] == "worker_fast"
    assert base["selected_model"] == latency["selected_model"] == large["selected_model"]
    assert base["review"]["reviewers"] == ["worker_balanced"]
    assert base["review"]["reviewer_models"] == [PRIMARY_ID]
    assert latency["review"]["reviewer_models"] == [ALT_ID]
    assert large["review"]["reviewer_models"] == [ALT_ID]
    assert not [f for f in latency["fallbacks_applied"] if "worker_balanced:" in f]


def test_long_context_pricing_is_recorded_where_the_price_is():
    """The registry's `price_per_mtok` is documentation, and documentation that
    states only the cheap half of a tiered price misleads exactly the reader it
    exists for."""
    xai = next(m for m in CFG["models"].values() if m["family"] == "xai")
    tier = xai["price_per_mtok"]["long_context"]
    assert tier["above_input_tokens"] == 200_000
    assert tier["input"] > xai["price_per_mtok"]["input"]
    assert tier["output"] > xai["price_per_mtok"]["output"]


def test_long_context_blocks_are_well_formed():
    """design §3 A1: long_context를 가진 모든 모델은 boundary 어휘와 양수
    요율을 갖추고, 장문맥 요율은 표준 요율보다 낮지 않다. 값 자체의 외부
    정확성은 ledger의 재검증 의무가 담당한다 — 가격을 여기 복제하면 두 번째
    진실 소스가 된다."""
    seen = 0
    for key, model in CFG["models"].items():
        lc = model.get("price_per_mtok", {}).get("long_context")
        if lc is None:
            continue
        seen += 1
        assert lc["boundary"] in ("inclusive", "exclusive"), key
        assert isinstance(lc["above_input_tokens"], int) and lc["above_input_tokens"] > 0, key
        std = model["price_per_mtok"]
        for axis in ("input", "output"):
            assert lc[axis] >= std[axis] > 0, (key, axis)
        if "cached_input" in std:
            assert lc["cached_input"] >= std["cached_input"] > 0, key
    assert seen >= 4, "xai + three openai models must carry a long_context tier"
