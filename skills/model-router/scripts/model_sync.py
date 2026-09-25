#!/usr/bin/env python3
"""Model lineage sync (design 2026-09-25 DD-A4 .. DD-A7).

Detects successor model ids for free from the local CLI catalogs, verifies
them with contained probes through `dispatch_agent.py run`, and (later tasks)
publishes the result as a generation of the local model state. The router
never imports this module; it only reads what this module commits.

This file so far holds:

* catalog adapters (DD-A4) — `read_codex_catalog`, `read_grok_catalog`,
  `read_claude_catalog`, `parse_grok_models_text`, `parse_cli_version`,
  `cli_versions`; candidates and notices — `find_candidates`,
  `retirement_notices`, `alias_probe_plan`.

Every catalog is an INTERNAL, undocumented vendor format. A catalog that
cannot be read, or any entry missing a key a filter depends on
(`visibility`, `supported_in_api`, `hidden`, `section`, `short_name`), makes
that whole family `discovery_unavailable` — fail closed, never a candidate
from a half-understood file. No replacement means the base stays.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import lineage  # noqa: E402
import strict_json  # noqa: E402
from secure_io import open_regular  # noqa: E402

CATALOG_MAX_BYTES = 8 * 1024 * 1024
CLI_VERSION_TIMEOUT = 5.0
FAMILY_CLI = {"openai": "codex", "claude": "claude", "xai": "grok"}


def default_catalog_paths(home: Path | None = None) -> dict[str, Path]:
    """The only home-relative defaults; every reader takes its path as an argument."""
    home = Path(home) if home is not None else Path.home()
    return {"openai": home / ".codex" / "models_cache.json",
            "xai": home / ".grok" / "models_cache.json",
            "claude": home / ".claude" / "cache" / "model-catalog"}


# --------------------------------------------------------------------------
# Catalogs (DD-A4)
# --------------------------------------------------------------------------

@dataclass
class Catalog:
    """One family's catalog, reduced to what candidate selection needs.

    `models` holds only ELIGIBLE entries (id -> {"efforts": [...], ...});
    `excluded` names every entry a filter removed and why; `listed_ids` is
    everything the file listed, eligible or not. `status` is `ok`,
    `discovery_unavailable` or (claude only) `absent`.
    """
    family: str
    status: str
    reason: str | None = None
    models: dict[str, dict] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)
    listed_ids: list[str] = field(default_factory=list)
    retirements: dict[str, dict] = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    sha256: str = hashlib.sha256(b"").hexdigest()


def _unavailable(family: str, reason: str, sha: str | None = None) -> Catalog:
    cat = Catalog(family, "discovery_unavailable", reason)
    if sha:
        cat.sha256 = sha
    return cat


def _read_catalog_file(path: Path) -> tuple[Any, str]:
    """Bounded, symlink-refusing, strict-JSON read. Raises OSError/ValueError."""
    fd, st = open_regular(Path(path))
    with os.fdopen(fd, "rb") as f:
        data = f.read(CATALOG_MAX_BYTES + 1)
    if len(data) > CATALOG_MAX_BYTES:
        raise ValueError("catalog exceeds its size budget")
    return strict_json.loads(data.decode("utf-8")), hashlib.sha256(data).hexdigest()


def _str_list(values: Any, key: str) -> list[str] | None:
    if not isinstance(values, list):
        return None
    out = []
    for v in values:
        if not isinstance(v, Mapping) or not isinstance(v.get(key), str):
            return None
        out.append(v[key])
    return out


def read_codex_catalog(path: Path) -> Catalog:
    """`~/.codex/models_cache.json`: account-scoped, `models[]` by `slug`.

    Eligible: `visibility == "list"` and `supported_in_api is True`. Efforts
    are `supported_reasoning_levels[].effort`. `upgrade.retirement_at` is the
    retirement of THAT id (a notice), never a successor announcement.
    """
    family = "openai"
    try:
        doc, sha = _read_catalog_file(path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _unavailable(family, f"unreadable: {exc.__class__.__name__}")
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), list):
        return _unavailable(family, "shape", sha)
    cat = Catalog(family, "ok", sha256=sha,
                  source={k: doc.get(k) for k in ("fetched_at", "etag", "client_version")})
    for m in doc["models"]:
        if not isinstance(m, dict) or not isinstance(m.get("slug"), str):
            return _unavailable(family, "shape", sha)
        if "visibility" not in m or "supported_in_api" not in m:
            return _unavailable(family, "missing_discriminator", sha)
        slug = m["slug"]
        cat.listed_ids.append(slug)
        efforts = _str_list(m.get("supported_reasoning_levels"), "effort")
        if efforts is None:
            return _unavailable(family, "shape", sha)
        upgrade = m.get("upgrade")
        if isinstance(upgrade, dict) and isinstance(upgrade.get("retirement_at"), str):
            cat.retirements[slug] = {"retirement_at": upgrade["retirement_at"],
                                     "upgrade_to": upgrade.get("model")}
        if m["visibility"] != "list":
            cat.excluded[slug] = "hidden"
        elif m["supported_in_api"] is not True:
            cat.excluded[slug] = "not_in_api"
        else:
            cat.models[slug] = {"efforts": efforts}
    return cat


def parse_grok_models_text(text: str | None) -> tuple[list[str], str | None]:
    """`grok models`: the ids under `Available models:` and the `*` default."""
    listed: list[str] = []
    default = None
    if not isinstance(text, str):
        return listed, default
    in_list = False
    for line in text.splitlines():
        if line.strip() == "Available models:":
            in_list = True
            continue
        if not in_list:
            continue
        m = re.fullmatch(r"\s*([*-])\s+(\S+)(?:\s+\(default\))?\s*", line)
        if not m:
            if line.strip():
                in_list = False
            continue
        listed.append(m.group(2))
        if m.group(1) == "*":
            default = m.group(2)
    return listed, default


def read_grok_catalog(path: Path, models_text: str | None) -> Catalog:
    """`~/.grok/models_cache.json` (`models{id: {info}}`) plus `grok models`.

    Eligible: `info.hidden is False` and, when the `grok models` text is
    available, listed there. Efforts are `info.reasoning_efforts[].id`.
    Variants such as `-build-fast` are excluded later by the anchored
    lineage template, not here.
    """
    family = "xai"
    try:
        doc, sha = _read_catalog_file(path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _unavailable(family, f"unreadable: {exc.__class__.__name__}")
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), dict):
        return _unavailable(family, "shape", sha)
    listed, default = parse_grok_models_text(models_text)
    cat = Catalog(family, "ok", sha256=sha,
                  source={"fetched_at": doc.get("fetched_at"), "etag": doc.get("etag"),
                          "client_version": doc.get("grok_version"),
                          "cli_default": default})
    for mid, value in doc["models"].items():
        info = value.get("info") if isinstance(value, dict) else None
        if not isinstance(info, dict):
            return _unavailable(family, "shape", sha)
        if "hidden" not in info:
            return _unavailable(family, "missing_discriminator", sha)
        cat.listed_ids.append(mid)
        efforts = _str_list(info.get("reasoning_efforts") or [], "id")
        if efforts is None:
            return _unavailable(family, "shape", sha)
        if info["hidden"] is not False:
            cat.excluded[mid] = "hidden"
        elif models_text is not None and mid not in listed:
            cat.excluded[mid] = "not_listed"
        else:
            cat.models[mid] = {"efforts": efforts}
    return cat


def _version_tuple(v: str) -> tuple[int, ...] | None:
    if not isinstance(v, str) or not re.fullmatch(r"\d+(?:\.\d+)*", v):
        return None
    return tuple(int(x) for x in v.split("."))


def read_claude_catalog(directory: Path, cli_version: str | None) -> Catalog:
    """`~/.claude/cache/model-catalog/*-cc.json`, `catalog.config.models[]`.

    Eligible: `section == "main"` and `min_claude_code_version` (when
    declared) at or below the installed CLI — an unknown installed version
    fails that check. `short_name` is matched against a row's
    `lineage.catalog_name` at candidate time. Efforts are
    `thinking.effort_options[].id` (none for a model without effort).
    No `*-cc.json` at all is `absent` (the alias-probe fallback); when
    several exist, the most recently fetched one is read.
    """
    family = "claude"
    files = sorted(glob.glob(os.path.join(glob.escape(str(directory)), "*-cc.json")))
    if not files:
        return Catalog(family, "absent", "no catalog")
    docs = []
    for name in files:
        try:
            doc, sha = _read_catalog_file(Path(name))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return _unavailable(family, f"unreadable: {exc.__class__.__name__}")
        if not isinstance(doc, dict):
            return _unavailable(family, "shape", sha)
        fetched = doc.get("fetchedAt")
        docs.append((fetched if isinstance(fetched, (int, float)) else -1, sha, doc))
    _, sha, doc = max(docs, key=lambda d: d[0])
    models = ((doc.get("catalog") or {}).get("config") or {}).get("models") \
        if isinstance(doc.get("catalog"), dict) else None
    if not isinstance(models, list):
        return _unavailable(family, "shape", sha)
    installed = _version_tuple(cli_version) if cli_version else None
    cat = Catalog(family, "ok", sha256=sha,
                  source={"fetched_at": doc.get("fetchedAt"), "etag": None,
                          "client_version": cli_version})
    for m in models:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            return _unavailable(family, "shape", sha)
        if "section" not in m or "short_name" not in m:
            return _unavailable(family, "missing_discriminator", sha)
        mid = m["id"]
        cat.listed_ids.append(mid)
        thinking = m.get("thinking") or {}
        efforts = _str_list(thinking.get("effort_options") or [], "id") \
            if isinstance(thinking, dict) else None
        if efforts is None:
            return _unavailable(family, "shape", sha)
        need = m.get("min_claude_code_version")
        if m["section"] != "main":
            cat.excluded[mid] = "not_main"
        elif need is not None and (installed is None or _version_tuple(need) is None
                                   or _version_tuple(need) > installed):
            cat.excluded[mid] = "min_cli"
        else:
            cat.models[mid] = {"efforts": efforts, "short_name": m["short_name"]}
    return cat


def parse_cli_version(banner: str | None) -> str | None:
    """The first dotted x.y.z in a `--version` banner (`codex-cli 0.157.0`,
    `2.1.282 (Claude Code)`, `grok 1.0.40 (…) [stable]`)."""
    if not isinstance(banner, str):
        return None
    m = re.search(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])", banner)
    return m.group(1) if m else None


def cli_versions(env: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """`<cli> --version` for the three CLIs on PATH. Offline and inference-
    free; a missing or failing CLI is None (the tick hash still changes when
    it reappears)."""
    env = dict(os.environ if env is None else env)
    out: dict[str, str | None] = {}
    for cli in ("codex", "claude", "grok"):
        exe = shutil.which(cli, path=env.get("PATH"))
        if exe is None:
            out[cli] = None
            continue
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  timeout=CLI_VERSION_TIMEOUT, env=env,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            out[cli] = None
            continue
        out[cli] = parse_cli_version(proc.stdout) if proc.returncode == 0 else None
    return out


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------

def _lineage_rows(rows: Mapping[str, dict]):
    for key, row in sorted(rows.items()):
        lin = row.get("lineage")
        if isinstance(lin, Mapping) and "history_of" not in row \
                and row.get("dispatchable", True) is not False:
            yield key, row, lin


def find_candidates(rows: Mapping[str, dict],
                    catalogs: Mapping[str, Catalog]) -> dict[str, dict]:
    """Per lineage row: the highest eligible catalog id that is a strictly
    newer generation of the row's CURRENT id (after the overlay).

    Result per key: `{"status": "candidate", "id", "from_id", "line",
    "family", "efforts", "catalog_sha256"}` or `{"status": "none" |
    "discovery_unavailable" | "alias_probe", "from_id", …}`.
    """
    out: dict[str, dict] = {}
    for key, row, lin in _lineage_rows(rows):
        family = row["family"]
        base = {"from_id": row["id"], "line": lin["line"], "family": family}
        cat = catalogs.get(family)
        if cat is None or cat.status == "discovery_unavailable":
            out[key] = {"status": "discovery_unavailable", **base}
            continue
        if cat.status == "absent":
            out[key] = {"status": "alias_probe" if family == "claude"
                        and lin.get("catalog_name") else "discovery_unavailable", **base}
            continue
        template = lin["template"]
        current = lineage.parse(template, row["id"])
        best = None
        for mid, meta in cat.models.items():
            if family == "claude" and meta.get("short_name") != lin.get("catalog_name"):
                continue
            gen = lineage.parse(template, mid)
            if gen is None or current is None or lineage.compare(gen, current) <= 0:
                continue
            if best is None or lineage.compare(gen, best[1]) > 0:
                best = (mid, gen)
        if best is None:
            out[key] = {"status": "none", **base}
        else:
            out[key] = {"status": "candidate", "id": best[0], **base,
                        "efforts": list(cat.models[best[0]]["efforts"]),
                        "catalog_sha256": cat.sha256}
    return out


def retirement_notices(rows: Mapping[str, dict],
                       catalogs: Mapping[str, Catalog]) -> list[dict]:
    """Current ids the vendor catalog says will retire (`status` reports them)."""
    notices = []
    for key, row in sorted(rows.items()):
        if "history_of" in row:
            continue
        cat = catalogs.get(row.get("family"))
        info = cat.retirements.get(row.get("id")) if cat is not None else None
        if info:
            notices.append({"key": key, "id": row["id"], **info})
    return notices


def alias_probe_plan(rows: Mapping[str, dict], claude_catalog: Catalog) -> list[dict]:
    """Only without a Claude catalog: probe each claude lineage row's alias
    (`catalog_name` lower-cased) through the same contained reviewer argv as
    every probe (DD-A0), and read the served id off its envelope. Executed by
    the probe harness, never here."""
    if claude_catalog.status != "absent":
        return []
    return [{"key": key, "family": "claude", "alias": lin["catalog_name"].lower()}
            for key, row, lin in _lineage_rows(rows)
            if row["family"] == "claude" and lin.get("catalog_name")]
