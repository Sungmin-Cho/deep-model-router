"""Local model state: where it lives, what a generation is, and how one merges
onto the bundled policy (design 2026-09-25 DD-A2).

The state root is ``$DEEP_MODEL_ROUTER_STATE_DIR``, else
``$XDG_STATE_HOME/deep-model-router``, else ``~/.local/state/deep-model-router``.
The router reads ``committed/`` only::

    committed/current.json                 {"generation_sha256": "<hex64>"}
    committed/generations/<sha256>.json    one generation, content-addressed
    committed/summaries/<sha256>.json      one probe summary, content-addressed
    work/...                               model_sync's own; never read here

Three shapes (`read_state`):

* ``absent``      no ``committed/`` (no root, only ``work/``, or only an
                  interrupted ``committed.tmp-*`` bootstrap) — route on the base
* ``ok``          pointer and generation admitted, content hash = file name
* ``unreadable``  the root itself fails admission (``root_unadmitted``: mode,
                  owner, type, a symlink — whether or not ``committed/``
                  exists), or ``committed/`` exists and anything on its read
                  path fails admission, decoding, hashing or the schema — fail
                  closed

`effective_config` is pure: base config + one generation -> the config the
router routes on, plus a provenance record that names registry KEYS and
counts, never model ids.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import lineage
from policy_digest import canonical_policy_sha256
from secure_io import StateError, StateRoot, canonical_json_bytes

OVERLAY_SCHEMA_VERSION = 1
MAX_PIN_CHAIN = 256
COMMITTED = "committed"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

GENERATION_KEYS = frozenset({
    "overlay_schema_version", "entries", "history", "blocked_ids",
    "parent_generation_sha256", "base_policy_sha256",
})
ENTRY_KEYS = frozenset({
    "line", "from_id", "id", "superseded", "effort_map", "effort_ceiling",
    "probe_summary_sha256",
})
HISTORY_COMMON_KEYS = frozenset({
    "key", "family", "capability_tier", "effort_ceiling", "effort_map", "source",
})
# Fields a summary must hold, and agree with its entry on, for the entry to apply.
SUMMARY_MATCH_KEYS = ("key", "line", "from_id", "id", "superseded", "effort_map",
                      "effort_ceiling", "overlay_schema_version")
PRICE_UNAVAILABLE = {"unavailable": "local_overlay"}


def is_hex64(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.match(value) is not None


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------

def state_root_path(env: Mapping[str, str], home: Path) -> Path:
    """DEEP_MODEL_ROUTER_STATE_DIR > XDG_STATE_HOME > ~/.local/state."""
    explicit = env.get("DEEP_MODEL_ROUTER_STATE_DIR")
    if explicit:
        return Path(explicit)
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path(home) / ".local" / "state"
    return base / "deep-model-router"


def overlay_off(env: Mapping[str, str]) -> bool:
    return env.get("DEEP_MODEL_ROUTER_OVERLAY") == "off"


# --------------------------------------------------------------------------
# Digests shared with model_sync (the summary records the same two values)
# --------------------------------------------------------------------------

def base_row_sha256(base: dict, key: str) -> str:
    return canonical_policy_sha256({"row": base["models"][key]})


def recipe_sha256(base: dict, family: str) -> str:
    """Every `mechanism*` string of every transport entry that targets
    `family`, as sorted ((runtime, entry, key), value) pairs. A direction with
    no plain `mechanism` key (to_xai) still contributes its reviewer and
    maker recipes, so changing either one makes an old probe stale."""
    pairs = []
    for runtime, entries in (base.get("transports") or {}).items():
        entry = (entries or {}).get(f"to_{family}")
        if not isinstance(entry, Mapping):
            continue
        for k, v in entry.items():
            if k.startswith("mechanism"):
                pairs.append([runtime, f"to_{family}", k, v])
    pairs.sort(key=lambda p: (p[0], p[1], p[2]))
    return canonical_policy_sha256({"recipes": pairs})


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

def _str(v) -> bool:
    return isinstance(v, str) and bool(v)


def _effort_map_ok(v) -> bool:
    return isinstance(v, dict) and all(_str(k) and _str(x) for k, x in v.items())


def validate_generation(gen: Any) -> None:
    """Structural schema of one generation. Raises StateError: a generation
    that does not parse as the schema is unreadable, never partly applied."""
    def bad(msg):
        raise StateError(f"generation schema: {msg}")
    if not isinstance(gen, dict) or set(gen) != GENERATION_KEYS:
        bad(f"top-level keys must be exactly {sorted(GENERATION_KEYS)}")
    if gen["overlay_schema_version"] != OVERLAY_SCHEMA_VERSION or isinstance(
            gen["overlay_schema_version"], bool):
        bad("unsupported overlay_schema_version")
    if not is_hex64(gen["base_policy_sha256"]):
        bad("base_policy_sha256 must be hex64")
    parent = gen["parent_generation_sha256"]
    if parent is not None and not is_hex64(parent):
        bad("parent_generation_sha256 must be hex64 or null")
    blocked = gen["blocked_ids"]
    if not isinstance(blocked, list) or not all(_str(b) for b in blocked) \
            or len(set(blocked)) != len(blocked):
        bad("blocked_ids must be a list of distinct ids")
    entries = gen["entries"]
    if not isinstance(entries, dict):
        bad("entries must be an object keyed by registry key")
    for key, e in entries.items():
        if not isinstance(e, dict) or set(e) != ENTRY_KEYS:
            bad(f"entry {key!r} keys must be exactly {sorted(ENTRY_KEYS)}")
        if not all(_str(e[k]) for k in ("line", "from_id", "id")):
            bad(f"entry {key!r} line/from_id/id must be non-empty strings")
        sup = e["superseded"]
        if not isinstance(sup, list) or not all(_str(s) for s in sup) or len(set(sup)) != len(sup):
            bad(f"entry {key!r} superseded must be a list of distinct ids")
        if not _effort_map_ok(e["effort_map"]):
            bad(f"entry {key!r} effort_map must map strings to strings")
        if e["effort_ceiling"] is not None and not _str(e["effort_ceiling"]):
            bad(f"entry {key!r} effort_ceiling must be a string or null")
        if not is_hex64(e["probe_summary_sha256"]):
            bad(f"entry {key!r} probe_summary_sha256 must be hex64")
    history = gen["history"]
    if not isinstance(history, dict):
        bad("history must be an object keyed by model id")
    for mid, rec in history.items():
        if not _str(mid) or not isinstance(rec, dict):
            bad("history records must be objects keyed by model id")
        source = rec.get("source")
        digest_key = {"base": "base_policy_sha256", "probe": "probe_summary_sha256"}.get(source)
        if digest_key is None:
            bad(f"history {mid!r} source must be base or probe")
        if set(rec) != HISTORY_COMMON_KEYS | {digest_key}:
            bad(f"history {mid!r} keys must be exactly "
                f"{sorted(HISTORY_COMMON_KEYS | {digest_key})}")
        if not is_hex64(rec[digest_key]):
            bad(f"history {mid!r} {digest_key} must be hex64")
        if not (_str(rec["key"]) and _str(rec["family"])):
            bad(f"history {mid!r} key/family must be non-empty strings")
        tier = rec["capability_tier"]
        if not isinstance(tier, int) or isinstance(tier, bool):
            bad(f"history {mid!r} capability_tier must be an integer")
        if rec["effort_ceiling"] is not None and not _str(rec["effort_ceiling"]):
            bad(f"history {mid!r} effort_ceiling must be a string or null")
        if not _effort_map_ok(rec["effort_map"]):
            bad(f"history {mid!r} effort_map must map strings to strings")


# --------------------------------------------------------------------------
# Reading committed state
# --------------------------------------------------------------------------

def _load_addressed(root: StateRoot, relpath: str, sha: str) -> Any:
    value, got = root.read_json_with_sha(relpath)
    if got != sha:
        raise StateError(f"{relpath}: content hash {got[:12]}… does not match its name")
    return value


class StateRead:
    """One read of the committed state. Holds the verified root fd open so
    the summary and ancestor reads it serves go through the same directory."""

    def __init__(self, shape: str, *, root: StateRoot | None = None,
                 generation: dict | None = None, generation_sha256: str | None = None,
                 detail: str | None = None, prefix: str = COMMITTED,
                 root_unadmitted: bool = False):
        self.shape = shape
        self.generation = generation
        self.generation_sha256 = generation_sha256
        self.detail = detail
        # The root itself failed admission (mode, owner, type, symlink): no
        # file under it was read, and `detail` names no model id.
        self.root_unadmitted = root_unadmitted
        self._root = root
        self._prefix = prefix

    def summary(self, sha: str) -> dict | None:
        """The committed probe summary with this content hash, or None when it
        is missing or fails admission — the entry naming it is then rejected,
        not the whole state."""
        if self._root is None or not is_hex64(sha):
            return None
        try:
            value = _load_addressed(self._root, f"{self._prefix}/summaries/{sha}.json", sha)
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def generation_at(self, sha: str) -> dict:
        """An ancestor generation, under the same admission, hash and schema
        rules as the current one. Raises StateError/OSError."""
        if self._root is None:
            raise StateError("no committed state")
        return load_generation(self._root, sha, prefix=self._prefix)

    def close(self) -> None:
        if self._root is not None:
            self._root.close()
            self._root = None

    def __enter__(self) -> "StateRead":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def load_generation(root: StateRoot, sha: str, *, prefix: str = COMMITTED) -> dict:
    if not is_hex64(sha):
        raise StateError("generation name is not hex64")
    gen = _load_addressed(root, f"{prefix}/generations/{sha}.json", sha)
    validate_generation(gen)
    return gen


def read_state(path: Path) -> StateRead:
    """Classify and read the committed state at `path` (see module doc)."""
    try:
        root = StateRoot.open(path)
    except FileNotFoundError:
        return StateRead("absent")
    except (OSError, ValueError) as exc:
        return StateRead("unreadable", detail=str(exc), root_unadmitted=True)
    try:
        if not root.lexists(COMMITTED):
            root.close()
            return StateRead("absent")
        pointer = root.read_json(f"{COMMITTED}/current.json")
        if not isinstance(pointer, dict) or set(pointer) != {"generation_sha256"} \
                or not is_hex64(pointer["generation_sha256"]):
            raise StateError("current.json must be exactly {generation_sha256: <hex64>}")
        sha = pointer["generation_sha256"]
        gen = load_generation(root, sha)
    except (OSError, ValueError) as exc:
        root.close()
        return StateRead("unreadable", detail=str(exc))
    return StateRead("ok", root=root, generation=gen, generation_sha256=sha)


def empty_generation(base_sha: str) -> dict:
    """No entries, no history, no revocations: what precedes the first
    generation. The pin walk merges it under the current revocations."""
    return {"overlay_schema_version": OVERLAY_SCHEMA_VERSION, "entries": {},
            "history": {}, "blocked_ids": [], "parent_generation_sha256": None,
            "base_policy_sha256": base_sha}


def root_admission(path: Path) -> dict:
    """Whether the state root passes admission, for `status`, `repair` and
    the terminal note: `{path, admitted (None when absent), detail, fix}`.
    The detail names the path and the failed check, never a model id."""
    try:
        StateRoot.open(Path(path)).close()
    except FileNotFoundError:
        return {"path": str(path), "admitted": None, "detail": "absent", "fix": None}
    except (OSError, ValueError) as exc:
        return {"path": str(path), "admitted": False, "detail": str(exc),
                "fix": root_fix(path)}
    return {"path": str(path), "admitted": True, "detail": None, "fix": None}


def root_fix(path: Path) -> str:
    return (f"chmod 700 {path} (the state root must be a real directory owned by "
            f"you with mode 0700), or point DEEP_MODEL_ROUTER_STATE_DIR elsewhere")


# --------------------------------------------------------------------------
# Writing (model_sync and tests; the router never writes)
# --------------------------------------------------------------------------

def write_addressed(root: StateRoot, directory: str, obj: Any) -> str:
    data = canonical_json_bytes(obj)
    sha = hashlib.sha256(data).hexdigest()
    root.write_bytes_atomic(f"{directory}/{sha}.json", data)
    return sha


def write_generation(root: StateRoot, gen: dict, *, prefix: str = COMMITTED) -> str:
    validate_generation(gen)
    return write_addressed(root, f"{prefix}/generations", gen)


def write_summary(root: StateRoot, summary: dict, *, prefix: str = COMMITTED) -> str:
    return write_addressed(root, f"{prefix}/summaries", summary)


def write_pointer(root: StateRoot, sha: str, *, prefix: str = COMMITTED) -> None:
    """The single linearisation point of a publication: one renameat."""
    if not is_hex64(sha):
        raise StateError("pointer target is not hex64")
    root.write_json_atomic(f"{prefix}/current.json", {"generation_sha256": sha})


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

@dataclass
class Provenance:
    status: str
    base_policy_sha256: str
    generation_sha256: str | None = None
    applied: list[str] = field(default_factory=list)
    noop: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    history_ids_synthesized: int = 0
    blocked_ids: int = 0
    state_reason: str | None = None
    # For the terminal note only (never in `to_json`): an id-free detail.
    state_detail: str | None = None

    def to_json(self) -> dict:
        """The route's `model_overlay`. Keys and counts only — never an id."""
        return {
            "status": self.status,
            "base_policy_sha256": self.base_policy_sha256,
            "generation_sha256": self.generation_sha256,
            "applied": list(self.applied),
            "noop": [dict(n) for n in self.noop],
            "rejected": [dict(r) for r in self.rejected],
            "history_ids_synthesized": self.history_ids_synthesized,
            "blocked_ids": self.blocked_ids,
            "state_reason": self.state_reason,
        }


def _gen(template: str, mid: str):
    try:
        return lineage.parse(template, mid)
    except ValueError:
        return None


def _entry_structural_reason(key: str, e: dict, base: dict, gen: dict) -> str | None:
    """Rule 1 — structure. None when the entry is well-formed against base."""
    row = base["models"].get(key)
    lin = row.get("lineage") if isinstance(row, Mapping) else None
    if not isinstance(lin, Mapping) or "history_of" in row:
        return "no_base_row"
    if lin.get("line") != e["line"]:
        return "line_mismatch"
    template = lin["template"]
    chain = [e["from_id"], *e["superseded"], e["id"]]
    parsed = [_gen(template, mid) for mid in chain]
    if any(p is None for p in parsed):
        return "template_mismatch"
    if any(lineage.compare(a, b) >= 0 for a, b in zip(parsed, parsed[1:])):
        return "generation_order"
    others = {m["id"] for k, m in base["models"].items() if k != key}
    others |= {x["id"] for k, x in gen["entries"].items() if k != key}
    others |= set(gen["history"])
    if e["id"] in others:
        return "id_conflict"
    same_gen = [mid for mid, rec in gen["history"].items()
                if rec["key"] == key and (g := _gen(template, mid)) is not None
                and lineage.compare(g, parsed[-1]) == 0]
    if same_gen:
        return "same_generation"
    return None


def _summary_reason(key: str, e: dict, base: dict,
                    summary: Callable[[str], dict | None]) -> str | None:
    """Rule 5 — the entry's own probe, still valid against today's base."""
    s = summary(e["probe_summary_sha256"])
    if s is None:
        return "unprobed"
    expected = {**e, "key": key, "overlay_schema_version": OVERLAY_SCHEMA_VERSION}
    for k in SUMMARY_MATCH_KEYS:
        if k not in s or s[k] != expected[k]:
            return "summary_mismatch"
    family = base["models"][key]["family"]
    if s.get("base_row_sha256") != base_row_sha256(base, key) \
            or s.get("recipe_sha256") != recipe_sha256(base, family):
        return "stale_probe"
    return None


def _history_reason(mid: str, rec: dict, base: dict, base_sha: str,
                    summary: Callable[[str], dict | None]) -> str | None:
    row = base["models"].get(rec["key"])
    lin = row.get("lineage") if isinstance(row, Mapping) else None
    if not isinstance(lin, Mapping) or "history_of" in row:
        return "history_no_base_row"
    if row["family"] != rec["family"]:
        return "history_family_mismatch"
    if _gen(lin["template"], mid) is None:
        return "history_template_mismatch"
    if rec["source"] == "base":
        # Another base cannot vouch for the snapshot — unless the current base
        # row still holds this very id, which it then re-snapshots.
        if rec["base_policy_sha256"] != base_sha and row["id"] != mid:
            return "history_base_changed"
    else:
        s = summary(rec["probe_summary_sha256"])
        if s is None or s.get("id") != mid or s.get("key") != rec["key"]:
            return "history_unprobed"
    return None


def _base_snapshot(base: dict, key: str, base_sha: str) -> dict:
    row = base["models"][key]
    return {"key": key, "family": row["family"], "capability_tier": row["capability_tier"],
            "effort_ceiling": row.get("effort_ceiling"),
            "effort_map": dict(row.get("effort_map") or {}),
            "source": "base", "base_policy_sha256": base_sha}


def effective_config(base: dict, generation: dict | None, *, apply_entries: bool,
                     summary: Callable[[str], dict | None],
                     blocked_ids: list[str] | None = None,
                     generation_sha256: str | None = None,
                     base_sha: str | None = None) -> tuple[dict, Provenance]:
    """Merge one generation onto `base` (DD-A2 merge table, in order).

    `blocked_ids` overrides the generation's own revocations — the pin path
    recomputes an ancestor under the CURRENT generation's revocations.
    `apply_entries=False` (DEEP_MODEL_ROUTER_OVERLAY=off) skips the entries
    and nothing else: history and revocations still apply.

    When nothing changes, the returned config IS `base` (same object), so the
    digest is byte-identical to a stateless route.
    """
    base_sha = base_sha or canonical_policy_sha256(base)
    prov = Provenance(status="noop", base_policy_sha256=base_sha,
                      generation_sha256=generation_sha256)
    if generation is None:
        return base, prov
    blocked = set(generation["blocked_ids"] if blocked_ids is None else blocked_ids)
    models = dict(base["models"])
    changed = False

    # Entries that did not replace their row but whose own probe summary still
    # names them: the router may have seated that id, so it stays valid
    # history input (rule 7) even though it is not dispatchable now.
    unseated: list[tuple[str, dict]] = []
    if not apply_entries:
        unseated = [(k, generation["entries"][k]) for k in sorted(generation["entries"])]
    if apply_entries:
        for key in sorted(generation["entries"]):
            e = generation["entries"][key]
            reason = _entry_structural_reason(key, e, base, generation)       # 1
            if reason:
                prov.rejected.append({"key": key, "reason": reason})
                continue
            template = base["models"][key]["lineage"]["template"]
            base_id = base["models"][key]["id"]
            if base_id == e["id"]:                                            # 2
                prov.noop.append({"key": key, "reason": "promoted"})
                continue
            if lineage.compare(_gen(template, base_id), _gen(template, e["id"])) > 0:
                prov.noop.append({"key": key, "reason": "base_newer"})       # 3
                unseated.append((key, e))
                continue
            if base_id != e["from_id"]:                                       # 4
                prov.rejected.append({"key": key, "reason": "from_id_mismatch"})
                unseated.append((key, e))
                continue
            if e["id"] in blocked:                                            # 5
                prov.rejected.append({"key": key, "reason": "blocked"})
                continue
            reason = _summary_reason(key, e, base, summary)
            if reason:
                prov.rejected.append({"key": key, "reason": reason})
                if reason == "stale_probe":
                    unseated.append((key, e))
                continue
            row = copy.deepcopy(base["models"][key])                          # 6
            row["id"] = e["id"]
            row.pop("effort_map", None)
            row.pop("effort_ceiling", None)
            if e["effort_map"]:
                row["effort_map"] = dict(e["effort_map"])
            if e["effort_ceiling"] is not None:
                row["effort_ceiling"] = e["effort_ceiling"]
            row["price_per_mtok"] = dict(PRICE_UNAVAILABLE)
            models[key] = row
            prov.applied.append(key)
            changed = True

    live_ids = {m["id"] for m in models.values()}

    def synthesize(mid: str, rec: dict) -> None:
        nonlocal changed
        row = {"id": mid, "history_of": rec["key"], "history_source": rec["source"],
               "family": rec["family"], "capability_tier": rec["capability_tier"],
               "dispatchable": False, "verified": True,
               "price_per_mtok": dict(PRICE_UNAVAILABLE)}
        if rec["effort_ceiling"] is not None:
            row["effort_ceiling"] = rec["effort_ceiling"]
        if rec["effort_map"]:
            row["effort_map"] = dict(rec["effort_map"])
        models[f"{rec['key']}@{mid}"] = row
        live_ids.add(mid)
        prov.history_ids_synthesized += 1
        changed = True

    for mid in sorted(generation["history"]):                                 # 7
        rec = generation["history"][mid]
        if mid in live_ids:
            continue            # already valid input as a live or base-history row
        reason = _history_reason(mid, rec, base, base_sha, summary)
        if reason:
            prov.rejected.append({"key": rec["key"], "reason": reason})
            continue
        if rec["source"] == "base" and base["models"][rec["key"]]["id"] == mid:
            rec = _base_snapshot(base, rec["key"], base_sha)   # the row's own id: re-snapshot
        synthesize(mid, rec)
    for key, e in unseated:
        mid = e["id"]
        if mid in live_ids or _entry_structural_reason(key, e, base, generation):
            continue
        s = summary(e["probe_summary_sha256"])
        if not isinstance(s, dict) or s.get("id") != mid or s.get("key") != key:
            continue
        row = base["models"][key]
        synthesize(mid, {"key": key, "family": row["family"],
                         "capability_tier": row["capability_tier"],
                         "effort_ceiling": e["effort_ceiling"],
                         "effort_map": dict(e["effort_map"]), "source": "probe"})

    prov.blocked_ids = len(blocked)
    if not changed and not blocked:
        return base, prov
    cfg = dict(base)
    cfg["models"] = models
    if blocked:                                                               # 8
        cfg["local_state"] = {"blocked_ids": sorted(blocked)}
    if prov.applied:
        prov.status = "partial" if prov.rejected else "applied"
    return cfg, prov
