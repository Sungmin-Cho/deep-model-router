"""Builders for local model state in tests (design 2026-09-25 DD-A2).

A test that needs committed state writes it here, under its own `tmp_path`,
through the same `StateRoot` / `model_state` writers model_sync uses — never
by hand-rolled paths — and passes an explicit environment naming that root.
Successor ids are derived from each row's lineage template, so no registry id
is spelled in test code.
"""
from __future__ import annotations

import copy
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import lineage  # noqa: E402
import secure_io  # noqa: E402
from model_state import (  # noqa: E402
    base_row_sha256, recipe_sha256, write_generation, write_pointer, write_summary,
)
from policy_digest import canonical_policy_sha256  # noqa: E402
from route_task import load_config  # noqa: E402
from secure_io import StateRoot  # noqa: E402

BASE = load_config()
BASE_SHA = canonical_policy_sha256(BASE)


def ID(key: str) -> str:
    return BASE["models"][key]["id"]


def successor(key: str, step: int = 1, base: dict = BASE) -> str:
    template = base["models"][key]["lineage"]["template"]
    major = lineage.parse(template, base["models"][key]["id"]).parts[0]
    return template.replace("{gen}", str(major + step)).replace("[-{date}]", "")


def sha_of(obj) -> str:
    return hashlib.sha256(secure_io.canonical_json_bytes(obj)).hexdigest()


def entry(key: str, new_id: str, *, from_id: str | None = None, superseded=(),
          effort_map=None, ceiling=None, base: dict = BASE) -> dict:
    return {"line": base["models"][key]["lineage"]["line"],
            "from_id": from_id or base["models"][key]["id"], "id": new_id,
            "superseded": list(superseded), "effort_map": dict(effort_map or {}),
            "effort_ceiling": ceiling, "probe_summary_sha256": "0" * 64}


def summary(key: str, e: dict, base: dict = BASE) -> dict:
    return {"key": key, "line": e["line"], "from_id": e["from_id"], "id": e["id"],
            "superseded": list(e["superseded"]), "effort_map": dict(e["effort_map"]),
            "effort_ceiling": e["effort_ceiling"], "overlay_schema_version": 1,
            "base_row_sha256": base_row_sha256(base, key),
            "recipe_sha256": recipe_sha256(base, base["models"][key]["family"])}


def base_record(key: str, base: dict = BASE, base_sha: str = BASE_SHA) -> dict:
    row = base["models"][key]
    return {"key": key, "family": row["family"], "capability_tier": row["capability_tier"],
            "effort_ceiling": row.get("effort_ceiling"),
            "effort_map": dict(row.get("effort_map", {})),
            "source": "base", "base_policy_sha256": base_sha}


def probe_record(key: str, summary_sha: str, base: dict = BASE) -> dict:
    rec = base_record(key, base)
    del rec["base_policy_sha256"]
    rec.update(source="probe", probe_summary_sha256=summary_sha)
    return rec


def generation(entries=None, history=None, blocked=(), parent=None,
               base_sha: str = BASE_SHA) -> dict:
    return {"overlay_schema_version": 1, "entries": dict(entries or {}),
            "history": dict(history or {}), "blocked_ids": list(blocked),
            "parent_generation_sha256": parent, "base_policy_sha256": base_sha}


def replacing(*keys: str, step: int = 1, parent: str | None = None,
              blocked=()) -> tuple[dict, dict]:
    """One generation replacing each key's live id with its successor: the
    entries, their summaries and the from_id history records."""
    entries, sums, history = {}, {}, {}
    for key in keys:
        e = entry(key, successor(key, step))
        s = summary(key, e)
        e["probe_summary_sha256"] = sha_of(s)
        entries[key] = e
        sums[sha_of(s)] = s
        history[ID(key)] = base_record(key)
    return generation(entries, history, blocked=blocked, parent=parent), sums


def state_root(tmp_path: Path, name: str = "state") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def publish(root: Path, gen: dict, sums: dict | None = None, *, point: bool = True) -> str:
    """Install summaries, then the generation, then (optionally) move the
    pointer — the publication order DD-A2 requires. Returns the generation
    sha256."""
    with StateRoot.open(root, create=True) as r:
        for s in (sums or {}).values():
            write_summary(r, s)
        sha = write_generation(r, gen)
        if point:
            write_pointer(r, sha)
    return sha


def env_for(root: Path, *, off: bool = False) -> dict:
    env = dict(os.environ)
    env["DEEP_MODEL_ROUTER_STATE_DIR"] = str(root)
    env["DEEP_MODEL_ROUTER_OVERLAY"] = "off" if off else "on"
    return env


def all_dispatchable_keys(base: dict = BASE) -> list[str]:
    return sorted(k for k, m in base["models"].items()
                  if m.get("dispatchable", True) is not False and "lineage" in m)


def deep(obj):
    return copy.deepcopy(obj)
