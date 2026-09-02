"""RouteRequestV1, identity fields, local_policy merge, locator."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from locate_router import locate_router_cli
from policy_digest import policy_sha256
from route_task import (  # noqa: E402
    CONFIG_PATH,
    Task,
    main,
    plugin_manifest_version,
    route,
)

from route_task import load_config as _load_config  # noqa: E402
_CFG = _load_config()
ID = lambda key: _CFG["models"][key]["id"]                       # noqa: E731


def r(**kw):
    kw.setdefault("complexity", 0)
    kw.setdefault("uncertainty", 0)
    kw.setdefault("blast_radius", 0)
    kw.setdefault("reversibility", 0)
    return route(Task(**kw))


def test_decision_carries_identity_fields():
    out = r(task_class="MECHANICAL")
    assert out["route_schema_version"] == 1
    assert out["router_plugin_version"] == plugin_manifest_version()
    assert re.fullmatch(r"[0-9a-f]{64}", out["policy_sha256"])
    assert out["policy_sha256"] == policy_sha256(CONFIG_PATH)


def test_policy_digest_ignores_comments(tmp_path):
    src = CONFIG_PATH.read_text(encoding="utf-8")
    a = policy_sha256(CONFIG_PATH)
    copy = tmp_path / "cfg.yaml"
    copy.write_text("# extra comment\n" + src, encoding="utf-8")
    assert policy_sha256(copy) == a


def test_plugin_manifest_walks_ancestors():
    scripts = Path(__file__).resolve().parent.parent / "scripts" / "route_task.py"
    root = Path(__file__).resolve()
    for parent in root.parents:
        manifest = parent / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            expected = json.loads(manifest.read_text(encoding="utf-8"))["version"]
            break
    else:
        raise AssertionError("no .claude-plugin/plugin.json above " + str(root))
    assert plugin_manifest_version(scripts) == expected


def test_request_json_unknown_field_exits_2(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
        "extra": True,
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 2


def test_unsupported_schema_version_exits_2(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 99, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 2


def test_allowed_families_empty_is_unsatisfiable(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
        "local_policy": {"allowed_families": []},
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 1
    # parse via route()
    out = route(Task(
        task_class="MECHANICAL", complexity=0, uncertainty=0,
        blast_radius=0, reversibility=0,
        _local_policy={"allowed_families": []},
    ))
    assert out["terminal"] == "UNSATISFIABLE_LOCAL_POLICY"


def test_allowed_families_claude_never_emits_openai():
    out = route(Task(
        task_class="MECHANICAL", complexity=0, uncertainty=0,
        blast_radius=0, reversibility=0,
        _local_policy={"allowed_families": ["claude"]},
    ))
    assert out["terminal"] is None
    from route_task import Policy, load_config
    fam = Policy.of(load_config()).family_of
    assert fam[out["selected_model"]] == "claude"


def test_minimum_effort_is_monotonic():
    out = route(Task(
        task_class="MECHANICAL", complexity=0, uncertainty=0,
        blast_radius=0, reversibility=0,
        _local_policy={"minimum_effort": "HIGH"},
    ))
    assert out["selected_effort"] == "HIGH"
    assert out["effective_policy"]["minimum_effort"] == "HIGH"


def test_isolation_evidence_without_isolation_exits_2(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
        "availability_snapshot": {"isolation_evidence": ["abc"]},
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 2


def test_nested_unknown_local_policy_field_exits_2(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
        "local_policy": {"nope": 1},
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 2


def test_gemini_is_never_selected():
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2)
    assert out.get("selected_model") != ID("gemini_flash")
    emitted = set()
    if out.get("selected_model"):
        emitted.add(out["selected_model"])
    emitted.update(out.get("review", {}).get("reviewer_models") or [])
    assert ID("gemini_flash") not in emitted


def test_locator_env_hit(tmp_path):
    target = tmp_path / "route_task.py"
    target.write_text("# stub\n")
    found = locate_router_cli({"DEEP_MODEL_ROUTER_CLI": str(target)}, home=tmp_path)
    assert found == target.resolve()


def test_locator_rejects_personal_symlink(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "model-router" / "scripts"
    skill.mkdir(parents=True)
    target = skill / "route_task.py"
    target.write_text("# stub\n")
    found = locate_router_cli(
        {"DEEP_MODEL_ROUTER_CLI": str(target)},
        home=tmp_path,
    )
    assert found is None


# ---------------------------------------------------------------------------
# B1 — `local_policy` VALUES are validated, not only its key names
#
# The audit of 2026-08-18 found this failing wrong in both directions: an
# unknown effort token exited 0 with the emitted route REPORTING the floor as
# applied while the membership test silently dropped it, and a non-numeric
# tier crashed to exit 5 — the status reserved for "never a routing outcome".
# Contract-violating input is exit 2, and a floor that is reported must be a
# floor that ran.
# ---------------------------------------------------------------------------

def _req(tmp_path, local_policy):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
        "local_policy": local_policy,
    }))
    return ["--request-json", str(p), "--format", "json"]


@pytest.mark.parametrize("local_policy", [
    {"minimum_effort": "SUPER_HIGH"},
    {"minimum_effort": "high"},          # the native spelling, not the level
    {"minimum_effort": 3},
    {"minimum_capability_tier": "abc"},
    {"minimum_capability_tier": -1},
    {"minimum_capability_tier": 1.5},
    {"minimum_capability_tier": True},   # bool is an int subclass; not a tier
    {"minimum_reviewers": "two"},
    {"minimum_reviewers": -1},
    {"minimum_reviewers": False},
    {"minimum_provider_families": "two"},
    {"minimum_provider_families": -1},
    {"minimum_provider_families": 2.0},
    {"allowed_families": "claude"},      # a bare string is not a list
    {"allowed_families": ["claude", "anthropic"]},
    {"allowed_families": [1]},
])
def test_invalid_local_policy_value_exits_2(tmp_path, local_policy):
    assert main(_req(tmp_path, local_policy)) == 2


@pytest.mark.parametrize("local_policy", [
    {"minimum_effort": "HIGH"},
    {"minimum_capability_tier": 0},
    {"minimum_reviewers": 0},
    {"minimum_provider_families": 1},
    {"allowed_families": ["claude", "openai"]},
    {},
])
def test_valid_local_policy_value_is_accepted(tmp_path, local_policy):
    assert main(_req(tmp_path, local_policy)) in (0, 1, 3, 4)


def test_route_never_reports_a_floor_it_did_not_apply():
    """The emit-boundary property B1 broke: `effective_policy` is what ran."""
    from route_task import Policy, ValidationError, load_config
    policy = Policy.of(load_config())
    for asked in policy.efforts:
        out = route(Task(
            task_class="MECHANICAL", complexity=0, uncertainty=0,
            blast_radius=0, reversibility=0,
            _local_policy={"minimum_effort": asked},
        ))
        assert out["effective_policy"]["minimum_effort"] == asked
        if out["terminal"] is None:
            assert policy.efforts.index(out["selected_effort"]) >= policy.efforts.index(asked)
    with pytest.raises(ValidationError):
        route(Task(
            task_class="MECHANICAL", complexity=0, uncertainty=0,
            blast_radius=0, reversibility=0,
            _local_policy={"minimum_effort": "SUPER_HIGH"},
        ))


# ---------------------------------------------------------------------------
# B3 / B4 — the locator resolves in its documented order, by parsed version,
# with the source-checkout guard on every tier
#
# Before the 2026-08-18 audit the two cache tiers were merged into one list and
# ordered by `sorted(..., key=str)[-1]`, which decided two things by accident:
# `.codex` sorts after `.claude`, so the documented step 4 always beat step 3,
# and `1.9.0` sorts after `1.10.0`, so the first 1.10 release would have sent
# every consumer to an older router. The guard that refuses a source checkout
# ran on the CLI tier only.
# ---------------------------------------------------------------------------

def _plant(root: Path, version: str) -> Path:
    p = root / "deep-model-router" / version / "skills" / "model-router" / "scripts" / "route_task.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# stub\n")
    return p.resolve()


def test_locator_prefers_claude_cache_over_codex_cache(tmp_path):
    claude = _plant(tmp_path / ".claude" / "plugins" / "cache" / "mk", "1.0.0")
    _plant(tmp_path / ".codex" / "plugins" / "mk", "9.9.9")
    assert locate_router_cli({}, home=tmp_path) == claude


def test_locator_orders_cache_hits_by_parsed_version(tmp_path):
    cache = tmp_path / ".claude" / "plugins" / "cache" / "mk"
    _plant(cache, "1.9.0")
    newest = _plant(cache, "1.10.0")
    _plant(cache, "1.2.0")
    assert locate_router_cli({}, home=tmp_path) == newest


def test_locator_falls_back_to_codex_cache_when_claude_has_none(tmp_path):
    codex = _plant(tmp_path / ".codex" / "plugins" / "mk", "1.0.0")
    (tmp_path / ".claude" / "plugins" / "cache").mkdir(parents=True)
    assert locate_router_cli({}, home=tmp_path) == codex


def test_locator_root_tier_resolves(tmp_path):
    root = tmp_path / "installed"
    target = root / "skills" / "model-router" / "scripts" / "route_task.py"
    target.parent.mkdir(parents=True)
    target.write_text("# stub\n")
    assert locate_router_cli({"DEEP_MODEL_ROUTER_ROOT": str(root)}, home=tmp_path) == target.resolve()


def test_locator_root_tier_refuses_a_source_checkout(tmp_path):
    root = tmp_path / "claude-plugins" / "deep-model-router"
    target = root / "skills" / "model-router" / "scripts" / "route_task.py"
    target.parent.mkdir(parents=True)
    target.write_text("# stub\n")
    cached = _plant(tmp_path / ".claude" / "plugins" / "cache" / "mk", "1.0.0")
    # The same path is already refused through DEEP_MODEL_ROUTER_CLI; refusing
    # it here too is what makes the rule a rule.
    assert locate_router_cli({"DEEP_MODEL_ROUTER_CLI": str(target)}, home=tmp_path) == cached
    assert locate_router_cli({"DEEP_MODEL_ROUTER_ROOT": str(root)}, home=tmp_path) == cached


def test_locator_cache_tier_refuses_a_personal_symlink(tmp_path):
    skill = tmp_path / ".claude" / "plugins" / "cache" / "mk" / "deep-model-router" / "1.0.0"
    real = skill / "skills" / "model-router" / "scripts"
    real.mkdir(parents=True)
    (real / "route_task.py").write_text("# stub\n")
    personal = tmp_path / ".claude" / "skills" / "model-router" / "scripts"
    personal.mkdir(parents=True)
    (personal / "route_task.py").write_text("# stub\n")
    # Only the cache copy is reachable through the glob, and it is the one
    # returned — the personal tree is never a locator answer.
    found = locate_router_cli({}, home=tmp_path)
    assert found == (real / "route_task.py").resolve()


def test_locator_missing_everywhere_is_none(tmp_path):
    assert locate_router_cli({}, home=tmp_path) is None


def test_locator_env_tier_beats_root_tier(tmp_path):
    cli = tmp_path / "cli" / "route_task.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# stub\n")
    root = tmp_path / "installed"
    (root / "skills" / "model-router" / "scripts").mkdir(parents=True)
    (root / "skills" / "model-router" / "scripts" / "route_task.py").write_text("# stub\n")
    found = locate_router_cli(
        {"DEEP_MODEL_ROUTER_CLI": str(cli), "DEEP_MODEL_ROUTER_ROOT": str(root)},
        home=tmp_path,
    )
    assert found == cli.resolve()


# ---------------------------------------------------------------------------
# Coverage top-ups from the 2026-08-18 audit §5
# ---------------------------------------------------------------------------

def test_a_plain_route_exits_0(tmp_path):
    """Exits 1 through 5 are each pinned by a test and 0 was not, so the
    contract's ordinary answer — "dispatchable as written" — was the one status
    nothing asserted."""
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 0
    assert main(["--class", "MECHANICAL", "--complexity", "0", "--uncertainty", "0",
                 "--blast-radius", "0", "--reversibility", "0", "--format", "json"]) == 0


def test_text_format_prints_the_executable_labels(capsys):
    """`--format text` is the default, and every assertion in the suite reads
    JSON. The non-terminal branch prints the worker, the effort pair and the
    reviewer models — the lines a human actually acts on — and nothing checked
    that any of them appear."""
    assert main(["--class", "IMPLEMENTATION", "--complexity", "1", "--uncertainty", "1",
                 "--blast-radius", "1", "--reversibility", "0"]) == 0
    out = capsys.readouterr().out
    for label in ("risk_score:", "risk_band:", "worker:", "effort:", "review:",
                  "  band:", "  reviewers:", "  models:", "  effort:",
                  "cross_family_review:", "fallbacks:", "confidence:"):
        assert label in out, label
    assert "TERMINAL:" not in out
    assert "(native:" in out


def test_text_format_withholds_bindings_on_a_terminal_route(capsys):
    """The terminal branch must print the review as policy and no model at
    all — the same withholding the JSON emit does, in the format a human
    reads."""
    assert main(["--class", "MECHANICAL", "--complexity", "0", "--uncertainty", "0",
                 "--blast-radius", "0", "--reversibility", "0",
                 "--prior-failures", "1", "--prior-models", "worker_fast"]) == 1
    out = capsys.readouterr().out
    assert "TERMINAL:" in out
    assert "review (policy only — not dispatchable):" in out
    assert "  models:" not in out


def test_policy_digest_reuses_its_cache_and_notices_a_real_edit(tmp_path):
    """The mtime cache has no test at all, and it is the one place a stale
    answer would be silent: the digest is emitted as `policy_sha256`, the field
    a consumer uses to say which policy produced a route."""
    import os
    from policy_digest import _DIGEST_CACHE

    cfg = tmp_path / "policy.yaml"
    cfg.write_text("a: 1\n", encoding="utf-8")
    first = policy_sha256(cfg)
    assert _DIGEST_CACHE[str(cfg.resolve())][1] == first
    assert policy_sha256(cfg) == first

    # A different mtime AND different content: a new digest.
    cfg.write_text("a: 2\n", encoding="utf-8")
    os.utime(cfg, ns=(1_000_000_000, 1_000_000_000))
    assert policy_sha256(cfg) != first

    # Keyed on the resolved path, so a symlink shares the target's entry
    # instead of digesting the same bytes twice under two names.
    link = tmp_path / "alias.yaml"
    link.symlink_to(cfg)
    before = len(_DIGEST_CACHE)
    assert policy_sha256(link) == policy_sha256(cfg)
    assert len(_DIGEST_CACHE) == before


def test_routing_confidence_kind_is_emitted_as_heuristic():
    """design §3 A5: 이 값은 gate용 heuristic이지 calibrated 성공 확률이
    아니다 — kind 필드가 그 사실을 스키마에 고정한다."""
    out = route(Task(task_class="MECHANICAL", complexity=0, uncertainty=0,
                     blast_radius=0, reversibility=0))
    assert out["routing_confidence_kind"] == "heuristic_policy_score"


def test_canonical_policy_digest_matches_file_digest_and_tracks_mutation(tmp_path):
    """design §4 B1: dict content-digest는 (i) 같은 내용의 파일 digest와
    동일하고, (ii) in-place 변이를 즉시 반영해야 한다(캐시 없음 — id 캐시는
    stale digest로 B1이 고치려는 결함을 재도입한다)."""
    import yaml
    from policy_digest import policy_sha256, canonical_policy_sha256
    from route_task import CONFIG_PATH
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    assert canonical_policy_sha256(raw) == policy_sha256(CONFIG_PATH)
    before = canonical_policy_sha256(raw)
    raw["router"]["default_worker"] = "worker_balanced"
    assert canonical_policy_sha256(raw) != before, "mutation must change the digest"
    # 내용이 같으면 같은 digest — 변이된 dict와 무관하게, 새로 읽은 사본은
    # 변이 전 값으로 돌아온다 (D9: 항진 단언 금지이므로 실제로 대조한다).
    copy = yaml.safe_load(CONFIG_PATH.read_text())
    assert canonical_policy_sha256(copy) == before
    assert canonical_policy_sha256(copy) == policy_sha256(CONFIG_PATH)
