"""Model lineages (design DD-A1): template parsing, generation order, and the
Policy load checks that keep a lineage declaration honest.

The parser fixtures mirror the DD-A1 table's shapes under a synthetic `demo-`
prefix: they are inputs to a pure function, and spelling them as vendor ids
would put registry ids (today's or a future promote's) into test code, which
`test_tests_never_hardcode_a_registry_model_id_in_code` forbids.
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from lineage import Generation, compare, is_successor, parse
from route_task import ConfigError, Policy, load_config

HAIKU = "demo-claude-haiku-{gen}[-{date}]"


# ---------------------------------------------------------------------------
# Parser — the DD-A1 fixture table, row by row
# ---------------------------------------------------------------------------

def test_dated_id_splits_generation_and_date():
    assert parse(HAIKU, "demo-claude-haiku-4-5-20251001") == Generation((4, 5), 20251001)
    assert parse(HAIKU, "demo-claude-haiku-4-5-20251001-20251002") is None


def test_longer_generation_outranks_a_dated_shorter_one():
    newer = parse(HAIKU, "demo-claude-haiku-4-5-1")
    assert newer == Generation((4, 5, 1), None)
    # generation first: (4,5,1) > (4,5,0) even though only the older one has a date
    assert compare(newer, parse(HAIKU, "demo-claude-haiku-4-5-20251001")) == 1
    assert is_successor(HAIKU, "demo-claude-haiku-4-5-20251001", "demo-claude-haiku-4-5-1")


def test_next_minor_is_a_successor():
    assert parse(HAIKU, "demo-claude-haiku-4-6") == Generation((4, 6), None)
    assert is_successor(HAIKU, "demo-claude-haiku-4-5-20251001", "demo-claude-haiku-4-6")


def test_anchored_match_rejects_a_suffixed_variant():
    assert parse("demo-grok-{gen}", "demo-grok-4.7-build-fast") is None
    assert not is_successor("demo-grok-{gen}", "demo-grok-4.7", "demo-grok-4.7-build-fast")


def test_dotted_to_major_is_a_successor():
    assert parse("demo-gpt-{gen}-sol", "demo-gpt-5.6-sol") == Generation((5, 6), None)
    assert parse("demo-gpt-{gen}-sol", "demo-gpt-6-sol") == Generation((6,), None)
    assert is_successor("demo-gpt-{gen}-sol", "demo-gpt-5.6-sol", "demo-gpt-6-sol")
    assert not is_successor("demo-gpt-{gen}-sol", "demo-gpt-6-sol", "demo-gpt-5.6-sol")


def test_zero_padding_makes_5_equal_5_0():
    assert compare(Generation((5,), None), Generation((5, 0), None)) == 0
    assert compare(Generation((5, 0, 0), None), Generation((5,), None)) == 0


def test_a_tie_is_not_a_successor():
    assert parse("demo-gpt-{gen}-sol", "demo-gpt-6.0-sol") == Generation((6, 0), None)
    assert not is_successor("demo-gpt-{gen}-sol", "demo-gpt-6-sol", "demo-gpt-6.0-sol")
    assert not is_successor("demo-gpt-{gen}-sol", "demo-gpt-6.0-sol", "demo-gpt-6-0-sol")
    assert not is_successor("demo-gpt-{gen}-sol", "demo-gpt-6-sol", "demo-gpt-6-sol")


def test_undated_sorts_below_dated_at_equal_generation():
    undated, dated = Generation((4, 5), None), Generation((4, 5), 20251001)
    assert compare(undated, dated) == -1
    assert compare(dated, undated) == 1
    assert compare(Generation((4, 5), 20250101), dated) == -1


def test_template_without_date_rejects_a_dated_snapshot():
    """A template without `[-{date}]` does not rank a dated snapshot: read as
    a generation part, `-20261101` would sort above every real generation
    (`…-5-20261101` > `…-5-9`). It is not a spelling of that lineage."""
    assert parse("demo-claude-haiku-{gen}", "demo-claude-haiku-4-5-20251001") is None
    assert parse("demo-claude-opus-{gen}", "demo-claude-opus-5-20261101") is None
    assert parse("demo-gpt-{gen}-sol", "demo-gpt-6-20261101-sol") is None
    assert not is_successor("demo-claude-opus-{gen}", "demo-claude-opus-5-9",
                            "demo-claude-opus-5-20261101")
    # Negative: ordinary multi-part generations and the dated template still parse.
    assert parse("demo-claude-opus-{gen}", "demo-claude-opus-5-5") == Generation((5, 5), None)
    assert parse("demo-claude-opus-{gen}", "demo-claude-opus-5-1234567") == \
        Generation((5, 1234567), None)
    assert parse(HAIKU, "demo-claude-haiku-4-5-20251001") == Generation((4, 5), 20251001)


def test_date_is_stripped_only_when_the_rest_still_matches():
    # "-20251001" is the WHOLE generation here; stripping it leaves no gen.
    assert parse(HAIKU, "demo-claude-haiku-20251001") == Generation((20251001,), None)
    assert parse(HAIKU, "demo-claude-haiku-4-5") == Generation((4, 5), None)


def test_non_matching_ids_parse_to_none():
    assert parse("demo-gpt-{gen}-sol", "demo-gpt-6-luna") is None
    assert parse("demo-gpt-{gen}-sol", "xdemo-gpt-6-sol") is None
    assert parse("demo-gpt-{gen}-sol", "demo-gpt--sol") is None
    assert parse("demo-claude-opus-{gen}", "demo-claude-opus-5.") is None
    assert not is_successor("demo-gpt-{gen}-sol", "demo-gpt-5.6-sol", "demo-gpt-6-luna")


def test_template_literals_are_escaped():
    # `.` in a template is a literal dot, not "any character"
    assert parse("a.b-{gen}", "a.b-1") == Generation((1,), None)
    assert parse("a.b-{gen}", "axb-1") is None


@pytest.mark.parametrize("template", [
    "demo-gpt-sol",                       # no {gen}
    "demo-gpt-{gen}-{gen}",               # two {gen}
    "demo-gpt-{generation}-sol",          # unknown placeholder
    "demo-gpt-{gen}-{date}",              # bare {date}
    "demo-claude-haiku-[-{date}]{gen}",   # optional date not at the end
    "demo-claude-haiku-{gen}[-{date}][-{date}]",
    "demo-gpt-{gen}-sol}",                # stray brace
    "",
])
def test_template_syntax_errors_raise(template):
    with pytest.raises(ValueError):
        parse(template, "demo-gpt-6-sol")


# ---------------------------------------------------------------------------
# Policy load checks
# ---------------------------------------------------------------------------

def _cfg():
    return copy.deepcopy(load_config())


def test_the_shipped_registry_declares_a_lineage_on_every_dispatchable_row():
    cfg = load_config()
    Policy(cfg)
    rows = {k: m for k, m in cfg["models"].items() if m.get("dispatchable", True) is not False}
    assert len(rows) == 9, sorted(rows)
    for key, row in rows.items():
        lin = row["lineage"]
        assert parse(lin["template"], row["id"]) is not None, key


def test_duplicate_line_is_a_config_error():
    cfg = _cfg()
    cfg["models"]["claude_senior"]["lineage"]["line"] = \
        cfg["models"]["claude_worker_balanced"]["lineage"]["line"]
    with pytest.raises(ConfigError, match="line"):
        Policy(cfg)


def test_row_id_outside_its_own_template_is_a_config_error():
    cfg = _cfg()
    cfg["models"]["claude_senior"]["lineage"]["template"] = \
        cfg["models"]["claude_worker_balanced"]["lineage"]["template"]
    with pytest.raises(ConfigError, match="template"):
        Policy(cfg)


def test_two_rows_with_the_same_id_is_a_config_error():
    """Before this check the `id_to_key` comprehension let the last row win in
    silence, so one of the two rows simply stopped existing for the router."""
    cfg = _cfg()
    cfg["models"]["claude_twin"] = copy.deepcopy(cfg["models"]["claude_senior"])
    cfg["models"]["claude_twin"]["lineage"]["line"] = "claude/twin"
    with pytest.raises(ConfigError, match="same id"):
        Policy(cfg)


def test_dispatchable_row_without_lineage_is_a_config_error():
    cfg = _cfg()
    del cfg["models"]["openai_reasoning"]["lineage"]
    with pytest.raises(ConfigError, match="lineage"):
        Policy(cfg)


def test_non_dispatchable_row_needs_no_lineage():
    cfg = _cfg()
    unbound = [k for k, m in cfg["models"].items()
               if m.get("dispatchable", True) is False and "history_of" not in m]
    assert unbound
    for key in unbound:
        assert "lineage" not in cfg["models"][key]
    Policy(cfg)


@pytest.mark.parametrize("mutate", [
    lambda lin: lin.update(template="gpt-{gen}-{gen}"),
    lambda lin: lin.update(line=""),
    lambda lin: lin.update(line=7),
    lambda lin: lin.update(extra="x"),
    lambda lin: lin.pop("template"),
    lambda lin: lin.update(served_forms="{id}"),
    lambda lin: lin.update(served_forms=["build"]),
    lambda lin: lin.update(catalog_name=""),
])
def test_malformed_lineage_is_a_config_error(mutate):
    cfg = _cfg()
    mutate(cfg["models"]["xai_frontier"]["lineage"])
    with pytest.raises(ConfigError):
        Policy(cfg)


def test_lineage_that_is_not_a_mapping_is_a_config_error():
    cfg = _cfg()
    cfg["models"]["xai_frontier"]["lineage"] = "xai/grok"
    with pytest.raises(ConfigError, match="lineage"):
        Policy(cfg)
