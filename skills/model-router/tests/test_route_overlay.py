"""The router on its effective policy (design 2026-09-25 DD-A2): overlay
provenance, the MODEL_STATE_UNAVAILABLE fail-closed terminal, OVERLAY=off,
and API hermeticity. Every state is built under `tmp_path` and reached only
through an explicit environment.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _overlay import (  # noqa: E402
    BASE, BASE_SHA, ID, env_for, generation, base_record, probe_record, publish,
    replacing, state_root, successor,
)
from policy_digest import canonical_policy_sha256  # noqa: E402
from route_task import Policy, Task, resolve_effective_policy, route  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "route_task.py"
TASK = dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, blast_radius=1,
            reversibility=1)
CLI = ["--class", "IMPLEMENTATION", "--complexity", "2", "--uncertainty", "1",
       "--blast-radius", "1", "--reversibility", "1", "--format", "json"]


def worker_key() -> str:
    out = route(Task(**TASK), BASE)
    return Policy.of(BASE).id_to_key[out["selected_model"]]


def every_id(cfg=BASE) -> set:
    return {m["id"] for m in cfg["models"].values()}


def cli(env, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *CLI, *args],
                          capture_output=True, text=True, env=env)


def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    return h


@pytest.fixture
def applied(tmp_path):
    key = worker_key()
    root = state_root(tmp_path)
    gen, sums = replacing(key)
    sha = publish(root, gen, sums)
    return key, root, sha


def test_an_applied_generation_seats_the_successor_and_says_so(tmp_path, applied):
    key, root, sha = applied
    out = route(Task(**TASK), env=env_for(root), home=home(tmp_path))
    assert out["terminal"] is None
    assert out["selected_model"] == successor(key)
    eff = resolve_effective_policy(env_for(root), home(tmp_path))
    assert out["policy_sha256"] == canonical_policy_sha256(eff.config) != BASE_SHA
    ov = out["model_overlay"]
    assert ov["status"] == "applied" and ov["applied"] == [key]
    assert ov["generation_sha256"] == sha and ov["base_policy_sha256"] == BASE_SHA
    assert ov["history_ids_synthesized"] == 1 and ov["state_reason"] is None
    note = next(n for n in out["notes"] if n.startswith(f"{key}: id from local model overlay"))
    assert "price unavailable" in note and "maker seat not re-probed" in note


def test_no_committed_state_routes_on_the_base_with_a_null_overlay(tmp_path):
    root = state_root(tmp_path)
    out = route(Task(**TASK), env=env_for(root), home=home(tmp_path))
    assert out["model_overlay"] is None
    assert out["policy_sha256"] == BASE_SHA
    assert out == route(Task(**TASK), BASE)


def _assert_state_unavailable(out, reason):
    assert out["terminal"] == "MODEL_STATE_UNAVAILABLE"
    assert out["model_overlay"]["status"] == "unavailable"
    assert out["model_overlay"]["state_reason"] == reason
    text = json.dumps(out)
    leaked = {i for i in every_id() | {successor(k) for k in ("openai_reasoning", "claude_senior")}
              if i in text}
    assert not leaked, leaked


def test_a_corrupt_pointer_fails_closed_with_no_model_named(tmp_path, applied):
    _, root, _ = applied
    (root / "committed" / "current.json").write_text("{not json")
    _assert_state_unavailable(route(Task(**TASK), env=env_for(root), home=home(tmp_path)),
                              "unreadable")
    proc = cli(env_for(root))
    assert proc.returncode == 1, proc.stderr
    _assert_state_unavailable(json.loads(proc.stdout), "unreadable")


def test_a_generation_hash_mismatch_fails_closed(tmp_path, applied):
    _, root, sha = applied
    path = root / "committed" / "generations" / f"{sha}.json"
    path.write_text(path.read_text().replace('"blocked_ids":[]', '"blocked_ids":["x"]'))
    _assert_state_unavailable(route(Task(**TASK), env=env_for(root), home=home(tmp_path)),
                              "unreadable")


def test_a_deleted_pointer_under_committed_fails_closed(tmp_path, applied):
    _, root, _ = applied
    (root / "committed" / "current.json").unlink()
    _assert_state_unavailable(route(Task(**TASK), env=env_for(root), home=home(tmp_path)),
                              "unreadable")


def test_deleting_work_state_changes_no_route(tmp_path, applied):
    _, root, _ = applied
    from secure_io import StateRoot
    with StateRoot.open(root) as r:
        r.write_json_atomic("work/state.json", {"tick": 1})
    before = route(Task(**TASK), env=env_for(root), home=home(tmp_path))
    (root / "work" / "state.json").unlink()
    assert route(Task(**TASK), env=env_for(root), home=home(tmp_path)) == before


def test_overlay_off_ignores_entries_but_not_revocations(tmp_path):
    key = worker_key()
    root = state_root(tmp_path)
    gen, sums = replacing(key, blocked=[ID(key)])
    publish(root, gen, sums)
    on = route(Task(**TASK), env=env_for(root), home=home(tmp_path))
    assert on["selected_model"] == successor(key)
    off = route(Task(**TASK), env=env_for(root, off=True), home=home(tmp_path))
    seated = {off["selected_model"], off["review"]["judge_model"], *off["review"]["reviewer_models"]}
    assert successor(key) not in seated, "OVERLAY=off must not apply entries"
    assert ID(key) not in seated, "a revoked base id must not come back under OVERLAY=off"
    assert off["model_overlay"]["applied"] == [] and off["model_overlay"]["blocked_ids"] == 1


def test_overlay_off_does_not_bypass_a_corrupt_state(tmp_path, applied):
    _, root, _ = applied
    (root / "committed" / "current.json").write_text("[]")
    _assert_state_unavailable(
        route(Task(**TASK), env=env_for(root, off=True), home=home(tmp_path)), "unreadable")


def test_an_explicit_cfg_never_reads_state(tmp_path, applied):
    _, root, _ = applied
    (root / "committed" / "current.json").write_text("{not json")
    out = route(Task(**TASK), BASE, env=env_for(root), home=home(tmp_path))
    assert out["terminal"] is None and out["model_overlay"] is None


def test_a_reverted_overlay_id_is_valid_retry_history(tmp_path):
    key = worker_key()
    root = state_root(tmp_path)
    gen, sums = replacing(key)
    publish(root, gen, sums)
    (s_sha,) = sums
    reverted = generation({}, {ID(key): base_record(key),
                               successor(key): probe_record(key, s_sha)},
                          blocked=[successor(key)])
    publish(root, reverted, sums)
    proc = cli(env_for(root), "--prior-failures", "1", "--prior-models", successor(key))
    assert proc.returncode != 2, proc.stderr
    out = json.loads(proc.stdout)
    assert out["selected_model"] != successor(key)


def test_a_developer_state_under_home_is_invisible_to_the_suite(tmp_path, applied):
    """conftest points DEEP_MODEL_ROUTER_STATE_DIR at a session directory, and
    that wins over HOME and XDG_STATE_HOME: a real ~/.local/state root never
    reaches a test route."""
    _, root, _ = applied
    fake_home = home(tmp_path)
    dev_root = fake_home / ".local" / "state"
    dev_root.mkdir(parents=True)
    root.rename(dev_root / "deep-model-router")
    (dev_root / "deep-model-router" / "committed" / "current.json").write_text("{not json")
    # Control: without the conftest variable the planted state IS what would be read.
    exposed = {k: v for k, v in os.environ.items() if k != "DEEP_MODEL_ROUTER_STATE_DIR"}
    exposed.pop("XDG_STATE_HOME", None)
    assert route(Task(**TASK), env=exposed, home=fake_home)["terminal"] == "MODEL_STATE_UNAVAILABLE"
    # The suite's own environment does not see it — API and CLI.
    assert route(Task(**TASK), env=os.environ, home=fake_home)["terminal"] is None
    proc = cli({**os.environ, "HOME": str(fake_home)})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_model_overlay_is_not_an_observation_decision_key():
    import validate_observation
    assert "model_overlay" not in validate_observation.DECISION_KEYS


def test_the_effective_config_is_interned_by_digest(tmp_path, applied):
    _, root, _ = applied
    a = resolve_effective_policy(env_for(root), home(tmp_path)).config
    b = resolve_effective_policy(env_for(root), home(tmp_path)).config
    assert a is b, "one digest must map to one config object, so Policy.of hits its cache"


def test_the_policy_cache_is_bounded(tmp_path):
    import copy
    for i in range(12):
        cfg = copy.deepcopy(BASE)
        cfg["router"]["confidence"]["escalate_below"] = 0.5 + i * 0.001
        Policy.of(cfg)
    assert len(Policy._cache) <= 8
