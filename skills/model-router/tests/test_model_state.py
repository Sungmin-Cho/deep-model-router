"""Local model state (design 2026-09-25 DD-A0, DD-A2): fd-rooted file access,
the three state shapes, the generation schema, and the merge table in order.

Every state lives under `tmp_path`; nothing here reads the session directory
conftest points DEEP_MODEL_ROUTER_STATE_DIR at. Successor ids are DERIVED from
a registry row's lineage template (never spelled), so a registry bump moves
these fixtures with it.
"""
import copy
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest

import lineage
import model_state
import secure_io
from model_state import (
    base_row_sha256, effective_config, read_state, recipe_sha256, state_root_path,
    write_generation, write_pointer, write_summary,
)
from policy_digest import canonical_policy_sha256
from route_task import Policy, Task, load_config, route
from secure_io import StateError, StateRoot

BASE = load_config()
BASE_SHA = canonical_policy_sha256(BASE)
ID = lambda key: BASE["models"][key]["id"]                       # noqa: E731
KEY = "openai_reasoning"
XAI = "xai_frontier"


def successor(key: str, step: int = 1) -> str:
    """An id `step` major generations above the row's live id, spelled by its
    own lineage template."""
    template = BASE["models"][key]["lineage"]["template"]
    major = lineage.parse(template, ID(key)).parts[0]
    return template.replace("{gen}", str(major + step)).replace("[-{date}]", "")


def spelled(key: str, gen: str) -> str:
    return BASE["models"][key]["lineage"]["template"].replace("{gen}", gen).replace("[-{date}]", "")


def summary_for(row_key, entry, base=BASE, **over):
    key = row_key
    s = {"key": key, "line": entry["line"], "from_id": entry["from_id"], "id": entry["id"],
         "superseded": list(entry["superseded"]), "effort_map": dict(entry["effort_map"]),
         "effort_ceiling": entry["effort_ceiling"], "overlay_schema_version": 1,
         "base_row_sha256": base_row_sha256(base, key),
         "recipe_sha256": recipe_sha256(base, base["models"][key]["family"]),
         "router_version": "test"}
    s.update(over)
    return s


def sha_of(obj) -> str:
    return hashlib.sha256(secure_io.canonical_json_bytes(obj)).hexdigest()


def entry_for(key, new_id, *, superseded=(), effort_map=None, ceiling=None, from_id=None):
    e = {"line": BASE["models"][key]["lineage"]["line"], "from_id": from_id or ID(key),
         "id": new_id, "superseded": list(superseded), "effort_map": effort_map or {},
         "effort_ceiling": ceiling, "probe_summary_sha256": "0" * 64}
    return e


def base_record(key, base_sha=BASE_SHA):
    row = BASE["models"][key]
    return {"key": key, "family": row["family"], "capability_tier": row["capability_tier"],
            "effort_ceiling": row.get("effort_ceiling"), "effort_map": dict(row.get("effort_map", {})),
            "source": "base", "base_policy_sha256": base_sha}


def generation(entries=None, history=None, blocked=(), parent=None, base_sha=BASE_SHA):
    return {"overlay_schema_version": 1, "entries": entries or {}, "history": history or {},
            "blocked_ids": list(blocked), "parent_generation_sha256": parent,
            "base_policy_sha256": base_sha}


def one_step(key=KEY, **entry_kw):
    """A first-generation replacement of `key`, with its summary and the
    from_id history record — the shape model_sync publishes."""
    e = entry_for(key, successor(key), **entry_kw)
    s = summary_for(key, e)
    e["probe_summary_sha256"] = sha_of(s)
    gen = generation({key: e}, {ID(key): base_record(key)})
    return gen, {sha_of(s): s}


def merge(gen, summaries, *, apply_entries=True, base=BASE, **kw):
    return effective_config(base, gen, apply_entries=apply_entries,
                            summary=summaries.get, **kw)


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def test_state_root_precedence(tmp_path):
    home = tmp_path / "home"
    assert state_root_path({}, home) == home / ".local/state/deep-model-router"
    assert state_root_path({"XDG_STATE_HOME": str(tmp_path / "x")}, home) == \
        tmp_path / "x" / "deep-model-router"
    assert state_root_path({"XDG_STATE_HOME": str(tmp_path / "x"),
                            "DEEP_MODEL_ROUTER_STATE_DIR": str(tmp_path / "s")}, home) == tmp_path / "s"


# ---------------------------------------------------------------------------
# State shapes
# ---------------------------------------------------------------------------

def make_root(tmp_path) -> Path:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def publish(root_path: Path, gen: dict, summaries: dict) -> str:
    with StateRoot.open(root_path, create=True) as root:
        for s in summaries.values():
            write_summary(root, s)
        sha = write_generation(root, gen)
        write_pointer(root, sha)
    return sha


def test_absent_when_the_root_does_not_exist(tmp_path):
    assert read_state(tmp_path / "nope").shape == "absent"


def test_absent_when_only_work_exists(tmp_path):
    root = make_root(tmp_path)
    with StateRoot.open(root) as r:
        r.write_json_atomic("work/state.json", {"tick": 1})
    assert read_state(root).shape == "absent"


def test_absent_when_only_an_interrupted_bootstrap_remains(tmp_path):
    root = make_root(tmp_path)
    gen, sums = one_step()
    with StateRoot.open(root) as r:
        sha = write_generation(r, gen, prefix="committed.tmp-abc")
        write_pointer(r, sha, prefix="committed.tmp-abc")
    assert read_state(root).shape == "absent"


def test_unreadable_when_committed_has_no_pointer(tmp_path):
    root = make_root(tmp_path)
    with StateRoot.open(root) as r:
        r.mkdir("committed")
    assert read_state(root).shape == "unreadable"


def test_unreadable_when_the_pointer_names_no_generation(tmp_path):
    root = make_root(tmp_path)
    with StateRoot.open(root) as r:
        write_pointer(r, "a" * 64)
    assert read_state(root).shape == "unreadable"


def test_ok_and_deleting_work_state_changes_nothing(tmp_path):
    root = make_root(tmp_path)
    gen, sums = one_step()
    sha = publish(root, gen, sums)
    with StateRoot.open(root) as r:
        r.write_json_atomic("work/state.json", {"tick": 1})
    with read_state(root) as before:
        assert before.shape == "ok" and before.generation_sha256 == sha
    (root / "work" / "state.json").unlink()
    with read_state(root) as after:
        assert after.shape == "ok" and after.generation == before.generation


def test_generation_whose_content_hash_is_not_its_name_is_unreadable(tmp_path):
    root = make_root(tmp_path)
    gen, sums = one_step()
    sha = publish(root, gen, sums)
    path = root / "committed" / "generations" / f"{sha}.json"
    tampered = json.loads(path.read_text())
    tampered["blocked_ids"] = []
    tampered["entries"] = {}
    path.write_text(json.dumps(tampered))
    assert read_state(root).shape == "unreadable"


def test_generation_schema_violation_is_unreadable(tmp_path):
    root = make_root(tmp_path)
    gen, _ = one_step()
    gen["surprise"] = 1
    data = secure_io.canonical_json_bytes(gen)
    sha = hashlib.sha256(data).hexdigest()
    with StateRoot.open(root) as r:
        r.write_bytes_atomic(f"committed/generations/{sha}.json", data)
        write_pointer(r, sha)
    assert read_state(root).shape == "unreadable"


def test_unreadable_when_the_root_is_group_readable(tmp_path):
    root = make_root(tmp_path)
    publish(root, *one_step())
    os.chmod(root, 0o755)
    assert read_state(root).shape == "unreadable"


# ---------------------------------------------------------------------------
# Root-fd discipline (DD-A0)
# ---------------------------------------------------------------------------

def _pointer_path(root: Path) -> Path:
    return root / "committed" / "current.json"


def test_a_swapped_root_does_not_redirect_an_open_root(tmp_path):
    root = make_root(tmp_path)
    with StateRoot.open(root) as r:
        r.write_json_atomic("work/marker.json", {"which": "original"})
        root.rename(tmp_path / "moved")
        decoy = make_root(tmp_path)
        (decoy / "work").mkdir(mode=0o700)
        (decoy / "work" / "marker.json").write_text('{"which": "decoy"}')
        os.chmod(decoy / "work" / "marker.json", 0o600)
        assert r.read_json("work/marker.json") == {"which": "original"}
        r.write_json_atomic("work/written.json", {"x": 1})
    assert (tmp_path / "moved" / "work" / "written.json").exists()
    assert not (decoy / "work" / "written.json").exists()


def test_an_intermediate_directory_symlink_is_refused_for_read_and_write(tmp_path):
    root = make_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "current.json").write_text('{"generation_sha256": "' + "a" * 64 + '"}')
    os.chmod(outside / "current.json", 0o600)
    (root / "committed").symlink_to(outside)
    assert read_state(root).shape == "unreadable"
    with StateRoot.open(root) as r:
        with pytest.raises(StateError):
            r.write_json_atomic("committed/escape.json", {"x": 1})
    assert not (outside / "escape.json").exists()


@pytest.mark.parametrize("damage", ["symlink", "fifo", "mode_644", "two_links", "oversize",
                                    "duplicate_key"])
def test_a_pointer_that_fails_admission_is_unreadable(tmp_path, damage):
    root = make_root(tmp_path)
    publish(root, *one_step())
    ptr = _pointer_path(root)
    body = ptr.read_text()
    if damage == "symlink":
        target = tmp_path / "real.json"
        target.write_text(body)
        os.chmod(target, 0o600)
        ptr.unlink()
        ptr.symlink_to(target)
    elif damage == "fifo":
        ptr.unlink()
        os.mkfifo(ptr, 0o600)
    elif damage == "mode_644":
        os.chmod(ptr, 0o644)
    elif damage == "two_links":
        os.link(ptr, tmp_path / "second-link")
    elif damage == "oversize":
        ptr.write_text(body.rstrip() + " " * (256 * 1024) + "\n")
    elif damage == "duplicate_key":
        sha = json.loads(body)["generation_sha256"]
        ptr.write_text('{"generation_sha256": "%s", "generation_sha256": "%s"}' % (sha, sha))
    assert read_state(root).shape == "unreadable", damage


def test_a_file_owned_by_another_uid_is_unreadable(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    publish(root, *one_step())
    real = os.getuid()
    calls = {"n": 0}

    # The root and committed/ pass as ours; everything after that reads as
    # someone else's — the file-level uid comparison is what is under test.
    def uid():
        calls["n"] += 1
        return real if calls["n"] <= 2 else real + 1
    monkeypatch.setattr(secure_io, "_current_uid", uid)
    assert read_state(root).shape == "unreadable"


def test_a_directory_owned_by_another_uid_is_unreadable(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    publish(root, *one_step())
    monkeypatch.setattr(secure_io, "_current_uid", lambda: os.getuid() + 1)
    assert read_state(root).shape == "unreadable"


def test_written_files_are_0600_and_directories_0700(tmp_path):
    root = make_root(tmp_path)
    publish(root, *one_step())
    for p in (root / "committed").rglob("*"):
        want = 0o700 if p.is_dir() else 0o600
        assert stat.S_IMODE(p.stat().st_mode) == want, p


def test_the_publication_lock_is_exclusive(tmp_path):
    import threading
    root = make_root(tmp_path)
    order = []
    with StateRoot.open(root) as r:
        with r.lock():
            def other():
                with StateRoot.open(root) as r2, r2.lock():
                    order.append("second")
            t = threading.Thread(target=other)
            t.start()
            t.join(0.2)
            order.append("first")
        t.join(5)
    assert order == ["first", "second"]


# ---------------------------------------------------------------------------
# Merge table (DD-A2), in order
# ---------------------------------------------------------------------------

def reasons(prov):
    return {r["key"]: r["reason"] for r in prov.rejected}


def test_rule6_an_admitted_entry_replaces_the_id_and_voids_the_price():
    gen, sums = one_step(effort_map={"LOW": "low"}, ceiling="HIGH")
    cfg, prov = merge(gen, sums)
    row = cfg["models"][KEY]
    assert row["id"] == successor(KEY)
    assert row["effort_map"] == {"LOW": "low"} and row["effort_ceiling"] == "HIGH"
    assert row["price_per_mtok"] == {"unavailable": "local_overlay"}
    assert row["family"] == BASE["models"][KEY]["family"]
    assert row["capability_tier"] == BASE["models"][KEY]["capability_tier"]
    assert prov.applied == [KEY] and prov.status == "applied"
    policy = Policy.of(cfg)
    assert successor(KEY) in policy.model_ids
    assert BASE["models"][KEY]["id"] == ID(KEY), "the base config must not be mutated"


def test_rule1_no_base_row():
    gen, sums = one_step()
    gen["entries"]["no_such_key"] = gen["entries"].pop(KEY)
    assert reasons(merge(gen, sums)[1]) == {"no_such_key": "no_base_row"}


def test_rule1_line_mismatch():
    gen, sums = one_step()
    gen["entries"][KEY]["line"] = "demo/other"
    assert reasons(merge(gen, sums)[1]) == {KEY: "line_mismatch"}


def test_rule1_template_mismatch():
    gen, sums = one_step()
    gen["entries"][KEY]["id"] = "demo-not-this-lineage"
    assert reasons(merge(gen, sums)[1]) == {KEY: "template_mismatch"}


def test_rule1_generation_order():
    gen, sums = one_step()
    gen["entries"][KEY]["superseded"] = [successor(KEY, 3)]
    assert reasons(merge(gen, sums)[1]) == {KEY: "generation_order"}


def test_rule1_two_ids_of_one_generation_in_one_chain_are_refused():
    major = lineage.parse(BASE["models"][KEY]["lineage"]["template"], ID(KEY)).parts[0] + 1
    gen, sums = one_step()
    gen["entries"][KEY]["superseded"] = [spelled(KEY, f"{major}.0")]
    gen["entries"][KEY]["id"] = spelled(KEY, f"{major}-0")
    assert reasons(merge(gen, sums)[1]) == {KEY: "generation_order"}


def test_rule1_same_generation_as_a_history_id_is_refused():
    major = lineage.parse(BASE["models"][KEY]["lineage"]["template"], ID(KEY)).parts[0] + 1
    gen, sums = one_step()
    gen["entries"][KEY]["id"] = spelled(KEY, f"{major}-0")
    other = spelled(KEY, f"{major}.0")
    gen["history"][other] = {**base_record(KEY), "source": "probe",
                             "probe_summary_sha256": "1" * 64}
    del gen["history"][other]["base_policy_sha256"]
    assert {"key": KEY, "reason": "same_generation"} in merge(gen, sums)[1].rejected


def test_rule1_id_conflict_with_another_row():
    gen, sums = one_step()
    gen["entries"][KEY]["id"] = spelled(KEY, "99")
    gen["entries"]["openai_worker_fast"] = dict(gen["entries"][KEY],
                                                line=BASE["models"]["openai_worker_fast"]["lineage"]["line"])
    # The second entry cannot even spell the id in its own template, but the
    # first must still see the clash with another entry's id.
    assert {"key": KEY, "reason": "id_conflict"} in merge(gen, sums)[1].rejected


def test_rule2_a_promoted_entry_is_noop_before_any_summary_check():
    """Order matters: promotion changed the base row, so the entry's old
    summary no longer matches `base_row_sha256` — judged at rule 5 it would be
    `stale_probe`. Rule 2 decides first."""
    gen, sums = one_step()
    promoted = copy.deepcopy(BASE)
    promoted["models"][KEY]["id"] = successor(KEY)
    gen["history"] = {}
    cfg, prov = merge(gen, {}, base=promoted)          # no summaries at all
    assert prov.noop == [{"key": KEY, "reason": "promoted"}] and not prov.rejected
    assert cfg["models"][KEY]["id"] == successor(KEY)


def test_rule3_a_newer_base_wins():
    gen, sums = one_step()
    newer = copy.deepcopy(BASE)
    newer["models"][KEY]["id"] = successor(KEY, 2)
    _, prov = merge(gen, sums, base=newer)
    assert prov.noop == [{"key": KEY, "reason": "base_newer"}]


def test_rule4_from_id_mismatch():
    gen, sums = one_step()
    moved = copy.deepcopy(BASE)
    moved["models"][KEY]["id"] = spelled(KEY, "5.9")
    if lineage.compare(lineage.parse(moved["models"][KEY]["lineage"]["template"], spelled(KEY, "5.9")),
                       lineage.parse(moved["models"][KEY]["lineage"]["template"], successor(KEY))) >= 0:
        pytest.skip("registry already past the synthetic from_id")
    assert {"key": KEY, "reason": "from_id_mismatch"} in merge(gen, sums, base=moved)[1].rejected


def test_rule5_a_blocked_id_is_not_applied():
    gen, sums = one_step()
    gen["blocked_ids"] = [successor(KEY)]
    cfg, prov = merge(gen, sums)
    assert reasons(prov) == {KEY: "blocked"}
    assert cfg["models"][KEY]["id"] == ID(KEY)


def test_rule5_an_entry_without_its_summary_is_unprobed():
    gen, _ = one_step()
    assert reasons(merge(gen, {})[1]) == {KEY: "unprobed"}


@pytest.mark.parametrize("field,value", [
    ("key", "openai_frontier"), ("line", "demo/x"), ("from_id", "demo-x"), ("id", "demo-y"),
    ("superseded", ["demo-z"]), ("effort_map", {"LOW": "min"}), ("effort_ceiling", "LOW"),
    ("overlay_schema_version", 2),
])
def test_rule5_a_summary_that_disagrees_with_its_entry_is_rejected(field, value):
    gen, _ = one_step()
    e = gen["entries"][KEY]
    s = summary_for(KEY, e, **{field: value})
    e["probe_summary_sha256"] = sha_of(s)
    assert reasons(merge(gen, {sha_of(s): s})[1]) == {KEY: "summary_mismatch"}, field


@pytest.mark.parametrize("field", ["base_row_sha256", "recipe_sha256"])
def test_rule5_a_summary_measured_against_another_base_is_stale(field):
    gen, _ = one_step()
    e = gen["entries"][KEY]
    s = summary_for(KEY, e, **{field: "f" * 64})
    e["probe_summary_sha256"] = sha_of(s)
    assert reasons(merge(gen, {sha_of(s): s})[1]) == {KEY: "stale_probe"}


def test_rule5_changing_only_mechanism_reviewer_makes_an_xai_probe_stale():
    gen, sums = one_step(XAI)
    assert merge(gen, sums)[1].applied == [XAI]
    edited = copy.deepcopy(BASE)
    runtime = next(rt for rt, t in edited["transports"].items()
                   if "mechanism_reviewer" in (t.get("to_xai") or {}))
    edited["transports"][runtime]["to_xai"]["mechanism_reviewer"] += " --demo"
    gen2 = copy.deepcopy(gen)
    gen2["history"][ID(XAI)]["base_policy_sha256"] = canonical_policy_sha256(edited)
    assert reasons(merge(gen2, sums, base=edited)[1]) == {XAI: "stale_probe"}


def test_rule7_the_base_predecessor_stays_valid_history_input():
    gen, sums = one_step()
    cfg, prov = merge(gen, sums)
    hist_key = f"{KEY}@{ID(KEY)}"
    assert cfg["models"][hist_key]["id"] == ID(KEY)
    assert cfg["models"][hist_key]["dispatchable"] is False
    assert prov.history_ids_synthesized == 1
    policy = Policy.of(cfg)
    assert ID(KEY) in policy.model_ids
    out = route(Task(task_class="DEBUGGING", complexity=1, uncertainty=2, blast_radius=1,
                     reversibility=1, prior_failures=1, prior_models=[ID(KEY)]), cfg)
    assert out["selected_model"] != ID(KEY)


def test_rule7_a_history_row_keeps_its_own_snapshot_not_the_successors():
    gen, sums = one_step(ceiling="HIGH")
    cfg, _ = merge(gen, sums)
    policy = Policy.of(cfg)
    assert policy.ceiling_of[successor(KEY)] == "HIGH"
    assert policy.ceiling_of[ID(KEY)] == BASE["models"][KEY].get("effort_ceiling")


def test_rule7_a_probe_record_without_its_summary_is_refused():
    gen, sums = one_step()
    ghost = successor(KEY, 5)
    gen["history"][ghost] = {**{k: v for k, v in base_record(KEY).items()
                                if k != "base_policy_sha256"},
                             "source": "probe", "probe_summary_sha256": "2" * 64}
    cfg, prov = merge(gen, sums)
    assert {"key": KEY, "reason": "history_unprobed"} in prov.rejected
    assert ghost not in {m["id"] for m in cfg["models"].values()}


def test_rule7_a_base_record_from_another_base_is_refused():
    gen, sums = one_step()
    gen["history"][ID(KEY)]["base_policy_sha256"] = "e" * 64
    cfg, prov = merge(gen, sums)
    assert {"key": KEY, "reason": "history_base_changed"} in prov.rejected
    assert f"{KEY}@{ID(KEY)}" not in cfg["models"]


def test_rule7_a_reverted_overlay_id_is_still_history_input():
    """After revert the live row is back on its base id and the overlay id is
    NEWER than it — still valid history input, never a seat."""
    gen, sums = one_step()
    (s_sha, s), = sums.items()
    new_id = successor(KEY)
    reverted = generation({}, {
        ID(KEY): base_record(KEY),
        new_id: {**{k: v for k, v in base_record(KEY).items() if k != "base_policy_sha256"},
                 "source": "probe", "probe_summary_sha256": s_sha},
    }, blocked=[new_id])
    cfg, prov = merge(reverted, sums)
    assert cfg["models"][KEY]["id"] == ID(KEY)
    assert prov.history_ids_synthesized == 1
    assert new_id in Policy.of(cfg).model_ids
    out = route(Task(task_class="DEBUGGING", complexity=1, uncertainty=2, blast_radius=1,
                     reversibility=1, prior_failures=1, prior_models=[new_id]), cfg)
    assert out["terminal"] is None or out["terminal"] != "RETRY_HISTORY_REQUIRED"


def test_rule8_a_blocked_base_id_is_never_seated_but_stays_valid_input():
    task = dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
                reversibility=2)
    before = route(Task(**task), BASE)
    victim = before["selected_model"]
    cfg, prov = merge(generation(blocked=[victim]), {})
    assert cfg["local_state"] == {"blocked_ids": [victim]} and prov.blocked_ids == 1
    after = route(Task(**task), cfg)
    seated = {after["selected_model"], after["review"]["judge_model"],
              *after["review"]["reviewer_models"]}
    assert victim not in seated
    assert victim not in after["unavailable_models"], "a revocation is not caller input"
    # still a valid history input
    again = route(Task(**task, prior_failures=1, prior_models=[victim]), cfg)
    assert again["terminal"] != "RETRY_HISTORY_REQUIRED"


def test_overlay_off_skips_entries_but_keeps_history_and_revocations():
    gen, sums = one_step()
    gen["blocked_ids"] = [ID("claude_senior")]
    cfg, prov = merge(gen, sums, apply_entries=False)
    assert cfg["models"][KEY]["id"] == ID(KEY) and prov.applied == []
    assert cfg["local_state"]["blocked_ids"] == [ID("claude_senior")]
    # the from_id record's id is the live id again, so nothing to synthesize;
    # a reverted overlay id's record would still be synthesized:
    assert prov.history_ids_synthesized == 0


def test_no_state_and_an_all_noop_generation_keep_the_base_digest():
    cfg, prov = effective_config(BASE, None, apply_entries=True, summary=lambda s: None)
    assert cfg is BASE and prov.status == "noop"
    gen, sums = one_step()
    promoted = copy.deepcopy(BASE)
    promoted["models"][KEY]["id"] = successor(KEY)
    gen["history"] = {}
    cfg2, prov2 = merge(gen, sums, base=promoted)
    assert cfg2 is promoted
    assert canonical_policy_sha256(cfg2) == canonical_policy_sha256(promoted)


def test_provenance_names_keys_and_counts_never_ids():
    gen, sums = one_step()
    gen["blocked_ids"] = [ID("claude_senior")]
    _, prov = merge(gen, sums)
    text = json.dumps(prov.to_json())
    for mid in {m["id"] for m in BASE["models"].values()} | {successor(KEY)}:
        assert mid not in text


def test_a_local_state_block_must_be_well_formed():
    from route_task import ConfigError
    bad = dict(BASE, local_state={"blocked_ids": "x"})
    with pytest.raises(ConfigError, match="local_state"):
        Policy(bad)
