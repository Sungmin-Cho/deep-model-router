"""policy_pin (design 2026-09-25 DD-A9): a caller names the policy digest it
started under, and the router reproduces that policy from the committed
generation chain — or stops with a named reason.

Generations are written by the `_overlay` helpers straight into `committed/`
(model_sync's publisher is Task A10); the pointer is moved only where a test
says so, which is how an uncommitted generation is made.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _overlay import (  # noqa: E402
    BASE, BASE_SHA, ID, base_record, entry, env_for, generation, probe_record, publish,
    replacing, sha_of, state_root, successor, summary,
)
import model_state  # noqa: E402
from policy_digest import canonical_policy_sha256  # noqa: E402
from route_task import (  # noqa: E402
    Policy, Task, ValidationError, request_sha256_of, route, task_from_request_v1,
)
from secure_io import StateRoot  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "route_task.py"
TASK = dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, blast_radius=1,
            reversibility=1)
CLI = ["--class", "IMPLEMENTATION", "--complexity", "2", "--uncertainty", "1",
       "--blast-radius", "1", "--reversibility", "1", "--format", "json"]
DECISION = ("policy_sha256", "decision_fingerprint", "selected_model", "selected_role",
            "selected_effort", "review", "terminal")


def worker_key() -> str:
    return Policy.of(BASE).id_to_key[route(Task(**TASK), BASE)["selected_model"]]


def pinned(pin, **kw):
    t = Task(**{**TASK, **kw})
    t._policy_pin = pin
    return t


def cli(env, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *CLI, *args],
                          capture_output=True, text=True, env=env)


def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    return h


def two_step(key):
    """gen1 replaces `key` with its successor; gen2 (parent gen1) moves on to
    the one after, keeping gen1's id as probe history."""
    gen1, sums1 = replacing(key)
    (s1_sha,) = sums1
    e2 = entry(key, successor(key, 2), superseded=[successor(key)])
    s2 = summary(key, e2)
    e2["probe_summary_sha256"] = sha_of(s2)
    return gen1, sums1, e2, s2, s1_sha


def assert_state_terminal(out, reason):
    assert out["terminal"] == "MODEL_STATE_UNAVAILABLE", out.get("terminal")
    assert out["model_overlay"]["state_reason"] == reason
    text = json.dumps(out)
    ids = {m["id"] for m in BASE["models"].values()} | {
        successor(k, n) for k in ("openai_reasoning", "claude_senior", "claude_worker_balanced")
        for n in (1, 2)}
    assert not {i for i in ids if i in text}


# ---------------------------------------------------------------------------
# Grammar and identity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["abc", "A" * 64, "g" * 64, "0" * 63, 5])
def test_a_pin_must_be_lowercase_hex64(tmp_path, bad):
    payload = {"route_schema_version": 1, **TASK, "policy_pin": bad}
    with pytest.raises(ValidationError):
        task_from_request_v1(payload)
    if isinstance(bad, str):
        proc = cli(env_for(state_root(tmp_path)), "--policy-pin", bad)
        assert proc.returncode == 2, proc.stdout


def test_a_pin_is_not_part_of_the_request_identity():
    plain = task_from_request_v1({"route_schema_version": 1, **TASK})
    with_pin = task_from_request_v1({"route_schema_version": 1, **TASK, "policy_pin": BASE_SHA})
    assert with_pin._policy_pin == BASE_SHA
    assert request_sha256_of(plain) == request_sha256_of(with_pin)


def test_a_pin_on_an_explicit_cfg_is_refused():
    with pytest.raises(ValidationError, match="policy_pin"):
        route(pinned(BASE_SHA), BASE)


def test_a_pin_equal_to_the_current_policy_routes_as_usual(tmp_path):
    root = state_root(tmp_path)
    out = route(pinned(BASE_SHA), env=env_for(root), home=home(tmp_path))
    assert out == route(Task(**TASK), env=env_for(root), home=home(tmp_path))
    proc = cli(env_for(root), "--policy-pin", BASE_SHA)
    assert proc.returncode == 0 and json.loads(proc.stdout)["policy_sha256"] == BASE_SHA


# ---------------------------------------------------------------------------
# Reproduction through the parent chain
# ---------------------------------------------------------------------------

def test_an_old_pin_reproduces_its_policy_after_a_newer_generation(tmp_path):
    key = worker_key()
    root = state_root(tmp_path)
    gen1, sums1, e2, s2, s1_sha = two_step(key)
    g1 = publish(root, gen1, sums1)
    env, h = env_for(root), home(tmp_path)
    before = route(Task(**TASK), env=env, home=h)
    assert before["selected_model"] == successor(key)
    gen2 = generation({key: e2}, {ID(key): base_record(key),
                                  successor(key): probe_record(key, s1_sha)}, parent=g1)
    publish(root, gen2, {sha_of(s2): s2})
    now = route(Task(**TASK), env=env, home=h)
    assert now["selected_model"] == successor(key, 2)
    again = route(pinned(before["policy_sha256"]), env=env, home=h)
    for k in DECISION:
        assert again[k] == before[k], k
    assert again["model_overlay"]["status"] == "pinned"
    assert again["model_overlay"]["generation_sha256"] == g1
    proc = cli(env, "--policy-pin", before["policy_sha256"])
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["decision_fingerprint"] == before["decision_fingerprint"]


def test_the_pin_is_resolved_before_validation(tmp_path):
    """An id that is history only in the PINNED generation is valid input."""
    key = worker_key()
    root = state_root(tmp_path)
    gen1, sums1, e2, s2, s1_sha = two_step(key)
    gen1 = generation({key: e2}, {ID(key): base_record(key),
                                  successor(key): probe_record(key, s1_sha)})
    sums = {**sums1, sha_of(s2): s2}
    g1 = publish(root, gen1, sums)
    env, h = env_for(root), home(tmp_path)
    p1 = route(Task(**TASK), env=env, home=h)["policy_sha256"]
    publish(root, generation(parent=g1), {})          # key back on its base id
    with pytest.raises(ValidationError):
        route(Task(**TASK, prior_failures=1, prior_models=[successor(key)]), env=env, home=h)
    out = route(pinned(p1, prior_failures=1, prior_models=[successor(key)]), env=env, home=h)
    assert out["policy_sha256"] == p1


def test_an_uncommitted_generation_cannot_be_pinned(tmp_path):
    key = worker_key()
    root = state_root(tmp_path)
    gen1, sums1 = replacing(key)
    g1 = publish(root, gen1, sums1)
    stray, stray_sums = replacing(key, step=3, parent=g1)
    publish(root, stray, stray_sums, point=False)     # crash before the pointer moved
    cfg, _ = model_state.effective_config(BASE, stray, apply_entries=True,
                                          summary=stray_sums.get)
    out = route(pinned(canonical_policy_sha256(cfg)), env=env_for(root), home=home(tmp_path))
    assert_state_terminal(out, "pin_generation_missing")


def test_an_unknown_pin_is_missing_and_exits_1(tmp_path):
    key = worker_key()
    root = state_root(tmp_path)
    publish(root, *replacing(key))
    proc = cli(env_for(root), "--policy-pin", "a" * 64)
    assert proc.returncode == 1
    assert_state_terminal(json.loads(proc.stdout), "pin_generation_missing")


def test_no_state_and_a_foreign_pin_is_missing(tmp_path):
    out = route(pinned("b" * 64), env=env_for(state_root(tmp_path)), home=home(tmp_path))
    assert_state_terminal(out, "pin_generation_missing")


def test_overlay_off_suppresses_a_pin_that_needs_entries(tmp_path):
    key = worker_key()
    root = state_root(tmp_path)
    publish(root, *replacing(key))
    p1 = route(Task(**TASK), env=env_for(root), home=home(tmp_path))["policy_sha256"]
    out = route(pinned(p1), env=env_for(root, off=True), home=home(tmp_path))
    assert_state_terminal(out, "pin_suppressed_by_off")


def _revoked_chain(root, key, gen1_base_sha=BASE_SHA):
    gen1, sums1 = replacing(key)
    gen1["base_policy_sha256"] = gen1_base_sha
    (s1_sha,) = sums1
    g1 = publish(root, gen1, sums1)
    cfg1, _ = model_state.effective_config(BASE, gen1, apply_entries=True, summary=sums1.get)
    revert = generation({}, {ID(key): base_record(key),
                             successor(key): probe_record(key, s1_sha)},
                        blocked=[successor(key)], parent=g1)
    publish(root, revert, sums1)
    return canonical_policy_sha256(cfg1)


def test_a_revocation_after_the_pin_wins(tmp_path):
    key = worker_key()
    root = state_root(tmp_path)
    p1 = _revoked_chain(root, key)
    out = route(pinned(p1), env=env_for(root), home=home(tmp_path))
    assert_state_terminal(out, "pin_revoked")


def test_revoked_is_reported_before_base_changed(tmp_path):
    """Both are true of this chain: gen1 recorded another base, and its id is
    revoked now. The order is fixed (off -> revoked -> base_changed -> missing)."""
    key = worker_key()
    root = state_root(tmp_path)
    p1 = _revoked_chain(root, key, gen1_base_sha="e" * 64)
    out = route(pinned(p1), env=env_for(root), home=home(tmp_path))
    assert_state_terminal(out, "pin_revoked")


def test_a_chain_from_another_base_reports_base_changed(tmp_path):
    key = worker_key()
    root = state_root(tmp_path)
    gen1, sums1 = replacing(key)
    gen1["base_policy_sha256"] = "e" * 64
    publish(root, gen1, sums1)
    out = route(pinned("c" * 64), env=env_for(root), home=home(tmp_path))
    assert_state_terminal(out, "pin_base_changed")


def test_off_is_reported_before_base_changed(tmp_path):
    """Both are true: the pin needs gen1's entry (suppressed by off) and gen1
    recorded another base. Off comes first."""
    key = worker_key()
    root = state_root(tmp_path)
    gen1, sums1 = replacing(key)
    gen1["base_policy_sha256"] = "e" * 64
    publish(root, gen1, sums1)
    p1 = route(Task(**TASK), env=env_for(root), home=home(tmp_path))["policy_sha256"]
    out = route(pinned(p1), env=env_for(root, off=True), home=home(tmp_path))
    assert_state_terminal(out, "pin_suppressed_by_off")


def test_pinned_routes_never_see_an_unpublished_combination(tmp_path):
    """A pointer flipping between two committed generations under a pinned
    route: every answer is the pinned policy, and every unpinned answer is one
    of the two published ones — never a mixture."""
    key = worker_key()
    root = state_root(tmp_path)
    gen1, sums1, e2, s2, s1_sha = two_step(key)
    g1 = publish(root, gen1, sums1)
    env, h = env_for(root), home(tmp_path)
    p1 = route(Task(**TASK), env=env, home=h)["policy_sha256"]
    gen2 = generation({key: e2}, {ID(key): base_record(key),
                                  successor(key): probe_record(key, s1_sha)}, parent=g1)
    g2 = publish(root, gen2, {sha_of(s2): s2})
    p2 = route(Task(**TASK), env=env, home=h)["policy_sha256"]
    stop = threading.Event()

    def flip():
        with StateRoot.open(root) as r:
            while not stop.is_set():
                model_state.write_pointer(r, g1)
                model_state.write_pointer(r, g2)
    t = threading.Thread(target=flip)
    t.start()
    try:
        for _ in range(60):
            assert route(pinned(p1), env=env, home=h)["policy_sha256"] == p1
            assert route(Task(**TASK), env=env, home=h)["policy_sha256"] in (p1, p2)
    finally:
        stop.set()
        t.join()
