"""The execution-difficulty axis (design 2026-09-03). T1/T20 here; later
tasks append T2, T4-T7, T9-T11, T18."""
import copy
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    ConfigError, Policy, Task, load_config, route, execution_score,
)

CFG = load_config()
ID = lambda key: CFG["models"][key]["id"]  # noqa: E731


def r(**kw):
    kw.setdefault("complexity", 0); kw.setdefault("uncertainty", 0)
    kw.setdefault("blast_radius", 0); kw.setdefault("reversibility", 0)
    return route(Task(**kw), CFG)


def _cfg(mutate):
    cfg = copy.deepcopy(CFG)
    mutate(cfg)
    return cfg


def _bad(mutate):
    with pytest.raises(ConfigError):
        Policy(_cfg(mutate))


# --- T1: load-time validation, fail-closed --------------------------------

def test_t1_execution_block_must_be_exact():
    _bad(lambda c: c.pop("execution"))
    _bad(lambda c: c["execution"].update(extra=1))
    _bad(lambda c: c["execution"].pop("bands"))
    _bad(lambda c: c.update(execution="not a mapping"))                   # [P1-sol-F6]


def test_t1_score_weights_are_exact_nonnegative_ints():
    _bad(lambda c: c["execution"]["score_weights"].pop("uncertainty"))
    _bad(lambda c: c["execution"]["score_weights"].update(blast_radius=1))
    _bad(lambda c: c["execution"]["score_weights"].update(complexity=True))
    _bad(lambda c: c["execution"]["score_weights"].update(complexity=-1))
    _bad(lambda c: c["execution"].update(score_weights=[3, 2]))            # not a mapping


def test_t1_flag_weights_are_exactly_the_three_context_flags():
    _bad(lambda c: c["execution"]["flag_weights"].pop("tool_heavy"))
    _bad(lambda c: c["execution"]["flag_weights"].update(long_horizon=1))
    _bad(lambda c: c["execution"]["flag_weights"].update(tool_heavy=False))


def test_t1_execution_bands_are_contiguous_unique_and_typed():
    _bad(lambda c: c["execution"]["bands"]["NORMAL"].update(min=10))          # gap
    _bad(lambda c: c["execution"]["bands"]["NORMAL"].update(max=12))          # overlap
    _bad(lambda c: c["execution"]["bands"]["HARD"].update(ordinal=1))         # duplicate
    _bad(lambda c: c["execution"]["bands"]["EASY"].update(min=True))          # bool
    _bad(lambda c: c["execution"]["bands"]["EASY"].pop("ordinal"))            # missing key
    _bad(lambda c: c["execution"]["bands"]["EASY"].update(colour="red"))      # extra key
    _bad(lambda c: c["execution"]["bands"]["VERY_HARD"].update(max=17))       # does not reach max
    _bad(lambda c: c["execution"]["bands"]["EASY"].update(max=-1))            # max < min [P3-opus-missing-1]
    _bad(lambda c: c["execution"]["bands"].update(EASY="0-8"))                # band not a mapping


def test_t1_router_bands_and_weights_get_the_same_load_time_checks():
    _bad(lambda c: c["router"]["bands"]["MEDIUM"].update(min=5))              # gap
    _bad(lambda c: c["router"]["bands"]["MEDIUM"].update(max=8))              # overlap
    _bad(lambda c: c["router"]["bands"]["HIGH"].update(ordinal=1))            # duplicate
    _bad(lambda c: c["router"]["bands"]["LOW"].update(min=True))              # bool
    _bad(lambda c: c["router"]["bands"]["LOW"].pop("max"))                    # missing key
    _bad(lambda c: c["router"]["bands"]["CRITICAL"].update(max=17))           # does not reach max
    _bad(lambda c: c["router"]["score_weights"].pop("complexity"))
    _bad(lambda c: c["router"]["score_weights"].update(extra=1))
    _bad(lambda c: c["router"]["score_weights"].update(complexity=-2))
    _bad(lambda c: c["router"]["score_weights"].update(reversibility=True))
    _bad(lambda c: c["router"].update(score_weights=None))


def test_t1_malformed_parents_raise_config_error_not_key_or_type_error():   # [P2-sol-F3]
    _bad(lambda c: c.pop("router"))
    _bad(lambda c: c.update(router="nope"))
    _bad(lambda c: c.pop("flags"))
    _bad(lambda c: c["flags"].update(context="unfamiliar_codebase"))        # a string, not a list
    _bad(lambda c: c["flags"].pop("context"))
    _bad(lambda c: c["flags"]["context"].append({"unhashable": True}))       # a set() would raise TypeError [P3-sol-F5]


def test_t1_a_non_dict_mapping_config_still_loads():
    class View(Mapping):
        def __init__(self, d): self._d = d
        def __getitem__(self, k): return self._d[k]
        def __iter__(self): return iter(self._d)
        def __len__(self): return len(self._d)
    policy = Policy(View(CFG))
    assert policy.execution_bands == ["EASY", "NORMAL", "HARD", "VERY_HARD"]


# --- T20: max_execution_score derivation and fail-closed coupling ----------

def test_t20_max_execution_score_is_derived_from_the_weights():
    policy = Policy.of(CFG)
    assert policy.max_execution_score == 3 * (3 + 2) + 3
    cfg = _cfg(lambda c: (c["execution"]["flag_weights"].update(tool_heavy=2),
                         c["execution"]["bands"]["VERY_HARD"].update(max=19)))
    assert Policy(cfg).max_execution_score == 19
    # The rationale's denominator is asserted in Task 4 (T7), once explain() names the axis [P3-opus-F1].


def test_t20_raising_a_flag_weight_without_moving_the_top_band_fails_closed():
    _bad(lambda c: c["execution"]["flag_weights"].update(tool_heavy=2))


def test_t20_execution_score_formula():
    t = Task(task_class="IMPLEMENTATION", complexity=3, uncertainty=1, blast_radius=0,
             reversibility=0, flags=["tool_heavy"])
    assert execution_score(t, CFG) == 3 * 3 + 2 * 1 + 1
    assert Policy.of(CFG).execution_band_of(12) == "HARD"


# --- T7: output contract ----------------------------------------------------

def test_t7_two_fields_next_to_risk_and_on_terminal_routes():
    out = r(task_class="IMPLEMENTATION", complexity=3)
    keys = list(out)
    assert keys.index("execution_score") == keys.index("risk_band") + 1
    assert keys.index("execution_band") == keys.index("execution_score") + 1
    assert (out["execution_score"], out["execution_band"]) == (9, "NORMAL")
    term = r(task_class="IMPLEMENTATION", complexity=3, prior_failures=1)   # RETRY_HISTORY_REQUIRED
    assert term["terminal"] == "RETRY_HISTORY_REQUIRED"
    assert (term["execution_score"], term["execution_band"]) == (9, "NORMAL")


def test_t7_rationale_names_both_axes_with_derived_denominators():
    out = r(task_class="IMPLEMENTATION", complexity=3)
    assert out["rationale"].startswith(
        "IMPLEMENTATION scored 3/18 (c=3 u=0 b=0 r=0) -> band LOW; execution 9/18 -> NORMAL.")


def test_t7_rationale_denominator_follows_a_mutated_policy():                # moved from T20 [P3-opus-F1]
    cfg = _cfg(lambda c: (c["execution"]["flag_weights"].update(tool_heavy=2),
                         c["execution"]["bands"]["VERY_HARD"].update(max=19)))
    out = route(Task(task_class="IMPLEMENTATION", complexity=3, uncertainty=0, blast_radius=0,
                     reversibility=0, flags=["tool_heavy"]), cfg)
    assert "execution 11/19 -> NORMAL." in out["rationale"]


def test_t7_text_output_prints_the_execution_lines(capsys):
    import route_task
    route_task._print_text(r(task_class="IMPLEMENTATION", complexity=3))
    lines = capsys.readouterr().out.splitlines()
    i = next(i for i, l in enumerate(lines) if l.startswith("risk_band:"))
    assert lines[i + 1].startswith("exec_score:  9")
    assert lines[i + 2].startswith("exec_band:   NORMAL")


def test_t7_schema_version_and_request_hash_are_untouched():
    import route_task
    assert route_task.ROUTE_SCHEMA_VERSION == 1
    a = route_task.request_sha256_of(Task(task_class="MECHANICAL", complexity=0, uncertainty=0,
                                          blast_radius=0, reversibility=0))
    # Same literal `test_host_seat.py::test_undeclared_preserves_legacy_hash_by_key_omission` pins.
    assert a == "c92c316c148058bee7609995a276c8607b5dd0eb822189de0686b5c085b3204e"


TIER = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
CTX = ["unfamiliar_codebase", "tool_heavy", "cross_service_change"]
ROLES = list(CFG["role_tiers"])


def _raised(out):  return [n for n in out["notes"] if n.startswith("execution band") and " raised worker" in n]
def _yielded(out): return [n for n in out["notes"] if n.startswith("execution band") and " yielded " in n]


# --- T1 (selection table) ------------------------------------------------------

def test_t1_execution_selection_matches_classes_and_bands_exactly():
    _bad(lambda c: c["execution_selection"].pop("REVIEW"))
    _bad(lambda c: c["execution_selection"].update(PLANNING=dict(c["execution_selection"]["REVIEW"])))
    _bad(lambda c: c["execution_selection"]["REVIEW"].pop("HARD"))
    _bad(lambda c: c["execution_selection"]["REVIEW"].update(HARD="grand_wizard"))
    _bad(lambda c: c.update(execution_selection=None))
    _bad(lambda c: c.update(execution_selection=[{"REVIEW": {}}]))           # a list of dicts, not a mapping [P3-sol-F5]


# --- T2: the reachable cross product ---------------------------------------

def test_t2_hard_but_isolated_raises_the_worker_and_leaves_review_low():
    out = r(task_class="IMPLEMENTATION", complexity=3, flags=CTX)     # exec 12 HARD, risk 3 LOW
    assert out["execution_band"] == "HARD" and out["risk_band"] == "LOW"
    assert TIER[out["selected_model"]] >= 1 and out["review"]["band"] == "LOW"
    assert _raised(out) == ["execution band HARD raised worker from worker_fast to worker_balanced"]


def test_t2_easy_but_sensitive_keeps_high_review_and_a_tier1_worker():
    out = r(task_class="IMPLEMENTATION", flags=["auth_sensitive"])
    assert out["execution_band"] == "EASY" and out["review"]["band"] == "HIGH"
    assert TIER[out["selected_model"]] >= 1


def test_t2_hard_and_sensitive_keeps_high_review():
    out = r(task_class="IMPLEMENTATION", complexity=3, flags=CTX + ["auth_sensitive"])
    assert out["execution_band"] == "HARD" and out["review"]["band"] == "HIGH"
    assert TIER[out["selected_model"]] >= 1


def test_t2_easy_and_low_is_unchanged_from_1_12_1():
    out = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=1)
    assert out["execution_band"] == "EASY" and out["selected_role"] == "worker_fast"
    assert not _raised(out) and not _yielded(out)


def test_t2_very_hard_times_low_is_unreachable():
    policy = Policy.of(CFG)
    best = max(3 * c + 2 * u + 3 for c in range(4) for u in range(4) if c + 2 * u <= 3)
    assert policy.execution_band_of(best) == "HARD"


# --- T9: strictly stronger, ties keep legacy --------------------------------

def test_t9_equal_tier_different_role_keeps_the_legacy_cell():
    # REVIEW c3 u2 b1 r1: risk 3+4+2+1 = 10 HIGH -> senior_engineer (opus, tier 2);
    # exec 13 HARD -> mutate to reasoning_specialist (sol, tier 2): equal tier -> legacy.
    cfg = _cfg(lambda c: c["execution_selection"]["REVIEW"].update(HARD="reasoning_specialist"))
    out = route(Task(task_class="REVIEW", complexity=3, uncertainty=2, blast_radius=1, reversibility=1), cfg)
    assert out["selected_role"] == "senior_engineer"
    assert not _raised(out) and not _yielded(out)


# --- T11: the by_reasoning_centric note fires once, for the ADOPTED cell -----

@pytest.mark.parametrize("rc", [False, True])
def test_t11_tie_keeps_the_legacy_note_once(rc):
    out = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=3, blast_radius=3,
            reversibility=2, reasoning_centric=rc)              # risk 17 CRITICAL, exec 15: both cells by_rc
    assert sum(n.startswith("reasoning_centric=") for n in out["notes"]) == 1


def test_t11_candidate_wins_note_from_the_execution_cell_only():
    # risk 9 HIGH -> worker_balanced (not by_rc); exec 15 VERY_HARD -> by_rc.
    code = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=2, blast_radius=1,
             flags=["unfamiliar_codebase", "tool_heavy"], reasoning_centric=False)
    assert code["selected_role"] == "senior_engineer" and len(_raised(code)) == 1
    assert [n for n in code["notes"] if n.startswith("reasoning_centric=")] == \
        ["reasoning_centric=False selected senior_engineer"]
    # rc=True: the candidate is reasoning_specialist (sol); the HIGH pair loses sol and the
    # substitute is the architect -> two claude reviewers -> cross_family_review false vs
    # legacy true -> row 8 yields, and the emitted (legacy) plan carries NO rc note.
    reason = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=2, blast_radius=1,
               flags=["unfamiliar_codebase", "tool_heavy"], reasoning_centric=True)
    assert reason["selected_role"] == "worker_balanced"
    assert _yielded(reason) == ["execution band VERY_HARD yielded reasoning_specialist: cross_family_review"]
    assert not any(n.startswith("reasoning_centric=") for n in reason["notes"])


# --- T6: the retry ladder and the execution cell ----------------------------

def test_t6_i_ladder_reaches_the_same_tier_so_no_raise_note():
    out = r(task_class="IMPLEMENTATION", complexity=3, prior_failures=1,
            prior_models=[ID("openai_worker_fast")])
    assert TIER[out["selected_model"]] == 1
    assert not _raised(out) and not _yielded(out)
    assert any("capability tier" in n for n in out["notes"])


def test_t6_ii_execution_cell_above_the_ladder_result_raises():
    # risk 7 MEDIUM (conf 0.95-0.08-0.05 = 0.82, no promotion); exec 9+4+2 = 15 VERY_HARD.
    # Legacy: worker_fast -> ladder above tier 0 -> worker_balanced (grok, tier 1).
    # Candidate: senior_engineer (opus, tier 2), already above the failed tier.
    # Both plans seat reasoning_specialist as the single MEDIUM reviewer -> equal contract.
    out = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=2, prior_failures=1,
            prior_models=[ID("openai_worker_fast")], flags=["unfamiliar_codebase", "tool_heavy"])
    assert TIER[out["selected_model"]] == 2 and out["review"]["band"] == "MEDIUM"
    assert _raised(out) == ["execution band VERY_HARD raised worker from worker_balanced to senior_engineer"]


@pytest.mark.parametrize("rc", [False, True])
def test_t6_iii_same_final_tier_keeps_the_legacy_model(rc):
    out = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=3, reasoning_centric=rc,
            prior_failures=1, prior_models=[ID("xai_frontier")])
    assert TIER[out["selected_model"]] == 2
    assert out["selected_model"] == ID("claude_senior")        # 1.12.1's ladder answer, both rc values
    assert not _raised(out) and not _yielded(out)


def test_t6_iv_top_tier_failure_exhausts_both_paths_identically():
    out = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=3, prior_failures=1,
            prior_models=[ID("claude_architect")])
    assert out["terminal"] == "HUMAN_REQUIRED"
    assert not _raised(out) and not _yielded(out)


# --- raised-note ceiling suffix ------------------------------------------------

def test_raised_note_names_the_ceiling_when_the_worker_is_clamped():
    # DEBUGGING c3 u1 + unknown_root_cause: risk 5 MEDIUM, exec 11 NORMAL -> grok (ceiling VERY_HIGH)
    # over luna; effort MAX -> effective VERY_HIGH; confidence 0.85 on both plans; MEDIUM reviewer
    # moves grok -> sol (tier 1 -> 2), cross-family on both -> adopted.
    out = r(task_class="DEBUGGING", complexity=3, uncertainty=1, flags=["unknown_root_cause"])
    assert (out["selected_effort"], out["selected_effort_effective"]) == ("MAX", "VERY_HIGH")
    assert _raised(out) == ["execution band NORMAL raised worker from worker_fast to worker_balanced"
                            " at effective effort VERY_HIGH (ceiling)"]


# --- the second plan is computed only when the cell won ------------------------

def test_second_plan_is_skipped_when_candidate_is_legacy(monkeypatch):
    import route_task as rt
    calls = []
    original = rt._plan
    monkeypatch.setattr(rt, "_plan", lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    r(task_class="IMPLEMENTATION", complexity=1, uncertainty=1)
    assert len(calls) == 1
    calls.clear()
    r(task_class="IMPLEMENTATION", complexity=3, uncertainty=2, blast_radius=1,
      flags=["unfamiliar_codebase", "tool_heavy"])
    assert len(calls) == 2


# --- T18: the contract partial order, row by row -----------------------------

from route_task import _contract_violation  # noqa: E402

def _plan_like(**over):
    base = {
        "terminal": None, "human_control_causes": [],
        "review": {"band": "HIGH", "reviewers": ["senior_engineer", "reasoning_specialist"],
                   "reviewer_models": [ID("claude_senior"), ID("openai_reasoning")],
                   "effort": "HIGH", "required_checks": [], "independence_required": True,
                   "review_independence": "degraded", "independence_compromised": False,
                   "band_floor_unsatisfiable": False, "judge_unavailable": False,
                   "judge_model": None, "review_depth_reduced": []},
        "cross_family_review": True, "effort_ceiling_applied": [],
        "selected_role": "worker_balanced", "selected_model": ID("xai_frontier"),
    }
    for k, v in over.items():
        if k.startswith("review."):
            base["review"][k[7:]] = v
        else:
            base[k] = v
    return base

POLICY = Policy.of(CFG)

# (row, candidate override, legacy override, symmetric) — symmetric rows are equality rows:
# the mirror direction is rejected with the same name. Ordered rows allow the mirror.
ROWS = [
    ("terminal", {"terminal": "SUPPLY_EXHAUSTED"}, {}, False),
    ("human_control_causes", {"human_control_causes": ["review_below_band"]}, {}, False),
    ("review.band", {"review.band": "CRITICAL"}, {}, True),
    ("review.shape", {"review.effort": "MEDIUM"}, {}, True),
    ("review_independence", {"review.review_independence": "unavailable"}, {}, False),
    ("review.flags", {"review.independence_compromised": True}, {}, False),
    ("judge_tier", {"review.judge_model": ID("claude_senior")}, {"review.judge_model": ID("claude_architect")}, False),
    ("cross_family_review", {"cross_family_review": False}, {}, False),
    ("reviewer_tiers", {"review.reviewer_models": [ID("claude_senior"), ID("xai_frontier")]}, {}, False),
    ("reviewer_efforts", {"effort_ceiling_applied": [{"role": "reasoning_specialist", "model": ID("openai_reasoning"),
                                                       "requested": "HIGH", "capped_at": "MEDIUM",
                                                       "floor_broken": None, "floor_requires": None}]}, {}, False),
    ("review_depth_reduced", {"review.review_depth_reduced": [{"reviewer": "x"}]}, {}, False),
    ("worker_floor_broken", {"effort_ceiling_applied": [{"role": "worker_balanced", "model": ID("xai_frontier"),
                                                          "requested": "MAX", "capped_at": "VERY_HIGH",
                                                          "floor_broken": "effort_floors.band_CRITICAL",
                                                          "floor_requires": "MAX"}]}, {}, False),
]

@pytest.mark.parametrize("row,cand,legacy,symmetric", ROWS)
def test_t18_each_row_rejects_its_worse_direction_and_names_itself(row, cand, legacy, symmetric):
    assert _contract_violation(POLICY, _plan_like(**cand), _plan_like(**legacy)) == row
    mirror = _contract_violation(POLICY, _plan_like(**legacy), _plan_like(**cand))
    if symmetric:
        assert mirror == row                                   # [P1-sol-F5][P1-opus-F3]
    elif row == "terminal":
        assert mirror is None                                  # legacy terminal, candidate not: unlock
    else:
        assert mirror is None


def test_t18_legacy_terminal_and_candidate_executable_is_allowed():
    assert _contract_violation(POLICY, _plan_like(), _plan_like(terminal="UNSATISFIABLE_LOCAL_POLICY")) is None


@pytest.mark.parametrize("over", [                                  # [P2-sol-missing-4]
    {"review.reviewers": ["senior_engineer"], "review.reviewer_models": [ID("claude_senior")]},
    {"review.effort": "MEDIUM"},
    {"review.required_checks": ["security"]},
    {"review.independence_required": False},
])
def test_t18_every_review_shape_component_is_compared(over):
    assert _contract_violation(POLICY, _plan_like(**over), _plan_like()) == "review.shape"


@pytest.mark.parametrize("flag", ["independence_compromised", "band_floor_unsatisfiable", "judge_unavailable"])
def test_t18_every_review_flag_is_compared(flag):
    assert _contract_violation(POLICY, _plan_like(**{f"review.{flag}": True}), _plan_like()) == "review.flags"
    assert _contract_violation(POLICY, _plan_like(), _plan_like(**{f"review.{flag}": True})) is None


def test_t18_independence_order_and_not_applicable():                        # [P3-sol-missing-5]
    order = ["unavailable", "degraded", "planned", "enforced"]
    for lo, hi in zip(order, order[1:]):
        assert _contract_violation(POLICY, _plan_like(**{"review.review_independence": lo}),
                                   _plan_like(**{"review.review_independence": hi})) == "review_independence"
        assert _contract_violation(POLICY, _plan_like(**{"review.review_independence": hi}),
                                   _plan_like(**{"review.review_independence": lo})) is None
    na = _plan_like(**{"review.review_independence": "not_applicable", "review.independence_required": False})
    assert _contract_violation(POLICY, na, na) is None
    mixed = _plan_like(**{"review.review_independence": "not_applicable"})   # shape equal, state mismatched
    assert _contract_violation(POLICY, mixed, _plan_like()) == "review_independence"


def test_t18_both_worker_floors_broken_compares_requires_and_capped():       # [P3-sol-missing-5]
    def broken(requires, capped):
        return {"effort_ceiling_applied": [{"role": "worker_balanced", "model": ID("xai_frontier"),
                                            "requested": requires, "capped_at": capped,
                                            "floor_broken": "effort_floors.band_CRITICAL", "floor_requires": requires}]}
    assert _contract_violation(POLICY, _plan_like(**broken("MAX", "VERY_HIGH")), _plan_like(**broken("MAX", "VERY_HIGH"))) is None
    assert _contract_violation(POLICY, _plan_like(**broken("MAX", "HIGH")), _plan_like(**broken("MAX", "VERY_HIGH"))) == "worker_floor_broken"
    assert _contract_violation(POLICY, _plan_like(**broken("MAX", "VERY_HIGH")), _plan_like(**broken("VERY_HIGH", "HIGH"))) == "worker_floor_broken"


def test_t18_judge_seated_on_one_side_only_is_not_compared():
    assert _contract_violation(POLICY, _plan_like(**{"review.judge_model": ID("claude_senior")}), _plan_like()) is None


def test_t18_a_stronger_substitute_reviewer_passes():
    cand = _plan_like(**{"review.reviewer_models": [ID("claude_architect"), ID("openai_reasoning")]})
    assert _contract_violation(POLICY, cand, _plan_like()) is None


def test_t18_evaluation_order_is_the_table_order():
    cand = _plan_like(**{"review.band": "CRITICAL", "cross_family_review": False})
    assert _contract_violation(POLICY, cand, _plan_like()) == "review.band"


# --- T4: each context flag moves the worker at the EASY/NORMAL boundary -------

@pytest.mark.parametrize("flag", CTX)
def test_t4_each_flag_crosses_the_boundary_and_moves_the_worker(flag):
    base = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=1)          # exec 8 EASY, risk 4 MEDIUM
    with_flag = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, flags=[flag])
    assert (base["execution_band"], base["selected_role"]) == ("EASY", "worker_fast")
    assert (with_flag["execution_band"], with_flag["selected_role"]) == ("NORMAL", "worker_balanced")
    assert (with_flag["risk_band"], with_flag["review"]["band"]) == (base["risk_band"], base["review"]["band"])


# --- T5: execution effort floors --------------------------------------------

def test_t5_hard_and_very_hard_floor_the_effort_and_say_so():
    hard = r(task_class="MECHANICAL", complexity=3, uncertainty=2)               # exec 13 HARD, risk 7 MEDIUM
    very = r(task_class="MECHANICAL", complexity=3, uncertainty=3)               # exec 15 VERY_HARD, risk 9 HIGH
    assert hard["selected_effort"] == "HIGH"
    assert [n for n in hard["notes"] if n.startswith("execution band HARD floored effort at HIGH")]
    assert very["selected_effort"] == "VERY_HIGH"
    assert sum(n.startswith("execution band VERY_HARD floored effort at VERY_HIGH") for n in very["notes"]) == 1
    assert very["review"]["band"] == "CRITICAL"          # 0.75 < 0.80 promoted the review; note still once


def test_t5_the_worker_floor_reporter_sees_the_execution_floor():
    import route_task as rt
    t = Task(task_class="MECHANICAL", complexity=3, uncertainty=3, blast_radius=0, reversibility=0)
    assert rt._worker_effort_floor(t, "HIGH", "VERY_HARD", Policy.of(CFG)) == \
        ("effort_floors.execution_VERY_HARD", "VERY_HIGH")
    # The stronger floor wins when the band's is stronger [P2-opus-missing-3]; on a tie the
    # first-listed rule (the band's) is reported, as `max` keeps the first maximum.
    assert rt._worker_effort_floor(t, "CRITICAL", "HARD", Policy.of(CFG)) == \
        ("effort_floors.band_CRITICAL", "VERY_HIGH")
    assert rt._worker_effort_floor(t, "CRITICAL", "VERY_HARD", Policy.of(CFG)) == \
        ("effort_floors.band_CRITICAL", "VERY_HIGH")


def test_s3_treats_an_unresolvable_cell_as_tier_minus_one(monkeypatch):     # [P2-opus-missing-2]
    import route_task as rt
    policy = rt.Policy.of(CFG)
    task = Task(task_class="IMPLEMENTATION", complexity=3, uncertainty=0, blast_radius=0, reversibility=0)
    task.validate(policy)
    resolver = rt.Resolver(task, policy); resolver.worker_writes = True
    real_peek = resolver.peek
    # Execution cell unresolvable, legacy fine -> legacy kept (no candidate).
    monkeypatch.setattr(resolver, "peek", lambda role, *, write=False: None if role == "worker_balanced" else real_peek(role, write=write))
    cand, legacy = rt.select_worker(task, "LOW", "NORMAL", policy, resolver)
    assert cand is legacy and legacy.role == "worker_fast"
    # Both unresolvable -> legacy kept, and the existing SUPPLY_EXHAUSTED path owns the outcome.
    monkeypatch.setattr(resolver, "peek", lambda role, *, write=False: None)
    cand, legacy = rt.select_worker(task, "LOW", "NORMAL", policy, resolver)
    assert cand is legacy


# --- T10: the MEDIUM reviewer follows the stronger worker (intended) ---------

def test_t10_medium_reviewer_identity_follows_the_raised_worker():
    out = r(task_class="IMPLEMENTATION", complexity=3, blast_radius=1)     # risk 5 MEDIUM, exec 9 NORMAL
    assert out["selected_role"] == "worker_balanced"
    assert out["review"]["band"] == "MEDIUM" and out["review"]["effort"] == "HIGH"
    assert out["review"]["reviewers"] == ["reasoning_specialist"]
    assert out["review"]["reviewer_models"] == [ID("openai_reasoning")]
