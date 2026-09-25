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
* probe gates (DD-A5), pure — `gate_p0` .. `gate_p4`, `retry_after_for`,
  `boot_input_tokens`, `parse_codex_debug_models`.
* contained probe argv (DD-A0) — `probe_argv` (read-only reviewer recipes
  only; there is no code path that builds a write recipe), `new_child_cwd`.
* the probe harness — `probe_candidate`: every call is a fresh
  `dispatch_agent.py run` subprocess, one id per process; the result is a
  content-addressed summary under `work/probes/summaries/` (publication
  copies it into `committed/summaries/`).

Every catalog is an INTERNAL, undocumented vendor format. A catalog that
cannot be read, or any entry missing a key a filter depends on
(`visibility`, `supported_in_api`, `hidden`, `section`, `short_name`), makes
that whole family `discovery_unavailable` — fail closed, never a candidate
from a half-understood file. No replacement means the base stays.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, NamedTuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import lineage  # noqa: E402
import model_state  # noqa: E402
import strict_json  # noqa: E402
from policy_digest import canonical_policy_sha256  # noqa: E402
from secure_io import StateRoot, open_regular  # noqa: E402

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


# --------------------------------------------------------------------------
# Probe gates (DD-A5) — pure
# --------------------------------------------------------------------------

EFFORT_LEVELS = ("MINIMAL", "LOW", "MEDIUM", "HIGH", "VERY_HIGH", "MAX")
# MINIMAL is unreachable (no `effort_by_work` entry names it): a probe checks
# LOW up to the row's ceiling. Missing top tokens become a ceiling override;
# a missing routine token (at or below HIGH) fails the candidate.
ROUTINE_CEILING = "HIGH"
P1_PROMPT = "Reply with exactly: pong\n"
P3_RATIO = 1.5
P3_ABSOLUTE_CAP = 60_000
TRANSIENT_RETRY = dt.timedelta(hours=6)
QUOTA_UNKNOWN_RETRY = dt.timedelta(hours=24)
UNSUPPORTED_MARKERS = ("not supported", "not found")


class ProbeContext(NamedTuple):
    """What a gate needs to date its verdict and key its expiry."""
    now: dt.datetime
    cli_version: str | None = None
    catalog_sha256: str | None = None
    quota_resets_at: int | None = None     # epoch seconds, codex rate_limits


def retry_after_for(reason: str, ctx: ProbeContext) -> dict:
    """When a negative result expires (DD-A5 "보류는 만료된다").

    quota -> the limit's reset time (24 h when unknown); cli_metadata /
    served_unproven / overhead -> when the CLI version changes;
    served_mismatch -> when the catalog changes; anything else (smoke,
    transport, effort) -> 6 hours.
    """
    if reason == "quota":
        if ctx.quota_resets_at is not None:
            at = dt.datetime.fromtimestamp(ctx.quota_resets_at, dt.timezone.utc)
        else:
            at = ctx.now + QUOTA_UNKNOWN_RETRY
        return {"kind": "time", "at": at.isoformat()}
    if reason in ("cli_metadata", "served_unproven", "overhead"):
        return {"kind": "cli_version_change", "cli_version": ctx.cli_version}
    if reason == "served_mismatch":
        return {"kind": "catalog_change", "catalog_sha256": ctx.catalog_sha256}
    return {"kind": "time", "at": (ctx.now + TRANSIENT_RETRY).isoformat()}


def _verdict(gate: str, outcome: str, reason: str | None, ctx: ProbeContext, **extra) -> dict:
    return {"gate": gate, "outcome": outcome, "reason": reason,
            "retry_after": None if outcome == "pass" else retry_after_for(reason, ctx),
            **extra}


def native_token(base: dict, row: dict, level: str) -> str:
    """The native effort token the router would spell for `row` at `level`."""
    override = row.get("effort_map") or {}
    return override.get(level) or base["effort_map"][row["family"]][level]


def reachable_levels(row: dict, ceiling: str | None = None) -> list[str]:
    top = ceiling or row.get("effort_ceiling") or "MAX"
    return list(EFFORT_LEVELS[1:EFFORT_LEVELS.index(top) + 1])


def _min_level(*levels: str | None) -> str | None:
    present = [lv for lv in levels if lv is not None]
    return min(present, key=EFFORT_LEVELS.index) if present else None


def gate_p0(*, family: str, candidate_id: str, row: dict, base: dict,
            catalog_efforts: list[str], cli_metadata_ids: list[str] | None,
            ctx: ProbeContext) -> dict:
    """P0, free: the effort vocabulary covers every token the router can emit
    for this row (else an override plan), and for openai the installed CLI
    carries metadata for the slug (`codex debug models`).

    An empty catalog vocabulary means the vendor publishes none for this
    model (e.g. a model without an effort control); it is left to P4.
    """
    if family == "openai" and (cli_metadata_ids is None
                               or candidate_id not in cli_metadata_ids):
        return _verdict("P0", "deferred", "cli_metadata", ctx, effort_ceiling=None)
    ceiling = None
    if catalog_efforts:
        levels = reachable_levels(row)
        covered = None
        for level in levels:
            if native_token(base, row, level) not in catalog_efforts:
                break
            covered = level
        if covered is None or EFFORT_LEVELS.index(covered) < EFFORT_LEVELS.index(ROUTINE_CEILING):
            return _verdict("P0", "failed", "effort_vocabulary", ctx, effort_ceiling=None)
        if covered != levels[-1]:
            ceiling = covered
    ceiling = _min_level(ceiling, row.get("effort_ceiling"))
    return _verdict("P0", "pass", None, ctx, effort_ceiling=ceiling)


def _succeeded(receipt: dict | None) -> bool:
    result = (receipt or {}).get("result") or {}
    return result.get("state") == "SUCCEEDED" and result.get("termination_confirmed") is True


def is_pong(stdout: str | None) -> bool:
    return isinstance(stdout, str) and stdout.strip() == "pong"


def gate_p1(receipt: dict | None, stdout: str | None, ctx: ProbeContext) -> dict:
    """P1 smoke: SUCCEEDED, termination confirmed, stdout exactly `pong`
    (surrounding whitespace only). Any single miss fails closed."""
    if _succeeded(receipt) and is_pong(stdout):
        return _verdict("P1", "pass", None, ctx)
    return _verdict("P1", "failed", "smoke", ctx)


def _served_forms(lin: Mapping, mid: str) -> set[str]:
    return {form.replace("{id}", mid) for form in (lin.get("served_forms") or ["{id}"])}


def gate_p2(receipt: dict, lin: Mapping, ctx: ProbeContext) -> dict:
    """P2 serving proof, from the P1 receipt.

    claude: every `served_models` entry is a served form of the id. grok: the
    session summary's `current_model_id` and every `served_models` entry are.
    codex: the plain-mode banner's header model equals the requested id and no
    metadata warning was printed — recorded as "id accepted", not served.
    """
    fmt = receipt.get("output_envelope")
    mid = receipt.get("model_id")
    env = (receipt.get("result") or {}).get("envelope") or {}
    if fmt == "codex-exec-text-v1":
        header = env.get("header_model")
        if header is None:
            return _verdict("P2", "deferred", "served_unproven", ctx, basis=None, observed=[])
        if header != mid:
            return _verdict("P2", "failed", "served_mismatch", ctx, basis=None, observed=[header])
        if env.get("metadata_warning") is not False:
            return _verdict("P2", "deferred", "cli_metadata", ctx, basis=None, observed=[header])
        return _verdict("P2", "pass", None, ctx, basis="id_accepted", observed=[header])
    observed: list[str] = []
    if fmt in ("claude-print-json-v1", "grok-headless-json-v1"):
        observed = list(env.get("served_models") or [])
        if fmt == "grok-headless-json-v1":
            summary = (receipt.get("session_evidence") or {}).get("summary") or {}
            current = summary.get("current_model_id")
            if isinstance(current, str):
                observed.append(current)
    if not observed:
        return _verdict("P2", "deferred", "served_unproven", ctx, basis=None, observed=[])
    forms = _served_forms(lin, mid)
    if any(o not in forms for o in observed):
        return _verdict("P2", "failed", "served_mismatch", ctx, basis=None,
                        observed=sorted(set(observed)))
    return _verdict("P2", "pass", None, ctx, basis="served", observed=sorted(set(observed)))


def boot_input_tokens(receipt: dict | None) -> int | None:
    """Total boot input for one call, cache INCLUDED (cache lowers the bill,
    not the count). codex --json: `input_tokens` is already the total; claude
    and grok split cache reads/creations out of `input_tokens`."""
    env = ((receipt or {}).get("result") or {}).get("envelope") or {}
    usage = env.get("usage")
    if not isinstance(usage, dict) or not isinstance(usage.get("input_tokens"), (int, float)):
        return None
    if (receipt or {}).get("output_envelope") == "codex-exec-json-v1":
        return int(usage["input_tokens"])
    return int(sum(usage.get(k, 0) or 0 for k in
                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))


def gate_p3(pair: Mapping, prior: int | None, cap: int, ctx: ProbeContext) -> dict:
    """P3 overhead: candidate boot input <= 1.5 x the current id's (paired, same
    run), and <= the absolute cap. When the current id is gone at the vendor,
    a prior baseline from the same CLI version stands in, else the cap alone."""
    cand = pair.get("candidate")
    if cand is None:
        return _verdict("P3", "failed", "smoke", ctx, p3_basis=None)
    if pair.get("current") is not None:
        basis, reference = "paired", pair["current"]
    elif pair.get("current_unsupported") and prior is not None:
        basis, reference = "prior_baseline", prior
    elif pair.get("current_unsupported"):
        basis, reference = "absolute_only", None
    else:
        return _verdict("P3", "failed", "smoke", ctx, p3_basis=None)
    ok = cand <= cap and (reference is None or cand <= P3_RATIO * reference)
    return _verdict("P3", "pass" if ok else "deferred", None if ok else "overhead", ctx,
                    p3_basis=basis)


def gate_p4(results: list[Mapping], ctx: ProbeContext) -> dict:
    """P4 top token: the first accepted level, top-down. Accepting below the
    first attempt sets `effort_ceiling`; nothing accepted fails."""
    for i, r in enumerate(results):
        if r.get("accepted"):
            return _verdict("P4", "pass", None, ctx,
                            effort_ceiling=r["effort"] if i else None)
    return _verdict("P4", "failed", "top_token", ctx, effort_ceiling=None)


def parse_codex_debug_models(text: str | None) -> list[str] | None:
    """Slugs from `codex debug models` JSON; None when it does not parse."""
    try:
        doc = strict_json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, list):
        return None
    return [m["slug"] for m in models if isinstance(m, dict) and isinstance(m.get("slug"), str)]


def codex_cli_metadata_ids(env: Mapping[str, str]) -> list[str] | None:
    """`codex debug models --bundled`: the metadata compiled into the
    installed CLI (offline). A failing command is None -> deferred."""
    exe = shutil.which("codex", path=env.get("PATH"))
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, "debug", "models", "--bundled"], capture_output=True,
                              text=True, timeout=CLI_VERSION_TIMEOUT * 4, env=dict(env),
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_codex_debug_models(proc.stdout) if proc.returncode == 0 else None


# --------------------------------------------------------------------------
# Contained probe argv (DD-A0)
# --------------------------------------------------------------------------

DISPATCH = HERE / "dispatch_agent.py"
GUARD = "darwin-sandbox-v1"
GROK_ENVELOPE = "grok-headless-json-v1"
CLAUDE_ENVELOPE = "claude-print-json-v1"
CODEX_ENVELOPES = {"text": "codex-exec-text-v1", "json": "codex-exec-json-v1"}
PROBE_RUNTIME = "model_sync"


def _child_argv(family: str, model_id: str, effort_native: str, *,
                codex_mode: str, session_id: str) -> list[str]:
    """The READ-ONLY reviewer recipe of each direction, prompt on stdin."""
    if family == "claude":
        return ["claude", "-p", "--model", model_id, "--effort", effort_native,
                "--permission-mode", "plan", "--allowedTools", "Read,Glob,Grep,LS",
                "--strict-mcp-config", "--output-format", "json"]
    if family == "openai":
        return ["codex", "exec", "-m", model_id, "-c",
                f"model_reasoning_effort={effort_native}", "-s", "read-only",
                "--skip-git-repo-check", *(["--json"] if codex_mode == "json" else []), "-"]
    if family == "xai":
        return ["grok", "--no-auto-update", "-m", model_id, "--effort", effort_native,
                "--output-format", "json", "-s", session_id, "--permission-mode", "plan",
                "--tools", "read_file,list_dir,grep", "--deny", "MCPTool",
                "--disable-web-search", "--sandbox", "read-only",
                "--prompt-file", "/dev/stdin"]
    raise ValueError(f"no probe recipe for family {family!r}")


def grok_session_dir(grok_home: Path, child_cwd: Path, session_id: str) -> Path:
    """`$GROK_HOME/sessions/<URL-encoded child cwd>/<session uuid>`
    (references/adapters.md, "Deriving the session evidence directory")."""
    return Path(grok_home) / "sessions" / urllib.parse.quote(str(child_cwd), safe="") / session_id


def probe_argv(*, family: str, model_id: str, effort_native: str, attempt_id: str,
               receipt_dir: Path, child_cwd: Path, prompt_file: Path,
               deadline_seconds: float, codex_mode: str = "text",
               grok_home: Path | None = None, session_id: str | None = None,
               receipt_guard: bool | None = None) -> list[str]:
    """`dispatch_agent.py run … -- <read-only reviewer recipe>`.

    claude: `--strict-mcp-config`, plan mode, `claude-print-json-v1`. codex:
    `-s read-only`, `codex-exec-text-v1` (P1/P2/P4) or `--json` +
    `codex-exec-json-v1` (P3), and NO receipt guard — a guarded codex sandbox
    cannot read and is refused (DD-B9). grok: the envelope, the session
    evidence bound by one uuid, `--expect-sandbox-profile read-only`. The
    receipt guard is on for claude and grok where it exists (macOS).
    """
    session_id = session_id or str(uuid.uuid4())
    guard = (sys.platform == "darwin") if receipt_guard is None else receipt_guard
    sup = [sys.executable, str(DISPATCH), "run",
           "--attempt-id", attempt_id, "--receipt-dir", str(receipt_dir),
           "--deadline-seconds", str(deadline_seconds), "--grace-seconds", "5",
           "--seat", "probe", "--runtime", PROBE_RUNTIME,
           "--model-id", model_id, "--effort-native", effort_native,
           "--transport-id", f"{PROBE_RUNTIME}.to_{family}",
           "--child-cwd", str(child_cwd), "--prompt-file", str(prompt_file),
           "--output-schema", "none"]
    if family == "claude":
        sup += ["--output-envelope", CLAUDE_ENVELOPE]
    elif family == "openai":
        sup += ["--output-envelope", CODEX_ENVELOPES[codex_mode]]
        guard = False
    elif family == "xai":
        home = Path(grok_home) if grok_home is not None else Path.home() / ".grok"
        sup += ["--output-envelope", GROK_ENVELOPE,
                "--session-evidence",
                f"grok-session-v1:{grok_session_dir(home, child_cwd, session_id)}",
                "--session-id", session_id, "--expect-sandbox-profile", "read-only"]
    if guard:
        sup += ["--receipt-guard", GUARD]
    return [*sup, "--", *_child_argv(family, model_id, effort_native,
                                     codex_mode=codex_mode, session_id=session_id)]


def new_child_cwd(parent: Path) -> Path:
    """A fresh, empty, private directory for one probe run."""
    Path(parent).mkdir(parents=True, exist_ok=True, mode=0o700)
    path = Path(tempfile.mkdtemp(prefix="cwd-", dir=parent))
    os.chmod(path, 0o700)
    return path.resolve()


# --------------------------------------------------------------------------
# The harness
# --------------------------------------------------------------------------

SUMMARY_PREFIX = "work/probes"
RECEIPTS_RELPATH = "work/probes/receipts"


class _Run:
    """One candidate's probe run: its scratch, receipts and call log."""

    def __init__(self, *, family, lin, state_root, scratch, env, deadline, run_id):
        self.family, self.lin, self.env, self.deadline = family, lin, env, deadline
        self.run_id = run_id
        with StateRoot.open(Path(state_root)) as root:
            root.mkdir(f"{RECEIPTS_RELPATH}/{run_id}")
        self.receipt_dir = Path(state_root) / RECEIPTS_RELPATH / run_id
        scratch = Path(scratch)
        self.child_cwd = new_child_cwd(scratch)
        prompt_dir = Path(tempfile.mkdtemp(prefix="prompt-", dir=scratch))
        self.prompt = prompt_dir / "prompt.txt"
        self.prompt.write_text(P1_PROMPT)
        self.calls: list[dict] = []
        self.inferences = 0

    def call(self, gate: str, model_id: str, effort_native: str, *,
             codex_mode: str = "text") -> tuple[dict | None, str | None, str]:
        attempt = f"{self.run_id}-{len(self.calls) + 1}"
        argv = probe_argv(family=self.family, model_id=model_id,
                          effort_native=effort_native, attempt_id=attempt,
                          receipt_dir=self.receipt_dir, child_cwd=self.child_cwd,
                          prompt_file=self.prompt, deadline_seconds=self.deadline,
                          codex_mode=codex_mode)
        try:
            subprocess.run(argv, env=dict(self.env), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=self.deadline + 120)
        except (OSError, subprocess.SubprocessError):
            pass
        receipt, receipt_sha = _read_evidence_json(self.receipt_dir / f"{attempt}.json")
        stdout = _read_evidence_text(self.receipt_dir / f"{attempt}.stdout")
        stderr = _read_evidence_text(self.receipt_dir / f"{attempt}.stderr")
        state = ((receipt or {}).get("result") or {}).get("state")
        if state not in (None, "START_FAILED"):
            self.inferences += 1
        self.calls.append({"gate": gate, "attempt_id": attempt, "model_id": model_id,
                           "effort_native": effort_native, "argv": argv,
                           "receipt_sha256": receipt_sha, "state": state})
        text = stdout
        if receipt is not None and receipt.get("output_envelope") in (CLAUDE_ENVELOPE, GROK_ENVELOPE):
            text = _envelope_text(stdout, receipt["output_envelope"])
        return receipt, text, (stderr or "") + (stdout or "")


def _read_evidence_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        fd, _ = open_regular(path)
        with os.fdopen(fd, "rb") as f:
            data = f.read(4 * 1024 * 1024)
        value = strict_json.loads(data.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None
    return (value if isinstance(value, dict) else None), hashlib.sha256(data).hexdigest()


def _read_evidence_text(path: Path) -> str | None:
    try:
        fd, _ = open_regular(path)
        with os.fdopen(fd, "rb") as f:
            return f.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return None


def _envelope_text(stdout: str | None, fmt: str) -> str | None:
    """The answer field of a claude/grok JSON document (never the raw JSON)."""
    try:
        doc = json.loads(stdout or "")
    except json.JSONDecodeError:
        return None
    key = "result" if fmt == CLAUDE_ENVELOPE else "text"
    return doc.get(key) if isinstance(doc, dict) and isinstance(doc.get(key), str) else None


def _accepted(receipt: dict | None, text: str | None) -> bool:
    return _succeeded(receipt) and is_pong(text)


def probe_candidate(*, key: str, candidate: Mapping, base: dict, state_root: Path,
                    env: Mapping[str, str], catalog: Catalog, current_id: str,
                    superseded: list[str], scratch: Path, ctx: ProbeContext,
                    cli_metadata_ids: list[str] | None | object = ...,
                    prior_baseline: Mapping | None = None,
                    cap: int = P3_ABSOLUTE_CAP, deadline_seconds: float = 300) -> dict:
    """Probe one candidate for one registry key: P0 -> P1/P2 -> P3 pair -> P4.

    Every inference is its own `dispatch_agent.py run` subprocess over the
    same fresh empty child cwd. Stops at the first gate that does not pass.
    Writes the summary (whatever the outcome — a negative result carries its
    `retry_after`) to `work/probes/summaries/<sha256>.json` and returns
    `{"summary", "summary_sha256", "inferences"}`.
    """
    row = base["models"][key]
    family, lin = row["family"], row["lineage"]
    cand_id = candidate["id"]
    ctx = ctx._replace(catalog_sha256=catalog.sha256)
    if cli_metadata_ids is ...:
        cli_metadata_ids = codex_cli_metadata_ids(env) if family == "openai" else None
    run = None
    verdicts: list[dict] = []
    record: dict[str, Any] = {"served_basis": None, "served_models": [],
                              "metadata_warning": None,
                              "input_tokens": {"current": None, "candidate": None},
                              "p3_basis": None, "effort_results": []}

    p0 = gate_p0(family=family, candidate_id=cand_id, row=row, base=base,
                 catalog_efforts=list(candidate.get("efforts") or []),
                 cli_metadata_ids=cli_metadata_ids, ctx=ctx)
    verdicts.append(p0)
    ceiling = p0.get("effort_ceiling")
    final = p0
    if p0["outcome"] == "pass":
        run = _Run(family=family, lin=lin, state_root=state_root, scratch=scratch,
                   env=env, deadline=deadline_seconds,
                   run_id=f"p{ctx.now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}")
        low = native_token(base, row, "LOW")
        p1_receipt, p1_text, _ = run.call("P1", cand_id, low)
        final = gate_p1(p1_receipt, p1_text, ctx)
        verdicts.append(final)
        if final["outcome"] == "pass":
            final = gate_p2(p1_receipt, lin, ctx)
            verdicts.append(final)
            record["served_basis"] = final.get("basis")
            record["served_models"] = final.get("observed", [])
            env_rec = (p1_receipt.get("result") or {}).get("envelope") or {}
            if "metadata_warning" in env_rec:
                record["metadata_warning"] = env_rec["metadata_warning"]
        if final["outcome"] == "pass":
            final = _p3(run, family, cand_id, current_id, low, p1_receipt,
                        prior_baseline, cap, ctx, record)
            verdicts.append(final)
        if final["outcome"] == "pass":
            final = _p4(run, base, row, cand_id, ceiling, ctx, record)
            verdicts.append(final)
            ceiling = _min_level(ceiling, final.get("effort_ceiling"))

    summary = build_summary(
        key=key, base=base, candidate=candidate, superseded=superseded,
        effort_ceiling=ceiling, final=final, verdicts=verdicts, catalog=catalog,
        calls=run.calls if run else [], record=record, ctx=ctx)
    with StateRoot.open(Path(state_root)) as root:
        sha = model_state.write_summary(root, summary, prefix=SUMMARY_PREFIX)
    return {"summary": summary, "summary_sha256": sha,
            "inferences": run.inferences if run else 0}


def _p3(run: _Run, family, cand_id, current_id, low, p1_receipt, prior_baseline,
        cap, ctx, record) -> dict:
    mode = "json" if family == "openai" else "text"
    cur_receipt, _, cur_output = run.call("P3", current_id, low, codex_mode=mode)
    if family == "openai":
        cand_receipt, _, _ = run.call("P3", cand_id, low, codex_mode=mode)
    else:
        cand_receipt = p1_receipt       # same argv shape: the P1 call is the pair's half
    current = boot_input_tokens(cur_receipt) if _succeeded(cur_receipt) else None
    unsupported = current is None and any(
        m in (cur_output or "").lower() for m in UNSUPPORTED_MARKERS)
    prior = None
    if prior_baseline and prior_baseline.get("cli_version") == ctx.cli_version:
        prior = prior_baseline.get("input_tokens")
    pair = {"current": current,
            "candidate": boot_input_tokens(cand_receipt) if _succeeded(cand_receipt) else None,
            "current_unsupported": unsupported}
    record["input_tokens"] = {"current": pair["current"], "candidate": pair["candidate"]}
    verdict = gate_p3(pair, prior, cap, ctx)
    record["p3_basis"] = verdict.get("p3_basis")
    return verdict


def _p4(run: _Run, base, row, cand_id, ceiling, ctx, record) -> dict:
    levels = reachable_levels(row, ceiling)
    results = []
    for level in (levels[-1], levels[-2]) if len(levels) > 1 else (levels[-1],):
        token = native_token(base, row, level)
        if results and token == results[-1]["native"]:
            continue
        receipt, text, _ = run.call("P4", cand_id, token)
        results.append({"effort": level, "native": token,
                        "accepted": _accepted(receipt, text)})
        if results[-1]["accepted"]:
            break
    record["effort_results"] = results
    return gate_p4(results, ctx)


def build_summary(*, key: str, base: dict, candidate: Mapping, superseded: list[str],
                  effort_ceiling: str | None, final: Mapping, verdicts: list[Mapping],
                  catalog: Catalog, calls: list[dict], record: Mapping,
                  ctx: ProbeContext) -> dict:
    """The probe summary (DD-A5). The fields an overlay entry is cross-checked
    against (model_state.SUMMARY_MATCH_KEYS, base_row_sha256, recipe_sha256)
    are computed with model_state's own functions, so a publication that
    copies an entry out of a passing summary is admitted as-is."""
    row = base["models"][key]
    family = row["family"]
    try:
        from route_task import plugin_manifest_version
        router_version = plugin_manifest_version()
    except Exception:  # noqa: BLE001 — provenance only
        router_version = None
    return {
        "key": key,
        "line": row["lineage"]["line"],
        "from_id": row["id"],
        "id": candidate["id"],
        "superseded": list(superseded),
        "effort_map": dict(row.get("effort_map") or {}),
        "effort_ceiling": _min_level(effort_ceiling, row.get("effort_ceiling")),
        "overlay_schema_version": model_state.OVERLAY_SCHEMA_VERSION,
        "base_row_sha256": model_state.base_row_sha256(base, key),
        "recipe_sha256": model_state.recipe_sha256(base, family),
        "family": family,
        "outcome": final["outcome"],
        "reason": final.get("reason"),
        "retry_after": final.get("retry_after"),
        "gates": [{k: v.get(k) for k in ("gate", "outcome", "reason")} for v in verdicts],
        "catalog": {"sha256": catalog.sha256, **{k: catalog.source.get(k) for k in
                                                ("fetched_at", "etag", "client_version")}},
        "cli_version": ctx.cli_version,
        "probes": [{k: c[k] for k in ("gate", "attempt_id", "model_id", "effort_native",
                                      "argv", "receipt_sha256", "state")} for c in calls],
        "served_basis": record.get("served_basis"),
        "served_models": list(record.get("served_models") or []),
        "metadata_warning": record.get("metadata_warning"),
        "input_tokens": dict(record.get("input_tokens") or {}),
        "p3_basis": record.get("p3_basis"),
        "effort_results": list(record.get("effort_results") or []),
        "guard": ("omitted: nested Seatbelt" if family == "openai"
                  else GUARD if sys.platform == "darwin" else "omitted: platform"),
        "base_policy_sha256": canonical_policy_sha256(base),
        "router_version": router_version,
        "date": ctx.now.date().isoformat(),
    }
