"""The gemini reviewer seat (design 2026-10-14, model-router 1.20.0; on
gemini-4-argon since 1.24.0, design 2026-10-27, with gemini-3.8-flash retired).

A dispatchable row in the gemini family, reachable from Claude Code and Codex
hosts, through a read-only `agy` print-mode recipe (Claude Code also ships a
maker recipe without write authority). It is bound to no role and sits behind
the Claude balanced model (and, on Claude Code, the meta seat) in the
worker_balanced fallback lists, so it is seated only where everything ahead of
it does not resolve for the route — and never where the work writes.

No agy flag contains this seat. The supervisor's `agy-reviewer-v1` profile
checks the reviewer agent, the deny-rule project, the telemetry setting and
the workspace before spawn, and the CLI's own log after the run. Every file
under fixtures/agy/ is a real agy 1.3.4 capture (the logs are excerpts) with
local paths and the account scrubbed; the fake `agy` below writes log lines in
the same spelling, which test_the_real_log_excerpts_grade_as_captured pins.
Nothing here runs the CLI or a model.

Run:  python3 -m pytest skills/model-router/tests/test_gemini_seat.py -q
"""

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

import dispatch_agent  # noqa: E402
import lineage  # noqa: E402
import model_sync  # noqa: E402
from route_task import Policy, Task, load_config, route  # noqa: E402

CFG = load_config()
ID = lambda key: CFG["models"][key]["id"]                        # noqa: E731
POLICY = Policy.of(CFG)
SEAT_KEY = "gemini_frontier"
GEMINI = ID(SEAT_KEY)
META = ID("meta_worker_balanced")
XAI = ID("xai_frontier")
BOTH_OUT = [XAI, META]
# Everything ahead of this seat in claude_code's worker_balanced list since
# 1.21.0 (user decision U-6): the xai seat, the Claude balanced model and meta.
AHEAD_OUT = [XAI, ID("claude_worker_balanced"), META]
SCRIPT = SKILL / "scripts" / "dispatch_agent.py"
FIXTURES = SKILL / "tests" / "fixtures" / "agy"
RECIPE = CFG["transports"]["claude_code"]["to_gemini"]["mechanism_reviewer"]
LOADED = dispatch_agent.AGY_GRANTS_LOADED
PROJECT = dispatch_agent.AGY_REVIEWER_PROJECT
# The agy 1.3.4 captures under fixtures/agy/ are runs of the seat's first model,
# retired in 1.24.0; they are graded against the id they ran (the log gate pins
# the declared model). The agy 1.4.8 capture is a run of the current seat.
FIXTURE_MODEL_134 = ID("gemini_worker_balanced")


# ---------------------------------------------------------------------------
# The registry row, the family map and the transport
# ---------------------------------------------------------------------------

def test_the_row_is_a_dispatchable_tier_1_seat_bound_to_no_role():
    """Tier 1 since 1.24.0: the 2026-10-27 real-review replay (T5) put
    gemini-4-argon at 0.233 recall, below the weakest live tier-2 row's bar
    (0.282) and above the tier-1 bar (0.118). The seat it replaced was tier 0."""
    row = CFG["models"][SEAT_KEY]
    assert row["family"] == "gemini"
    assert row["capability_tier"] == 1
    assert row.get("dispatchable", True) is True
    assert row["effort_ceiling"] == "HIGH"
    bound = {k for binding in CFG["role_bindings"].values() for k in binding.values()}
    assert SEAT_KEY not in bound


def test_the_unbound_gemini_row_is_untouched_and_outside_the_lineage():
    """deep-loop pins the unbound row's id; the seat's lineage must not adopt
    it as history, nor the catalogue's effort-suffixed spellings as successors."""
    old = CFG["models"]["gemini_flash"]
    assert old["dispatchable"] is False and old["verified"] is False
    template = CFG["models"][SEAT_KEY]["lineage"]["template"]
    assert lineage.parse(template, GEMINI) is not None
    assert lineage.parse(template, old["id"]) is None
    for effort in ("low", "medium", "high"):
        assert lineage.parse(template, f"{GEMINI}-{effort}") is None


def test_the_ceiling_keeps_every_routed_effort_inside_the_probed_tokens():
    """The CLI accepts low, medium and high only; the ceiling clamps before the
    family map is read, so VERY_HIGH and MAX never reach it for this seat."""
    assert POLICY.ceiling_of[GEMINI] == "HIGH"
    probed = {"low", "medium", "high"}
    for level in ("MINIMAL", "LOW", "MEDIUM", "HIGH"):
        assert CFG["effort_map"]["gemini"][level] in probed, level


def test_claude_code_and_codex_offer_the_seat_and_only_to_read():
    """1.21.0: the Codex direction is probed (design DD-60); the grok one is
    not. Claude Code ships a maker recipe (design DD-59) whose write
    authority is withheld (user decision U-5), so no host can seat this
    family where the work writes."""
    for runtime, entries in CFG["transports"].items():
        assert ("to_gemini" in entries) == (runtime in ("claude_code", "codex")), runtime
    cc = CFG["transports"]["claude_code"]["to_gemini"]
    assert cc["verified"] is True and cc["write_verified"] is False
    assert set(k for k in cc if k.startswith("mechanism")) == {"mechanism_reviewer",
                                                              "mechanism_maker"}
    cx = CFG["transports"]["codex"]["to_gemini"]
    assert cx["verified"] is True and cx["write_verified"] is False
    assert set(k for k in cx if k.startswith("mechanism")) == {"mechanism_reviewer"}
    assert cx["mechanism_reviewer"] == cc["mechanism_reviewer"]
    for runtime in CFG["runtimes"]:
        assert POLICY.write_capable(runtime, "gemini") is False, runtime


def test_the_fallback_lists_name_the_seat_after_claude_on_claude_code_and_codex():
    """1.21.0 (user decisions U-6, U-7): the Claude balanced model first on
    both hosts; on Claude Code the meta seat, then this one; on Codex this one
    right after Claude. Grok hosts never list it."""
    want = {("claude_code", "worker_balanced"): [
                "claude_worker_balanced", "meta_worker_balanced", SEAT_KEY,
                "openai_worker_balanced", "claude_senior"],
            ("codex", "worker_balanced"): [
                "claude_worker_balanced", SEAT_KEY, "openai_worker_balanced",
                "openai_reasoning"]}
    for runtime, per_role in CFG["fallbacks"].items():
        for role, keys in per_role.items():
            if (runtime, role) in want:
                assert keys == want[(runtime, role)], (runtime, keys)
            else:
                assert SEAT_KEY not in keys, (runtime, role)


def _recipe_argv(log="/abs/agy.log", prompt="review this"):
    toks = RECIPE.replace('"<prompt>"', "<prompt>").split()
    subs = {"<id>": GEMINI, "<native-effort>": "medium", "<agy-log>": log, "<prompt>": prompt}
    return [subs.get(t, t) for t in toks]


def test_the_reviewer_recipe_is_the_profile_argv():
    """The shipped string, with its slots filled, is exactly what the
    supervisor admits — so the YAML and the gate cannot drift apart."""
    argv = _recipe_argv()
    assert dispatch_agent._agy_argv_problem(argv, Path("/abs/agy.log"), GEMINI) is None
    flags, _ = dispatch_agent._agy_flag_view(argv)
    assert flags["agent"] == dispatch_agent.AGY_REVIEWER_AGENT
    assert flags["project"] == PROJECT
    assert flags["output-format"] == "json"
    assert flags["print-timeout"] == "0s"
    assert flags["disable-slash-commands"] == "true"
    for forbidden in ("--mode", "--dangerously-skip-permissions", "--add-dir",
                      "--sandbox", "--continue", "|"):
        assert forbidden not in RECIPE.split(), forbidden


def test_the_ledger_row_binds_the_shipped_recipe():
    from test_docs import _assert_direction_row_binds, _whole_token
    needle = "transports.claude_code.to_gemini.mechanism_reviewer"
    rows = [e for e in CFG["verification_ledger"]["entries"]
            if _whole_token(needle, str(e.get("item", "")))]
    assert rows, "no ledger row vouches for the gemini reviewer recipe"
    for row in rows:
        assert row["status"] == "verified", row["item"]
        _assert_direction_row_binds(RECIPE, row, row["item"])


def test_the_adapters_fences_mirror_the_recipe_and_the_project():
    text = (SKILL / "references" / "adapters.md").read_text()
    fences = re.findall(r"```bash\n(agy [^`]*)```", text)
    assert fences, "adapters.md has no agy fence"
    want = RECIPE.split()
    assert any([t for t in f.split() if t != "\\"] == want for f in fences), fences
    docs = re.findall(r"```json\n(\{[^`]*\"" + PROJECT + r"\"[^`]*)```", text)
    assert docs, "adapters.md does not show the reviewer project document"
    assert json.loads(docs[0]) == dispatch_agent.AGY_REVIEWER_PROJECT_DOC
    agents = re.findall(r"```markdown\n(---\nname: " + dispatch_agent.AGY_REVIEWER_AGENT
                        + r"\n[^`]*)```", text)
    assert agents == [dispatch_agent.AGY_REVIEWER_AGENT_TEXT], agents


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def without_seat(cfg):
    out = copy.deepcopy(cfg)
    del out["models"][SEAT_KEY]
    for entries in out["transports"].values():
        entries.pop("to_gemini", None)
    for per_role in out["fallbacks"].values():
        for role, lst in per_role.items():
            per_role[role] = [k for k in lst if k != SEAT_KEY]
    return out


BASE = without_seat(CFG)
CLASSES = list(CFG["worker_selection"])


def _project(r):
    rv = r.get("review") or {}
    return (r.get("terminal"), r.get("selected_model"), r.get("selected_effort_effective"),
            tuple(rv.get("reviewer_models") or ()), rv.get("judge_model"), rv.get("band"),
            r.get("cross_family_review"), r.get("routing_confidence"),
            tuple(r.get("notes") or ()), tuple(r.get("fallbacks_applied") or ()))


def _emitted(r):
    rv = r.get("review") or {}
    return {r.get("selected_model"), rv.get("judge_model"), *(rv.get("reviewer_models") or ())}


@pytest.mark.parametrize("withheld", [[], [XAI]], ids=["all_up", "xai_out"])
@pytest.mark.parametrize("runtime", sorted(CFG["runtimes"]))
def test_while_the_meta_seat_resolves_no_route_moves(runtime, withheld):
    for cls in CLASSES:
        for dims in ((0, 0, 0, 0), (1, 1, 1, 1), (2, 1, 2, 1), (3, 2, 3, 2)):
            kw = dict(task_class=cls, complexity=dims[0], uncertainty=dims[1],
                      blast_radius=dims[2], reversibility=dims[3], runtime=runtime,
                      unavailable_models=withheld)
            assert _project(route(Task(**kw), CFG)) == _project(route(Task(**kw), BASE)), (cls, dims)


@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_a_low_xai_quota_moves_only_read_workers_to_this_same_tier_seat(runtime):
    """1.24.0: `low` moves the worker to the first same-tier model of another
    family in the list. The tier-1 seat is now that model on both hosts, ahead
    of the OpenAI balanced model, where the worker reads; nothing else moves
    (review i3: the Codex half too)."""
    extra = {"_family_quota": {"xai": "low"}}
    moved = 0
    for cls in CLASSES:
        for dims in ((1, 1, 1, 0), (2, 1, 2, 1), (3, 2, 3, 2)):
            kw = dict(task_class=cls, complexity=dims[0], uncertainty=dims[1],
                      blast_radius=dims[2], reversibility=dims[3], runtime=runtime,
                      **copy.deepcopy(extra))
            after, before = route(Task(**kw), CFG), route(Task(**kw), BASE)
            if _project(after) == _project(before):
                continue
            moved += 1
            assert POLICY.worker_seat_kind(cls) == "read_only", (cls, dims)
            assert after["selected_model"] == GEMINI, (cls, dims)
            assert POLICY.tier_of[before["selected_model"]] == POLICY.tier_of[GEMINI], (cls, dims)
    assert moved, "the swap never reached the seat"
    # Review i2 (fable): the write half — a writer on the balanced role under the
    # same swap never takes this read-only seat; its route is the one the
    # registry without the seat gives (on Claude Code the same-tier OpenAI
    # balanced model).
    kw = dict(task_class="IMPLEMENTATION", complexity=0, uncertainty=0, blast_radius=3,
              reversibility=2, runtime=runtime, **copy.deepcopy(extra))
    out, without = route(Task(**kw), CFG), route(Task(**kw), BASE)
    assert out["selected_role"] == "worker_balanced"
    assert out["selected_model"] != GEMINI
    assert out["selected_model"] == without["selected_model"]
    if runtime == "claude_code":
        assert out["selected_model"] == ID("openai_worker_balanced"), out["selected_model"]
        assert any(n.startswith("family_quota: xai low") for n in out["notes"])


@pytest.mark.parametrize("extra", [
    {"_review_context": {"target_sha256": "0" * 64, "author_families": ["xai"]}},
    "allowed-without-xai",
], ids=["xai-author", "allowed-without-xai"])
def test_the_other_ways_the_xai_seat_drops_out_move_nothing_either(extra):
    """design review r1 fable: the replay covers these; the suite pins them."""
    if extra == "allowed-without-xai":
        extra = {"_local_policy": {"allowed_families": sorted(
            f for f in POLICY.families if f != "xai")}}
    for cls in CLASSES:
        if "_review_context" in extra and cls != "REVIEW":
            continue
        for dims in ((1, 1, 1, 0), (2, 1, 2, 1), (3, 2, 3, 2)):
            kw = dict(task_class=cls, complexity=dims[0], uncertainty=dims[1],
                      blast_radius=dims[2], reversibility=dims[3], runtime="claude_code",
                      **copy.deepcopy(extra))
            assert _project(route(Task(**kw), CFG)) == _project(route(Task(**kw), BASE)), (cls, dims)


def test_the_high_ceiling_adds_its_cause_to_a_gate_already_raised():
    """r2 sol: most of the disclosed cases join an existing CRITICAL gate."""
    kw = dict(task_class="DOCUMENTATION", complexity=0, uncertainty=1, blast_radius=3,
              reversibility=3, runtime="claude_code", unavailable_models=AHEAD_OUT,
              worker_seat="read_only")
    after, before = route(Task(**kw), CFG), route(Task(**kw), BASE)
    assert before["requires_human_confirmation"] is True
    assert "effort_below_floor" not in (before.get("human_control_causes") or [])
    assert "effort_below_floor" in after["human_control_causes"]


def test_the_high_ceiling_can_raise_a_human_gate_and_says_why():
    """design review r1 sol F4 / fable F2: where this seat becomes a worker a
    floor wanted at VERY_HIGH, the effort is capped at HIGH and the route asks
    for a human with `effort_below_floor`; the registry without the seat seated
    a model that reached the floor."""
    kw = dict(task_class="DOCUMENTATION", complexity=3, uncertainty=3, blast_radius=0,
              reversibility=0, runtime="claude_code", unavailable_models=AHEAD_OUT,
              worker_seat="read_only")
    after, before = route(Task(**kw), CFG), route(Task(**kw), BASE)
    assert after["selected_model"] == GEMINI
    assert after["selected_effort_effective"] == "HIGH"
    assert "effort_below_floor" in after["human_control_causes"]
    assert after["requires_human_confirmation"] is True
    assert "effort_below_floor" not in (before.get("human_control_causes") or [])
    assert before["requires_human_confirmation"] is False


@pytest.mark.parametrize("runtime", ["grok"])
def test_no_other_host_ever_seats_the_gemini_model(runtime):
    """Grok hosts have no probed direction (1.21.0: the grok account refused
    the probes). Codex hosts do — see the test below."""
    for cls in CLASSES:
        for extra in ({"unavailable_models": BOTH_OUT},
                      {"unavailable_models": BOTH_OUT, "_family_quota": {"openai": "exhausted"}},
                      {"unavailable_models": BOTH_OUT, "_family_quota": {"claude": "exhausted"}}):
            out = route(Task(task_class=cls, complexity=1, uncertainty=1, blast_radius=1,
                             reversibility=1, runtime=runtime, **extra), CFG)
            assert GEMINI not in _emitted(out), (cls, extra)


def test_a_codex_host_seats_gemini_to_read_only_where_claude_does_not_resolve():
    """1.21.0 (design DD-60, user decision U-7): codex's worker_balanced list
    names this seat right after the Claude model, so while Claude resolves
    no Codex route moves; where it does not, a reviewer outside the host's
    family takes the seat — and never one that writes."""
    for cls in CLASSES:
        kw = dict(task_class=cls, complexity=1, uncertainty=1, blast_radius=1,
                  reversibility=1, runtime="codex", unavailable_models=[XAI])
        assert GEMINI not in _emitted(route(Task(**kw), CFG)), cls
        out = route(Task(**kw, _family_quota={"claude": "exhausted"}), CFG)
        if POLICY.worker_seat_kind(cls) == "write":
            assert out["selected_model"] != GEMINI, cls
    # A reading lead takes the seat; a MEDIUM REVIEW lead is reviewer-1 and needs
    # the tier-1 floor, which the tier-1 seat meets (1.24.0; the tier-0 seat it
    # replaced yielded to the OpenAI balanced model from 1.22.0).
    out = route(Task(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime="codex", unavailable_models=[XAI],
                     _family_quota={"claude": "exhausted"}), CFG)
    assert out["selected_model"] == GEMINI
    out = route(Task(task_class="REVIEW", complexity=1, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime="codex", unavailable_models=[XAI],
                     _family_quota={"claude": "exhausted"}), CFG)
    assert out["selected_model"] == GEMINI
    assert out["review"]["review_depth_reduced"] == []


def test_a_read_route_with_everything_ahead_out_seats_gemini_as_the_lead():
    out = route(Task(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime="claude_code", unavailable_models=AHEAD_OUT), CFG)
    assert out["selected_model"] == GEMINI
    assert out["selected_capability_tier"] == 1
    assert any(f.endswith(f"-> {GEMINI}") for f in out["fallbacks_applied"])
    # The same state for a MEDIUM REVIEW: its lead is a reviewer and MEDIUM's
    # floor is tier 1, which this seat meets (1.24.0), so it leads.
    out = route(Task(task_class="REVIEW", complexity=1, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime="claude_code", unavailable_models=AHEAD_OUT), CFG)
    assert out["selected_model"] == GEMINI
    assert out["review"]["review_depth_reduced"] == []


def test_an_exhausted_meta_quota_is_the_meta_seat_out():
    out = route(Task(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
                     reversibility=0, runtime="claude_code",
                     unavailable_models=[XAI, ID("claude_worker_balanced")],
                     _family_quota={"meta": "exhausted"}), CFG)
    assert out["selected_model"] == GEMINI


def test_an_author_excluded_meta_and_claude_seat_gemini_with_xai_out():
    out = route(Task(task_class="REVIEW", complexity=1, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime="claude_code", unavailable_models=[XAI],
                     _review_context={"target_sha256": "0" * 64,
                                      "author_families": ["meta", "claude"]}), CFG)
    # 1.24.0: the seat is tier 1, MEDIUM's floor, and outside the authors'
    # families, so it leads (from 1.22.0 to 1.23 the tier-0 seat yielded).
    assert out["selected_model"] == GEMINI
    assert out["cross_family_review"] is True


WRITE_AT_BALANCED = dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=1,
                         blast_radius=2, reversibility=1, runtime="claude_code",
                         unavailable_models=AHEAD_OUT)


def test_the_write_filter_is_per_role_so_a_balanced_writer_keeps_gemini_off_review():
    out = route(Task(**WRITE_AT_BALANCED), CFG)
    assert out["selected_role"] == "worker_balanced"
    assert out["selected_model"] == ID("openai_worker_balanced")
    assert GEMINI not in _emitted(out)
    assert any("no write-capable gemini seat on claude_code" in n for n in out["notes"]), \
        out["notes"]


@pytest.mark.parametrize("cls", ["IMPLEMENTATION", "DEBUGGING", "MIGRATION", "REFACTORING"])
def test_the_gemini_model_never_fills_a_write_seat(cls):
    for dims in ((1, 1, 1, 1), (2, 1, 2, 1), (2, 2, 2, 1), (3, 3, 2, 2)):
        for extra in ({"unavailable_models": BOTH_OUT},
                      {"unavailable_models": BOTH_OUT, "_family_quota": {"openai": "exhausted"}},
                      {"unavailable_models": BOTH_OUT, "_family_quota": {"claude": "exhausted"}},
                      {"unavailable_models": [XAI], "_family_quota": {"meta": "low"}}):
            out = route(Task(task_class=cls, complexity=dims[0], uncertainty=dims[1],
                             blast_radius=dims[2], reversibility=dims[3],
                             runtime="claude_code", **extra), CFG)
            assert out["selected_model"] != GEMINI, (cls, dims, extra)


def test_a_write_override_on_a_read_class_never_seats_gemini():
    for cls in ("REVIEW", "INVESTIGATION"):
        out = route(Task(task_class=cls, complexity=1, uncertainty=1, blast_radius=1,
                         reversibility=1, runtime="claude_code", worker_seat="write",
                         unavailable_models=BOTH_OUT), CFG)
        assert out["selected_model"] != GEMINI, cls


def test_an_allowed_families_list_of_gemini_alone_cannot_staff_a_write_route():
    out = route(Task(task_class="IMPLEMENTATION", complexity=1, uncertainty=1, blast_radius=1,
                     reversibility=1, runtime="claude_code",
                     _local_policy={"allowed_families": ["gemini"]}), CFG)
    assert out["terminal"] is not None
    assert out["selected_model"] is None


# ---------------------------------------------------------------------------
# Gemini 3.8 Flash retired (1.24.0, user instruction 2026-10-27)
# ---------------------------------------------------------------------------

RETIRED_KEY = "gemini_worker_balanced"
RETIRED = ID(RETIRED_KEY)


def test_the_retired_flash_row_is_history_input_only():
    """Not a seat: not dispatchable, no lineage (model_sync offers no
    successor), no binding, no fallback list. Not a `<live>@<id>` history row
    either: the seat that replaced it is another line, whose template does not
    parse its id."""
    row = CFG["models"][RETIRED_KEY]
    assert row["family"] == "gemini" and row["dispatchable"] is False
    assert "lineage" not in row and "history_of" not in row
    bound = {k for binding in CFG["role_bindings"].values() for k in binding.values()}
    listed = {k for per_role in CFG["fallbacks"].values() for keys in per_role.values() for k in keys}
    assert RETIRED_KEY not in bound and RETIRED_KEY not in listed
    template = CFG["models"][SEAT_KEY]["lineage"]["template"]
    assert lineage.parse(template, RETIRED) is None
    assert lineage.parse(template, GEMINI) is not None


def test_no_row_that_is_not_dispatchable_is_named_by_a_binding_or_fallback():
    """Review i1 (fable): history rows are refused there at load time; a retired
    row kept without `history_of` (the gemini seat's first model, the unbound
    gemini row) is held here, so re-listing one cannot pass as a live seat."""
    off = {k for k, m in CFG["models"].items() if m.get("dispatchable", True) is False}
    assert {RETIRED_KEY, "gemini_flash"} <= off
    named = {k for binding in CFG["role_bindings"].values() for k in binding.values()}
    named |= {k for per_role in CFG["fallbacks"].values() for keys in per_role.values() for k in keys}
    assert not (off & named), sorted(off & named)


@pytest.mark.parametrize("runtime", ["claude_code", "codex"])
def test_the_retired_flash_id_stays_valid_history_input(runtime):
    """A control loop holding a 1.23 attempt on it keeps working across the
    release boundary: as a failed model and as an unavailable model. (No host
    runs the gemini family, so it was never valid host input.)"""
    assert RETIRED in POLICY.model_ids
    base = dict(task_class="INVESTIGATION", complexity=1, uncertainty=1, blast_radius=1,
                reversibility=0, runtime=runtime, unavailable_models=[XAI])
    retry = route(Task(**base, prior_failures=1, prior_models=[RETIRED]), CFG)
    assert retry["terminal"] is None and RETIRED not in _emitted(retry)
    out = route(Task(**{**base, "unavailable_models": [XAI, RETIRED]}), CFG)
    assert out["terminal"] is None and RETIRED not in _emitted(out)


def test_the_retired_flash_model_is_never_emitted():
    """Wherever the gemini seat could be reached — everything ahead of it out,
    and the seat itself out too — the retired model never comes back."""
    scenarios = (
        {"unavailable_models": AHEAD_OUT},
        {"unavailable_models": [*AHEAD_OUT, GEMINI]},
        {"unavailable_models": [XAI, META], "_family_quota": {"claude": "exhausted"}},
        {"unavailable_models": [XAI, META, GEMINI], "_family_quota": {"claude": "exhausted"}},
        {"unavailable_models": [XAI, META, GEMINI], "_family_quota": {"openai": "exhausted"}},
        {"_local_policy": {"allowed_families": ["gemini"]}},
    )
    for runtime in sorted(CFG["runtimes"]):
        for cls in CLASSES:
            for dims in ((1, 1, 1, 0), (2, 2, 2, 1), (3, 3, 3, 3)):
                for extra in scenarios:
                    out = route(Task(task_class=cls, complexity=dims[0], uncertainty=dims[1],
                                     blast_radius=dims[2], reversibility=dims[3],
                                     runtime=runtime, **copy.deepcopy(extra)), CFG)
                    assert RETIRED not in _emitted(out), (runtime, cls, dims, extra)


def test_a_claude_shortage_keeps_the_fast_writer_for_this_seats_cross_family_review():
    """Disclosed in 1.24.0 (model-profiles.md): at tier 1 the seat staffs the
    MEDIUM review a write route needs once xai, meta and Claude are out, so the
    execution cell yields the OpenAI balanced writer to keep that cross-family
    review — the trade 1.19.0 disclosed for the meta seat, which the tier-0
    seat it replaced could not cause. Without the seat the cell raises the
    writer and an OpenAI model reviews it."""
    kw = dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=0,
              reversibility=0, runtime="claude_code", unavailable_models=[XAI, META],
              _family_quota={"claude": "exhausted"})
    after, before = route(Task(**kw), CFG), route(Task(**kw), BASE)
    assert after["selected_model"] == ID("openai_worker_fast")
    assert after["review"]["reviewer_models"] == [GEMINI]
    assert after["cross_family_review"] is True
    assert "execution band NORMAL yielded worker_balanced: cross_family_review" in after["notes"]
    assert before["selected_model"] == ID("openai_worker_balanced")
    assert before["cross_family_review"] is False
    assert GEMINI not in _emitted(before)


def _seat_at_tier_0():
    """The 1.23 routing shape: the gemini place held by a tier-0 seat."""
    cfg = copy.deepcopy(CFG)
    cfg["models"][SEAT_KEY]["capability_tier"] = 0
    return cfg


@pytest.mark.parametrize("scenario,expected", [
    ({"unavailable_models": [XAI], "_family_quota": {"claude": "exhausted"}},
     {(cls, (0, 3, 0, 0), lc, None, "ESCALATE_ROUTING")
      for cls in ("MECHANICAL", "DOCUMENTATION", "TESTING", "IMPLEMENTATION", "DEBUGGING")
      for lc in (False, True)}),
    ({"unavailable_models": [XAI, ID("openai_worker_fast"), ID("openai_frontier")],
      "_family_quota": {"claude": "exhausted"}},
     {("INVESTIGATION", d, lc, "INDEPENDENCE_UNAVAILABLE", "ESCALATE_ROUTING")
      for d in ((0, 3, 0, 0), (0, 3, 0, 1), (1, 3, 0, 0)) for lc in (False, True)}),
], ids=["xai-out-claude-exhausted", "fast-xai-frontier-out-claude-exhausted"])
def test_the_disclosed_terminal_changes_are_exactly_these(scenario, expected):
    """Review i1 (fable F1/F2): the tier-1 seat's place costs routes carrying an
    unknown root cause a fallback charge the tier-0 seat's yielding avoided, so
    on Codex these first attempts newly ask to be re-routed (0.59 < 0.60) —
    ten that were dispatchable and six that were INDEPENDENCE_UNAVAILABLE. No
    other first attempt in these scenarios changes terminal."""
    import itertools
    old, got = _seat_at_tier_0(), set()
    for cls in CLASSES:
        for dims in itertools.product(range(4), repeat=4):
            for lc in (False, True):
                kw = dict(task_class=cls, complexity=dims[0], uncertainty=dims[1],
                          blast_radius=dims[2], reversibility=dims[3], runtime="codex",
                          flags=["unknown_root_cause", *(["large_context"] if lc else [])],
                          **copy.deepcopy(scenario))
                before, after = route(Task(**kw), old), route(Task(**kw), CFG)
                if before.get("terminal") != after.get("terminal"):
                    got.add((cls, dims, lc, before.get("terminal"), after.get("terminal")))
                    assert after["routing_confidence"] < 0.60, (cls, dims, lc)
    assert got == expected


@pytest.mark.parametrize("withheld,before_key", [
    ([], "openai_reasoning"),
    (["openai_reasoning"], "openai_frontier"),
], ids=["sol-available", "sol-out"])
def test_a_retry_after_the_meta_seat_fails_now_lands_on_this_seat(withheld, before_key):
    """Review i2 (fable F2) / i3 (fable F1), disclosed: a retry climbs strictly
    above the tier that failed. After the tier-0 meta seat fails on Claude Code
    with xai out and Claude exhausted, the first model above tier 0 is now this
    tier-1 seat, where the tier-0 shape went on to the Sol seat (tier 2), or the
    frontier OpenAI seat (tier 3) with Sol out too — a retry lower than before,
    still above what failed."""
    kw = dict(task_class="INVESTIGATION", complexity=0, uncertainty=0, blast_radius=2,
              reversibility=0, runtime="claude_code",
              unavailable_models=[XAI, *(ID(k) for k in withheld)],
              _family_quota={"claude": "exhausted"})
    old = _seat_at_tier_0()
    assert route(Task(**kw), CFG)["selected_model"] == META == route(Task(**kw), old)["selected_model"]
    after = route(Task(**kw, prior_failures=1, prior_models=[META]), CFG)
    before = route(Task(**kw, prior_failures=1, prior_models=[META]), old)
    assert after["selected_model"] == GEMINI and after["selected_capability_tier"] == 1
    assert before["selected_model"] == ID(before_key)
    assert after["selected_capability_tier"] > POLICY.tier_of[META]


def test_the_argon_row_states_its_documented_facts():
    """The provider's pricing and model pages (2026-10-27): introductory rates
    through 2026-11-30 that double on 2026-12-01, so the row goes stale before
    they move; implicit caching only (no cache-write rate, an unavailable
    quote); one 1,048,576-token window."""
    import datetime as dt
    row = CFG["models"][SEAT_KEY]
    price = row["price_per_mtok"]
    assert (price["input"], price["output"], price["cached_input"]) == (2.0, 10.0, 0.1)
    assert "cache_write" not in price and "long_context" not in price
    stale = dt.date.fromisoformat(str(price["verified_on"])) + dt.timedelta(days=price["max_age_days"])
    assert stale < dt.date(2026, 12, 1)
    assert row["context_window"] == 1048576
    assert row["lineage"]["served_forms"] == ["{id}", "{id}-low", "{id}-medium", "{id}-high"]


def test_the_unbound_gemini_row_is_still_never_emitted():
    for extra in ({}, {"unavailable_models": BOTH_OUT}):
        out = route(Task(task_class="REVIEW", complexity=1, uncertainty=1, blast_radius=1,
                         reversibility=1, runtime="claude_code", **extra), CFG)
        assert ID("gemini_flash") not in _emitted(out)


def test_a_downed_bridge_never_reaches_the_gemini_seat():
    for cls in CLASSES:
        out = route(Task(task_class=cls, complexity=1, uncertainty=1, blast_radius=1,
                         reversibility=1, runtime="claude_code", flags=["bridge_down"],
                         unavailable_models=BOTH_OUT), CFG)
        assert GEMINI not in _emitted(out), cls


# ---------------------------------------------------------------------------
# The supervisor: agy-print-json-v1 and agy-reviewer-v1
# ---------------------------------------------------------------------------

SUCCESS_DOC = FIXTURES / "agy-1.3.4-success.json"
ERROR_DOC = FIXTURES / "agy-1.3.4-error.json"
REVIEW_TEXT = "Looked at the diff; nothing blocks it.\n=== REVIEW ===\nverdict: PASS\nconfidence: 0.9\n"


def _home(tmp_path, *, project=None, settings=None, agent=None) -> Path:
    home = tmp_path / "home"
    projects = home / ".gemini" / "config" / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    agents = home / ".gemini" / "config" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    text = dispatch_agent.AGY_REVIEWER_AGENT_TEXT if agent is None else agent
    if text is not False:
        (agents / f"{dispatch_agent.AGY_REVIEWER_AGENT}.md").write_text(text)
    cli = home / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True, exist_ok=True)
    doc = dispatch_agent.AGY_REVIEWER_PROJECT_DOC if project is None else project
    if doc is not False:
        (projects / f"{PROJECT}.json").write_text(json.dumps(doc))
    conf = {"toolPermission": "always-proceed", "telemetryEnabled": False} \
        if settings is None else settings
    if conf is not False:
        (cli / "settings.json").write_text(json.dumps(conf))
    return home


def _fake_agy(tmp_path, *, doc=None, exit_code=0, grants=(LOADED,), resolved=None,
              replace_log=False, extra_lines=()) -> Path:
    """An executable named `agy` that writes the given log lines to its
    `--log-file` and the given document to stdout."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    resolved = resolved or f"{GEMINI}-medium"
    body = json.loads(SUCCESS_DOC.read_text()) if doc is None else doc
    if doc is None:
        body["response"] = REVIEW_TEXT
    lines = [f'I1014 11:00:00.000000       1 model_resolver.go:155] model alias '
             f'"{GEMINI}" resolved to "{resolved}"']
    lines += [f"I1014 11:00:01.000000       1 cli_setting_manager.go:211] "
              f"ApplyProjectPermissionGrants: {g}" for g in grants]
    lines += list(extra_lines)
    fake = bindir / "agy"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "log = args[args.index('--log-file') + 1]\n"
        f"if {replace_log!r}:\n"
        "    os.unlink(log)\n"
        f"with open(log, 'a') as f:\n"
        f"    f.write({json.dumps(chr(10).join(lines) + chr(10))})\n"
        f"sys.stdout.write({json.dumps(json.dumps(body))})\n"
        "sys.stdout.flush()\n"
        f"sys.exit({exit_code})\n")
    fake.chmod(0o755)
    return fake


def _argv(fake: Path, log: Path, **override):
    argv = _recipe_argv(log=str(log), prompt="Review the diff. End with a verdict line.")
    argv[0] = str(fake)
    for flag, value in override.items():
        i = argv.index(flag)
        argv[i + 1] = value
    return argv


def _workspace(tmp_path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _run(tmp_path, argv, *, home, log, attempt="g1", extra=None, transport="claude_code.to_gemini",
         schema="review", cwd=None, model_id=GEMINI):
    base = ["--output-envelope", "agy-print-json-v1", "--seat-profile", "agy-reviewer-v1",
            "--agy-log", str(log), "--transport-id", transport,
            "--child-cwd", str(cwd or _workspace(tmp_path))]
    if model_id is not None:
        base += ["--model-id", model_id]
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "run", "--attempt-id", attempt,
         "--receipt-dir", str(tmp_path / "receipts"), "--deadline-seconds", "60",
         "--grace-seconds", "1", "--seat", "reviewer-1", "--output-schema", schema,
         *(base if extra is None else extra), "--", *argv],
        capture_output=True, text=True, timeout=120, env=env)
    path = tmp_path / "receipts" / f"{attempt}.json"
    return proc, (json.loads(path.read_text()) if path.exists() else None)


def test_a_contained_review_is_succeeded_with_its_verdict_and_grants(tmp_path):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    proc, receipt = _run(tmp_path, _argv(_fake_agy(tmp_path), log), home=home, log=log)
    assert proc.returncode == 0, proc.stderr
    result = receipt["result"]
    assert result["state"] == "SUCCEEDED" and result["verdict"] == "PASS"
    assert result["envelope"]["stop_reason"] == "SUCCESS"
    assert result["envelope"]["error_type"] is None
    assert result["envelope"]["usage"]["input_tokens"] > 0
    assert result["agy_project_grants"]["lines"] == [LOADED]
    assert result["agy_project_grants"]["resolved_model"] == f"{GEMINI}-medium"
    assert receipt["seat_profile"] == "agy-reviewer-v1"
    assert receipt["agy_log"] == str(log)
    assert set(result["envelope"]) == {"parse_ok", "stop_reason", "session_id",
                                       "served_models", "error_type", "usage", "error_reason"}


def test_a_failed_document_is_failed_and_says_why(tmp_path):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    fake = _fake_agy(tmp_path, doc=json.loads(ERROR_DOC.read_text()), exit_code=1)
    proc, receipt = _run(tmp_path, _argv(fake, log), home=home, log=log)
    assert receipt["result"]["state"] == "FAILED"
    env = receipt["result"]["envelope"]
    assert env["error_type"] == "error" and env["stop_reason"] == "ERROR"
    assert "not recognized" in env["error_reason"]


@pytest.mark.parametrize("doc", [
    {"status": "SUCCESS", "response": REVIEW_TEXT, "error": ""},
    {"status": "SUCCESS", "response": REVIEW_TEXT, "error": None},
    {"status": "CANCELED", "response": REVIEW_TEXT},
    {"status": "INTERRUPTED", "response": REVIEW_TEXT},
    {"response": REVIEW_TEXT},
    {"status": "success", "response": REVIEW_TEXT},
    {"status": "SUCCESS"},
    {"status": "SUCCESS", "response": None},
    {"status": "SUCCESS", "response": 123},
    {"status": "SUCCESS", "response": [REVIEW_TEXT]},
], ids=["empty-error", "null-error", "canceled", "interrupted", "no-status", "lowercase",
        "no-response", "null-response", "numeric-response", "array-response"])
def test_anything_but_a_clean_success_document_is_invalid_output(tmp_path, doc):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    proc, receipt = _run(tmp_path, _argv(_fake_agy(tmp_path, doc=doc), log), home=home, log=log)
    assert receipt["result"]["state"] == "INVALID_OUTPUT", receipt["result"]
    assert receipt["result"]["verdict"] is None


def test_an_error_reason_is_capped():
    env = dispatch_agent._decode_envelope(
        json.dumps({"status": "ERROR", "error": "x" * 5000}).encode(),
        dispatch_agent.AGY_JSON_FORMAT)
    assert len(env["error_reason"]) == dispatch_agent.AGY_ERROR_REASON_MAX_CHARS


@pytest.mark.parametrize("data", [b"[]", b"PONG", b'{"status": "SUCCESS", "n": ' + b"9" * 5000 + b"}"],
                         ids=["array", "text", "long-int"])
def test_a_document_that_is_not_one_object_is_unparseable(data):
    env = dispatch_agent._decode_envelope(data, dispatch_agent.AGY_JSON_FORMAT)
    assert dispatch_agent._grade_envelope(env) == ["envelope_unparseable"]


def test_a_non_string_response_parses_but_carries_no_answer():
    """Python reads 1e999999 as inf: the document is one object, so it is not
    unparseable, but it has no answer, and the review schema then fails it."""
    env = dispatch_agent._decode_envelope(b'{"status": "SUCCESS", "response": 1e999999}',
                                          dispatch_agent.AGY_JSON_FORMAT)
    assert env["parse_ok"] is True and env["text"] is None


def test_the_document_keeps_the_shared_four_mib_budget():
    assert dispatch_agent._envelope_budget("agy-print-json-v1") == dispatch_agent.ENVELOPE_MAX_BYTES


# --- the grants gate ------------------------------------------------------

@pytest.mark.parametrize("grants,why", [
    ((), "none"),
    (('no grants for project "CLI Project", cleared project permissions',), "cleared"),
    (('stored 0 allow, 6 deny grants from project "other"',), "other"),
    ((LOADED, 'no grants for project "CLI Project", cleared project permissions'), "mixed"),
    (('stored 1 allow, 6 deny grants from project "dmr-reviewer-v1"',), "an-allow"),
    (('stored 0 allow, 6 deny grants from project "dmr-reviewer-v1"',), "six-denies"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_a_run_whose_log_does_not_show_the_deny_rules_loaded_is_invalid(tmp_path, grants, why):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    proc, receipt = _run(tmp_path, _argv(_fake_agy(tmp_path, grants=grants), log),
                         home=home, log=log)
    result = receipt["result"]
    assert result["state"] == "INVALID_OUTPUT", (why, result)
    assert any(r.startswith("agy_project_grants:") for r in result["invalid_reasons"]), why


def test_a_replaced_log_is_refused_by_identity(tmp_path):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    proc, receipt = _run(tmp_path, _argv(_fake_agy(tmp_path, replace_log=True), log),
                         home=home, log=log)
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert any(r.startswith("agy_log_identity_replaced:")
               for r in receipt["result"]["invalid_reasons"])


def test_an_oversized_log_fails_closed(tmp_path, monkeypatch):
    log = tmp_path / "big.log"
    log.write_bytes(b"x" * (dispatch_agent.AGY_LOG_MAX_BYTES + 1))
    view, reasons = dispatch_agent._grade_agy_log(log, {})
    assert reasons == ["agy_project_grants:log_oversized"]


# --- the preflight --------------------------------------------------------

def _refused(tmp_path, argv=None, *, home=None, log=None, extra=None, transport="claude_code.to_gemini",
             cwd=None):
    home = home or _home(tmp_path)
    log = log or tmp_path / "agy.log"
    fake = _fake_agy(tmp_path)
    proc, receipt = _run(tmp_path, argv or _argv(fake, log), home=home, log=log,
                         extra=extra, transport=transport, cwd=cwd)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert receipt is None
    return proc.stderr


@pytest.mark.parametrize("extra", [
    [],
    ["--output-envelope", "agy-print-json-v1"],
    ["--output-envelope", "claude-print-json-v1", "--seat-profile", "agy-reviewer-v1",
     "--agy-log", "LOG"],
    ["--seat-profile", "agy-reviewer-v1", "--agy-log", "LOG"],
], ids=["nothing", "envelope-only", "other-envelope", "profile-only"])
def test_a_gemini_transport_requires_the_envelope_and_the_profile(tmp_path, extra):
    log = tmp_path / "agy.log"
    extra = [str(log) if t == "LOG" else t for t in extra] + ["--transport-id", "claude_code.to_gemini"]
    _refused(tmp_path, log=log, extra=extra)
    assert not log.exists()


def test_a_log_without_the_profile_and_a_profile_without_a_log_are_refused(tmp_path):
    log = tmp_path / "agy.log"
    _refused(tmp_path, log=log, extra=["--output-envelope", "agy-print-json-v1",
                                       "--agy-log", str(log)], transport=None)
    _refused(tmp_path, log=log, extra=["--output-envelope", "agy-print-json-v1",
                                       "--seat-profile", "agy-reviewer-v1"], transport=None)


@pytest.mark.parametrize("mutate,why", [
    (lambda a: a + ["--mode", "plan"], "mode"),
    (lambda a: a + ["--dangerously-skip-permissions"], "bypass"),
    (lambda a: a + ["-add-dir", "/"], "add-dir"),
    (lambda a: a + ["extra"], "positional"),
    (lambda a: a + ["-p", "second prompt"], "two-prompts"),
    (lambda a: [x.replace("dmr-reviewer-v1", "default-cli-project") for x in a], "project"),
    (lambda a: [x for x in a if x != "--disable-slash-commands"], "slash"),
    (lambda a: [("--disable-slash-commands=false" if x == "--disable-slash-commands" else x)
                for x in a], "slash-false"),
    (lambda a: [x.replace("json", "text") if x == "json" else x for x in a], "text"),
    (lambda a: [x for i, x in enumerate(a)
                if x != "--model" and (i == 0 or a[i - 1] != "--model")], "no-model"),
    (lambda a: [x for i, x in enumerate(a)
                if x != "--agent" and (i == 0 or a[i - 1] != "--agent")], "no-agent"),
    (lambda a: [x for i, x in enumerate(a)
                if x != "--print-timeout" and (i == 0 or a[i - 1] != "--print-timeout")],
     "no-print-timeout"),
    (lambda a: [("30s" if i and a[i - 1] == "--print-timeout" else x) for i, x in enumerate(a)],
     "short-print-timeout"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_an_argv_outside_the_recipe_is_refused_before_spawn(tmp_path, mutate, why):
    log = tmp_path / "agy.log"
    argv = mutate(_argv(_fake_agy(tmp_path), log))
    _refused(tmp_path, argv, log=log)


def test_the_profile_requires_the_review_schema(tmp_path):
    """design review r1 sol F3: under schema `none` an answer that is missing
    or not a string would leave nothing to fail on."""
    home, log = _home(tmp_path), tmp_path / "agy.log"
    proc, receipt = _run(tmp_path, _argv(_fake_agy(tmp_path), log), home=home, log=log,
                         schema="none")
    assert proc.returncode == 2 and receipt is None
    assert not log.exists()


@pytest.mark.parametrize("raw", [
    # A widening value followed by the canonical one: an ordinary parser keeps
    # the last and would compare equal (design review r1 sol F2).
    '{"id": "dmr-reviewer-v1", "name": "dmr-reviewer-v1", '
    '"projectResources": {"resources": [{"folderUri": "file:///"}]}, "projectResources": {}, '
    '"permissionGrants": {"permissionGrants": {"deny": ' + json.dumps(
        list(dispatch_agent.AGY_REVIEWER_DENY)) + '}}}',
    '{"id": "dmr-reviewer-v1", "name": "dmr-reviewer-v1", "projectResources": {}, '
    '"permissionGrants": {"permissionGrants": {"deny": [], "deny": ' + json.dumps(
        list(dispatch_agent.AGY_REVIEWER_DENY)) + '}}}',
], ids=["duplicate-resources", "duplicate-deny"])
def test_a_project_file_with_duplicate_keys_is_refused(tmp_path, raw):
    home = _home(tmp_path)
    home.joinpath(*dispatch_agent.AGY_PROJECT_RELPATH).write_text(raw)
    _refused(tmp_path, home=home)


def test_settings_with_a_duplicate_telemetry_key_are_refused(tmp_path):
    home = _home(tmp_path)
    home.joinpath(*dispatch_agent.AGY_SETTINGS_RELPATH).write_text(
        '{"telemetryEnabled": true, "telemetryEnabled": false}')
    _refused(tmp_path, home=home)


def test_equals_and_separate_values_are_one_flag():
    log = Path("/abs/agy.log")
    argv = ["agy", "--agent=dmr-reviewer-v1", f"--model={GEMINI}", "--effort", "low",
            "--project=dmr-reviewer-v1", "--disable-slash-commands", "--output-format=json",
            "--print-timeout=0s", f"--log-file={log}", "-p", "x"]
    assert dispatch_agent._agy_argv_problem(argv, log, GEMINI) is None
    assert "repeats" in dispatch_agent._agy_argv_problem(argv + ["-p", "y"], log, GEMINI)
    assert "repeats" in dispatch_agent._agy_argv_problem(argv + ["--project", "x"], log, GEMINI)


@pytest.mark.parametrize("respell", [
    lambda a: [("-agent" if x == "--agent" else x) for x in a],
    lambda a: [("-project" if x == "--project" else x) for x in a],
    lambda a: [("--p" if x == "-p" else x) for x in a],
    lambda a: [("-model=" + a[i + 1] if x == "--model" else x) for i, x in enumerate(a)
               if not (i and a[i - 1] == "--model")],
], ids=["single-dash-agent", "single-dash-project", "double-dash-p", "single-dash-model-eq"])
def test_unprobed_spellings_are_refused(respell):
    """i1 fable: only `--name` and `-p` were probed. Go's `flag` reads
    `-agent=…` the same, but a parser reading single-dash letters as a cluster
    would not, and the agent layer would drop without a word. Each case
    respells one shipped flag, so nothing else about the argv is wrong."""
    argv = respell(_recipe_argv())
    flags, why = dispatch_agent._agy_flag_view(argv)
    assert flags is None and "spellings" in why, why


@pytest.mark.parametrize("alias,why", [("--print", "allow-list"), ("--prompt", "allow-list"),
                                       ("-print", "spellings"), ("--print=x", "allow-list"),
                                       ("-p=x", "spellings")])
def test_the_unprobed_prompt_aliases_are_refused(alias, why):
    """design review r2 fable F1: only `-p` was probed as a string flag. Were
    `--print` a boolean, `--print --dangerously-skip-permissions` would read
    here as a prompt and reach the CLI as a bypass."""
    argv = _recipe_argv()
    i = argv.index("-p")
    argv[i:i + 2] = [alias] if "=" in alias else [alias, "--dangerously-skip-permissions"]
    flags, reason = dispatch_agent._agy_flag_view(argv)
    assert flags is None and why in reason, reason


def test_a_prompt_that_looks_like_flags_is_a_value():
    log = Path("/abs/agy.log")
    argv = _recipe_argv(log=str(log), prompt="--mode plan --dangerously-skip-permissions")
    assert dispatch_agent._agy_argv_problem(argv, log, GEMINI) is None


def test_the_log_must_be_the_reserved_absolute_path(tmp_path):
    log = tmp_path / "agy.log"
    argv = _argv(_fake_agy(tmp_path), log, **{"--log-file": "agy.log"})
    _refused(tmp_path, argv, log=log)
    argv = _argv(_fake_agy(tmp_path), log, **{"--log-file": str(tmp_path / "other.log")})
    _refused(tmp_path, argv, log=log)


def test_an_existing_log_or_one_in_the_receipt_store_is_refused(tmp_path):
    log = tmp_path / "agy.log"
    log.write_text("stale")
    _refused(tmp_path, log=log)
    store_log = tmp_path / "receipts" / "agy.log"
    (tmp_path / "receipts").mkdir(exist_ok=True)
    _refused(tmp_path, log=store_log)
    assert not store_log.exists()
    # Neither refusal left a claim behind: the reservation is the last refusal
    # before the STARTING receipt, and it gives the claim back.
    assert not list((tmp_path / "receipts").glob("*.claim"))


@pytest.mark.parametrize("project", [
    False,
    {**dispatch_agent.AGY_REVIEWER_PROJECT_DOC, "settings": {"fileAccessPolicy": "ALLOW"}},
    {**dispatch_agent.AGY_REVIEWER_PROJECT_DOC, "name": "other"},
    {**dispatch_agent.AGY_REVIEWER_PROJECT_DOC,
     "permissionGrants": {"permissionGrants": {"deny": list(dispatch_agent.AGY_REVIEWER_DENY[:-1])}}},
    {**dispatch_agent.AGY_REVIEWER_PROJECT_DOC,
     "permissionGrants": {"permissionGrants": {"deny": list(dispatch_agent.AGY_REVIEWER_DENY)
                                               + ["command(*)"]}}},
    {**dispatch_agent.AGY_REVIEWER_PROJECT_DOC,
     "permissionGrants": {"permissionGrants": {"deny": list(dispatch_agent.AGY_REVIEWER_DENY),
                                               "allow": ["command(*)"]}}},
    {**dispatch_agent.AGY_REVIEWER_PROJECT_DOC, "projectResources": {"resources": []}},
], ids=["missing", "settings", "name", "deny-short", "deny-dup", "allow", "resources"])
def test_a_project_file_that_is_not_the_canonical_document_is_refused(tmp_path, project):
    _refused(tmp_path, home=_home(tmp_path, project=project))


def test_the_deny_rules_may_come_in_any_order(tmp_path):
    doc = copy.deepcopy(dispatch_agent.AGY_REVIEWER_PROJECT_DOC)
    doc["permissionGrants"]["permissionGrants"]["deny"].reverse()
    assert dispatch_agent._is_reviewer_project(doc)


def test_a_symlinked_project_file_is_refused(tmp_path):
    home = _home(tmp_path)
    path = home.joinpath(*dispatch_agent.AGY_PROJECT_RELPATH)
    real = tmp_path / "real.json"
    real.write_text(path.read_text())
    path.unlink()
    path.symlink_to(real)
    _refused(tmp_path, home=home)


@pytest.mark.parametrize("settings", [
    False, {}, {"telemetryEnabled": True}, {"telemetryEnabled": "false"}, {"telemetryEnabled": 0},
    [], {"enableTelemetry": False},
    {"telemetryEnabled": False, "enableTelemetry": True},
], ids=["missing", "absent-key", "true", "string", "zero", "array", "documented-spelling-only",
        "contradiction"])
def test_telemetry_must_be_off_in_the_cli_settings(tmp_path, settings):
    """The key is the one the CLI keeps (it drops the documented
    `enableTelemetry` on every run, measured); the documented spelling alone is
    not the setting, and a `true` under it contradicts the one that is."""
    err = _refused(tmp_path, home=_home(tmp_path, settings=settings))
    assert "telemetryEnabled" in err or "settings.json" in err


# --- design v2: the agent layer, the workspace, the model, the log ---------

@pytest.mark.parametrize("agent", [
    False,
    dispatch_agent.AGY_REVIEWER_AGENT_TEXT.replace("  - view_file\n",
                                                   "  - view_file\n  - run_command\n"),
    dispatch_agent.AGY_REVIEWER_AGENT_TEXT + "\n",
], ids=["missing", "widened-tools", "trailing-byte"])
def test_an_agent_file_that_is_not_the_canonical_text_is_refused(tmp_path, agent):
    _refused(tmp_path, home=_home(tmp_path, agent=agent))


def test_the_agent_allows_one_reading_tool_and_is_never_a_subagent():
    text = dispatch_agent.AGY_REVIEWER_AGENT_TEXT
    front = text.split("---")[1]
    assert re.search(r"^tools:\n  - view_file\n(?!  -)", front, re.M), front
    assert "subagent: false" in front and "mainAgent: true" in front


@pytest.mark.parametrize("layout", ["cwd", "repo-root", "between"])
@pytest.mark.parametrize("name", [".agents", ".gemini"])
def test_a_workspace_carrying_agy_customizations_is_refused(tmp_path, layout, name):
    """Measured: the CLI runs `.agents/hooks.json` from the workspace root — the
    cwd, or the repository root enclosing it — as host commands, and its
    `.agents/agents/` shadow the reviewer agent."""
    repo = tmp_path / "repo"
    cwd = repo / "a" / "b"
    cwd.mkdir(parents=True)
    (repo / ".git").mkdir()
    where = {"cwd": cwd, "repo-root": repo, "between": repo / "a"}[layout]
    (where / name).mkdir()
    err = _refused(tmp_path, cwd=cwd)
    assert name in err


def test_a_planted_marker_does_not_shorten_the_audit(tmp_path):
    """design review r2 sol F1 / fable F2: a reviewed tree can add a `.jj`,
    `.hg` or a fake `.git` below the real root; the audit walks every ancestor
    regardless, so the root's `.agents` is still seen."""
    repo = tmp_path / "repo"
    cwd = repo / "a" / "b"
    cwd.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".agents").mkdir()
    for marker in (".jj", ".hg", ".git"):
        planted = repo / "a" / marker
        planted.write_text("planted")
        assert dispatch_agent._agy_workspace_problem(cwd, None), marker
        planted.unlink()


def test_a_nested_customization_below_the_cwd_is_not_refused(tmp_path):
    """Measured: a `.agents` deeper in the tree was not loaded."""
    cwd = tmp_path / "ws"
    (cwd / "nested" / ".agents").mkdir(parents=True)
    assert dispatch_agent._agy_workspace_problem(cwd, None) is None


def test_home_is_admitted_only_where_the_cli_cannot_adopt_it(tmp_path):
    """r2 sol F3 / fable F4: HOME's `.gemini` is the CLI's own configuration
    home and is always admitted; its `.agents` only when HOME is a strict
    ancestor of the cwd and not a repository root."""
    home = tmp_path / "home"
    cwd = home / "work" / "repo"
    (cwd / ".git").mkdir(parents=True)
    (home / ".gemini").mkdir()
    (home / ".agents").mkdir()
    assert dispatch_agent._agy_workspace_problem(cwd, home) is None
    assert dispatch_agent._agy_workspace_problem(cwd, None)            # HOME unknown: refused
    assert dispatch_agent._agy_workspace_problem(home, home)           # cwd == HOME: .agents refused
    (home / ".git").mkdir()
    assert dispatch_agent._agy_workspace_problem(cwd, home)            # HOME a repository root
    (home / ".agents").rmdir()
    assert dispatch_agent._agy_workspace_problem(cwd, home) is None    # .gemini alone is fine
    assert dispatch_agent._agy_workspace_problem(home, home) is None


def test_a_case_aliased_home_is_still_home(tmp_path):
    """i3 fable F1: on a case-insensitive volume `/Users/U/...` is HOME too;
    a spelling comparison missed it and refused HOME's own `.gemini`."""
    home = tmp_path / "home"
    (home / "work" / "repo" / ".git").mkdir(parents=True)
    (home / ".gemini").mkdir()
    alias = tmp_path / "HOME"
    if not alias.exists():
        pytest.skip("case-sensitive volume")
    assert dispatch_agent._agy_workspace_problem(alias / "work" / "repo", home) is None
    assert dispatch_agent._agy_workspace_problem(home / "work" / "repo", alias) is None
    (home / ".agents").mkdir()
    assert dispatch_agent._agy_workspace_problem(alias, home)          # cwd == HOME: .agents refused


def test_a_symlinked_cwd_is_audited_under_both_spellings(tmp_path):
    """r2 fable F3: a Go child may take its cwd from $PWD, the literal path,
    whose ancestors can differ from the real path's."""
    real = tmp_path / "clean" / "ws"
    real.mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".agents").mkdir()
    link = repo / "ws"
    link.symlink_to(real)
    err = _refused(tmp_path, cwd=link)
    assert ".agents" in err


def test_the_profile_requires_a_child_cwd(tmp_path):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    argv = _argv(_fake_agy(tmp_path), log)
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "run", "--attempt-id", "g1",
         "--receipt-dir", str(tmp_path / "receipts"), "--deadline-seconds", "60",
         "--seat", "reviewer-1", "--output-schema", "review", "--model-id", GEMINI,
         "--output-envelope", "agy-print-json-v1", "--seat-profile", "agy-reviewer-v1",
         "--agy-log", str(log), "--transport-id", "claude_code.to_gemini", "--", *argv],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2 and "--child-cwd" in proc.stderr


@pytest.mark.parametrize("model,model_id", [
    (ID("claude_worker_balanced"), ID("claude_worker_balanced")),
    (GEMINI, None),
    (GEMINI, GEMINI + "-high"),
    ("gpt-oss-120b-medium", "gpt-oss-120b-medium"),
], ids=["other-family", "undeclared", "mismatch", "foreign-catalogue-id"])
def test_the_model_is_pinned_to_the_declared_gemini_id(tmp_path, model, model_id):
    """design review r1 fable F1: the CLI's catalogue serves other vendors'
    models through the same account."""
    log = tmp_path / "agy.log"
    argv = _argv(_fake_agy(tmp_path), log, **{"--model": model})
    home = _home(tmp_path)
    proc, receipt = _run(tmp_path, argv, home=home, log=log, model_id=model_id,
                         attempt="gm")
    assert proc.returncode == 2 and receipt is None


def test_a_resolution_to_another_model_fails_the_gate(tmp_path):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    fake = _fake_agy(tmp_path, resolved="claude-sonnet-4-6")
    proc, receipt = _run(tmp_path, _argv(fake, log), home=home, log=log)
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert "agy_resolved_model:claude-sonnet-4-6" in receipt["result"]["invalid_reasons"]


@pytest.mark.parametrize("line", [
    'W1014 11:50:56.313661       1 permissions.go:216] Invalid "command" permission rules '
    'found; they are ignored and grant nothing:',
    'W1014 11:52:43.197302     508 permission_grant_store.go:415] ignoring invalid deny entry '
    '"command(*)": unknown action "command" in grant string: "command(*)"',
], ids=["invalid-rules", "invalid-entry"])
def test_a_rule_the_cli_reports_invalid_fails_the_gate(tmp_path, line):
    """The way a deny rule silently stops denying (measured on `unsandboxed`
    and on unknown actions): the CLI says so in this log and nowhere else."""
    home, log = _home(tmp_path), tmp_path / "agy.log"
    fake = _fake_agy(tmp_path, extra_lines=(line,))
    proc, receipt = _run(tmp_path, _argv(fake, log), home=home, log=log)
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert any(r.startswith("agy_project_grants:invalid_rule:")
               for r in receipt["result"]["invalid_reasons"])


def test_a_marker_outside_a_settings_manager_record_is_not_a_record(tmp_path):
    """Only a whole glog line from the CLI's settings manager is a grants
    record; the marker echoed anywhere else fails rather than counts."""
    home, log = _home(tmp_path), tmp_path / "agy.log"
    echoed = (f'I1014 11:00:02.000000       1 printmode.go:202] Print mode: starting '
              f'(model="x ApplyProjectPermissionGrants: {LOADED}")')
    fake = _fake_agy(tmp_path, grants=(), extra_lines=(echoed,))
    proc, receipt = _run(tmp_path, _argv(fake, log), home=home, log=log)
    reasons = receipt["result"]["invalid_reasons"]
    assert any(r.startswith("agy_project_grants:not_a_record:") for r in reasons), reasons
    assert "agy_project_grants:none" in reasons


@pytest.mark.parametrize("flag,value", [("--model", GEMINI + "\nI1014"),
                                        ("--effort", "low\r"), ("--log-file", "/abs/a\x00b")])
def test_a_control_character_in_a_value_is_refused(flag, value):
    argv = _recipe_argv()
    argv[argv.index(flag) + 1] = value
    flags, why = dispatch_agent._agy_flag_view(argv)
    assert flags is None and "control character" in why


def test_the_child_sees_the_audited_cwd_in_pwd(tmp_path):
    """i1 sol F1: the supervisor's own $PWD is not the child's. A Go child
    would use an inherited $PWD naming the same directory through another
    path, whose ancestors the audit never walked."""
    home, log = _home(tmp_path), tmp_path / "agy.log"
    fake = _fake_agy(tmp_path)
    seen = tmp_path / "pwd.txt"
    body = fake.read_text().replace(
        "import json, os, sys\n",
        f"import json, os, sys\nopen({str(seen)!r}, 'w').write(os.environ.get('PWD', ''))\n", 1)
    fake.write_text(body)
    real = tmp_path / "clean" / "ws"
    real.mkdir(parents=True)
    alias_root = tmp_path / "review" / "repo"
    (alias_root / ".git").mkdir(parents=True)
    (alias_root / ".agents").mkdir()
    (alias_root / "link").symlink_to(real)
    env_pwd = str(alias_root / "link")
    argv = _argv(fake, log)
    base = ["--output-envelope", "agy-print-json-v1", "--seat-profile", "agy-reviewer-v1",
            "--agy-log", str(log), "--transport-id", "claude_code.to_gemini",
            "--child-cwd", str(real), "--model-id", GEMINI]
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "run", "--attempt-id", "g1",
         "--receipt-dir", str(tmp_path / "receipts"), "--deadline-seconds", "60",
         "--grace-seconds", "1", "--seat", "reviewer-1", "--output-schema", "review",
         *base, "--", *argv],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "HOME": str(home), "PWD": env_pwd})
    assert proc.returncode == 0, proc.stderr
    assert seen.read_text() == os.path.realpath(real)


def test_the_pwd_pin_composes_with_a_grok_home(tmp_path):
    """i2: the agy profile's PWD pin and an attempt-private GROK_HOME share
    one child environment; neither replaces the other."""
    home, log = _home(tmp_path), tmp_path / "agy.log"
    fake = _fake_agy(tmp_path)
    seen = tmp_path / "env.json"
    fake.write_text(fake.read_text().replace(
        "import json, os, sys\n",
        "import json, os, sys\n"
        f"json.dump({{'PWD': os.environ.get('PWD'), 'GROK_HOME': os.environ.get('GROK_HOME')}}, "
        f"open({str(seen)!r}, 'w'))\n", 1))
    cwd = _workspace(tmp_path)
    gh = tmp_path / "gh"
    proc, receipt = _run(tmp_path, _argv(fake, log), home=home, log=log, cwd=cwd,
                         extra=["--output-envelope", "agy-print-json-v1", "--seat-profile",
                                "agy-reviewer-v1", "--agy-log", str(log), "--transport-id",
                                "claude_code.to_gemini", "--child-cwd", str(cwd),
                                "--model-id", GEMINI, "--grok-home", str(gh)])
    assert receipt["result"]["state"] == "SUCCEEDED", (proc.stderr, receipt["result"])
    got = json.loads(seen.read_text())
    assert got == {"PWD": os.path.realpath(cwd), "GROK_HOME": os.path.realpath(gh)}


def test_a_newline_in_the_prompt_is_the_prompt():
    argv = _recipe_argv(prompt="line one\nline two")
    flags, why = dispatch_agent._agy_flag_view(argv)
    assert why is None and flags["prompt"] == "line one\nline two"


def test_a_refusal_before_the_reservation_leaves_no_log(tmp_path):
    """design review r1 fable F4: a pre-spawn refusal leaves no reserved log,
    so the same path is usable on the retry."""
    home, log = _home(tmp_path), tmp_path / "agy.log"
    argv = _argv(_fake_agy(tmp_path), log)
    proc, _ = _run(tmp_path, argv, home=home, log=log,
                   extra=["--output-envelope", "agy-print-json-v1", "--seat-profile",
                          "agy-reviewer-v1", "--agy-log", str(log), "--transport-id",
                          "claude_code.to_gemini", "--child-cwd", str(_workspace(tmp_path)),
                          "--model-id", GEMINI, "--require-artifact-sha256", "x=" + "0" * 64])
    assert proc.returncode == 2 and not log.exists()
    proc, receipt = _run(tmp_path, argv, home=home, log=log)
    assert receipt["result"]["state"] == "SUCCEEDED", receipt["result"]


def _excerpt_lines(name):
    return (FIXTURES / name).read_text().splitlines()


def test_the_real_log_excerpts_grade_as_captured(tmp_path):
    """r2 fable F5: the gate against the CLI's own lines, not author-written
    ones. A clean v2 run passes; the v1 run whose project held `unsandboxed(*)`
    fails on the CLI's invalid-rule report and on the six-rule record."""
    clean = tmp_path / "clean.log"
    clean.write_text((FIXTURES / "agy-1.3.4-reviewer.log").read_text())
    view, reasons = dispatch_agent._grade_agy_log(clean, {}, FIXTURE_MODEL_134)
    assert reasons == [] and view["lines"] == [LOADED]
    assert view["resolved_model"] == f"{FIXTURE_MODEL_134}-low"
    # The same capture is not a run of the current seat: the model pin refuses it.
    _, reasons = dispatch_agent._grade_agy_log(clean, {}, GEMINI)
    assert any(r.startswith("agy_resolved_model:") for r in reasons), reasons
    v1 = tmp_path / "v1.log"
    v1.write_text((FIXTURES / "agy-1.3.4-invalid-rule.log").read_text())
    _, reasons = dispatch_agent._grade_agy_log(v1, {}, FIXTURE_MODEL_134)
    assert any(r.startswith("agy_project_grants:invalid_rule:") for r in reasons), reasons
    assert any("6 deny grants" in r for r in reasons), reasons


def test_the_agy_1_4_8_argon_excerpt_grades_as_captured(tmp_path):
    """1.24.0: the lines the gate reads did not move between agy 1.3.4 and
    1.4.8 (art-low-rel, the current seat's model id round trip)."""
    log = tmp_path / "argon.log"
    log.write_text((FIXTURES / "agy-1.4.8-argon-reviewer.log").read_text())
    view, reasons = dispatch_agent._grade_agy_log(log, {}, GEMINI)
    assert reasons == [] and view["lines"] == [LOADED]
    assert view["resolved_model"] == f"{GEMINI}-low"


def test_a_cleared_record_before_the_loaded_one_fails(tmp_path):
    lines = _excerpt_lines("agy-1.3.4-reviewer.log")
    cleared = ('I1014 12:01:27.300000       1 cli_setting_manager.go:218] '
               'ApplyProjectPermissionGrants: no grants for project "CLI Project", '
               'cleared project permissions')
    log = tmp_path / "a.log"
    log.write_text("\n".join([cleared, *lines]) + "\n")
    _, reasons = dispatch_agent._grade_agy_log(log, {}, FIXTURE_MODEL_134)
    assert any("cleared project permissions" in r for r in reasons), reasons


def test_crlf_records_are_records(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("\r\n".join(_excerpt_lines("agy-1.3.4-reviewer.log")) + "\r\n")
    _, reasons = dispatch_agent._grade_agy_log(log, {}, FIXTURE_MODEL_134)
    assert reasons == []


def test_a_bad_record_after_the_twentieth_still_fails(tmp_path):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    bad = 'no grants for project "CLI Project", cleared project permissions'
    fake = _fake_agy(tmp_path, grants=(LOADED,) * 25 + (bad,))
    proc, receipt = _run(tmp_path, _argv(fake, log), home=home, log=log)
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert len(receipt["result"]["agy_project_grants"]["lines"]) == 20


@pytest.mark.parametrize("extra_line,why", [
    (f'I1014 11:00:00.500000       1 model_resolver.go:155] model alias "other-model" '
     f'resolved to "other-model-high"', "another-alias"),
    (f'I1014 11:00:00.500000       1 model_resolver.go:155] model alias "{GEMINI}" '
     f'resolved to "claude-sonnet-4-6"', "foreign-target"),
    # A resolution TO this model's spelling, but of another alias: the record
    # is not about `--model` at all, so it vouches for nothing.
    (f'I1014 11:00:00.500000       1 model_resolver.go:155] model alias "auto" '
     f'resolved to "{GEMINI}-high"', "unbound-alias"),
], ids=lambda v: v if v in ("another-alias", "foreign-target", "unbound-alias") else None)
def test_every_resolution_must_be_of_the_pinned_model(tmp_path, extra_line, why):
    """r2 sol: not only the last resolution, and bound to its alias."""
    home, log = _home(tmp_path), tmp_path / "agy.log"
    fake = _fake_agy(tmp_path, extra_lines=(extra_line,))
    proc, receipt = _run(tmp_path, _argv(fake, log), home=home, log=log)
    assert receipt["result"]["state"] == "INVALID_OUTPUT", why
    assert any(r.startswith("agy_resolved_model:") for r in receipt["result"]["invalid_reasons"])


def test_the_host_layout_runs_end_to_end(tmp_path):
    """i1 fable: the real layout — the cwd under HOME, HOME holding the CLI's
    `.gemini` (and another tool's `.agents`) — is admitted by a full run."""
    home = _home(tmp_path)
    (home / ".agents").mkdir()
    cwd = home / "dev" / "repo"
    (cwd / ".git").mkdir(parents=True)
    log = tmp_path / "agy.log"
    proc, receipt = _run(tmp_path, _argv(_fake_agy(tmp_path), log), home=home, log=log, cwd=cwd)
    assert receipt["result"]["state"] == "SUCCEEDED", (proc.stderr, receipt["result"])


def test_an_empty_log_fails_on_both_counts(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("")
    _, reasons = dispatch_agent._grade_agy_log(log, {}, GEMINI)
    assert "agy_project_grants:none" in reasons
    assert "agy_resolved_model:<absent>" in reasons


def test_a_log_under_a_symlinked_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real)
    _refused(tmp_path, log=tmp_path / "linked" / "agy.log")
    assert not (real / "agy.log").exists()


@pytest.mark.parametrize("home_value", [None, "relative/home"], ids=["unset", "relative"])
def test_an_unset_or_relative_home_is_refused(tmp_path, home_value):
    log = tmp_path / "agy.log"
    argv = _argv(_fake_agy(tmp_path), log)
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    if home_value is not None:
        env["HOME"] = home_value
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "run", "--attempt-id", "g1",
         "--receipt-dir", str(tmp_path / "receipts"), "--deadline-seconds", "60",
         "--seat", "reviewer-1", "--output-schema", "review", "--model-id", GEMINI,
         "--output-envelope", "agy-print-json-v1", "--seat-profile", "agy-reviewer-v1",
         "--agy-log", str(log), "--transport-id", "claude_code.to_gemini",
         "--child-cwd", str(_workspace(tmp_path)), "--", *argv],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2 and "HOME" in proc.stderr and not log.exists()


def test_a_refusal_after_the_claim_leaves_no_log_and_no_claim(tmp_path):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    proc, receipt = _run(tmp_path, _argv(_fake_agy(tmp_path), log), home=home, log=log)
    assert receipt["result"]["state"] == "SUCCEEDED"
    log2 = tmp_path / "agy2.log"
    proc, _ = _run(tmp_path, _argv(_fake_agy(tmp_path), log2), home=home, log=log2)
    assert proc.returncode == 2 and "already has a receipt" in proc.stderr
    assert not log2.exists()
    assert not list((tmp_path / "receipts").glob("*.claim"))


def test_more_than_twenty_records_still_pass_and_are_capped_in_the_receipt(tmp_path):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    fake = _fake_agy(tmp_path, grants=(LOADED,) * 25)
    proc, receipt = _run(tmp_path, _argv(fake, log), home=home, log=log)
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert len(receipt["result"]["agy_project_grants"]["lines"]) == 20


# --- the evidence readers -------------------------------------------------

def _verify(tmp_path, ids):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "verify-evidence", "--receipt-dir",
         str(tmp_path / "receipts"), "--ids", ",".join(ids), "--expect-count", str(len(ids))],
        capture_output=True, text=True, timeout=60)


def test_verify_evidence_accepts_a_contained_gemini_receipt(tmp_path):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    _run(tmp_path, _argv(_fake_agy(tmp_path), log), home=home, log=log)
    proc = _verify(tmp_path, ["g1"])
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("edit", [
    lambda r: r.update(seat_profile=None),
    lambda r: r.update(output_envelope="claude-print-json-v1"),
    lambda r: r["result"].update(agy_project_grants=None),
    lambda r: r["result"]["agy_project_grants"].update(lines=[]),
    lambda r: r["result"]["agy_project_grants"].update(
        lines=['no grants for project "CLI Project", cleared project permissions']),
    lambda r: r["result"]["agy_project_grants"].update(resolved_model=None),
    lambda r: r["result"]["agy_project_grants"].update(resolved_model="claude-sonnet-4-6"),
    lambda r: r.update(model_id=None),
    lambda r: r.update(model_id="claude-sonnet-4-6"),
], ids=["no-profile", "other-envelope", "no-grants", "empty-grants", "cleared",
        "no-resolution", "foreign-resolution", "no-model-id", "foreign-model-id"])
def test_verify_evidence_refuses_a_gemini_receipt_without_contained_evidence(tmp_path, edit):
    home, log = _home(tmp_path), tmp_path / "agy.log"
    _run(tmp_path, _argv(_fake_agy(tmp_path), log), home=home, log=log)
    path = tmp_path / "receipts" / "g1.json"
    receipt = json.loads(path.read_text())
    edit(receipt)
    path.write_text(json.dumps(receipt))
    proc = _verify(tmp_path, ["g1"])
    assert proc.returncode == 1
    assert "dispatches into agy" in proc.stderr


# --- RouteObservationV1 receipts (design DD-6b) ---------------------------

def _obs_receipt(stop_reason="SUCCESS", error_type=None):
    from test_receipt_semantics import receipt
    rec = receipt()
    rec["output_envelope"] = "agy-print-json-v1"
    rec["result"]["envelope"] = dict(parse_ok=True, stop_reason=stop_reason, session_id="s",
                                     served_models=None, error_type=error_type,
                                     usage={"input_tokens": 1}, error_reason=None)
    return rec


def test_an_observation_accepts_a_successful_agy_receipt(tmp_path):
    from test_receipt_semantics import check, materialize
    path, root = materialize(tmp_path, _obs_receipt())
    check(path, root)


@pytest.mark.parametrize("over", [dict(stop_reason="CANCELED"), dict(stop_reason="end_turn"),
                                  dict(error_type="error")])
def test_an_observation_refuses_an_agy_success_its_envelope_contradicts(tmp_path, over):
    import validate_observation as obs
    from test_receipt_semantics import check, materialize
    path, root = materialize(tmp_path, _obs_receipt(**over))
    with pytest.raises(obs.ValidateError, match="contradicts its envelope"):
        check(path, root)


def test_an_agy_receipt_never_attests_a_served_model(tmp_path):
    import validate_observation as obs
    from test_receipt_semantics import check, materialize, observed
    path, root = materialize(tmp_path, _obs_receipt(), observed)
    with pytest.raises(obs.ValidateError, match="I-OBS-MODEL"):
        check(path, root)


# ---------------------------------------------------------------------------
# model_sync with the row (design DD-9)
# ---------------------------------------------------------------------------

def test_model_sync_reports_the_gemini_line_without_a_catalog():
    """1.21.0 (design 2026-10-14 DD-61a, issue #61) gave the line a reader:
    with no catalogue at all the row is still discovery_unavailable, never a
    candidate; the tick's placeholder makes it a run-time fetch, and the
    family has a CLI and an inference budget of its own.
    tests/test_catalog_readers.py holds the reader's behaviour."""
    rows = {k: m for k, m in CFG["models"].items()}
    found = model_sync.find_candidates(rows, {})
    assert found[SEAT_KEY]["status"] == "discovery_unavailable"
    placeholder = model_sync.Catalog("gemini", "absent", "fetched at run")
    assert model_sync.find_candidates(rows, {"gemini": placeholder})[SEAT_KEY]["status"] \
        == "catalog_fetch"
    assert model_sync.FAMILY_CLI["gemini"] == "agy" and model_sync.MAX_INFERENCES["gemini"] == 4
    assert model_sync.retirement_notices(rows, {}) == []


def test_model_sync_tick_and_status_run_with_the_gemini_row(tmp_path):
    from test_model_sync import NOW, cli_sync, fake_bin, fake_home, new_root, sync_env
    bindir, _ = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    spawned = []
    model_sync.tick(env=env, home=home, now=NOW, spawn=spawned.append)
    assert all(SEAT_KEY not in keys for keys in spawned)
    status = cli_sync(env, "status")
    assert status.returncode == 0, status.stderr + status.stdout
    json.loads(status.stdout)


def test_a_model_sync_run_on_the_gemini_key_probes_nothing_without_agy(tmp_path):
    """No `agy` on PATH: the tick's offline gate makes the row
    discovery_unavailable, so `run --key` fetches nothing and probes nothing."""
    from test_model_sync import calls_of, cli_sync, fake_bin, fake_home, new_root, sync_env
    bindir, fakedir = fake_bin(tmp_path)
    home = fake_home(tmp_path)
    root = new_root(tmp_path)
    env = sync_env(tmp_path, root, home, bindir)
    proc = cli_sync(env, "run", "--key", SEAT_KEY)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    for name in ("codex", "claude", "grok"):
        assert calls_of(fakedir, name) == [], name
    assert SEAT_KEY in proc.stdout


def test_the_codex_row_binds_the_shipped_recipe():
    from test_docs import _assert_direction_row_binds, _whole_token
    needle = "transports.codex.to_gemini.mechanism_reviewer"
    rows = [e for e in CFG["verification_ledger"]["entries"]
            if _whole_token(needle, str(e.get("item", "")))]
    assert len(rows) == 1 and rows[0]["status"] == "verified"
    _assert_direction_row_binds(CFG["transports"]["codex"]["to_gemini"]["mechanism_reviewer"],
                                rows[0], rows[0]["item"])


@pytest.mark.parametrize("key", ["gemini_worker_balanced", "meta_worker_balanced"])
def test_the_tier_measurement_supersedes_the_external_reading(key):
    """issue #58: the measured row is new; the external one stays as history
    and the new row points back at it. (The gemini row measured then is the
    seat retired in 1.24.0; its rows stay as the record.)"""
    items = {e["item"]: e for e in CFG["verification_ledger"]["entries"]}
    old = f"{key} tier evidence (external, not measured)"
    new = f"{key} tier measurement (real-review replay, 2026-10-14)"
    assert old in items and new in items
    assert items[new]["status"] == "verified"
    assert items[new]["supersedes"] == [old]
    assert "tier 1 confirmed" in " ".join(items[new]["evidence"].split())
    # issue #63: that row's anchor was a history row; the live-anchor row
    # supersedes it in turn, and the current tier is the one it records.
    later = "Reviewer seat tiers against live anchors (real-review replay, 2026-10-14)"
    assert new in items[later]["supersedes"]
    assert CFG["models"][key]["capability_tier"] == 0
