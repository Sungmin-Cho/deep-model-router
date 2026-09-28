"""T19 — the 1.12.1 snapshot is a hermetic oracle, not a copy that reads the
current repo through a side door (design §4 T3 / T19)."""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
from _baseline import (  # noqa: E402
    BASELINE_POLICY_SHA, BASELINE_VERSION, SNAPSHOT_DIR, baseline_cfg, load_baseline,
)
from _baseline import (  # noqa: E402
    BASELINE_1161_POLICY_SHA, BASELINE_1161_VERSION, SNAPSHOT_1161_DIR, SNAPSHOT_1161_PREFIX,
    baseline_1161_cfg, load_baseline_1161,
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


# Every model id each live key has held, oldest first: key -> [..., current].
# Read from `fixtures/id-succession.json` — a literal record written in the
# same change that bumps the registry, never derived from it, so the move is
# stated somewhere a reader can diff and a swap cannot pass by asserting the
# registry against itself. A chain may reach back past 1.12.1
# (`claude_architect` moved before the snapshot was cut); the snapshot's id
# must be ON the chain and the live id must be its LAST element.
SUCCESSION = json.loads((HERE / "fixtures" / "id-succession.json").read_text())
ID_SUCCESSION = {key: tuple(chain) for key, chain in SUCCESSION["chains"].items()}
KEY_RENAMES = SUCCESSION["key_renames"]


def moved_since_baseline() -> dict:
    """key -> (1.12.1 id, current id) for every chain whose id moved after the
    snapshot was cut. What the quality-disclosure obligation keys off."""
    old = baseline_cfg(load_baseline())["models"]
    return {key: (old[key]["id"], chain[-1]) for key, chain in ID_SUCCESSION.items()
            if key in old and old[key]["id"] != chain[-1]}


def test_every_superseded_id_is_kept_as_a_history_row():
    """adapters.md: "bump the id, re-probe, and keep the retired id as a
    non-dispatchable history row." Without it a control loop holding a
    pre-bump failure gets exit 2 — invalid input — for a model it really did
    dispatch. The succession chains are where that obligation is checkable."""
    import route_task as live
    models = live.load_config()["models"]
    history = {m["id"]: (key, m) for key, m in models.items() if "history_of" in m}
    for key, chain in ID_SUCCESSION.items():
        assert len(chain) == len(set(chain)) >= 2, (key, chain)
        assert models[key]["id"] == chain[-1], (key, "the chain must end at the live id")
        for was in chain[:-1]:
            assert was in history, (key, was, "superseded id has no history row")
            hkey, row = history[was]
            assert hkey == f"{key}@{was}", hkey
            assert row["history_of"] == key, hkey
            assert row["dispatchable"] is False, hkey
            assert row["family"] == models[key]["family"], hkey


def test_key_renames_point_retired_keys_at_live_history_rows():
    """A rename names a pre-DD-A3 `_retired` key (1.12.1 had one of the two;
    1.15.0 added the other) and the history row that replaced it."""
    import route_task as live
    old = baseline_cfg(load_baseline())["models"]
    new = live.load_config()["models"]
    assert any(was in old for was in KEY_RENAMES), "no rename reaches the snapshot"
    for was, now in KEY_RENAMES.items():
        assert was not in new, was
        assert now in new and new[now]["history_of"] == now.split("@", 1)[0], now
        assert now == f"{new[now]['history_of']}@{new[now]['id']}", now
        if was in old:
            assert old[was]["id"] == new[now]["id"], (was, now)


def test_the_floor_tables_did_not_move():
    """T21 [P2-opus-missing-1]: the plan's strongest constraint — the risk-band
    worker table, review policy and effort table remain what 1.12.1 shipped.
    Model generations and their binding/fallback inventory evolve separately."""
    import route_task as live
    mod = load_baseline()
    old, new = baseline_cfg(mod), live.load_config()
    for key in ("worker_selection", "effort_by_work",
                "role_tiers", "effort_map", "worker_balanced_selection"):
        assert new[key] == old[key], key
    # `review` is the table Part B changes on purpose (U-1). It is held to the
    # 1.16.1 snapshot plus exactly the edits each rule declares below
    # (plan B4, design DD-B11) — an edit no rule names fails here.
    assert review_as_declared() == new["review"]
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
    #
    # `lineage` and `history_of` are exempt as ADDITIONS (design 2026-09-25
    # DD-A1, DD-A3): 1.12.1 had neither, and the router routes on `id` and
    # `dispatchable`, never on the template or the pointer. A history row moved
    # key (`claude_architect_retired` -> `claude_architect@<id>`); the fixture's
    # rename table is how the old key is found, so a rename it does not state
    # fails here as a missing key.
    exempt = ("price_per_mtok", "id", "lineage", "history_of")
    for key, historical in old["models"].items():
        now = KEY_RENAMES.get(key, key)
        assert now in new["models"], (key, now)
        assert {k: v for k, v in new["models"][now].items()
                if k not in exempt} == {
                    k: v for k, v in historical.items()
                    if k not in exempt
                }, key
        chain = ID_SUCCESSION.get(key)
        if chain is None:
            assert new["models"][now]["id"] == historical["id"], key
            continue
        # On the chain, and the live id is the chain's end: reverting a key to
        # an earlier id, or pointing it at one the chain never names, fails.
        assert historical["id"] in chain, (key, historical["id"], chain)
        assert new["models"][now]["id"] == chain[-1], (key, chain)


# --- the 1.16.1 oracle (Part B, plan B0 Step 1) -------------------------------


def _insert_after(items, anchor, item):
    out = list(items)
    out.insert(out.index(anchor) + 1, item)
    return out


# Each Part B rule's edit to the `review` table, as (rule, edit(review) -> None).
REVIEW_EDITS = [
    # C4 (DD-B5): the MEDIUM reviewer fits the floor; the preference table goes
    # and the binding-only alt seat becomes a candidate.
    ("c4", lambda rv: rv["MEDIUM"].pop("preferred_by_implementer")),
    ("c4", lambda rv: rv["MEDIUM"].__setitem__("candidates", _insert_after(
        rv["MEDIUM"]["candidates"], "worker_balanced", "worker_balanced_alt"))),
    # C3 (DD-B4): LOW review is the deterministic checks — no model seat, so
    # no effort — and nothing else about the band moves.
    ("c3", lambda rv: rv.__setitem__("LOW", {"reviewers": [], "effort": None,
                                             "independent": False,
                                             "required_checks": ["tests", "lint"]})),
    # REVIEW lead (DD-B7): the lead is reviewer-1 of every REVIEW task.
    ("review_lead", lambda rv: rv.__setitem__("review_class_lead_counts", True)),
]


def review_as_declared() -> dict:
    import copy
    review = copy.deepcopy(baseline_1161_cfg()["review"])
    for _rule, edit in REVIEW_EDITS:
        edit(review)
    return review


_SNAPSHOT_1161_MODULES = ("route_task", "policy_digest", "strict_json", "lineage",
                          "model_state", "secure_io")


def test_1161_manifest_matches_every_snapshot_file():
    lines = [l for l in (SNAPSHOT_1161_DIR / "MANIFEST.sha256").read_text().splitlines()
             if l and not l.startswith("#")]
    listed = {line.split()[1] for line in lines}
    assert listed == {f"scripts/{SNAPSHOT_1161_PREFIX}{m}.py" for m in _SNAPSHOT_1161_MODULES} \
        | {"config/model-routing.yaml"}
    for line in lines:
        digest, rel = line.split()
        assert hashlib.sha256((SNAPSHOT_1161_DIR / rel).read_bytes()).hexdigest() == digest, rel
    on_disk = {str(p.relative_to(SNAPSHOT_1161_DIR)) for p in SNAPSHOT_1161_DIR.rglob("*")
               if p.is_file() and p.name != "MANIFEST.sha256" and "__pycache__" not in p.parts}
    assert on_disk == listed, on_disk ^ listed
    assert not (SNAPSHOT_1161_DIR / ".claude-plugin").exists()


def test_1161_snapshot_imports_no_live_module():
    """Every sibling import is rewritten to a `baseline_1_16_1_*` name, so a
    live module already in `sys.modules` cannot bind in its place."""
    scripts = SNAPSHOT_1161_DIR / "scripts"
    for mod in _SNAPSHOT_1161_MODULES:
        text = (scripts / f"{SNAPSHOT_1161_PREFIX}{mod}.py").read_text()
        for other in _SNAPSHOT_1161_MODULES:
            assert f"\nimport {other}\n" not in text, (mod, other)
            assert f"\nfrom {other} import" not in text, (mod, other)
    load_baseline_1161()
    loaded = {n: m for n, m in sys.modules.items() if n.startswith(SNAPSHOT_1161_PREFIX)}
    assert set(loaded) == {f"{SNAPSHOT_1161_PREFIX}{m}" for m in _SNAPSHOT_1161_MODULES}
    for name, module in loaded.items():
        assert Path(module.__file__).resolve().parent == scripts.resolve(), name


def test_1161_snapshot_reads_only_its_own_config_and_seeded_version():
    mod = load_baseline_1161()
    assert Path(mod.CONFIG_PATH).resolve() == (SNAPSHOT_1161_DIR / "config" / "model-routing.yaml").resolve()
    assert mod.plugin_manifest_version() == BASELINE_1161_VERSION
    assert Path(mod.policy_sha256.__code__.co_filename).resolve() \
        == (SNAPSHOT_1161_DIR / "scripts" / f"{SNAPSHOT_1161_PREFIX}policy_digest.py").resolve()
    for attr in ("lineage", "model_state"):
        assert Path(getattr(mod, attr).__file__).resolve().parent \
            == (SNAPSHOT_1161_DIR / "scripts").resolve(), attr


def test_1161_snapshot_route_carries_its_release_sentinels():
    mod = load_baseline_1161()
    out = mod.route(mod.Task(task_class="MECHANICAL", complexity=0, uncertainty=0,
                             blast_radius=0, reversibility=0), baseline_1161_cfg())
    assert out["router_plugin_version"] == BASELINE_1161_VERSION
    assert out["policy_sha256"] == BASELINE_1161_POLICY_SHA
    assert "execution_band" in out and out["model_overlay"] is None


def test_1161_snapshot_policy_cache_is_separate_from_the_live_one():
    import route_task as live
    mod = load_baseline_1161()
    assert mod.Policy is not live.Policy
    assert mod.Policy._cache is not live.Policy._cache
    assert mod.model_state is not sys.modules.get("model_state")
