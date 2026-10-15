"""A model that failed is eligibility, not scarcity (issue #70, 1.23.1).

`prior_failures` already charges a retry for the failure. 1.23.0 charged it a
second time, as a scarcity fallback, whenever the seat that replaced the failed
one was reached through a role's fallback list: once Sonnet 5.5 moved to tier 2
(#65) the retry ladder's weakest role above tier 1 became the balanced role,
whose own binding is the xai seat that had just failed. The retry sat at 0.54
instead of the 0.60 threshold and asked to be re-routed.

The rule under test (`Resolver._retry_baseline`): when the baseline a role is
compared against is a model that ONLY failed, it moves to the first seat the
role's `_candidates` ladder offers that did not only fail. A model that failed
AND is withheld right now (the caller's `unavailable_models`, an exhausted
family, a local revocation) is a real supply observation and keeps counting; so
does a withheld successor. The ladder is read in the policy's order, before the
DD-B8 low-quota swap; the swap is exempt only when the seat it moved off was
usable. Compensation reads the old baseline, as before, and its note names the
retry, the outage, or both (`Resolver.retry_compensations`).

Two layers: direct `Resolver` controls that pin one channel each, and routes.

Run:  python3 -m pytest skills/model-router/tests/test_retry_eligibility.py -q
"""

import copy
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

import route_task as rt  # noqa: E402
from route_task import Policy, SupplyExhausted, Task, route  # noqa: E402

CFG = rt.load_config()
POLICY = Policy.of(CFG)
ID = lambda key: CFG["models"][key]["id"]                        # noqa: E731
XAI, SON, OPUS = ID("xai_frontier"), ID("claude_worker_balanced"), ID("claude_senior")
FABLE, SOL, ASTRA = ID("claude_architect"), ID("openai_reasoning"), ID("openai_frontier")
MUSE, TERRA = ID("meta_worker_balanced"), ID("openai_worker_balanced")
LUNA, TERRA_FAST = ID("openai_worker_fast"), ID("openai_worker_balanced")
GEMINI = ID("gemini_worker_balanced")
ARCH_COMP = "raise_effort_to_MAX_and_add_second_review"
SCARCITY = "worker_balanced: {} unavailable -> {}".format
DEGRADED = "binding degraded to claude_only (cross-provider bridge down)"


def resolve(roles, *, task_class="DEBUGGING", write_role=None, writes=False,
            runtime="claude_code", cfg=CFG, **task):
    """(resolved, fallbacks, compensations), or "exhausted"."""
    t = Task(task_class=task_class, complexity=0, uncertainty=0, blast_radius=0,
             reversibility=0, runtime=runtime, **task)
    resolver = rt.Resolver(t, Policy.of(cfg))
    resolver.worker_writes = writes
    try:
        return resolver.resolve(roles, write_role=write_role)
    except SupplyExhausted:
        return "exhausted"


def failed(*ids, **task):
    return dict(prior_failures=len(ids), prior_models=list(ids), **task)


def with_local_revocation(*ids):
    cfg = copy.deepcopy(CFG)
    cfg["local_state"] = {"blocked_ids": list(ids)}
    return cfg


# ---------------------------------------------------------------------------
# Direct Resolver controls: one channel each
# ---------------------------------------------------------------------------

def test_a_seat_that_only_failed_is_not_a_scarcity_fallback():
    assert resolve(["worker_balanced"], **failed(XAI)) == ({"worker_balanced": SON}, [], [])


@pytest.mark.parametrize("withheld", [
    pytest.param(dict(unavailable_models=[XAI]), id="caller_unavailable"),
    pytest.param(dict(unavailable_roles=["worker_balanced"]), id="role_binding_unavailable"),
    pytest.param(dict(_family_quota={"xai": "exhausted"}), id="family_quota_exhausted"),
    pytest.param(dict(cfg=with_local_revocation(XAI)), id="local_revocation"),
])
def test_a_failed_seat_that_is_also_withheld_keeps_the_real_scarcity(withheld):
    """A caller's declaration is a CURRENT supply observation, not a redundant
    encoding of the history: the failure must not launder it."""
    out = resolve(["worker_balanced"], **failed(XAI), **withheld)
    assert out == ({"worker_balanced": SON}, [SCARCITY(XAI, SON)], [])


def test_a_withheld_successor_of_a_failed_seat_is_still_counted():
    out = resolve(["worker_balanced"], **failed(XAI), unavailable_models=[SON])
    assert out[1] == [SCARCITY(SON, MUSE)] and out[0] == {"worker_balanced": MUSE}


def test_a_failed_and_withheld_successor_is_still_counted():
    """The failed-only primary is followed by a successor that failed AND is
    withheld: that successor is the baseline, and it is genuinely missing."""
    out = resolve(["worker_balanced"], **failed(XAI, SON), unavailable_models=[SON])
    assert out[1] == [SCARCITY(SON, MUSE)]


def test_a_withheld_primary_followed_by_a_failed_only_successor_is_counted_at_the_primary():
    out = resolve(["worker_balanced"], **failed(XAI, SON), unavailable_models=[XAI])
    assert out[1] == [SCARCITY(XAI, MUSE)]


def test_several_failures_move_the_baseline_past_all_of_them():
    assert resolve(["worker_balanced"], **failed(XAI, SON)) == (
        {"worker_balanced": MUSE}, [], [])


def test_nothing_left_after_the_failures_is_still_a_supply_shortage():
    everyone = [m["id"] for m in CFG["models"].values()]
    assert resolve(["worker_balanced"], **failed(*everyone)) == "exhausted"


def test_an_unrelated_reviewer_outage_is_still_recorded_next_to_a_failed_seat():
    out = resolve(["worker_balanced", "senior_engineer"], **failed(XAI),
                  unavailable_models=[OPUS])
    assert out[1] == [f"senior_engineer: {OPUS} unavailable -> {SOL}"]
    assert out[0] == {"worker_balanced": SON, "senior_engineer": SOL}


def test_the_failure_of_a_reviewer_seat_alone_is_not_a_fallback_either():
    out = resolve(["senior_engineer"], **failed(OPUS))
    assert out[1] == [] and out[0]["senior_engineer"] != OPUS


def test_a_degraded_binding_keeps_its_note_and_a_failed_seat_adds_nothing():
    """bridge_down degrades the binding (a note); the failed degraded seat is
    replaced without a second record."""
    plain = resolve(["worker_balanced"], flags=["bridge_down"])
    assert plain[1] == [DEGRADED]
    out = resolve(["worker_balanced"], flags=["bridge_down"], **failed(SON))
    assert out[1] == [DEGRADED] and out[0]["worker_balanced"] == OPUS


def test_a_same_model_retry_is_not_a_failure_for_the_baseline_or_the_seat():
    out = resolve(["worker_balanced"], **failed(XAI),
                  _same_model_retry={"model": XAI, "effort": "HIGH"})
    assert out == ({"worker_balanced": XAI}, [], [])
    # Every OTHER failed model still counts as failed for the seat.
    out = resolve(["worker_balanced"], **failed(XAI, SON),
                  _same_model_retry={"model": XAI, "effort": "HIGH"})
    assert out == ({"worker_balanced": XAI}, [], [])


def test_the_preferred_alt_seat_that_failed_is_not_a_fallback():
    """`large_context` prefers the alt seat (Sonnet) over the xai binding."""
    assert resolve(["worker_balanced"], flags=["large_context"], **failed(SON)) == (
        {"worker_balanced": XAI}, [], [])
    # The same seat merely withheld is an outage.
    assert resolve(["worker_balanced"], flags=["large_context"], unavailable_models=[SON])[1] == [
        SCARCITY(SON, XAI)]


# --- author / write / allowed-family eligibility is the ladder's, not ours ---

def test_the_author_boundary_decides_the_baseline_before_the_failure_does():
    """The xai seat is the author's family; the first eligible seat is Sonnet,
    which failed. The baseline is the next eligible seat, not Sonnet."""
    ctx = {"author_model_ids": [XAI]}
    out = resolve(["worker_balanced"], **failed(SON), _review_context=ctx)
    assert out == ({"worker_balanced": MUSE}, [], [])
    # Withheld rather than failed: the same seat IS missing.
    out = resolve(["worker_balanced"], unavailable_models=[SON], _review_context=ctx)
    assert out[1] == [SCARCITY(SON, MUSE)]


def test_a_seat_with_no_write_recipe_is_never_the_baseline_of_a_write_seat():
    """On Codex the xai seat has no write recipe: the baseline is Sonnet. When
    Sonnet failed the write seat is compared against the next WRITE-CAPABLE
    seat, and the non-writing ones in between are not offered."""
    out = resolve(["worker_balanced"], write_role="worker_balanced", writes=True,
                  runtime="codex", **failed(SON))
    assert out == ({"worker_balanced": TERRA}, [], [])
    # The xai failure is irrelevant to a seat that could never be xai.
    assert resolve(["worker_balanced"], write_role="worker_balanced", writes=True,
                   runtime="codex", **failed(XAI)) == ({"worker_balanced": SON}, [], [])


def test_a_family_the_caller_never_offered_is_not_a_baseline():
    """allowed_families is eligibility. With only Claude offered the xai seat
    was never going to run: a failure of it adds nothing to say. (Without the
    failure the recorded 1.23.0 line stays — the baseline is the binding.)"""
    lp = {"allowed_families": ["claude"]}
    assert resolve(["worker_balanced"], _local_policy=lp)[1] == [SCARCITY(XAI, SON)]
    assert resolve(["worker_balanced"], _local_policy=lp, **failed(XAI))[1] == []


def test_the_promotion_floor_ladder_decides_the_baseline():
    """INVESTIGATION + unknown_root_cause floors the balanced role at its
    binding's tier for the worker seat (only on a first attempt, so a real
    request never has both a floor and a failed model: the history is injected
    here to hold the ladder's choice). The baseline walks the FLOORED ladder
    (`_candidates`); a hand-built raw ladder would name the tier-0 seat in
    between and record a fallback that is not one."""
    kw = dict(task_class="INVESTIGATION", write_role="worker_balanced",
              flags=["unknown_root_cause"])
    assert resolve(["worker_balanced"], **kw, prior_failures=0, prior_models=[XAI, SON]) == (
        {"worker_balanced": TERRA}, [], [])
    # Withheld instead of failed, the same floor still records the real outage.
    assert resolve(["worker_balanced"], **kw, unavailable_models=[XAI, SON])[1] == [
        SCARCITY(XAI, TERRA)]


# --- low-quota: a binding decision, never a fallback ---

LOW_CLAUDE = dict(write_role="worker_balanced", _family_quota={"claude": "low"})


def test_a_low_quota_swap_after_a_failed_seat_is_no_fake_fallback():
    """Failed xai, Sonnet's family low on quota: the seat moves to the first
    same-tier seat of another family. The reordered ladder starts there."""
    assert resolve(["worker_balanced"], **LOW_CLAUDE, **failed(XAI)) == (
        {"worker_balanced": SOL}, [], [])


def test_a_withheld_primary_with_a_low_quota_swap_still_records_the_outage():
    out = resolve(["worker_balanced"], **LOW_CLAUDE, unavailable_models=[XAI])
    assert out[1] == [SCARCITY(XAI, SOL)]


def test_a_withheld_successor_behind_a_failed_seat_with_low_quota_still_counts():
    out = resolve(["worker_balanced"], **LOW_CLAUDE, **failed(XAI), unavailable_models=[SON])
    assert out[1] == [SCARCITY(SON, MUSE)]


# The swap reorders the ladder the seat is chosen from. The baseline walks the
# ladder BEFORE the swap: the swap is exempt only when the seat it moved off
# was usable (a binding decision), never when that seat was withheld (an
# outage the swap merely happened to land past).

LOW_META = dict(write_role="worker_balanced", _family_quota={"meta": "low"})


def test_a_low_quota_swap_does_not_hide_a_withheld_successor():
    """Failed xai, Sonnet withheld, Meta low: the seat lands on the Meta seat's
    same-tier swap. Sonnet is still missing, and the record says so — exactly
    as it does when no quota is low and the seat lands on Muse."""
    assert resolve(["worker_balanced"], write_role="worker_balanced", **failed(XAI),
                   unavailable_models=[SON]) == (
        {"worker_balanced": MUSE}, [SCARCITY(SON, MUSE)], [])
    assert resolve(["worker_balanced"], **LOW_META, **failed(XAI), unavailable_models=[SON]) == (
        {"worker_balanced": GEMINI}, [SCARCITY(SON, GEMINI)], [])


def test_a_low_quota_swap_off_a_usable_successor_stays_a_binding_decision():
    """Both failures are history only: the first seat that did not fail (Muse)
    is usable, so moving off it for quota is the DD-B8 decision, not an outage."""
    assert resolve(["worker_balanced"], **LOW_META, **failed(XAI, SON)) == (
        {"worker_balanced": GEMINI}, [], [])
    # Control: the low family is not reached at all when Sonnet is available.
    assert resolve(["worker_balanced"], **LOW_META, **failed(XAI)) == (
        {"worker_balanced": SON}, [], [])


def test_a_low_quota_swap_on_a_review_seat_that_writes_keeps_the_withheld_successor():
    """Opus failed, Sol withheld, Claude low: the senior seat swaps from Fable
    to Astra at tier 3. Sol is the seat that is really missing."""
    kw = dict(write_role="senior_engineer", **failed(OPUS), unavailable_models=[SOL])
    assert resolve(["senior_engineer"], **kw) == (
        {"senior_engineer": FABLE}, [f"senior_engineer: {SOL} unavailable -> {FABLE}"], [])
    assert resolve(["senior_engineer"], **kw, _family_quota={"claude": "low"}) == (
        {"senior_engineer": ASTRA}, [f"senior_engineer: {SOL} unavailable -> {ASTRA}"], [])


def test_a_codex_write_seat_low_quota_swap_keeps_the_withheld_successor():
    """On Codex the write ladder is Sonnet, Terra, Sol, Opus. Sonnet failed and
    Terra is withheld; the OpenAI family low moves the seat from Sol to Opus.
    Terra is the baseline either way."""
    kw = dict(write_role="worker_balanced", writes=True, runtime="codex",
              **failed(SON), unavailable_models=[TERRA])
    assert resolve(["worker_balanced"], **kw, _family_quota={"openai": "low"}) == (
        {"worker_balanced": OPUS}, [SCARCITY(TERRA, OPUS)], [])
    assert resolve(["worker_balanced"], **kw, _family_quota={"claude": "low"}) == (
        {"worker_balanced": SOL}, [SCARCITY(TERRA, SOL)], [])


def test_the_author_boundary_and_a_low_quota_swap_compose_on_the_pre_swap_ladder():
    """xai is the author's family, Sonnet failed, the three seats behind it
    withheld, Claude low: the seat swaps from Opus to Sol. The first seat the
    author boundary offers that did not fail is Muse, and Muse is missing."""
    ctx = {"author_model_ids": [XAI]}
    kw = dict(write_role="worker_balanced", _review_context=ctx, **failed(SON),
              unavailable_models=[MUSE, GEMINI, TERRA])
    assert resolve(["worker_balanced"], **kw) == (
        {"worker_balanced": OPUS}, [SCARCITY(MUSE, OPUS)], [])
    assert resolve(["worker_balanced"], **kw, _family_quota={"claude": "low"}) == (
        {"worker_balanced": SOL}, [SCARCITY(MUSE, SOL)], [])


def test_the_author_and_write_baselines_without_a_capability_failure_are_1_23_0():
    """Bounded compatibility control. The author and write-eligibility steps
    read the first eligible seat the way 1.23.0 did (the swapped ladder) on
    every route with no capability failure, and on a route whose failure sits
    elsewhere on the ladder. Issue #70 keeps such routes as they are; that a
    low quota leaves the withheld Sonnet unrecorded on them is known and out
    of scope (docs/research/issue-70/red-quota-baseline.txt), not fixed here.
    Only a first eligible seat that merely FAILED reads the pre-swap ladder."""
    codex = dict(write_role="worker_balanced", writes=True, runtime="codex",
                 unavailable_models=[SON, TERRA], _family_quota={"openai": "low"})
    unchanged = ({"worker_balanced": OPUS}, [], [])
    assert resolve(["worker_balanced"], **codex) == unchanged
    assert resolve(["worker_balanced"], **codex, **failed(FABLE)) == unchanged
    author = dict(write_role="worker_balanced", _review_context={"author_model_ids": [XAI]},
                  unavailable_models=[SON, MUSE, GEMINI, TERRA], _family_quota={"claude": "low"})
    unchanged = ({"worker_balanced": SOL}, [], [])
    assert resolve(["worker_balanced"], **author) == unchanged
    assert resolve(["worker_balanced"], **author, **failed(FABLE)) == unchanged
    # A swap off a seat that IS usable stays a binding decision in both steps.
    assert resolve(["worker_balanced"], write_role="worker_balanced", writes=True,
                   runtime="codex", _family_quota={"claude": "low"}) == (
        {"worker_balanced": SOL}, [], [])


# --- compensation reads the old baseline ---

def test_the_architect_downgrade_is_still_compensated_without_a_fallback_record():
    """Fable and Astra failed: the architect role lands on Opus (tier 2 < 3).
    No scarcity fallback — and the second review is still owed."""
    out = resolve(["principal_architect"], **failed(FABLE, ASTRA))
    assert out == ({"principal_architect": OPUS}, [], [ARCH_COMP])


def test_the_reasoning_specialist_family_loss_is_still_compensated():
    out = resolve(["reasoning_specialist", "senior_engineer"], **failed(SOL, ASTRA))
    assert out[0]["reasoning_specialist"] == OPUS and out[1] == []
    assert out[2] == ["raise_effort_one_level"]


def test_a_downgrade_that_the_caller_also_withheld_keeps_record_and_compensation():
    out = resolve(["principal_architect"], **failed(FABLE, ASTRA), unavailable_models=[FABLE])
    assert out[1] == [f"principal_architect: {FABLE} unavailable -> {OPUS}"]
    assert out[2] == [ARCH_COMP]


def test_the_compensation_baseline_is_the_1_23_0_effective_one_under_author_write_and_low_quota():
    """Reproduced against the 1.23.0 archive. Fable is the author's model, Astra
    merely failed, Claude is low and the architect seat writes: 1.23.0 compared
    the seat against the first seat of the SWAPPED ladder (Sol itself) and owed
    nothing. The retry's fallback baseline reads the pre-swap ladder; the
    compensation baseline must not move with it."""
    kw = dict(write_role="principal_architect", _review_context={"author_model_ids": [FABLE]},
              _family_quota={"claude": "low"})
    assert resolve(["principal_architect"], **kw, **failed(ASTRA)) == (
        {"principal_architect": SOL}, [], [])
    # Astra withheld instead: the same 1.23.0 answer, nothing to exempt.
    assert resolve(["principal_architect"], **kw, unavailable_models=[ASTRA]) == (
        {"principal_architect": SOL}, [], [])
    # The reasoning seat under the author boundary: 1.23.0 owed the same
    # compensation, and recorded the failed Astra as a fallback (the #70
    # double charge). The compensation stays; only that record goes.
    out = resolve(["reasoning_specialist", "senior_engineer"], write_role="reasoning_specialist",
                  _review_context={"author_model_ids": [SOL]}, _family_quota={"openai": "low"},
                  **failed(ASTRA))
    assert out == ({"reasoning_specialist": OPUS, "senior_engineer": OPUS}, [],
                   ["raise_effort_one_level"])


def test_a_failed_architect_replaced_at_its_own_tier_needs_no_compensation():
    assert resolve(["principal_architect"], **failed(FABLE)) == (
        {"principal_architect": ASTRA}, [], [])


def _resolver(**task):
    t = Task(task_class="DEBUGGING", complexity=0, uncertainty=0, blast_radius=0,
             reversibility=0, runtime="claude_code", **task)
    return rt.Resolver(t, POLICY)


def test_the_compensation_is_attributed_to_the_retry_when_no_fallback_is_recorded():
    resolver = _resolver(**failed(FABLE, ASTRA))
    resolver.resolve(["principal_architect"])
    assert resolver.retry_compensations == {ARCH_COMP: "retry"}
    # A downgrade no failure explains is not attributed to one.
    resolver = _resolver(unavailable_models=[FABLE, ASTRA])
    assert resolver.resolve(["principal_architect"])[2] == [ARCH_COMP]
    assert resolver.retry_compensations == {}


def test_a_retry_and_a_withheld_successor_both_explain_the_compensation():
    """Sol failed (history), Astra withheld (outage): the reasoning seat lands
    on Opus next to the Opus senior. The outage is recorded against Astra, the
    compensation is still owed, and its cause names BOTH, not the retry alone."""
    resolver = _resolver(**failed(SOL), unavailable_models=[ASTRA])
    out = resolver.resolve(["reasoning_specialist", "senior_engineer"])
    assert out[0]["reasoning_specialist"] == OPUS
    assert out[1] == [f"reasoning_specialist: {ASTRA} unavailable -> {OPUS}"]
    assert out[2] == ["raise_effort_one_level"]
    assert resolver.retry_compensations == {"raise_effort_one_level": "retry_and_scarcity"}


def test_the_diversity_note_names_every_cause_truthfully():
    plain = rt.diversity_compensation_note(None)
    retry = rt.diversity_compensation_note("retry")
    both = rt.diversity_compensation_note("retry_and_scarcity")
    assert len({plain, retry, both}) == 3
    assert "fallback" in plain and "failed" not in plain
    assert "already-failed" in retry and "unavailable" not in retry
    assert "already-failed" in both and "unavailable" in both
    assert all(n.startswith("compensation: ") and n.endswith("effort +1") for n in (plain, retry, both))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

DEBUG = dict(task_class="DEBUGGING", complexity=0, uncertainty=3, blast_radius=0,
             reversibility=2, runtime="claude_code", flags=["unknown_root_cause"])
BANDS = list(CFG["router"]["bands"])


def test_a_retry_after_the_xai_seat_fails_dispatches_sonnet_at_the_threshold():
    """The issue's example. The route is the pure-failure case: nothing in the
    supply is missing, so the retry ladder's Sonnet seat is not a fallback."""
    out = route(Task(**DEBUG, prior_failures=1, prior_models=[XAI]), CFG)
    assert out["terminal"] is None
    assert out["selected_model"] == SON
    assert out["routing_confidence"] == pytest.approx(0.60)
    assert out["fallbacks_applied"] == [] and out["excluded_prior_failures"] == [XAI]
    # The ladder asks for a tier strictly above what failed (tier 1); the
    # expensive own-bound senior seat is not chosen where Sonnet satisfies it.
    assert POLICY.tier_of[out["selected_model"]] == 2


@pytest.mark.parametrize("supply,label", [
    (dict(unavailable_models=[XAI]), "caller_unavailable"),
    (dict(_family_quota={"xai": "exhausted"}), "family_exhausted"),
])
def test_a_failure_does_not_improve_a_route_whose_supply_is_really_short(supply, label):
    fresh = route(Task(**DEBUG, **supply), CFG)
    retry = route(Task(**DEBUG, prior_failures=1, prior_models=[XAI], **supply), CFG)
    assert fresh["terminal"] == "ESCALATE_ROUTING"
    assert fresh["routing_confidence"] == pytest.approx(0.59)
    # The failure penalty (0.05) is smaller than the scarcity the failure
    # would otherwise launder (0.06): the retry must not read BETTER.
    assert retry["terminal"] == "ESCALATE_ROUTING"
    assert retry["routing_confidence"] == pytest.approx(0.54)
    assert retry["routing_confidence"] < fresh["routing_confidence"]


def test_a_failed_seat_with_a_local_revocation_keeps_the_scarcity():
    cfg = with_local_revocation(XAI)
    retry = route(Task(**DEBUG, prior_failures=1, prior_models=[XAI]), cfg)
    assert retry["terminal"] == "ESCALATE_ROUTING"
    assert retry["routing_confidence"] == pytest.approx(0.54)


def test_a_retry_whose_balanced_successor_is_really_missing_stays_a_fallback():
    """Retry after the xai seat failed, Sonnet withheld: the seat that replaces
    Sonnet is a genuine fallback and stays one — recorded against the model that
    is actually missing (Sonnet), not the one that merely failed (xai). The
    confidence is the same 0.64 either way: failure 0.05 + fallback 0.06 +
    uncertainty 0.20."""
    kw = dict(task_class="IMPLEMENTATION", complexity=1, uncertainty=3, blast_radius=1,
              reversibility=2, runtime="claude_code")
    out = route(Task(**kw, prior_failures=1, prior_models=[XAI], unavailable_models=[SON]), CFG)
    assert out["fallbacks_applied"] == [SCARCITY(SON, ASTRA)]
    assert out["routing_confidence"] == pytest.approx(0.64)


def test_a_same_model_retry_route_is_unchanged():
    out = route(Task(**DEBUG, prior_failures=1, prior_models=[XAI],
                     _same_model_retry={"model": XAI, "effort": "HIGH"}), CFG)
    assert out["fallbacks_applied"] == [] and out["routing_confidence"] == pytest.approx(0.60)
    assert out["terminal"] is None


def test_no_history_and_operational_history_route_as_before():
    assert route(Task(**DEBUG), CFG)["routing_confidence"] == pytest.approx(0.65)
    cfg = CFG
    base = dict(route_schema_version=1, **{k: v for k, v in DEBUG.items()})

    def row(kind, i=1, recovered=False):
        r = dict(attempt_id=f"attempt-{i}", model_id=XAI, kind=kind, evidence_sha256="a" * 64)
        if recovered:
            r["recovery_sha256"] = "b" * 64
        if kind == "capability_failure":
            r.update(effort="HIGH", retry_evidence_sha256="c" * 64)
        return r

    def go(history):
        return route(rt.task_from_request_v1({**base, "attempt_outcomes": history}), cfg)

    held = go([row("transport_failure")])
    assert held["terminal"] == "OPERATIONAL_RECOVERY_REQUIRED" and held["selected_model"] is None
    recovered = go([row("transport_failure", recovered=True)])
    assert recovered["selected_model"] == XAI and recovered["excluded_prior_failures"] == []
    assert recovered["routing_confidence"] == pytest.approx(0.65)
    # Typed capability history is the retry the rule is about.
    typed = go([row("capability_failure")])
    assert typed["selected_model"] == SON and typed["fallbacks_applied"] == []
    # A recovered operational record next to it changes nothing about that.
    both = go([row("capability_failure"), row("transport_failure", 2, True)])
    assert both["selected_model"] == SON and both["routing_confidence"] == pytest.approx(0.60)


def test_a_failed_reviewer_seat_of_a_review_task_is_not_a_fallback():
    """The REVIEW lead is a review seat: the reasoning seat failed, the lead
    moves to Astra with no scarcity record (0.84 -> 0.90)."""
    out = route(Task(task_class="REVIEW", complexity=0, uncertainty=1, blast_radius=1,
                     reversibility=3, runtime="claude_code",
                     prior_failures=1, prior_models=[SOL]), CFG)
    assert out["fallbacks_applied"] == [] and out["routing_confidence"] == pytest.approx(0.90)
    assert out["review"]["reviewer_models"] == [ASTRA]


def test_the_first_review_attempt_under_a_two_family_shortage_is_still_asked_to_reroute():
    """The 264 population is NOT a retry: nothing failed, xai is withheld and
    OpenAI exhausted, so the lead's fallback is a genuine one. Kept on purpose
    (issue #70); a blanket exemption for assignment/fallback would hide it."""
    kw = dict(task_class="REVIEW", complexity=0, uncertainty=3, blast_radius=1,
              reversibility=3, runtime="claude_code", flags=["unknown_root_cause"],
              unavailable_models=[XAI], _family_quota={"openai": "exhausted"})
    out = route(Task(**kw), CFG)
    assert out["terminal"] == "ESCALATE_ROUTING"
    assert out["routing_confidence"] == pytest.approx(0.59)
    # Its retry adds a failure on top of the same real shortage: still 0.54.
    retry = route(Task(**kw, prior_failures=1, prior_models=[OPUS]), CFG)
    assert retry["terminal"] == "ESCALATE_ROUTING"
    assert retry["routing_confidence"] < out["routing_confidence"]


def test_failed_luna_review_retry_keeps_the_execution_guarded_tier_change():
    """The accepted #66 execution guard can leave the retry at a lower band
    while its worker still rises strictly above the failed capability tier."""
    kw = dict(task_class="REVIEW", complexity=3, uncertainty=0, blast_radius=0,
              reversibility=0, runtime="codex", flags=["unknown_root_cause"],
              unavailable_models=[XAI])
    fresh = route(Task(**kw), CFG)
    retry = route(Task(**kw, prior_failures=1, prior_models=[LUNA]), CFG)
    assert fresh["selected_model"] == LUNA
    assert POLICY.tier_of[fresh["selected_model"]] == 0
    assert retry["selected_model"] == TERRA_FAST
    assert POLICY.tier_of[retry["selected_model"]] > POLICY.tier_of[LUNA]
    assert retry["selected_capability_tier"] == 1
    assert retry["risk_band"] == "LOW"
    assert retry["selected_effort"] == "HIGH"
    assert retry["review"]["band"] == "LOW"
    assert retry["routing_confidence"] == pytest.approx(0.80)
    assert retry["fallbacks_applied"] == [] and retry["terminal"] is None
    assert retry["review"]["band_floor_unsatisfiable"] is False
    assert retry["review"]["review_depth_reduced"] == []
    assert "execution band NORMAL yielded worker_balanced: review.band" in retry["notes"]

    # 1.23.0 compared the retry to the original primary and therefore used its
    # genuine XAI-to-Sonnet fallback. Keep that contrast pinned explicitly.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rt.Resolver, "_retry_baseline",
                   lambda self, role, primary_id, *, write: primary_id)
        historical = route(Task(**kw, prior_failures=1, prior_models=[LUNA]), CFG)
    assert historical["selected_role"] == "worker_balanced"
    assert historical["selected_capability_tier"] == 2
    assert historical["review"]["band"] == "MEDIUM"
    assert POLICY.tier_of[historical["selected_model"]] > POLICY.tier_of[LUNA]
    assert historical["routing_confidence"] == pytest.approx(0.74)
    assert historical["fallbacks_applied"] == [SCARCITY(XAI, SON)]


def test_the_architect_downgrade_is_compensated_on_the_route_without_a_fallback():
    out = route(Task(task_class="ARCHITECTURE", complexity=0, uncertainty=2, blast_radius=3,
                     reversibility=3, runtime="claude_code",
                     prior_failures=2, prior_models=[FABLE, ASTRA]), CFG)
    assert out["fallback_compensations_applied"] == [ARCH_COMP]
    assert out["routing_confidence"] == pytest.approx(0.72)
    assert not any("unavailable ->" in f for f in out["fallbacks_applied"])
    assert out["excluded_prior_failures"] == sorted([FABLE, ASTRA])


# --- the 0.80 gate: a spurious fallback no longer promotes the review -------

def _band_floor_holds(out):
    assert BANDS.index(out["review"]["band"]) >= BANDS.index(out["risk_band"]), out["review"]["band"]


@pytest.mark.parametrize("name,kw,history,conf,band,risk", [
    # (one failure + uncertainty 2: 0.82, was 0.76) the promotion no longer fires
    ("pf1_u2", dict(task_class="IMPLEMENTATION", complexity=0, uncertainty=2, blast_radius=0,
                    reversibility=2, flags=[]), [XAI], 0.82, "MEDIUM", "MEDIUM"),
    # (one failure + unknown root cause: exactly 0.80, was 0.74)
    ("pf1_unknown", dict(task_class="IMPLEMENTATION", complexity=0, uncertainty=0,
                         blast_radius=0, reversibility=0, flags=["unknown_root_cause"]),
     [XAI], 0.80, "LOW", "LOW"),
    # (two failures, uncertainty 0: exactly 0.80, was 0.74); the worker was
    # already raised past the xai seat, the CRITICAL review stays.
    ("pf2_u0", dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=0, blast_radius=3,
                    reversibility=3, flags=[]), [XAI, SON], 0.80, "CRITICAL", "CRITICAL"),
    # a CRITICAL band reached by the promotion alone returns to HIGH
    ("critical_to_high", dict(task_class="IMPLEMENTATION", complexity=0, uncertainty=0,
                              blast_radius=3, reversibility=2, flags=["unknown_root_cause"]),
     [XAI], 0.80, "HIGH", "HIGH"),
])
def test_the_extra_review_gate_reads_the_fallbacks_that_remain(name, kw, history, conf, band, risk):
    out = route(Task(runtime="claude_code", prior_failures=len(history), prior_models=history,
                     **kw), CFG)
    assert out["fallbacks_applied"] == []
    assert out["routing_confidence"] == pytest.approx(conf)
    assert (out["review"]["band"], out["risk_band"]) == (band, risk)
    _band_floor_holds(out)
    assert out["terminal"] is None


def test_the_review_never_falls_below_the_risk_band_over_a_retry_sweep():
    import itertools
    checked = 0
    for c, u, b, r, flags, history in itertools.product(
            (0, 2), range(4), (0, 3), (0, 3), ([], ["unknown_root_cause"]),
            ([XAI], [XAI, SON])):
        out = route(Task(task_class="IMPLEMENTATION", complexity=c, uncertainty=u,
                         blast_radius=b, reversibility=r, runtime="claude_code", flags=flags,
                         prior_failures=len(history), prior_models=history), CFG)
        if out["terminal"]:
            continue
        _band_floor_holds(out)
        checked += 1
    assert checked > 100


def test_a_confirmation_depending_on_the_fixed_point_reads_the_final_fallbacks():
    """The review/judge allocation can change between the preliminary and the
    final resolve. The CRITICAL reviewer and judge here are seated after the
    worker; the emitted confidence and the emitted fallbacks must agree."""
    out = route(Task(task_class="IMPLEMENTATION", complexity=2, uncertainty=0, blast_radius=3,
                     reversibility=3, runtime="claude_code", prior_failures=2,
                     prior_models=[XAI, SON]), CFG)
    assert out["review"]["band"] == "CRITICAL" and out["review"]["judge_model"]
    counted = sum(1 for f in out["fallbacks_applied"] if "unavailable ->" in f)
    assert counted == 0 and out["routing_confidence"] == pytest.approx(0.80)
