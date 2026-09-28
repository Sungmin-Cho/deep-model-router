"""Canonical SHA-256 of model-routing.yaml (spec §11.4)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml


def _canonical(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


_DIGEST_CACHE: dict[str, tuple[int, str]] = {}


def canonical_policy_sha256(cfg: dict) -> str:
    """Content digest of a policy dict — the SSOT the file API delegates to.

    No cache: a ~30KB canonical dump+sha256 is sub-millisecond, and an
    id()-keyed cache would return a stale digest for an in-place-mutated
    dict — reintroducing the defect this function exists to fix. Callers
    must pass a real dict; non-dict Mappings (test instrumentation) are the
    caller's job to route to the file digest instead.
    """
    blob = json.dumps(
        cfg, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, default=_canonical,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def policy_sha256(config_path: Path) -> str:
    path = Path(config_path)
    key = str(path.resolve())
    mtime_ns = path.stat().st_mtime_ns
    cached = _DIGEST_CACHE.get(key)
    if cached and cached[0] == mtime_ns:
        return cached[1]
    digest = canonical_policy_sha256(yaml.safe_load(path.read_text(encoding="utf-8")))
    _DIGEST_CACHE[key] = (mtime_ns, digest)
    return digest
