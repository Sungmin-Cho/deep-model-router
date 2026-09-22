"""T19 — the 1.12.1 snapshot is a hermetic oracle, not a copy that reads the
current repo through a side door (design §4 T3 / T19)."""
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
from _baseline import (  # noqa: E402
    BASELINE_POLICY_SHA, BASELINE_VERSION, SNAPSHOT_DIR, baseline_cfg, load_baseline,
)


def test_manifest_matches_every_snapshot_file():
    lines = [l for l in (SNAPSHOT_DIR / "MANIFEST.sha256").read_text().splitlines()
             if l and not l.startswith("#")]
    listed = {line.split()[1] for line in lines}
    assert listed == {"scripts/route_task.py", "scripts/policy_digest.py", "config/model-routing.yaml"}
    for line in lines:
        digest, rel = line.split()
        assert hashlib.sha256((SNAPSHOT_DIR / rel).read_bytes()).hexdigest() == digest, rel
    # Every importable source in the snapshot is manifested; nothing else ships.
    on_disk = {str(p.relative_to(SNAPSHOT_DIR)) for p in SNAPSHOT_DIR.rglob("*")
               if p.is_file() and p.name != "MANIFEST.sha256" and "__pycache__" not in p.parts}
    assert on_disk == listed, on_disk ^ listed
    assert not (SNAPSHOT_DIR / ".claude-plugin").exists()      # no nested plugin manifest


def test_snapshot_reads_only_its_own_config_and_seeded_version():
    mod = load_baseline()
    assert Path(mod.CONFIG_PATH).resolve() == (SNAPSHOT_DIR / "config" / "model-routing.yaml").resolve()
    assert mod.plugin_manifest_version() == BASELINE_VERSION     # seeded cache, not a manifest walk
    # Its digest helper is the vendored one, not the live module.
    assert Path(mod.policy_sha256.__code__.co_filename).resolve() \
        == (SNAPSHOT_DIR / "scripts" / "policy_digest.py").resolve()


def test_snapshot_route_carries_the_1_12_1_sentinels():
    mod = load_baseline()
    cfg = baseline_cfg(mod)
    out = mod.route(mod.Task(task_class="MECHANICAL", complexity=0, uncertainty=0,
                             blast_radius=0, reversibility=0), cfg)
    assert out["router_plugin_version"] == BASELINE_VERSION
    assert out["policy_sha256"] == BASELINE_POLICY_SHA
    assert "execution_band" not in out          # the oracle predates the axis


def test_snapshot_policy_cache_is_separate_from_the_live_one():
    import route_task as live
    mod = load_baseline()
    assert mod.Policy is not live.Policy
    assert mod.Policy._cache is not live.Policy._cache


# Every model id that has moved since the 1.12.1 baseline: key -> (was, is).
# One line per generation bump, added in the same change that bumps the
# registry, so the move is stated somewhere a reader can diff.
ID_SUCCESSION = {
    "xai_frontier": ("grok-4.6", "grok-4.7"),      # 2026-09-22, xAI default moved
}


def test_every_superseded_id_is_kept_as_a_retired_row():
    """adapters.md: "bump the id, re-probe, and keep the retired id as a
    non-dispatchable history row." Without it a control loop holding a
    pre-bump failure gets exit 2 — invalid input — for a model it really did
    dispatch. The succession table is where that obligation is checkable."""
    import route_task as live
    models = live.load_config()["models"]
    retired = {m["id"]: (key, m) for key, m in models.items()
               if key.endswith("_retired")}
    for key, (was, _is) in ID_SUCCESSION.items():
        assert was in retired, (key, was, "superseded id has no retired row")
        assert retired[was][1]["dispatchable"] is False, retired[was][0]
        assert retired[was][1]["family"] == models[key]["family"], retired[was][0]


def test_the_floor_tables_did_not_move():
    """T21 [P2-opus-missing-1]: the plan's strongest constraint — the risk-band
    worker table, review policy and effort table remain what 1.12.1 shipped.
    Model generations and their binding/fallback inventory evolve separately."""
    import route_task as live
    mod = load_baseline()
    old, new = baseline_cfg(mod), live.load_config()
    for key in ("worker_selection", "review", "effort_by_work",
                "role_tiers", "effort_map", "worker_balanced_selection"):
        assert new[key] == old[key], key
    # Preserve every historical model field except independently refreshed
    # billing quotes, and the provider `id` ONLY where this file names the
    # move. Everything the router reads off a model — family, capability_tier,
    # effort_ceiling, effort_map, context_window, dispatchable, verified —
    # stays pinned, so a generation refresh cannot quietly re-tier a seat or
    # lift its ceiling.
    #
    # A blanket `id` exemption was the first attempt and it was wrong. The
    # ledger rule it leaned on ("a verified id must be named verbatim in a
    # verified row") is satisfied by ANY verified row, and a predecessor is
    # named in its own: reverting `xai_frontier` to grok-4.6, or pointing it
    # at grok-4.5, would have passed both guards. Naming each move closes
    # that — an id that moves anywhere this table does not say fails here.
    for key, historical in old["models"].items():
        assert {k: v for k, v in new["models"][key].items()
                if k not in ("price_per_mtok", "id")} == {
                    k: v for k, v in historical.items()
                    if k not in ("price_per_mtok", "id")
                }, key
        move = ID_SUCCESSION.get(key)
        if move is None:
            assert new["models"][key]["id"] == historical["id"], key
            continue
        assert (historical["id"], new["models"][key]["id"]) == move, (key, move)
