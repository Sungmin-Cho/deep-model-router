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
