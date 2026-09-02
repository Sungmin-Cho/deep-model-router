"""Tests for issue #19 tranche 1: the router consumes transport seat capability.

`role_bindings.default.worker_balanced` is the xai frontier seat, but
`transports.*.to_xai` ships only `mechanism_reviewer` — the write-capable
maker seat failed its escape-denial gate on grok 1.0.5 (2026-08-25) and again
on grok 1.0.13 (2026-09-01, hard link through `--sandbox workspace` AND
`--sandbox strict`). Until this tranche the router had no way to know that,
so it emitted an xai worker for write-capable work and the caller had to
route around it by hand with `--unavailable-models`.

Run:  python3 -m pytest skills/model-router/tests/ -q
"""

import json as _json
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    Task,
    ValidationError,
    main,
    request_sha256_of,
    route,
    task_from_request_v1,
    ConfigError,
    Policy,
    load_config,
)

CFG = load_config()
ID = lambda key: CFG["models"][key]["id"]                        # noqa: E731
ARCHITECT_ID = CFG["models"]["claude_architect"]["id"]
POLICY = Policy.of(CFG)


# ---------------------------------------------------------------------------
# The naming convention the capability lookup rests on
# ---------------------------------------------------------------------------

def test_every_cross_provider_transport_key_names_a_registered_family():
    """`write_capable` finds a direction by spelling `to_<family>`.

    That derivation is the reason no new config key was added to map families
    onto transport entries. It is only safe while the convention holds, so the
    convention is asserted rather than assumed: every non-native key under a
    runtime's transports must be `to_` plus a family that the model registry
    actually registers.
    """
    families = {m["family"] for m in CFG["models"].values()}
    for runtime, entries in CFG["transports"].items():
        for name in entries:
            if name == "native":
                continue
            assert name.startswith("to_"), (runtime, name)
            assert name[len("to_"):] in families, (runtime, name, sorted(families))


# ---------------------------------------------------------------------------
# The capability predicate itself
# ---------------------------------------------------------------------------

def test_the_host_own_family_is_write_capable_through_its_native_seat():
    """A grok host writing with grok is not a transport at all.

    `transports.grok.native` is the host session itself, so requiring a
    `mechanism_maker` there would refuse the one seat that needs no bridge.
    This is what keeps the grok-hosted routes untouched by the coupling.
    """
    for runtime in sorted(POLICY.runtimes):
        local = POLICY.local_family[runtime]
        assert POLICY.write_capable(runtime, local) is True, (runtime, local)


def test_a_seat_split_transport_without_a_maker_is_not_write_capable():
    """A direction that only carries `mechanism_reviewer` is not write-capable.
    The shipped Claude Code maker is the opposite case; Codex -> xAI still
    has no write authorization (verified and write_verified stay false)."""
    assert POLICY.write_capable("codex", "xai") is False


def test_the_shipped_claude_code_maker_is_write_capable():
    """Issue #19 shipping half: recipe + write_verified + ledger together."""
    assert "mechanism_maker" in CFG["transports"]["claude_code"]["to_xai"]
    assert CFG["transports"]["claude_code"]["to_xai"]["write_verified"] is True
    assert POLICY.write_capable("claude_code", "xai") is True


def test_a_seat_agnostic_verified_transport_is_write_capable():
    """`to_openai` / `to_claude` carry one `mechanism` that already takes a
    sandbox or permission-mode slot, so they seat write work as they are."""
    assert POLICY.write_capable("claude_code", "openai") is True
    assert POLICY.write_capable("codex", "claude") is True
    assert POLICY.write_capable("grok", "claude") is True
    assert POLICY.write_capable("grok", "openai") is True


def test_an_unverified_direction_is_not_write_capable():
    """`verified` is per direction. A recipe that was never probed in this
    direction is not a recipe you may dispatch write work through."""
    cfg = _cfg_with_maker(verified=False)
    policy = Policy(cfg)
    assert policy.write_capable("claude_code", "xai") is False


def test_shipping_a_verified_maker_restores_the_family_without_a_code_change():
    """The coupling must be a lookup, not a hardcoded xai exclusion: the day a
    maker recipe passes its gate, the same code has to let xai back in."""
    cfg = _cfg_with_maker(verified=True)
    policy = Policy(cfg)
    assert policy.write_capable("claude_code", "xai") is True


def test_a_family_with_no_transport_entry_is_not_write_capable():
    """gemini is registered for id spelling only; no direction bridges to it."""
    assert POLICY.write_capable("claude_code", "gemini") is False


def _cfg_with_maker(*, verified: bool) -> dict:
    """A config where the xai write seat has actually SHIPPED.

    All three parts, because any one of them alone is not authorization: the
    recipe, the direction's own `write_verified`, and the verification ledger
    recording the probe. An earlier version of this helper set only the recipe
    and the direction flag, which made "shipping a maker" look like a one-line
    config edit — the exact bypass round 1's security review reproduced.
    """
    import copy
    cfg = copy.deepcopy(CFG)
    entry = cfg["transports"]["claude_code"]["to_xai"]
    entry["mechanism_maker"] = "grok --no-auto-update -m <id> --prompt-file /dev/stdin"
    entry["verified"] = verified
    entry["write_verified"] = True
    for item in cfg["verification_ledger"]["entries"]:
        if "maker seat recipe" in item["item"]:
            item["status"] = "verified"
    return cfg


# ---------------------------------------------------------------------------
# task_write_seat: which routes need a write-capable worker at all
# ---------------------------------------------------------------------------

def test_task_write_seat_covers_every_task_class_exactly():
    """A class the table forgets has no default, and the router would have to
    invent one at decision time — which is how a fail-open default gets in."""
    assert set(CFG["task_write_seat"]) == set(CFG["worker_selection"])


def test_only_the_two_judgement_classes_default_to_read_only():
    """Fail-closed: `read_only` is for the classes whose WORKER output is a
    judgement rather than an artifact. Everything else is presumed to need a
    write-capable recipe, and a caller who knows better says so."""
    read_only = {k for k, v in CFG["task_write_seat"].items() if v == "read_only"}
    assert read_only == {"REVIEW", "INVESTIGATION"}


def test_policy_exposes_the_class_default():
    assert POLICY.worker_seat_kind("IMPLEMENTATION") == "write"
    assert POLICY.worker_seat_kind("REVIEW") == "read_only"


def test_an_unknown_seat_kind_in_the_config_is_refused_at_policy_build():
    """A typo here silently decides thousands of routes, so it is a build
    error rather than a value the lookup shrugs at."""
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["task_write_seat"]["IMPLEMENTATION"] = "writeable"
    with pytest.raises(ConfigError, match="task_write_seat"):
        Policy(cfg)


def test_a_task_class_missing_from_the_table_is_refused_at_policy_build():
    import copy
    cfg = copy.deepcopy(CFG)
    del cfg["task_write_seat"]["MIGRATION"]
    with pytest.raises(ConfigError, match="task_write_seat"):
        Policy(cfg)


# ---------------------------------------------------------------------------
# The caller's override
# ---------------------------------------------------------------------------

def _task(**over):
    base = dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=1,
                blast_radius=1, reversibility=1)
    base.update(over)
    return Task(**base)


def test_route_reports_the_class_default_and_where_it_came_from():
    out = route(_task(), CFG)
    assert out["worker_seat"]["kind"] == "write"
    assert out["worker_seat"]["source"] == "task_class"


def test_route_reports_a_read_only_class_default():
    out = route(_task(task_class="REVIEW"), CFG)
    assert out["worker_seat"]["kind"] == "read_only"
    assert out["worker_seat"]["source"] == "task_class"


def test_a_declaration_overrides_the_class_default_in_both_directions():
    """The generalisation is wrong in both directions — an INVESTIGATION that
    files its report as a file writes, an IMPLEMENTATION spike that only reads
    does not — so the override has to work both ways, not just tighten."""
    up = route(_task(task_class="INVESTIGATION", worker_seat="write"), CFG)
    assert up["worker_seat"]["kind"] == "write"
    assert up["worker_seat"]["source"] == "declared"
    down = route(_task(worker_seat="read_only"), CFG)
    assert down["worker_seat"]["kind"] == "read_only"
    assert down["worker_seat"]["source"] == "declared"


def test_an_unknown_worker_seat_value_is_an_input_error():
    with pytest.raises(ValidationError, match="worker_seat"):
        route(_task(worker_seat="writeable"), CFG)


def test_the_cli_accepts_worker_seat(capsys):
    code = main(["--class", "IMPLEMENTATION", "--complexity", "2",
                 "--uncertainty", "1", "--blast-radius", "1",
                 "--reversibility", "1", "--worker-seat", "read_only",
                 "--format", "json"])
    assert code == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["worker_seat"]["kind"] == "read_only"
    assert out["worker_seat"]["source"] == "declared"


def test_request_v1_accepts_worker_seat():
    task = task_from_request_v1({
        "route_schema_version": 1, "task_class": "REVIEW", "complexity": 1,
        "uncertainty": 1, "blast_radius": 1, "reversibility": 1,
        "worker_seat": "write",
    })
    assert task.worker_seat == "write"
    assert route(task, CFG)["worker_seat"]["source"] == "declared"


def test_declaring_the_seat_changes_the_request_fingerprint():
    """A declaration is part of the request even when it restates the class
    default — the same rule `host_seat` follows. Two callers who asked
    different questions must not share one request digest."""
    assert request_sha256_of(_task()) != request_sha256_of(_task(worker_seat="write"))


# ---------------------------------------------------------------------------
# The coupling itself — a write route cannot be staffed by a seat that cannot
# write, and nothing else moves
# ---------------------------------------------------------------------------

# A write-class route that selected the xai worker before this tranche.
WRITE_ROUTE = dict(task_class="IMPLEMENTATION", complexity=0, uncertainty=0,
                   blast_radius=3, reversibility=2)
# A write-class route whose worker is the fast seat and whose REVIEWER is xai.
REVIEWED_ROUTE = dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=1,
                      blast_radius=1, reversibility=1)
# A read-only class that selected the xai worker before this tranche.
READ_ONLY_ROUTE = dict(task_class="REVIEW", complexity=0, uncertainty=0,
                       blast_radius=1, reversibility=2)


def test_a_write_route_is_staffed_by_the_shipped_claude_code_maker():
    """The defect issue #19 reported is closed on the probed direction:
    `worker_balanced` binds the xai seat and `claude_code.to_xai` now
    ships a write-capable maker, so the emitted worker has an argv of record."""
    out = route(Task(**WRITE_ROUTE, runtime="claude_code"), CFG)
    assert out["selected_model"] == ID("xai_frontier")
    assert POLICY.write_capable("claude_code", "xai")


def test_the_same_defect_on_the_codex_host():
    out = route(Task(**WRITE_ROUTE, runtime="codex"), CFG)
    assert out["selected_model"] != ID("xai_frontier")


def test_the_read_only_reviewer_seat_is_untouched_on_a_write_route():
    """The maker gate failing must not cost the reviewer seat that passed it.
    This route WRITES and still seats the verified read-only xai reviewer."""
    out = route(Task(**REVIEWED_ROUTE, runtime="claude_code"), CFG)
    assert out["worker_seat"]["kind"] == "write"
    assert ID("xai_frontier") in out["review"]["reviewer_models"]


def test_the_host_own_family_still_works_natively():
    """A grok host implementing with grok is the host session writing, not a
    bridge with a missing recipe. Blocking it would be the coupling
    over-reaching into the one seat that needs no transport."""
    out = route(Task(**WRITE_ROUTE, runtime="grok"), CFG)
    assert out["selected_model"] == ID("xai_frontier")


def test_a_read_only_class_still_seats_the_xai_worker():
    out = route(Task(**READ_ONLY_ROUTE, runtime="claude_code"), CFG)
    assert out["worker_seat"]["kind"] == "read_only"
    assert out["selected_model"] == ID("xai_frontier")


def test_declaring_read_only_returns_the_seat_to_a_write_class_route():
    out = route(Task(**WRITE_ROUTE, runtime="claude_code",
                     worker_seat="read_only"), CFG)
    assert out["selected_model"] == ID("xai_frontier")


def test_shipping_a_verified_maker_returns_the_seat_with_no_code_change():
    """The coupling reads the transport table, so the fix for issue #19's
    OTHER half — shipping a maker recipe — needs no routing change at all."""
    out = route(Task(**WRITE_ROUTE, runtime="claude_code"),
                _cfg_with_maker(verified=True))
    assert out["selected_model"] == ID("xai_frontier")


def test_the_skip_is_disclosed_as_the_policy_decision_it_is():
    """It goes in `notes` — "every promotion, floor, compensation and policy
    decision the route actually made" — naming the seat, the host direction and
    what was seated instead. After the Claude Code maker shipped, the skip
    remains on the unprobed Codex direction."""
    out = route(Task(**WRITE_ROUTE, runtime="codex"), CFG)
    assert any("no write-capable xai seat" in n and "codex" in n
               for n in out["notes"]), out["notes"]
    # Families, never ids: a terminal route must withhold every execution
    # binding, and `notes` is part of the route.
    assert not any(m in n for n in out["notes"] for m in POLICY.model_ids)


def test_the_skip_is_not_billed_as_scarcity():
    """`fallbacks_applied` feeds `routing_confidence`, which can promote the
    review band. A seat the policy never offered for this kind of work is a
    binding decision, not a model that went missing — recording it as one
    promoted the review of every cross-family write route on the two hosts that
    bridge to xai, permanently, with no change in the risk review answers to.

    Same rule the alt-seat preference already follows: a swap the policy made
    on purpose must not be reported as scarcity.
    """
    write = route(Task(**WRITE_ROUTE, runtime="claude_code"), CFG)
    read = route(Task(**WRITE_ROUTE, runtime="claude_code",
                      worker_seat="read_only"), CFG)
    assert not [f for f in write["fallbacks_applied"] if "worker_balanced:" in f]
    assert write["routing_confidence"] == read["routing_confidence"]
    assert write["review"]["band"] == read["review"]["band"]


def test_a_read_only_route_records_no_write_seat_note():
    """The filter is a no-op there, and a note for a filter that did nothing is
    the forbidden shape: a recorded decision that was never made."""
    out = route(Task(**READ_ONLY_ROUTE, runtime="claude_code"), CFG)
    assert not any("no write-capable" in n for n in out["notes"])


def test_a_role_holds_one_model_so_the_skip_reaches_that_whole_role():
    """`resolved` is keyed by role, so a role holds exactly one model per
    route: the xai seat cannot be skipped as the worker and simultaneously
    seated as a reviewer under the same role.

    This is a real limit and it is recorded here rather than left to be
    rediscovered. It costs nothing that existed before — on this route the xai
    seat was the WORKER, never a reviewer — and the alternative was measured:
    scoping the requirement to the seat instead of the role made the one
    role-keyed entry resolve to the worker's model and the route came out
    INDEPENDENCE_UNAVAILABLE.
    """
    task = Task(task_class="MECHANICAL", complexity=2, uncertainty=2,
                blast_radius=2, reversibility=0, runtime="codex",
                unavailable_models=[ARCHITECT_ID,
                                    ID("claude_worker_fast"),
                                    ID("claude_senior")])
    out = route(task, CFG)
    assert out["terminal"] is None
    assert out["selected_role"] == "worker_balanced"
    assert out["selected_model"] == ID("claude_worker_balanced")
    # Same capability tier as the seat it replaced, so the route is not
    # weakened — and the reviewers are exactly what they were before.
    assert POLICY.tier_of[ID("claude_worker_balanced")] == POLICY.tier_of[ID("xai_frontier")]
    assert ID("xai_frontier") not in out["review"]["reviewer_models"]


# ---------------------------------------------------------------------------
# Swept invariants — the property, not a handful of examples
# ---------------------------------------------------------------------------

def _sweep_routes():
    import itertools
    for task_class in POLICY.task_classes:
        for runtime in sorted(POLICY.runtimes):
            for c, u, b, rev in itertools.product(range(4), repeat=4):
                task = Task(task_class=task_class, complexity=c, uncertainty=u,
                            blast_radius=b, reversibility=rev, runtime=runtime)
                out = route(task, CFG)
                if out["terminal"]:
                    continue
                yield task, out


def test_no_dispatchable_write_route_names_a_seat_that_cannot_write():
    """The property this tranche exists to establish, over the whole space
    rather than the examples above."""
    checked = 0
    for task, out in _sweep_routes():
        if out["worker_seat"]["kind"] != "write":
            continue
        checked += 1
        family = POLICY.family_of[out["selected_model"]]
        assert POLICY.write_capable(task.runtime, family), (
            f"{task.task_class}/{task.runtime} seated {out['selected_model']} "
            f"({family}) for write work with no recipe of record")
    assert checked > 5_000, f"the sweep only reached {checked} write routes"


def test_the_coupling_changes_nothing_on_the_grok_host():
    """The native seat must be untouched, proved by difference rather than by
    construction: every grok-hosted route is re-run with the requirement turned
    off and must select the same model."""
    from dataclasses import replace as _replace
    checked = 0
    for task, out in _sweep_routes():
        if task.runtime != "grok":
            continue
        checked += 1
        off = route(_replace(task, worker_seat="read_only"), CFG)
        assert out["selected_model"] == off["selected_model"], (
            task.task_class, out["selected_model"], off["selected_model"])
        assert not any("no write-capable" in n for n in out["notes"])
    assert checked > 1_000, checked


def test_the_unprobed_codex_direction_does_not_seat_the_xai_worker_for_write_work():
    """claude_code.to_xai is write-verified; codex.to_xai is not."""
    left = [(t, o) for t, o in _sweep_routes()
            if t.runtime == "codex"
            and o["selected_model"] == ID("xai_frontier")]
    assert left, "the xai seat must survive for read-only work"
    assert all(o["worker_seat"]["kind"] == "read_only" for _, o in left)
    claude_write = [(t, o) for t, o in _sweep_routes()
                    if t.runtime == "claude_code"
                    and o["worker_seat"]["kind"] == "write"
                    and o["selected_model"] == ID("xai_frontier")]
    assert claude_write, "the shipped maker must staff Claude Code write work"


def test_write_capable_reads_a_mapping_config_not_only_a_dict():
    """`Policy.of` explicitly supports a non-dict Mapping — the config audit's
    read-recorder is one, and the docstring there says a `dict` subclass was
    rejected on purpose. Guarding the transport entry with
    `isinstance(entry, dict)` therefore short-circuited to False for EVERY
    cross-family direction under such a config, silently taking the verified
    openai and claude bridges down with the unshipped xai maker.

    Caught by `test_d14`, which noticed `transports.*.verified` was never read
    across 66,528 routes — the guard returned before reaching it.
    """
    from collections.abc import Mapping as _Mapping

    class _View(_Mapping):
        def __init__(self, data):
            self._d = data

        def __getitem__(self, k):
            v = self._d[k]
            return _View(v) if isinstance(v, dict) else v

        def __iter__(self):
            return iter(self._d)

        def __len__(self):
            return len(self._d)

    policy = Policy(_View(CFG))
    assert policy.write_capable("claude_code", "openai") is True
    assert policy.write_capable("codex", "claude") is True
    assert policy.write_capable("claude_code", "xai") is True
    assert policy.write_capable("codex", "xai") is False


# ---------------------------------------------------------------------------
# Round-1 review findings
# ---------------------------------------------------------------------------

def test_a_real_outage_after_the_policy_skip_is_still_recorded():
    """The policy skip must not swallow the NEXT seat's genuine unavailability.

    Round 1 (codex-review, Critical): the skip nulled `primary_id`, which is
    also the baseline a real fallback is measured against, so an outage of the
    first write-capable candidate produced no record, no confidence penalty and
    no review promotion. Recording a change that did not happen and failing to
    record one that did are the same defect from opposite sides; the first fix
    must not buy itself by committing the second.
    """
    task = Task(task_class="REFACTORING", complexity=0, uncertainty=2,
                blast_radius=0, reversibility=0, runtime="codex",
                unavailable_models=[ID("claude_worker_balanced")])
    out = route(task, CFG)
    assert out["selected_model"] == ID("openai_worker_balanced")
    assert any(f"{ID('claude_worker_balanced')} unavailable" in f for f in out["fallbacks_applied"]), \
        out["fallbacks_applied"]
    # The penalty is applied; a lone fallback no longer promotes on its own (DD-3, 1.9.0).
    assert out["routing_confidence"] == 0.81
    assert out["review"]["band"] == "MEDIUM"


def test_a_real_outage_plus_a_second_signal_still_promotes_the_review():
    """The consequence round 1 pinned — promotion — now needs a second signal."""
    task = Task(task_class="REFACTORING", complexity=0, uncertainty=2,
                blast_radius=0, reversibility=0, runtime="codex",
                unavailable_models=[ID("claude_worker_balanced")],
                prior_failures=1, prior_models=[ID("openai_worker_fast")])
    out = route(task, CFG)
    assert any(f"{ID('claude_worker_balanced')} unavailable" in f for f in out["fallbacks_applied"])
    assert out["routing_confidence"] == 0.76
    assert out["review"]["band"] == "HIGH"


def test_the_policy_skip_alone_still_records_no_fallback():
    """The other side of the same rule, pinned so a fix for the one above
    cannot re-introduce the scarcity report the skip must not make."""
    out = route(Task(**WRITE_ROUTE, runtime="codex"), CFG)
    assert out["selected_model"] != ID("xai_frontier")
    assert not [f for f in out["fallbacks_applied"] if "worker_balanced:" in f]


def _with_transport(**changes):
    """A config whose `claude_code.to_xai` entry carries `changes`."""
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["transports"]["claude_code"]["to_xai"].update(changes)
    return cfg


def test_a_maker_recipe_alone_does_not_authorize_write_dispatch():
    """Round 1 (codex-adversarial, Critical): `verified: true` on the direction
    attests the REVIEWER seat — the ledger says the maker seat is `not_shipped`
    in the same file. Reusing the direction flag as maker authorization meant a
    one-line config addition re-opened the measured hard-link and `~/.grok`
    escape paths without any probe."""
    cfg = _with_transport(mechanism_maker="UNPROBED-MAKER", write_verified=False)
    for item in cfg["verification_ledger"]["entries"]:
        if "maker seat recipe" in item["item"]:
            item["status"] = "not_shipped"
    assert Policy(cfg).write_capable("claude_code", "xai") is False


def test_renaming_the_reviewer_recipe_does_not_authorize_write_dispatch():
    """Round 1 (claude-opus, W6): capability was inferred from a key NAME, so
    renaming `mechanism_reviewer` to `mechanism` — a plausible tidy-up, since
    it is the only seat in that direction — routed write work onto the
    read-only grok argv."""
    import copy
    cfg = copy.deepcopy(CFG)
    entry = cfg["transports"]["claude_code"]["to_xai"]
    entry["mechanism"] = entry.pop("mechanism_reviewer")
    entry.pop("mechanism_maker", None)
    entry["write_verified"] = False
    for item in cfg["verification_ledger"]["entries"]:
        if "maker seat recipe" in item["item"]:
            item["status"] = "not_shipped"
    assert Policy(cfg).write_capable("claude_code", "xai") is False


def test_write_verified_must_agree_with_the_verification_ledger():
    """Fail closed on inconsistency: a direction cannot declare its write seat
    verified while the ledger still records that seat as not shipped. The
    ledger is the record; a second source that can silently disagree with it is
    the sand this file's header refuses to build on."""
    cfg = _with_transport(write_verified=True)
    for item in cfg["verification_ledger"]["entries"]:
        if "maker seat recipe" in item["item"]:
            item["status"] = "not_shipped"
    with pytest.raises(ConfigError, match="verification_ledger"):
        Policy(cfg)


def test_a_claude_code_ledger_row_does_not_authorize_codex():
    """The ledger item names the probed direction. A wildcard
    `transports.*.to_xai.mechanism_maker` row would let Codex flip
    write_verified without a Codex-host probe."""
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["transports"]["codex"]["to_xai"]["verified"] = True
    cfg["transports"]["codex"]["to_xai"]["write_verified"] = True
    with pytest.raises(ConfigError, match="verification_ledger"):
        Policy(cfg)


def test_an_empty_maker_recipe_does_not_authorize_write_dispatch():
    cfg = _with_transport(mechanism_maker="   ")
    with pytest.raises(ConfigError, match="write_verified"):
        Policy(cfg)


def test_write_verified_without_a_ledger_row_is_refused():
    cfg = _with_transport(write_verified=True)
    cfg["verification_ledger"]["entries"] = [
        item for item in cfg["verification_ledger"]["entries"]
        if "maker seat recipe" not in item.get("item", "")
    ]
    with pytest.raises(ConfigError, match="verification_ledger"):
        Policy(cfg)


def test_write_verified_requires_a_recipe_to_dispatch():
    """The mirror check: a direction cannot claim a verified write seat while
    carrying no write-capable mechanism string at all."""
    cfg = _with_transport(write_verified=True)
    del cfg["transports"]["claude_code"]["to_xai"]["mechanism_maker"]
    with pytest.raises(ConfigError, match="write_verified"):
        Policy(cfg)


def test_the_native_family_must_agree_with_the_transports_table():
    """Round 1 (claude-opus, W7): the native-seat shortcut read `local_family`,
    derived from `runtimes.<rt>.degraded_binding` — a different table from the
    one its docstring names. A `degraded_binding` edit therefore made a genuine
    CROSS-family direction answer `True` without reading a verification flag or
    a mechanism string at all: the one branch with no other guard, fail-open."""
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["runtimes"]["claude_code"]["degraded_binding"] = "xai_only"
    with pytest.raises(ConfigError, match="native"):
        Policy(cfg)


def test_a_verified_write_seat_that_agrees_with_the_ledger_is_authorized():
    """The positive path stays open: ship the recipe, record the probe in the
    ledger, and the direction comes back with no code change."""
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["transports"]["claude_code"]["to_xai"].update(
        mechanism_maker="grok --no-auto-update -m <id> --prompt-file /dev/stdin",
        write_verified=True)
    for entry in cfg["verification_ledger"]["entries"]:
        if "maker seat recipe" in entry["item"]:
            entry["status"] = "verified"
    policy = Policy(cfg)
    assert policy.write_capable("claude_code", "xai") is True
    assert route(Task(**WRITE_ROUTE, runtime="claude_code"), cfg)["selected_model"] == ID("xai_frontier")


def test_supply_exhausted_names_the_write_seat_when_that_is_the_cause():
    """Round 1 (claude-opus, W5): the terminal reason enumerated three causes —
    unavailable, already failed, downed bridge — and on this path none of them
    is true: the candidate exists, is available, has not failed, and the bridge
    is up. A reason that states things which did not happen is the shape this
    module spends its rounds removing."""
    task = Task(task_class="IMPLEMENTATION", complexity=1, uncertainty=1,
                blast_radius=1, reversibility=1, runtime="codex")
    task._local_policy = {"allowed_families": ["xai"]}
    out = route(task, CFG)
    assert out["terminal"] == "SUPPLY_EXHAUSTED"
    reason = next(n for n in out["notes"] if n.startswith("supply exhausted:"))
    assert "write-capable" in reason, reason


def test_declaring_a_weaker_seat_than_the_class_default_is_disclosed():
    """Round 1 (claude-opus, W3): `--worker-seat read_only` on a write class
    puts the unshipped-maker family straight back on the worker seat, and the
    route said so nowhere — while the opposite direction gets a note. A caller
    assertion that reverses a security-derived control is exactly what this
    module discloses loudly everywhere else."""
    out = route(Task(**WRITE_ROUTE, runtime="claude_code",
                     worker_seat="read_only"), CFG)
    assert out["selected_model"] == ID("xai_frontier")
    assert out["worker_seat"]["overrode_class_default"] is True
    assert any("declared read_only" in n and "IMPLEMENTATION" in n
               for n in out["notes"]), out["notes"]
    assert not any(m in n for n in out["notes"] for m in POLICY.model_ids)


def test_a_declaration_that_matches_the_class_default_is_not_flagged():
    out = route(Task(**WRITE_ROUTE, runtime="claude_code",
                     worker_seat="write"), CFG)
    assert out["worker_seat"]["overrode_class_default"] is False
    assert not any("declared read_only" in n for n in out["notes"])
