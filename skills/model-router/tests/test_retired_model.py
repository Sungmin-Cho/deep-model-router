"""History rows (design 2026-09-25 DD-A3): a superseded id stays in the
registry as a non-dispatchable row that names its live row with `history_of`,
under the lossless key `<live key>@<id>`. Valid as HISTORY input
(`--prior-models`, `--unavailable-models`, `--host-model`), never seated.

Rows are found by `history_of`, never by a key suffix. The expected CLI exit
for a prior failure on each history id lives in `fixtures/id-succession.json`
and is set by a person, not derived from the router under test.
"""
import copy
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from route_task import ConfigError, Policy, Task, default_config, load_config, route

CFG = default_config()
ID = lambda key: CFG["models"][key]["id"]          # noqa: E731
ARCHITECT_ID = ID("claude_architect")
BASE = dict(task_class="IMPLEMENTATION", complexity=1, uncertainty=1,
            blast_radius=1, reversibility=1, runtime="claude_code")
SUCCESSION = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "id-succession.json").read_text())

# Every history row, not just the first one written. A generation bump that
# adds a row and forgets it here would leave the new row untested while every
# assertion below still passed on the architect's.
HISTORY_KEYS = sorted(k for k, m in CFG["models"].items() if "history_of" in m)

# What `--prior-failures 1 --prior-models <history id>` must exit with, per
# key. The ladder's answer depends on the row's tier — a tier-3 failure
# exhausts it (1, HUMAN_REQUIRED) and a tier-1 failure escalates (0) — so the
# expectation is stated per key rather than relaxed to "0 or 1". Copied
# verbatim from the pre-generalisation table; a new history row must name its
# own expectation in the fixture.
PRIOR_FAILURE_EXIT = SUCCESSION["prior_failure_exit"]

# The architect's history row: the one the tier-3 assertions below are about.
ARCHITECT_HISTORY_KEY = SUCCESSION["key_renames"]["claude_architect_retired"]

# A host model must belong to its runtime's native family, so `--host-model`
# on a history id has to be asked from the runtime that family is native to.
NATIVE_RUNTIME = {CFG["role_bindings"][spec["degraded_binding"]]["senior_engineer"]: rt
                  for rt, spec in CFG["runtimes"].items()}
RUNTIME_OF_FAMILY = {CFG["models"][key]["family"]: rt
                     for key, rt in NATIVE_RUNTIME.items()}


def _t(**over):
    return Task(**{**BASE, **over})


def _r(**over):
    return route(_t(**over), CFG)


def _history(key=ARCHITECT_HISTORY_KEY):
    row = CFG["models"].get(key)
    assert row is not None, f"{key} is not in the registry"
    assert "history_of" in row, f"{key} is not a history row"
    return row


def test_every_history_key_names_a_distinct_id_and_is_verified():
    """A history row exists to answer for one concrete past id. Two rows on the
    same id, or an unverified one, would make it answer for nothing."""
    assert HISTORY_KEYS, "the registry has no history row at all"
    assert ARCHITECT_HISTORY_KEY in HISTORY_KEYS
    ids = [_history(k)["id"] for k in HISTORY_KEYS]
    assert len(set(ids)) == len(ids), ids
    live = {m["id"] for k, m in CFG["models"].items() if "history_of" not in m}
    for key, hid in zip(HISTORY_KEYS, ids):
        assert hid not in live, (key, hid, "a history id is still seated live")
        assert _history(key)["verified"] is True, key
        assert key == f"{_history(key)['history_of']}@{hid}", key


def test_the_fixture_names_exactly_the_history_rows():
    assert sorted(PRIOR_FAILURE_EXIT) == HISTORY_KEYS
    # Every history row is a superseded link of a succession chain. A rename
    # names only the pre-DD-A3 `_retired` keys; a row that `promote` added
    # never had one, so the renames cover a subset of the rows, not all.
    assert sorted(f"{key}@{was}" for key, chain in SUCCESSION["chains"].items()
                  for was in chain[:-1]) == HISTORY_KEYS
    assert set(SUCCESSION["key_renames"].values()) <= set(HISTORY_KEYS)


@pytest.mark.parametrize("key", HISTORY_KEYS)
def test_history_key_is_bound_nowhere_and_not_dispatchable(key):
    assert _history(key)["dispatchable"] is False
    for binding in CFG["role_bindings"].values():
        assert key not in binding.values(), binding
    for runtime, per_role in CFG["fallbacks"].items():
        for role, keys in per_role.items():
            assert key not in keys, (runtime, role, keys)


@pytest.mark.parametrize("key", HISTORY_KEYS)
def test_history_id_is_valid_history_input_and_is_never_re_seated(key):
    history_id = _history(key)["id"]
    base = _r()
    withheld = _r(unavailable_models=[history_id])
    assert withheld["selected_model"] == base["selected_model"]
    assert withheld["fallbacks_applied"] == []                       # nothing was removed
    failed = _r(prior_failures=1, prior_models=[history_id])
    # Where the ladder goes depends on the history row's tier — a tier-3
    # failure exhausts it, a tier-1 failure escalates — but a history id is
    # never what comes back out.
    assert failed["selected_model"] != history_id


def test_architect_history_failure_exhausts_the_ladder():
    failed = _r(prior_failures=1, prior_models=[_history()["id"]])
    assert failed["terminal"] == "HUMAN_REQUIRED"                    # a tier-3 failure has no tier above: valid input, exhausted ladder [P2-sol-F8]
    assert failed["selected_model"] is None


@pytest.mark.parametrize("key", HISTORY_KEYS)
def test_history_id_cli_exit_statuses_are_never_invalid_input(key):
    """Valid history input means exit 0 or 1 — never 2 (invalid input)."""
    script = Path(__file__).resolve().parent.parent / "scripts" / "route_task.py"
    row = _history(key)
    runtime = RUNTIME_OF_FAMILY[row["family"]]
    base = [sys.executable, str(script), "--runtime", runtime, "--class", "IMPLEMENTATION",
            "--complexity", "1", "--uncertainty", "1", "--blast-radius", "1", "--reversibility", "1"]
    hid = row["id"]
    run = lambda *extra: subprocess.run(base + list(extra), capture_output=True, text=True).returncode  # noqa: E731
    assert run("--unavailable-models", hid) == 0
    assert run("--host-model", hid, "--host-effort", "HIGH") == 0
    assert key in PRIOR_FAILURE_EXIT, (key, "name this key's expected exit status")
    assert run("--prior-failures", "1", "--prior-models", hid) == PRIOR_FAILURE_EXIT[key]


def test_history_host_model_is_still_compared_by_tier():
    task = _t(task_class="MECHANICAL", complexity=1, uncertainty=0, blast_radius=0, reversibility=0)
    task._host_seat = {"model": _history()["id"], "effort": "HIGH"}
    assert route(task, CFG)["host_seat_advisory"]["model_comparison"] == "above"


def test_live_architect_host_model_compares_above_on_low_work():
    task = _t(task_class="MECHANICAL", complexity=1, uncertainty=0, blast_radius=0, reversibility=0)
    task._host_seat = {"model": ARCHITECT_ID, "effort": "HIGH"}
    assert route(task, CFG)["host_seat_advisory"]["model_comparison"] == "above"


# ---------------------------------------------------------------------------
# Policy checks on `history_of` — each one refused at load
# ---------------------------------------------------------------------------

def _cfg_with(key=ARCHITECT_HISTORY_KEY):
    cfg = copy.deepcopy(load_config())
    return cfg, cfg["models"][key]


def _rekey(cfg, old, new):
    cfg["models"] = {(new if k == old else k): v for k, v in cfg["models"].items()}


def test_history_row_pointing_at_no_row_is_a_config_error():
    cfg, row = _cfg_with()
    row["history_of"] = "no_such_row"
    _rekey(cfg, ARCHITECT_HISTORY_KEY, f"no_such_row@{row['id']}")
    with pytest.raises(ConfigError, match="history_of"):
        Policy(cfg)


def test_history_row_pointing_at_a_row_without_lineage_is_a_config_error():
    cfg, row = _cfg_with()
    target = next(k for k, m in cfg["models"].items()
                  if "lineage" not in m and "history_of" not in m)
    row["history_of"] = target
    row["family"] = cfg["models"][target]["family"]
    _rekey(cfg, ARCHITECT_HISTORY_KEY, f"{target}@{row['id']}")
    with pytest.raises(ConfigError, match="lineage"):
        Policy(cfg)


def test_history_row_of_another_family_is_a_config_error():
    cfg, row = _cfg_with()
    row["family"] = next(f for f in cfg["effort_map"] if f != row["family"])
    with pytest.raises(ConfigError, match="family"):
        Policy(cfg)


def test_history_id_at_a_higher_generation_is_a_config_error():
    """A history row answers for a PAST id. Pointing it at a newer spelling of
    the lineage would make the live row the history of its own successor."""
    cfg, row = _cfg_with()
    live = cfg["models"][row["history_of"]]["id"]
    newer = live + "-9"                   # one more generation component
    old_key = ARCHITECT_HISTORY_KEY
    row["id"] = newer
    _rekey(cfg, old_key, f"{row['history_of']}@{newer}")
    with pytest.raises(ConfigError, match="generation"):
        Policy(cfg)


def test_history_id_at_the_live_generation_is_a_config_error():
    cfg, row = _cfg_with(SUCCESSION["key_renames"]["xai_frontier_retired"])
    live = cfg["models"][row["history_of"]]["id"]
    tie = live + ".0"                     # same generation, different spelling
    row["id"] = tie
    _rekey(cfg, SUCCESSION["key_renames"]["xai_frontier_retired"], f"{row['history_of']}@{tie}")
    with pytest.raises(ConfigError, match="generation"):
        Policy(cfg)


def test_history_id_outside_the_live_template_is_a_config_error():
    cfg, row = _cfg_with()
    other = ID("claude_senior") + "-x"
    row["id"] = other
    _rekey(cfg, ARCHITECT_HISTORY_KEY, f"{row['history_of']}@{other}")
    with pytest.raises(ConfigError, match="template"):
        Policy(cfg)


def test_history_row_in_a_binding_is_a_config_error():
    cfg, _row = _cfg_with()
    cfg["role_bindings"]["claude_only"]["principal_architect"] = ARCHITECT_HISTORY_KEY
    with pytest.raises(ConfigError, match="bound|binding"):
        Policy(cfg)


def test_history_row_in_a_fallback_list_is_a_config_error():
    cfg, _row = _cfg_with()
    runtime = next(iter(cfg["fallbacks"]))
    role = next(iter(cfg["fallbacks"][runtime]))
    cfg["fallbacks"][runtime][role] = list(cfg["fallbacks"][runtime][role]) + [ARCHITECT_HISTORY_KEY]
    with pytest.raises(ConfigError, match="fallback"):
        Policy(cfg)


@pytest.mark.parametrize("bad_key", [
    "claude_architect_retired",                  # the old suffix form
    "claude_architect@claude-fable",             # a slug, not the id
    "claude_senior@{id}",                        # wrong live key
])
def test_history_key_other_than_live_at_id_is_a_config_error(bad_key):
    cfg, row = _cfg_with()
    _rekey(cfg, ARCHITECT_HISTORY_KEY, bad_key.format(id=row["id"]))
    with pytest.raises(ConfigError, match="@"):
        Policy(cfg)


def test_dispatchable_history_row_is_a_config_error():
    cfg, row = _cfg_with()
    row["dispatchable"] = True
    with pytest.raises(ConfigError, match="dispatchable"):
        Policy(cfg)


def test_at_sign_key_without_history_of_is_a_config_error():
    cfg, row = _cfg_with()
    del row["history_of"]
    with pytest.raises(ConfigError, match="@"):
        Policy(cfg)
