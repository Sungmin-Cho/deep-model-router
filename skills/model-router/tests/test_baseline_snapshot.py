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
    # Preserve every historical model field except the two that are supposed to
    # move on their own evidence: independently refreshed billing quotes, and the
    # provider `id` when a family ships a new generation. Everything the ROUTER
    # reads off a model — family, capability_tier, effort_ceiling, effort_map,
    # context_window, dispatchable, verified — stays pinned here, so a generation
    # refresh cannot quietly re-tier a seat or lift its ceiling. The id itself is
    # guarded elsewhere and harder: `test_every_verified_model_id_is_named_
    # verbatim_in_a_verified_ledger_row` refuses any verified id that no verified
    # ledger row names, so a silent swap fails there rather than passing here.
    MOVES_ON_ITS_OWN_EVIDENCE = ("price_per_mtok", "id")
    for key, historical in old["models"].items():
        assert {k: v for k, v in new["models"][key].items()
                if k not in MOVES_ON_ITS_OWN_EVIDENCE} == {
                    k: v for k, v in historical.items()
                    if k not in MOVES_ON_ITS_OWN_EVIDENCE
                }, key
