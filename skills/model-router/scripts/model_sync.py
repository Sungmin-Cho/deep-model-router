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
  `retirement_notices`, `alias_probe_plan` (executed by `probe_alias` when
  no Claude catalog exists).
* probe gates (DD-A5), pure — `gate_p0` .. `gate_p4`, `retry_after_for`,
  `boot_input_tokens`, `parse_codex_debug_models`.
* contained probe argv (DD-A0) — `probe_argv` (read-only reviewer recipes
  only; there is no code path that builds a write recipe), `new_child_cwd`.
* the probe harness — `probe_candidate`: every call is a fresh
  `dispatch_agent.py run` subprocess, one id per process; the result is a
  content-addressed summary under `work/probes/summaries/` (publication
  copies it into `committed/summaries/`).
* publication and the CLI (DD-A2, DD-A6) — `publish_results`,
  `publish_generation` (summaries -> generation -> fsync -> in-lock recheck
  -> pointer renameat; an atomic `committed.tmp-*` bootstrap first), `tick`,
  `run` (12-inference budget, codex quota gate), `revert`, `unblock`,
  `repair`, `disable`/`enable`, `status`, `read_quota`, and `main`
  (`tick|run|status|revert|unblock|repair|disable|enable|quota|probe-maker`).
  The attended maker probe lives in `probe_maker.py`, imported only by the
  `probe-maker` command: the automatic path never builds a write recipe.

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


# grok checks for (and may install) updates on start unless told not to; the
# global flag applies to `--version` and `models` too (grok 1.0.40, measured
# 2026-09-25). The tick and run stay offline.
OFFLINE_FLAGS = {"grok": ("--no-auto-update",)}


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
            proc = subprocess.run([exe, *OFFLINE_FLAGS.get(cli, ()), "--version"],
                                  capture_output=True, text=True,
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
    every probe (DD-A0), and read the served id off its envelope. The plan
    only; `plan()` makes each row an `alias_probe` candidate and `run()`
    executes it with `probe_alias` — a newer served id is then probed through
    every gate like any catalog candidate before anything is published."""
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
# An effort/argument rejection (P4): one of these on an ERROR line that also
# names the effort, the argument or the token. Anything else that fails is
# transient — never a vendor verdict on the effort.
REJECTION_MARKERS = ("not supported", "unsupported", "invalid", "not allowed", "unknown",
                     "must be one of", "not a valid", "not available")
_ERROR_LINE = re.compile(r"^\s*(error|fatal)\b", re.I)


def error_lines(output: str | None) -> list[str]:
    """The error text of a probe's combined output: plain lines that START
    with `error`/`fatal` (a CLI's own error line, not a warning that merely
    mentions a phrase), and the message of a JSON error event (`type: error`,
    `turn.failed`, or `is_error: true`)."""
    out = []
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                doc = json.loads(stripped)
            except ValueError:
                doc = None
            if isinstance(doc, dict):
                if doc.get("type") in ("error", "turn.failed") or doc.get("is_error") is True:
                    err = doc.get("error")
                    for v in (doc.get("message"), doc.get("result"),
                              err.get("message") if isinstance(err, dict) else err):
                        if isinstance(v, str):
                            out.append(v)
                continue
        if _ERROR_LINE.match(stripped):
            out.append(stripped)
    return out


def effort_rejected(output: str | None, token: str) -> bool:
    for line in error_lines(output):
        low = line.lower()
        if any(m in low for m in REJECTION_MARKERS) and (
                "effort" in low or "argument" in low or "reasoning" in low
                or re.search(r"(^|[^a-z0-9])" + re.escape(token.lower()) + r"([^a-z0-9]|$)", low)):
            return True
    return False


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
    """P4 top token: the first accepted level, top-down. Only a level the
    vendor REJECTED (an effort/argument error in the receipt) lets the next
    one down count, and accepting below the first attempt sets
    `effort_ceiling`; a failure that is not a rejection (timeout, 5xx, crash)
    is `transient` and lowers nothing. Every level rejected fails."""
    for i, r in enumerate(results):
        if r.get("accepted"):
            return _verdict("P4", "pass", None, ctx,
                            effort_ceiling=r["effort"] if i else None)
        if not r.get("rejected"):
            return _verdict("P4", "failed", "transient", ctx, effort_ceiling=None)
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

    def __init__(self, *, family, lin, state_root, scratch, env, deadline, run_id,
                 on_attempt=None):
        self.family, self.lin, self.env, self.deadline = family, lin, env, deadline
        self.run_id = run_id
        self.on_attempt = on_attempt
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
        if self.on_attempt is not None:
            # Recorded BEFORE spawn so `disable` can cancel it by attempt id;
            # the callback raises to stop the run instead.
            self.on_attempt(attempt, self.receipt_dir)
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
        return receipt, text, "\n".join(x for x in (stderr, stdout) if x)


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
                    cap: int = P3_ABSOLUTE_CAP, deadline_seconds: float = 300,
                    on_attempt=None) -> dict:
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
                   env=env, deadline=deadline_seconds, on_attempt=on_attempt,
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


ALIAS_INFERENCES = 1


def probe_alias(*, key: str, candidate: Mapping, base: dict, state_root: Path,
                env: Mapping[str, str], scratch: Path, ctx: ProbeContext,
                on_attempt=None, deadline_seconds: float = 300) -> dict:
    """DD-A4 fallback, only when no Claude catalog exists: ONE call of the
    contained read-only reviewer recipe (`probe_argv`, `--strict-mcp-config`,
    plan mode, the receipt guard where it exists) through `dispatch_agent.py
    run`, on the row's alias. The served id is read off the receipt envelope.

    Returns `{"outcome": "candidate" | "none" | "failed", "id", "reason",
    "retry_after", "inferences"}`: `candidate` only when the call answered
    `pong` with termination confirmed, served exactly one id, and that id is
    a strictly newer generation of the row's current id under its template.
    Nothing is published from this — the id still has to pass P0..P4."""
    row = base["models"][key]
    lin = row["lineage"]
    run_ = _Run(family="claude", lin=lin, state_root=state_root, scratch=scratch, env=env,
                deadline=deadline_seconds, on_attempt=on_attempt,
                run_id=f"a{ctx.now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}")
    receipt, text, _ = run_.call("alias", candidate["alias"], native_token(base, row, "LOW"))
    out = {"id": None, "inferences": run_.inferences, "reason": None, "retry_after": None}
    if not _accepted(receipt, text):
        return {**out, "outcome": "failed", "reason": "smoke",
                "retry_after": retry_after_for("smoke", ctx)}
    env_ = ((receipt or {}).get("result") or {}).get("envelope") or {}
    served = sorted(set(env_.get("served_models") or []))
    template = lin["template"]
    parsed = lineage.parse(template, served[0]) if len(served) == 1 else None
    if parsed is None:
        return {**out, "outcome": "failed", "reason": "served_unproven",
                "retry_after": retry_after_for("served_unproven", ctx)}
    current = lineage.parse(template, candidate["from_id"])
    if current is None or lineage.compare(parsed, current) <= 0:
        return {**out, "outcome": "none", "id": served[0]}
    return {**out, "outcome": "candidate", "id": served[0]}


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
        m in line.lower() for line in error_lines(cur_output) for m in UNSUPPORTED_MARKERS)
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
        receipt, text, output = run.call("P4", cand_id, token)
        accepted = _accepted(receipt, text)
        results.append({"effort": level, "native": token, "accepted": accepted,
                        "rejected": not accepted and effort_rejected(output, token)})
        if accepted or not results[-1]["rejected"]:
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


# ==========================================================================
# Publication, tick, disable/enable, revert/unblock, repair, status, quota
# (DD-A2, DD-A6). Probes run outside every lock; only reflecting their
# result into committed/ and work/state.json happens under
# `work/publish.lock`, and the one linearisation point of a publication is
# the renameat of `committed/current.json` (of `committed/` itself on the
# first, bootstrap, publication).
# ==========================================================================

import argparse  # noqa: E402
import copy  # noqa: E402
import secrets  # noqa: E402

from secure_io import StateError  # noqa: E402

WORK_STATE = "work/state.json"
RUN_LOCK = "work/run.lock"
MAKERS_PREFIX = "work/probes/makers"
INFERENCE_BUDGET = 12
# Upper bound per candidate, checked BEFORE it is probed: P1 + the P3 pair
# (codex measures both ids in --json mode; claude/grok reuse the P1 call as
# the candidate half) + P4 top token and one step down.
MAX_INFERENCES = {"openai": 5, "claude": 4, "xai": 4}
LINEAGE_INTERVAL = dt.timedelta(hours=24)
QUOTA_DEFER_PERCENT = 90
QUOTA_STALE_AFTER = dt.timedelta(hours=6)
# DD-A6: an UNKNOWN quota (no fresh record) defers for 6 h; a known one
# until its reset. (retry_after_for's 24 h is a probe-reported quota without a
# reset time, DD-A5.)
QUOTA_UNKNOWN_DEFER = dt.timedelta(hours=6)
QUOTA_TAIL_BYTES = 4 * 1024 * 1024
QUOTA_MAX_FILES = 50
RECENT_KEEP = 20
CANCEL_GRACE_SECONDS = "5"
ENTRY_FIELDS = ("line", "from_id", "id", "superseded", "effort_map", "effort_ceiling")


class SyncError(Exception):
    """A refusal the operator must act on (CLI exit 1, reason on stderr)."""


class Disabled(Exception):
    """Auto-upgrade was turned off while a run was probing."""


def _fault(stage: str) -> None:
    """Crash-injection seam between publication steps (tests only)."""


def _utcnow(now: dt.datetime | None = None) -> dt.datetime:
    return now if now is not None else dt.datetime.now(dt.timezone.utc)


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def autoupgrade_env_off(env: Mapping[str, str]) -> bool:
    return env.get("DEEP_MODEL_ROUTER_AUTOUPGRADE") == "0"


def _home(env: Mapping[str, str], home: Path | None) -> Path:
    return Path(home) if home is not None else Path(env.get("HOME") or Path.home())


# --- work/state.json (model_sync's own; the router never reads it) ---------

def default_work_state() -> dict:
    return {"schema": 1, "auto_upgrade": "enabled", "tick_hash": None, "negatives": {},
            "last_probe": {}, "in_flight": [], "recent": [], "makers": {}}


def read_work_state(root: StateRoot) -> dict:
    try:
        value = root.read_json(WORK_STATE)
    except FileNotFoundError:
        return default_work_state()
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise StateError("work/state.json has an unknown shape")
    st = default_work_state()
    st.update(value)
    return st


def update_work_state(state_path: Path, fn, *, tolerate_unreadable: bool = False):
    """Read-modify-write under the publication lock. `fn(state)` mutates in
    place and may raise to abort without writing."""
    with StateRoot.open(Path(state_path), create=True) as root, root.lock():
        try:
            st = read_work_state(root)
        except StateError:
            if not tolerate_unreadable:
                raise
            st = default_work_state()
        out = fn(st)
        root.write_json_atomic(WORK_STATE, st)
        return out


def _peek_work_state(state_path: Path) -> dict:
    try:
        with StateRoot.open(Path(state_path)) as root:
            return read_work_state(root)
    except FileNotFoundError:
        return default_work_state()


def auto_upgrade_recheck(env: Mapping[str, str]):
    """The in-lock recheck right before the pointer swap (DD-A6)."""
    def recheck(root: StateRoot) -> bool:
        if autoupgrade_env_off(env):
            return False
        try:
            return read_work_state(root)["auto_upgrade"] != "disabled"
        except (OSError, ValueError):
            return False
    return recheck


# --- the committed view ----------------------------------------------------

@dataclass
class CommittedView:
    shape: str
    generation: dict | None = None
    generation_sha256: str | None = None
    config: dict | None = None
    provenance: Any = None


def committed_view(state_path: Path, base: dict, *, base_sha: str | None = None) -> CommittedView:
    """The committed generation merged onto `base` with its entries ON
    (whether the router applies them is DEEP_MODEL_ROUTER_OVERLAY's
    business). Unreadable committed state is a refusal, never "absent"."""
    with model_state.read_state(Path(state_path)) as st:
        if st.shape == "unreadable":
            raise SyncError(f"committed state is unreadable ({st.detail}); "
                            f"see `model_sync.py repair`")
        if st.shape == "absent":
            return CommittedView("absent", config=base)
        cfg, prov = model_state.effective_config(
            base, st.generation, apply_entries=True, summary=st.summary,
            generation_sha256=st.generation_sha256, base_sha=base_sha)
        return CommittedView("ok", st.generation, st.generation_sha256, cfg, prov)


def superseded_for(generation: dict | None, key: str, base: dict) -> list[str]:
    """The ids a new entry for `key` supersedes, from the CURRENT generation:
    the live overlay entry's chain plus its id (6 -> 7 gives [6])."""
    e = ((generation or {}).get("entries") or {}).get(key)
    if not e or e["from_id"] != base["models"][key]["id"] or e["id"] == e["from_id"]:
        return []
    return [*e["superseded"], e["id"]]


def _base_history_record(base: dict, key: str, base_sha: str) -> dict:
    row = base["models"][key]
    return {"key": key, "family": row["family"], "capability_tier": row["capability_tier"],
            "effort_ceiling": row.get("effort_ceiling"),
            "effort_map": dict(row.get("effort_map") or {}),
            "source": "base", "base_policy_sha256": base_sha}


def _probe_history_record(base: dict, key: str, e: Mapping) -> dict:
    """The id as it was live: the base row's family and tier, the ENTRY's
    effort fields (a history row never inherits a successor's ceiling)."""
    row = base["models"][key]
    return {"key": key, "family": row["family"], "capability_tier": row["capability_tier"],
            "effort_ceiling": e["effort_ceiling"], "effort_map": dict(e["effort_map"]),
            "source": "probe", "probe_summary_sha256": e["probe_summary_sha256"]}


def _next_generation(cur: dict | None, cur_sha: str | None, base_sha: str) -> dict:
    cur = cur or {}
    return {"overlay_schema_version": model_state.OVERLAY_SCHEMA_VERSION,
            "entries": copy.deepcopy(cur.get("entries") or {}),
            "history": copy.deepcopy(cur.get("history") or {}),
            "blocked_ids": list(cur.get("blocked_ids") or []),
            "parent_generation_sha256": cur_sha if cur else None,
            "base_policy_sha256": base_sha}


# --- publication -------------------------------------------------------------

def publish_generation(state_path: Path, build, *, recheck=None) -> str | None:
    """Publish what `build(current StateRead)` returns — `(generation,
    {summary_sha: summary})` or None for nothing to do — under the lock:
    summaries -> generation -> fsync -> recheck -> pointer renameat -> fsync.
    A crash anywhere before the pointer moves leaves the old generation
    current and the new file unreachable (no pointer names it)."""
    state_path = Path(state_path)
    with StateRoot.open(state_path, create=True) as root, root.lock():
        with model_state.read_state(state_path) as st:
            if st.shape == "unreadable":
                raise SyncError(f"committed state is unreadable ({st.detail}); "
                                f"see `model_sync.py repair`")
            built = build(st)
        if built is None:
            return None
        gen, installs = built
        model_state.validate_generation(gen)
        if not root.lexists(model_state.COMMITTED):
            return _bootstrap(root, state_path, gen, installs, recheck)
        for s in installs.values():
            model_state.write_summary(root, s)
        _fault("after_summaries")
        sha = model_state.write_generation(root, gen)
        root.fsync_dir(f"{model_state.COMMITTED}/generations")
        _fault("after_generation")
        if recheck is not None and not recheck(root):
            return None
        _fault("before_pointer")
        model_state.write_pointer(root, sha)
        root.fsync_dir(model_state.COMMITTED)
        _fault("after_pointer")
        return sha


def _bootstrap(root: StateRoot, state_path: Path, gen: dict, installs: dict,
               recheck) -> str | None:
    """First publication: everything under `committed.tmp-<rand>/`, then ONE
    renameat to `committed/`. Interrupted, only the tmp directory remains —
    which the router reads as `absent`."""
    tmp = f"committed.tmp-{secrets.token_hex(8)}"
    root.mkdir(tmp)
    for s in installs.values():
        model_state.write_summary(root, s, prefix=tmp)
    sha = model_state.write_generation(root, gen, prefix=tmp)
    model_state.write_pointer(root, sha, prefix=tmp)
    root.fsync_dir(tmp)
    _fault("before_bootstrap_rename")
    if recheck is not None and not recheck(root):
        shutil.rmtree(Path(state_path) / tmp, ignore_errors=True)
        return None
    root.rename(tmp, model_state.COMMITTED)
    root.fsync_dir()
    return sha


def _apply_results(cur: dict | None, cur_sha: str | None, base: dict, base_sha: str,
                   results: list) -> tuple[dict, dict]:
    gen = _next_generation(cur, cur_sha, base_sha)
    installs: dict[str, dict] = {}
    for key, summary, sha in results:
        e = {k: copy.deepcopy(summary[k]) for k in ENTRY_FIELDS}
        e["probe_summary_sha256"] = sha
        prev = gen["entries"].get(key)
        if prev == e:
            continue
        if prev and prev["from_id"] == e["from_id"] \
                and prev["id"] not in (e["id"], base["models"][key]["id"]):
            gen["history"][prev["id"]] = _probe_history_record(base, key, prev)
        if e["from_id"] != e["id"]:
            gen["history"][e["from_id"]] = _base_history_record(base, key, base_sha)
        gen["history"].pop(e["id"], None)
        gen["entries"][key] = e
        installs[sha] = summary
    return gen, installs


def publish_results(state_path: Path, base: dict, results: list, *, recheck=None) -> str | None:
    """Publish passing probe results `[(key, summary, summary_sha256)]`.

    Re-derived under the lock from whatever is current THEN: a result whose
    `superseded` no longer matches the current chain was measured against
    another generation and is dropped; an entry that would not apply
    against today's base (rule 1-5, e.g. a second spelling of a generation
    already in history) is dropped; nothing left is a no-op (None). So of
    two writers publishing the same result, the second does nothing.
    """
    base_sha = canonical_policy_sha256(base)

    def build(st):
        cur = st.generation if st.shape == "ok" else None
        cur_sha = st.generation_sha256 if cur else None
        todo = [r for r in results if r[1].get("superseded") == superseded_for(cur, r[0], base)]
        while todo:
            gen, installs = _apply_results(cur, cur_sha, base, base_sha, todo)
            if not installs:
                return None
            _, prov = model_state.effective_config(
                base, gen, apply_entries=True, base_sha=base_sha,
                summary=lambda s: installs.get(s) or st.summary(s))
            new_keys = {k for k, _, sha in todo if sha in installs}
            bad = new_keys - set(prov.applied)
            if not bad:
                return gen, installs
            todo = [r for r in todo if r[0] not in bad]
        return None
    return publish_generation(state_path, build, recheck=recheck)


def revert(state_path: Path, key: str, *, base: dict | None = None) -> dict:
    """Drop `key`'s entry and revoke its id in one new generation; the id's
    history record (and its summary) stays, so it remains valid input."""
    from route_task import load_config
    base = base if base is not None else load_config()
    base_sha = canonical_policy_sha256(base)
    out: dict = {}

    def build(st):
        if st.shape != "ok" or key not in st.generation["entries"]:
            raise SyncError(f"no overlay entry for {key!r} in the committed generation")
        gen = _next_generation(st.generation, st.generation_sha256, base_sha)
        e = gen["entries"].pop(key)
        if e["id"] not in gen["blocked_ids"]:
            gen["blocked_ids"] = sorted([*gen["blocked_ids"], e["id"]])
        if e["id"] != base["models"][key]["id"]:
            gen["history"][e["id"]] = _probe_history_record(base, key, e)
        out.update(key=key, blocked_id=e["id"])
        return gen, {}
    sha = publish_generation(state_path, build)
    return {"status": "reverted", "generation_sha256": sha, **out}


def unblock(state_path: Path, model_id: str, *, base: dict | None = None) -> dict:
    """Lift a revocation. A reverted overlay id whose probe still applies is
    re-seated as its entry (and leaves history — an entry id may not also be
    a history id); a base id simply sits again."""
    from route_task import load_config
    base = base if base is not None else load_config()
    base_sha = canonical_policy_sha256(base)
    out: dict = {"readded": None}

    def build(st):
        if st.shape != "ok" or model_id not in st.generation["blocked_ids"]:
            raise SyncError(f"{model_id!r} is not revoked in the committed generation")
        gen = _next_generation(st.generation, st.generation_sha256, base_sha)
        gen["blocked_ids"] = [b for b in gen["blocked_ids"] if b != model_id]
        rec = gen["history"].get(model_id)
        if rec and rec["source"] == "probe" and rec["key"] not in gen["entries"] \
                and rec["key"] in base["models"] \
                and base["models"][rec["key"]]["id"] != model_id:
            s = st.summary(rec["probe_summary_sha256"])
            if s and s.get("id") == model_id and s.get("key") == rec["key"] \
                    and all(k in s for k in ENTRY_FIELDS):
                trial = copy.deepcopy(gen)
                e = {k: copy.deepcopy(s[k]) for k in ENTRY_FIELDS}
                e["probe_summary_sha256"] = rec["probe_summary_sha256"]
                trial["entries"][rec["key"]] = e
                trial["history"].pop(model_id)
                _, prov = model_state.effective_config(base, trial, apply_entries=True,
                                                       summary=st.summary, base_sha=base_sha)
                if rec["key"] in prov.applied:
                    gen = trial
                    out["readded"] = rec["key"]
        return gen, {}
    sha = publish_generation(state_path, build)
    return {"status": "unblocked", "model_id": model_id, "generation_sha256": sha, **out}


def _admitted_generations(state_path: Path) -> list[dict]:
    found = []
    try:
        with StateRoot.open(Path(state_path)) as root:
            names = root.listdir(f"{model_state.COMMITTED}/generations")
            for name in names:
                sha = name[:-5] if name.endswith(".json") else ""
                if not model_state.is_hex64(sha):
                    continue
                try:
                    gen = model_state.load_generation(root, sha)
                except (OSError, ValueError):
                    continue
                found.append({"generation_sha256": sha,
                              "parent_generation_sha256": gen["parent_generation_sha256"],
                              "entries": sorted(gen["entries"]),
                              "blocked_ids": list(gen["blocked_ids"])})
    except (OSError, ValueError):
        pass
    named = {g["parent_generation_sha256"] for g in found}
    for g in found:
        g["orphan"] = g["generation_sha256"] not in named
    return found


def repair(state_path: Path, *, to: str | None = None, force: bool = False) -> dict:
    """A sound pointer is left alone. A damaged one is NEVER repaired by
    choice of this tool — an older generation would silently drop later
    replacements and revocations — only by `--to <generation_sha256>`,
    which restores that generation's revocations and names the ones that
    disappear.

    `--to` needs `--force` when the current pointer is sound (a rollback),
    and when the target is an ORPHAN: a generation no other generation names
    as its parent and the pointer does not name — what a crash before the
    pointer moved, or a publication the recheck refused after `disable`,
    leaves behind. Without a pointer log the newest committed generation is
    indistinguishable from such a file, so it needs `--force` too."""
    state_path = Path(state_path)
    admission = model_state.root_admission(state_path)
    if admission["admitted"] is False:
        raise SyncError(f"the state root fails admission ({admission['detail']}); "
                        f"nothing under it was read. Fix: {admission['fix']}")
    if to is None:
        with model_state.read_state(state_path) as st:
            if st.shape != "unreadable":
                return {"status": st.shape, "generation_sha256": st.generation_sha256}
            detail = st.detail
        listing = "\n".join(
            f"  {g['generation_sha256']}  parent={g['parent_generation_sha256']}  "
            f"entries={','.join(g['entries']) or '-'}  blocked={','.join(g['blocked_ids']) or '-'}"
            + ("  [orphan: no generation names it as a parent; --force to adopt]"
               if g["orphan"] else "")
            for g in _admitted_generations(state_path)) or "  (none admitted)"
        raise SyncError(
            f"the committed pointer is damaged ({detail}). Nothing was chosen "
            f"automatically. Pick a generation with `model_sync.py repair --to "
            f"<generation_sha256>`; admitted generations:\n{listing}")
    if not model_state.is_hex64(to):
        raise SyncError("--to must be a lowercase 64-hex generation sha256")
    with StateRoot.open(state_path) as root, root.lock():
        if not root.lexists(model_state.COMMITTED):
            raise SyncError("there is no committed state to repair")
        try:
            target = model_state.load_generation(root, to)
        except (OSError, ValueError) as exc:
            raise SyncError(f"generation {to} is not admitted: {exc}") from None
        with model_state.read_state(state_path) as st:
            current = set(st.generation["blocked_ids"]) if st.shape == "ok" else None
            current_sha = st.generation_sha256 if st.shape == "ok" else None
        admitted = _admitted_generations(state_path)
        if current is not None and to != current_sha and not force:
            raise SyncError(f"the committed pointer is sound ({current_sha}); moving it "
                            f"to {to} is a rollback — repeat with --force to confirm")
        orphan = next((g["orphan"] for g in admitted if g["generation_sha256"] == to), True)
        if orphan and to != current_sha and not force:
            raise SyncError(f"generation {to} is an orphan: no generation names it as a "
                            f"parent and the pointer does not name it (a crash or a "
                            f"refused publication leaves such files). Repeat with "
                            f"--force to adopt it anyway")
        if current is None:
            current = {b for g in admitted for b in g["blocked_ids"]}
        disappearing = sorted(current - set(target["blocked_ids"]))
        model_state.write_pointer(root, to)
        root.fsync_dir(model_state.COMMITTED)
    return {"status": "repaired", "generation_sha256": to,
            "blocked_ids": list(target["blocked_ids"]),
            "disappearing_revocations": disappearing}


def disable(state_path: Path) -> dict:
    """Persistently off, then cancel every recorded in-flight probe attempt
    through `dispatch_agent.py cancel` — which confirms the process identity
    itself. No pid is ever signalled from here (pids are reused)."""
    def off(st):
        st["auto_upgrade"] = "disabled"
        return list(st["in_flight"])
    attempts = update_work_state(state_path, off, tolerate_unreadable=True)
    cancelled = []
    for a in attempts:
        try:
            proc = subprocess.run(
                [sys.executable, str(DISPATCH), "cancel", "--attempt-id", a["attempt_id"],
                 "--receipt-dir", a["receipt_dir"], "--grace-seconds", CANCEL_GRACE_SECONDS],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
            code = proc.returncode
        except (OSError, subprocess.SubprocessError):
            code = None
        cancelled.append({"attempt_id": a["attempt_id"], "cancel_exit": code})
    return {"status": "disabled", "cancelled": cancelled}


def enable(state_path: Path) -> dict:
    def on(st):
        st["auto_upgrade"] = "enabled"
    update_work_state(state_path, on, tolerate_unreadable=True)
    return {"status": "enabled"}


# --- quota (local codex rollout records; codex is never run) ----------------

def codex_sessions_dir(home: Path, env: Mapping[str, str]) -> Path:
    codex_home = env.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else Path(home) / ".codex") / "sessions"


def _last_token_count(path: Path) -> dict | None:
    try:
        fd, st = open_regular(path)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if st.st_size > QUOTA_TAIL_BYTES:
            f.seek(st.st_size - QUOTA_TAIL_BYTES)
        data = f.read(QUOTA_TAIL_BYTES)
    for line in reversed(data.decode("utf-8", errors="replace").splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        payload = ev.get("payload") if isinstance(ev, dict) else None
        if ev.get("type") == "event_msg" and isinstance(payload, dict) \
                and payload.get("type") == "token_count" \
                and isinstance((payload.get("rate_limits") or {}).get("primary"), dict) \
                and isinstance(ev.get("timestamp"), str):
            return ev
    return None


def _fullest_window(limits: Mapping) -> tuple[str, dict] | None:
    """The window (primary, or a non-null secondary) with the highest
    `used_percent`: where primary is the short window, weekly exhaustion is
    only in secondary. None when a present window is malformed."""
    best = None
    for name in ("primary", "secondary"):
        w = limits.get(name)
        if w is None and name == "secondary":
            continue
        if not isinstance(w, Mapping):
            return None
        used, resets = w.get("used_percent"), w.get("resets_at")
        if not isinstance(used, (int, float)) or isinstance(used, bool) \
                or not isinstance(resets, int) or isinstance(resets, bool):
            return None
        if best is None or used > best[1]["used_percent"]:
            best = (name, {"used_percent": used, "resets_at": resets})
    return best


def read_quota(sessions_dir: Path, now: dt.datetime | None = None) -> dict:
    """The fullest `rate_limits` window (primary, or secondary when present)
    of the last `token_count` event in the newest rollout that has one
    (`sessions/YYYY/MM/DD/rollout-*.jsonl`). A record older than 6 hours, or
    none at all, is `unknown` — and unknown defers."""
    now = _utcnow(now)
    pattern = os.path.join(glob.escape(str(sessions_dir)), "[0-9]" * 4, "[0-9]" * 2,
                           "[0-9]" * 2, "rollout-*.jsonl")
    files = sorted(glob.glob(pattern), reverse=True)[:QUOTA_MAX_FILES]
    for name in files:
        ev = _last_token_count(Path(name))
        if ev is None:
            continue
        try:
            ts = _parse_time(ev["timestamp"])
        except ValueError:
            continue
        window = _fullest_window(ev["payload"]["rate_limits"])
        if window is None:
            return {"status": "unknown", "reason": "shape", "source": name}
        which, w = window
        used, resets = w["used_percent"], w["resets_at"]
        out = {"source": name, "event_timestamp": ev["timestamp"], "used_percent": used,
               "resets_at": resets, "window": which,
               "resets_at_iso": dt.datetime.fromtimestamp(resets, dt.timezone.utc).isoformat()}
        if now - ts > QUOTA_STALE_AFTER:
            return {"status": "unknown", "reason": "stale", **out}
        return {"status": "ok", "reason": None, **out}
    return {"status": "unknown", "reason": "no_record", "source": str(sessions_dir)}


def quota_defers(q: Mapping) -> bool:
    return q.get("status") != "ok" or q.get("used_percent", 100) >= QUOTA_DEFER_PERCENT


def quota_retry_after(q: Mapping, now: dt.datetime) -> dict:
    if q.get("status") == "ok" and isinstance(q.get("resets_at"), int):
        return {"kind": "time",
                "at": dt.datetime.fromtimestamp(q["resets_at"], dt.timezone.utc).isoformat()}
    return {"kind": "time", "at": (now + QUOTA_UNKNOWN_DEFER).isoformat()}


# --- candidates this machine may probe now ------------------------------------

def grok_models_text(env: Mapping[str, str]) -> str | None:
    exe = shutil.which("grok", path=env.get("PATH"))
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, *OFFLINE_FLAGS["grok"], "models"],
                              capture_output=True, text=True,
                              timeout=CLI_VERSION_TIMEOUT * 2, env=dict(env),
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def load_catalogs(home: Path, versions: Mapping[str, str | None], *,
                  grok_text: str | None = None) -> dict[str, Catalog]:
    paths = default_catalog_paths(home)
    return {"openai": read_codex_catalog(paths["openai"]),
            "xai": read_grok_catalog(paths["xai"], grok_text),
            "claude": read_claude_catalog(paths["claude"], versions.get("claude"))}


def tick_hash(base_sha: str, generation_sha: str | None, catalogs: Mapping[str, Catalog],
              versions: Mapping[str, str | None]) -> str:
    """Lineage-relevant catalog content + the three CLI banners (+ the base
    and the committed generation): a CLI update alone re-runs the tick."""
    return canonical_policy_sha256({
        "base": base_sha, "generation": generation_sha, "versions": dict(versions),
        "catalogs": {f: {"status": c.status, "reason": c.reason, "models": c.models,
                         "retirements": c.retirements} for f, c in sorted(catalogs.items())}})


def _negative_expired(neg: Mapping, versions: Mapping, catalogs: Mapping,
                      now: dt.datetime) -> bool:
    ra = neg.get("retry_after") or {}
    kind = ra.get("kind")
    try:
        if kind == "time":
            return now >= _parse_time(ra["at"])
        if kind == "cli_version_change":
            return versions.get(FAMILY_CLI.get(neg.get("family"))) != ra.get("cli_version")
        if kind == "catalog_change":
            cat = catalogs.get(neg.get("family"))
            return cat is None or cat.sha256 != ra.get("catalog_sha256")
    except (KeyError, ValueError, TypeError):
        return True
    return True


def _expired_time_deferrals(work: Mapping, now: dt.datetime) -> bool:
    for neg in (work.get("negatives") or {}).values():
        ra = neg.get("retry_after") or {}
        if ra.get("kind") == "time":
            try:
                if now >= _parse_time(ra["at"]):
                    return True
            except (KeyError, ValueError, TypeError):
                return True
    return False


def plan(*, view: CommittedView, catalogs: Mapping[str, Catalog], versions: Mapping,
         work: Mapping, now: dt.datetime, keys=None, interval: bool = True):
    """Candidates to probe now, and why every other lineage row is skipped
    (no candidate, revoked, a live deferral, the 24-hour lineage interval)."""
    found = find_candidates(view.config["models"], catalogs)
    blocked = set((view.generation or {}).get("blocked_ids") or [])
    eligible, skipped = [], {}
    for key in sorted(found):
        c = found[key]
        if keys is not None and key not in keys:
            continue
        if c["status"] == "alias_probe":
            # No Claude catalog: the row's alias is probed instead (DD-A4);
            # a deferral recorded against the alias itself holds it back.
            row = view.config["models"][key]
            c = {**c, "id": None, "alias": row["lineage"]["catalog_name"].lower()}
            neg = (work.get("negatives") or {}).get(key)
            if neg and neg.get("id") is None and neg.get("alias") == c["alias"] \
                    and not _negative_expired(neg, versions, catalogs, now):
                skipped[key] = f"deferred:{neg.get('reason')}"
                continue
        elif c["status"] != "candidate":
            skipped[key] = c["status"]
            continue
        elif c["id"] in blocked:
            skipped[key] = "blocked"
            continue
        neg = (work.get("negatives") or {}).get(key)
        if c["id"] is not None and neg and neg.get("id") == c["id"] \
                and not _negative_expired(neg, versions, catalogs, now):
            skipped[key] = f"deferred:{neg.get('reason')}"
            continue
        last = (work.get("last_probe") or {}).get(key)
        if interval and last and now - _parse_time(last) < LINEAGE_INTERVAL:
            skipped[key] = "interval"
            continue
        eligible.append({**c, "key": key})
    return eligible, skipped


# --- tick ------------------------------------------------------------------------

def _spawn_detached(env: Mapping[str, str], digest: str | None = None) -> None:
    extra = ["--tick-hash", digest] if digest else []
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", *extra],
                     env=dict(env), cwd="/", stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def tick(*, env: Mapping[str, str], home: Path | None = None, now: dt.datetime | None = None,
         detach: bool = True, spawn=None, versions=None) -> dict:
    """Offline and inference-free. Same catalogs + CLI banners as the last
    COMPLETED tick and no expired deferral -> return at once. Otherwise, when
    a candidate is eligible, start `run` (detached: a new session, stdio on
    /dev/null) and return.

    The hash is a completion record: stored here only when nothing is
    eligible, and otherwise by the run this tick starts once it has probed
    everything it planned (`run --tick-hash`). A run that dies first, or is
    cut short by the budget, leaves the tick due. A run already holding
    `work/run.lock` makes this tick `busy` and stores nothing."""
    if autoupgrade_env_off(env):
        return {"status": "disabled", "reason": "DEEP_MODEL_ROUTER_AUTOUPGRADE=0"}
    from route_task import load_config
    home, now = _home(env, home), _utcnow(now)
    state_path = model_state.state_root_path(env, home)
    with StateRoot.open(state_path, create=True) as root:
        work = read_work_state(root)
    if work["auto_upgrade"] == "disabled":
        return {"status": "disabled", "reason": "model_sync.py disable"}
    base = load_config()
    base_sha = canonical_policy_sha256(base)
    view = committed_view(state_path, base, base_sha=base_sha)
    versions = versions if versions is not None else cli_versions(env)
    catalogs = load_catalogs(home, versions)
    digest = tick_hash(base_sha, view.generation_sha256, catalogs, versions)
    if digest == work["tick_hash"] and not _expired_time_deferrals(work, now):
        return {"status": "unchanged"}
    eligible, skipped = plan(view=view, catalogs=catalogs, versions=versions, work=work,
                             now=now)
    if not eligible:
        _store_tick_hash(state_path, digest)
        return {"status": "no_candidates", "skipped": skipped}
    if _run_lock_held(state_path):
        return {"status": "busy"}
    keys = [c["key"] for c in eligible]
    if spawn is not None:
        spawn(keys)
    elif detach:
        _spawn_detached(env, digest)
    else:
        return {"status": "ran", "run": run(env=env, home=home, now=now, tick_hash=digest)}
    return {"status": "spawned", "keys": keys, "tick_hash": digest}


def _store_tick_hash(state_path: Path, digest: str) -> None:
    def store(st):
        st["tick_hash"] = digest
    update_work_state(state_path, store)


def _run_lock_held(state_path: Path) -> bool:
    with StateRoot.open(state_path, create=True) as root:
        try:
            with root.lock(timeout=0, relpath=RUN_LOCK):
                return False
        except StateError:
            return True


# --- run -------------------------------------------------------------------------

def _disabled_now(state_path: Path, env: Mapping[str, str]) -> bool:
    if autoupgrade_env_off(env):
        return True
    try:
        return _peek_work_state(state_path)["auto_upgrade"] == "disabled"
    except (OSError, ValueError):
        return True


def _clear_in_flight(state_path: Path) -> None:
    def clear(st):
        st["in_flight"] = []
    try:
        update_work_state(state_path, clear, tolerate_unreadable=True)
    except (OSError, ValueError):
        pass


def _record(state_path: Path, key: str, cand: Mapping, *, outcome: str, reason: str | None,
            retry_after: Mapping | None, summary_sha: str | None, inferences: int,
            now: dt.datetime) -> None:
    def rec(st):
        if inferences:
            st["last_probe"][key] = now.isoformat()
        st["recent"] = ([{"key": key, "id": cand["id"], "outcome": outcome, "reason": reason,
                          "summary_sha256": summary_sha, "at": now.isoformat()}]
                        + list(st["recent"]))[:RECENT_KEEP]
        if outcome in ("pass", "none"):
            st["negatives"].pop(key, None)
        elif outcome == "held":
            pass
        else:
            st["negatives"][key] = {"id": cand["id"], "family": cand["family"],
                                    "outcome": outcome, "reason": reason,
                                    "retry_after": retry_after, "summary_sha256": summary_sha,
                                    "at": now.isoformat()}
            if cand.get("alias"):
                st["negatives"][key]["alias"] = cand["alias"]
    update_work_state(state_path, rec)


def run(*, env: Mapping[str, str], home: Path | None = None, now: dt.datetime | None = None,
        keys=None, budget: int = INFERENCE_BUDGET, probe=None, versions=None,
        grok_text: Any = ..., tick_hash: str | None = None, alias_probe=None) -> dict:
    """Probe every eligible candidate within the inference budget and
    publish the passing ones. One run at a time (`work/run.lock`); an
    explicit `keys` list is a manual request and skips the 24-hour lineage
    interval (never a deferral or a revocation). `tick_hash` is the digest of
    the tick that started this run: stored as that tick's completion record
    once every planned candidate was handled (none cut by the budget)."""
    if autoupgrade_env_off(env):
        return {"status": "disabled", "reason": "DEEP_MODEL_ROUTER_AUTOUPGRADE=0"}
    home, now = _home(env, home), _utcnow(now)
    state_path = model_state.state_root_path(env, home)
    with StateRoot.open(state_path, create=True) as root:
        lock = root.lock(timeout=0, relpath=RUN_LOCK)
        try:
            lock.__enter__()
        except StateError:
            return {"status": "busy"}
        try:
            return _run_locked(root, state_path, env=env, home=home, now=now, keys=keys,
                               budget=budget, probe=probe or probe_candidate,
                               versions=versions, grok_text=grok_text,
                               tick_hash=tick_hash,
                               alias_probe=alias_probe or probe_alias)
        finally:
            lock.__exit__(None, None, None)


def _run_locked(root, state_path, *, env, home, now, keys, budget, probe, versions,
                grok_text, tick_hash=None, alias_probe=None) -> dict:
    from route_task import load_config
    if read_work_state(root)["auto_upgrade"] == "disabled":
        return {"status": "disabled", "reason": "model_sync.py disable"}
    work = read_work_state(root)
    base = load_config()
    base_sha = canonical_policy_sha256(base)
    view = committed_view(state_path, base, base_sha=base_sha)
    versions = versions if versions is not None else cli_versions(env)
    text = grok_models_text(env) if grok_text is ... else grok_text
    catalogs = load_catalogs(home, versions, grok_text=text)
    eligible, skipped = plan(view=view, catalogs=catalogs, versions=versions, work=work,
                             now=now, keys=keys, interval=keys is None)
    rep = {"status": "ok", "probed": [], "passed": [], "deferred": [], "failed": [],
           "over_budget": [], "interval": sorted(k for k, r in skipped.items() if r == "interval"),
           "skipped": skipped, "inferences": 0, "generation_sha256": None, "aliases": {}}
    blocked = set((view.generation or {}).get("blocked_ids") or [])

    def register(attempt: str, receipt_dir: Path) -> None:
        if autoupgrade_env_off(env):
            raise Disabled()

        def add(st):
            if st["auto_upgrade"] == "disabled":
                raise Disabled()
            st["in_flight"].append({"attempt_id": attempt, "receipt_dir": str(receipt_dir)})
        update_work_state(state_path, add)

    passing = []
    quota = None
    scratch = Path(tempfile.mkdtemp(prefix="dmr-probe-"))
    try:
        for cand in eligible:
            key, fam = cand["key"], cand["family"]
            aliased = cand["status"] == "alias_probe"
            need = MAX_INFERENCES[fam] + (ALIAS_INFERENCES if aliased else 0)
            if rep["inferences"] + need > budget:
                rep["over_budget"].append(key)
                continue
            ctx = ProbeContext(now=now, cli_version=versions.get(FAMILY_CLI[fam]))
            if aliased:
                found = alias_probe(key=key, candidate=cand, base=base,
                                    state_root=state_path, env=env, scratch=scratch,
                                    ctx=ctx, on_attempt=register)
                _clear_in_flight(state_path)
                if _disabled_now(state_path, env):
                    raise Disabled()
                rep["inferences"] += found["inferences"]
                rep["aliases"][key] = found["outcome"]
                neg = (read_work_state(root).get("negatives") or {}).get(key)
                if found["outcome"] == "candidate" and found["id"] not in blocked and not (
                        neg and neg.get("id") == found["id"]
                        and not _negative_expired(neg, versions, catalogs, now)):
                    cand = {**cand, "status": "candidate", "id": found["id"],
                            "efforts": [], "catalog_sha256": catalogs[fam].sha256}
                else:
                    # a served successor that is revoked, or deferred already,
                    # is held: recorded (the interval counts) without touching
                    # the deferral that holds it
                    _record(state_path, key, {**cand, "id": None}, now=now,
                            outcome=found["outcome"] if found["outcome"] != "candidate"
                            else "held", reason=found["reason"],
                            retry_after=found["retry_after"], summary_sha=None,
                            inferences=found["inferences"])
                    if found["outcome"] == "failed":
                        rep["failed"].append(key)
                    continue
            if fam == "openai":
                if quota is None:
                    quota = read_quota(codex_sessions_dir(home, env), now)
                    rep["quota"] = quota
                if quota["status"] == "ok":
                    ctx = ctx._replace(quota_resets_at=quota["resets_at"])
                if quota_defers(quota):
                    _record(state_path, key, cand, outcome="deferred", reason="quota",
                            retry_after=quota_retry_after(quota, now), summary_sha=None,
                            inferences=0, now=now)
                    rep["deferred"].append(key)
                    continue
            result = probe(key=key, candidate=cand, base=base, state_root=state_path,
                           env=env, catalog=catalogs[fam], current_id=cand["from_id"],
                           superseded=superseded_for(view.generation, key, base),
                           scratch=scratch, ctx=ctx, on_attempt=register)
            _clear_in_flight(state_path)
            if _disabled_now(state_path, env):
                raise Disabled()
            s = result["summary"]
            rep["inferences"] += result["inferences"]
            rep["probed"].append(key)
            _record(state_path, key, cand, outcome=s["outcome"], reason=s.get("reason"),
                    retry_after=s.get("retry_after"), summary_sha=result["summary_sha256"],
                    inferences=result["inferences"], now=now)
            if s["outcome"] == "pass":
                passing.append((key, s, result["summary_sha256"]))
                rep["passed"].append(key)
            else:
                rep["deferred" if s["outcome"] == "deferred" else "failed"].append(key)
        if passing:
            rep["generation_sha256"] = publish_results(
                state_path, base, passing, recheck=auto_upgrade_recheck(env))
        if tick_hash is not None and not rep["over_budget"]:
            _store_tick_hash(state_path, tick_hash)
    except Disabled:
        rep["status"] = "disabled_during_run"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        _clear_in_flight(state_path)
    return rep


# --- status ------------------------------------------------------------------------

def status(*, env: Mapping[str, str], home: Path | None = None,
           now: dt.datetime | None = None, versions=None) -> dict:
    from route_task import load_config
    home, now = _home(env, home), _utcnow(now)
    state_path = model_state.state_root_path(env, home)
    base = load_config()
    base_sha = canonical_policy_sha256(base)
    try:
        work = _peek_work_state(state_path)
    except (OSError, ValueError) as exc:
        work = default_work_state()
        work["unreadable"] = str(exc)
    out: dict[str, Any] = {
        "state_dir": str(state_path),
        "state_root": model_state.root_admission(state_path),
        "auto_upgrade": "disabled" if autoupgrade_env_off(env)
        or work["auto_upgrade"] == "disabled" else "enabled",
        "auto_upgrade_env_off": autoupgrade_env_off(env)}
    try:
        view = committed_view(state_path, base, base_sha=base_sha)
    except SyncError as exc:
        view = CommittedView("unreadable", config=base)
        out["committed"] = {"shape": "unreadable", "detail": str(exc)}
    if view.shape != "unreadable":
        entries = {}
        prov = view.provenance
        for key, e in sorted(((view.generation or {}).get("entries") or {}).items()):
            state = "applied" if prov and key in prov.applied else next(
                (f"noop:{n['reason']}" for n in (prov.noop if prov else []) if n["key"] == key),
                next((f"rejected:{r['reason']}" for r in (prov.rejected if prov else [])
                      if r["key"] == key), "unknown"))
            entries[key] = {"id": e["id"], "from_id": e["from_id"],
                            "superseded": e["superseded"], "state": state,
                            "probe_summary_sha256": e["probe_summary_sha256"]}
        out["committed"] = {"shape": view.shape, "generation_sha256": view.generation_sha256,
                            "entries": entries,
                            "blocked_ids": list((view.generation or {}).get("blocked_ids") or []),
                            "history_ids": sorted((view.generation or {}).get("history") or {})}
    versions = versions if versions is not None else cli_versions(env)
    catalogs = load_catalogs(home, versions)
    out["cli_versions"] = dict(versions)
    out["catalogs"] = {f: {"status": c.status, "reason": c.reason, "sha256": c.sha256}
                       for f, c in sorted(catalogs.items())}
    out["retirement_notices"] = retirement_notices(view.config["models"], catalogs)
    eligible, skipped = plan(view=view, catalogs=catalogs, versions=versions, work=work, now=now)
    out["candidates"] = {c["key"]: c["id"] or f"alias:{c['alias']}" for c in eligible}
    out["skipped"] = skipped
    out["deferred"] = {k: {**n, "expired": _negative_expired(n, versions, catalogs, now)}
                       for k, n in sorted(work["negatives"].items())}
    summaries = Path(state_path) / SUMMARY_PREFIX / "summaries"
    out["recent"] = [{**r, "summary_path": str(summaries / f"{r['summary_sha256']}.json")
                      if r.get("summary_sha256") else None} for r in work["recent"]]
    out["makers"] = work["makers"]
    out["in_flight"] = work["in_flight"]
    return out


# --- CLI ---------------------------------------------------------------------------

def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _confirm_on_tty(plan_: Mapping) -> bool:
    print("probe-maker is ATTENDED: it seats a write-capable CLI once, in a "
          "disposable directory, to re-verify the maker seat for one id.")
    for line in plan_.get("lines", []):
        print(f"  {line}")
    try:
        answer = input("Type y to run this maker probe: ")
    except EOFError:
        return False
    return answer.strip() == "y"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="model_sync.py",
        description="Follow model lineages: detect, probe, publish, revert (design 2026-09-25).")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick", help="offline check; starts `run` when a candidate is due")
    t.add_argument("--detach", action="store_true", help="start run in a new session and return")
    t.add_argument("--json", action="store_true", help="print the tick result")
    r = sub.add_parser("run", help="probe eligible candidates and publish passing ones")
    r.add_argument("--key", action="append", dest="keys", metavar="KEY",
                   help="only this registry key (repeatable); skips the 24h interval")
    r.add_argument("--tick-hash", default=None, metavar="SHA256",
                   help=argparse.SUPPRESS)      # set by `tick --detach` only
    sub.add_parser("status", help="committed generation, deferrals, notices, recent summaries")
    rv = sub.add_parser("revert", help="drop a key's overlay entry and revoke its id")
    rv.add_argument("key")
    ub = sub.add_parser("unblock", help="lift a revocation")
    ub.add_argument("model_id")
    rp = sub.add_parser("repair", help="check the pointer; --to moves it to a named generation")
    rp.add_argument("--to", metavar="GENERATION_SHA256")
    rp.add_argument("--force", action="store_true",
                    help="with --to: replace a sound pointer, or adopt an orphan generation")
    sub.add_parser("disable", help="turn auto-upgrade off and cancel in-flight probes")
    sub.add_parser("enable", help="turn auto-upgrade back on")
    sub.add_parser("quota", help="codex rate-limit usage from local rollout records")
    pm = sub.add_parser("probe-maker", help="ATTENDED maker-seat probe for one key (needs a TTY)")
    pm.add_argument("key")
    pm.add_argument("--runtime", default=None, help="host runtime whose write_verified "
                    "direction to probe (default: the first that has one)")
    _add_promote_parser(sub)
    args = p.parse_args(argv)

    env = dict(os.environ)
    home = _home(env, None)
    state_path = model_state.state_root_path(env, home)
    if args.cmd == "tick":
        try:
            out = tick(env=env, home=home, detach=args.detach)
        except Exception as exc:  # noqa: BLE001 — a session hook never fails the session
            out = {"status": "error", "detail": f"{exc.__class__.__name__}: {exc}"}
        if args.json:
            _print(out)
        return 0
    if args.cmd == "probe-maker" and not sys.stdin.isatty():
        print("model_sync: probe-maker is attended and needs a TTY on stdin to "
              "confirm; refusing", file=sys.stderr)
        return 2
    try:
        if args.cmd == "run":
            from route_task import load_config
            base = load_config()
            for k in args.keys or []:
                if "lineage" not in base["models"].get(k, {}):
                    print(f"model_sync: {k!r} is not a lineage row", file=sys.stderr)
                    return 2
            if args.tick_hash is not None and not model_state.is_hex64(args.tick_hash):
                print("model_sync: --tick-hash must be 64 lowercase hex", file=sys.stderr)
                return 2
            _print(run(env=env, home=home, keys=args.keys, tick_hash=args.tick_hash))
        elif args.cmd == "status":
            _print(status(env=env, home=home))
        elif args.cmd == "revert":
            _print(revert(state_path, args.key))
        elif args.cmd == "unblock":
            _print(unblock(state_path, args.model_id))
        elif args.cmd == "repair":
            _print(repair(state_path, to=args.to, force=args.force))
        elif args.cmd == "disable":
            _print(disable(state_path))
        elif args.cmd == "enable":
            _print(enable(state_path))
        elif args.cmd == "quota":
            _print(read_quota(codex_sessions_dir(home, env)))
        elif args.cmd == "probe-maker":
            import probe_maker
            res = probe_maker.probe_maker(key=args.key, env=env, home=home, now=None,
                                          state_path=state_path, confirm=_confirm_on_tty,
                                          runtime=args.runtime)
            _print(res)
            return {"pass": 0, "aborted": 1, "failed": 1}.get(res["status"], 2)
        elif args.cmd == "promote":
            return _promote_cli(args, env=env, home=home, state_path=state_path)
    except SyncError as exc:
        print(f"model_sync: {exc}", file=sys.stderr)
        return 1
    except (StateError, OSError) as exc:
        print(f"model_sync: state error: {exc}", file=sys.stderr)
        return 1
    return 0


# ==========================================================================
# promote (DD-A7): one overlay entry -> a repository id bump
# ==========================================================================

import shlex  # noqa: E402

REPO_CONFIG = Path("skills/model-router/config/model-routing.yaml")
REPO_SUCCESSION = Path("skills/model-router/tests/fixtures/id-succession.json")
PRICE_RATES = ("input", "output", "cached_input", "cache_write")
RUNTIME_OF_FAMILY = {"claude": "claude_code", "openai": "codex", "xai": "grok"}


def parse_price(spec: str | None) -> dict:
    """`input=…,output=…,cached_input=…,source=https://…,verified_on=YYYY-MM-DD`
    (+ optional `cache_write=…`, `max_age_days=N`). Nothing is ever guessed:
    a missing or malformed field stops promote."""
    if not spec:
        raise SyncError("--price is required (input=,output=,cached_input=,source=,"
                        "verified_on=) — promote never invents a price")
    out: dict[str, Any] = {}
    for part in spec.split(","):
        k, sep, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if not sep or not k or k in out:
            raise SyncError(f"--price: malformed or repeated field {part!r}")
        if k in PRICE_RATES:
            try:
                num = float(v)
            except ValueError:
                raise SyncError(f"--price: {k} must be a number") from None
            if not num >= 0 or num == float("inf"):
                raise SyncError(f"--price: {k} must be a finite number >= 0")
            out[k] = num
        elif k == "source":
            if not v.startswith("https://"):
                raise SyncError("--price: source must be an https:// URL")
            out[k] = v
        elif k == "verified_on":
            try:
                dt.date.fromisoformat(v)
            except ValueError:
                raise SyncError("--price: verified_on must be YYYY-MM-DD") from None
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise SyncError("--price: verified_on must be YYYY-MM-DD")
            out[k] = v
        elif k == "max_age_days":
            if not v.isdigit() or not 0 <= int(v) <= 30:
                raise SyncError("--price: max_age_days must be an integer 0..30")
            out[k] = int(v)
        else:
            raise SyncError(f"--price: unknown field {k!r}")
    missing = [k for k in ("input", "output", "cached_input", "source", "verified_on")
               if k not in out]
    if missing:
        raise SyncError(f"--price: missing {', '.join(missing)}")
    return out


def _q(value: Any) -> str:
    """A YAML scalar: JSON strings are YAML double-quoted strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value)


def _flow(mapping: Mapping) -> str:
    return "{ " + ", ".join(f"{k}: {_q(v)}" for k, v in mapping.items()) + " }"


def _folded(text: str, indent: int) -> list[str]:
    pad = " " * indent
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > 76 - indent:
            lines.append(pad + cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(pad + cur)
    return lines


def _row_block(lines: list[str], key: str) -> tuple[int, int]:
    """[header, end) of `key`'s row in `models:` (end = the next two-space-
    indented line, trailing blank lines excluded)."""
    top = next((i for i, l in enumerate(lines) if l.rstrip() == "models:"), None)
    if top is None:
        raise SyncError("config has no models: section")
    head = next((i for i in range(top + 1, len(lines))
                 if lines[i].rstrip() in (f"  {key}:", f'  "{key}":')), None)
    if head is None:
        raise SyncError(f"config has no row block for {key!r}")
    end = next((i for i in range(head + 1, len(lines)) if re.match(r"^\S", lines[i])
                or re.match(r"^  \S", lines[i])), len(lines))
    while end > head + 1 and not lines[end - 1].strip():
        end -= 1
    return head, end


def _sub_block(block: list[str], field_: str) -> tuple[int, int] | None:
    """[start, end) of a four-space field and its deeper continuation lines."""
    start = next((i for i, l in enumerate(block) if re.match(rf"^    {field_}:", l)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(block) and (not block[end].strip()
                                or len(block[end]) - len(block[end].lstrip()) > 4):
        end += 1
    return start, end


def _ledger_end(lines: list[str]) -> int:
    top = next((i for i, l in enumerate(lines) if l.rstrip() == "verification_ledger:"), None)
    if top is None:
        raise SyncError("config has no verification_ledger: section")
    end = next((i for i in range(top + 1, len(lines)) if re.match(r"^\S", lines[i])),
               len(lines))
    while end > top + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def dump_succession(doc: Mapping) -> str:
    """tests/fixtures/id-succession.json in its own layout: two-space
    nesting, id lists inline."""
    def val(v, indent):
        if isinstance(v, dict):
            if not v:
                return "{}"
            pad = " " * (indent + 2)
            body = ",\n".join(f"{pad}{json.dumps(k)}: {val(x, indent + 2)}" for k, x in v.items())
            return "{\n" + body + "\n" + " " * indent + "}"
        if isinstance(v, list):
            return "[" + ", ".join(json.dumps(x) for x in v) + "]"
        return json.dumps(v)
    return val(dict(doc), 0) + "\n"


def _history_row(key: str, mid: str, rec: Mapping, price_lines: list[str] | None) -> list[str]:
    out = ["",
           "  # History row added by `model_sync.py promote` (design 2026-09-25 DD-A3,",
           "  # DD-A7): a superseded id of this key, valid history input, never seated.",
           f'  "{key}@{mid}":', f"    id: {mid}", f"    history_of: {key}",
           f"    family: {rec['family']}", f"    capability_tier: {rec['capability_tier']}"]
    if rec.get("effort_ceiling") is not None:
        out.append(f"    effort_ceiling: {rec['effort_ceiling']}")
    if rec.get("effort_map"):
        out.append(f"    effort_map: {_flow(rec['effort_map'])}")
    out += ["    dispatchable: false", "    verified: true"]
    return out + (price_lines or [])


def _ledger_row(item: str, fields: list[tuple[str, Any]], evidence: str) -> list[str]:
    out = ["", f"    - item: {_q(item)}"]
    for k, v in fields:
        if v is None:
            continue
        if isinstance(v, list):
            out += [f"      {k}:", *(f"        - {_q(x)}" for x in v)]
        else:
            out.append(f"      {k}: {v if k == 'status' else _q(v)}")
    return out + ["      evidence: >", *_folded(evidence, 8)]


def _child_argv_text(argv: list[str] | None) -> str | None:
    if not argv:
        return None
    return shlex.join(argv[argv.index("--") + 1:] if "--" in argv else argv)


def _superseded_items(key: str, ids: list[str], ledger: list) -> list[str]:
    """The existing ledger rows the quality disclosure points back at: every
    row whose item names a superseded id as a whole token, and an earlier
    disclosure for this key (a second bump chains to the first). The docs
    guard requires a non-empty list of items that exist; an empty one means
    there is nothing on record to call inherited, which promote does not
    invent."""
    def names(mid: str, text: str) -> bool:
        return re.search(r"(^|[^A-Za-z0-9._-])" + re.escape(mid)
                         + r"([^A-Za-z0-9._-]|$)", text) is not None
    out = []
    for row in ledger:
        item = str(row.get("item", ""))
        if any(names(mid, item) for mid in ids) or (
                row.get("status") == "quality_inherited_not_remeasured" and key in item):
            if item not in out:
                out.append(item)
    if not out:
        raise SyncError(f"{key}: no ledger row names {ids}; the quality disclosure would "
                        f"point at nothing — record the superseded evidence first")
    return out


def _ledger_rows(key: str, e: Mapping, summary: Mapping, price: Mapping,
                 maker: Mapping | None, base_row: Mapping, ledger: list) -> list[str]:
    new, old = e["id"], e["from_id"]
    probes = list(summary.get("probes") or [])
    p1 = next((p for p in probes if p.get("gate") == "P1"), {})
    codex = summary.get("served_basis") == "id_accepted"
    served = ("the CLI header reported the requested id (id accepted, not served-model "
              "attestation)" if codex else
              f"served_models {summary.get('served_models')} are served forms of the id")
    tokens = []
    for p in probes:
        if p.get("model_id") == new and p.get("state") == "SUCCEEDED" \
                and p.get("effort_native") not in tokens:
            tokens.append(p["effort_native"])
    it = summary.get("input_tokens") or {}
    rows = _ledger_row(f"{new} model id", [
        ("status", "verified"), ("probed_on", summary.get("date")),
        ("attempt_id", p1.get("attempt_id")), ("cli_version", summary.get("cli_version")),
        ("argv", _child_argv_text(p1.get("argv")))],
        f"model_sync contained read-only probe of {key} ({old} -> {new}): P1 smoke "
        f"answered pong with termination confirmed; P2 {served}; P3 boot input "
        f"{it.get('candidate')} vs {it.get('current')} for the current id "
        f"({summary.get('p3_basis')}). Quality is not measured by this row.")
    rows += _ledger_row(f"{new} reasoning-effort values", [
        ("status", "verified"), ("probed_on", summary.get("date"))],
        f"Live-probed tokens for {new}: {', '.join(tokens) or 'none'} (accepted). "
        f"Other levels were not live-probed for this id; the ceiling is unchanged "
        f"from {old} ({base_row.get('effort_ceiling') or 'none'}).")
    rates = ", ".join(f"{k} {price[k]}" for k in PRICE_RATES if k in price)
    rows += _ledger_row(f"{new} pricing and context window", [
        ("status", "documented"), ("probed_on", price["verified_on"]),
        ("source", price["source"])],
        f"Reference API rates per MTok read from the source on {price['verified_on']}: "
        f"{rates}. A reference record, not the marginal cost of a subscription "
        f"dispatch. The context window was not re-recorded by promote.")
    rows += _ledger_row(f"{key} quality evidence after the {new} id bump", [
        ("status", "quality_inherited_not_remeasured"),
        ("supersedes", _superseded_items(key, [old, *e.get("superseded", [])], ledger))],
        f"capability_tier {base_row['capability_tier']} is inherited from the {key} "
        f"lineage; no quality measurement of {new} has been run. Earlier quality "
        f"rows about {old} describe that id, not this one.")
    if maker and maker.get("outcome") == "pass" and maker.get("id") == new:
        art = maker.get("artifact") or {}
        rows += _ledger_row(f"{new} maker seat", [
            ("status", "verified"), ("probed_on", maker.get("date")),
            ("attempt_id", maker.get("attempt_id")), ("cli_version", maker.get("cli_version")),
            ("argv", _child_argv_text(maker.get("argv")))],
            f"Attended model_sync probe-maker through {maker.get('transport_id')} "
            f"({maker.get('recipe_key')}): the maker recipe with -m {new}, in a disposable "
            f"single-linked child cwd, ended {maker.get('state')}; the written file's "
            f"content hash {art.get('sha256')} equals the expected token's. Receipt "
            f"sha256 {maker.get('receipt_sha256')}.")
    else:
        rows += _ledger_row(f"{new} maker seat", [("status", "maker_not_reprobed")],
                            f"No attended probe-maker summary for {new}. Containment of the "
                            f"write seat is the transport recipe's and does not depend on the "
                            f"id; whether this id's maker seat works was not re-probed.")
    return rows


def _maker_summary(state_path: Path, key: str, model_id: str) -> dict | None:
    try:
        work = _peek_work_state(state_path)
        rec = work["makers"].get(key) or {}
        if rec.get("id") != model_id or not model_state.is_hex64(rec.get("summary_sha256")):
            return None
        with StateRoot.open(Path(state_path)) as root:
            value, got = root.read_json_with_sha(
                f"{MAKERS_PREFIX}/{rec['summary_sha256']}.json")
        return value if got == rec["summary_sha256"] and isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prior_failure_hint(cfg: dict, history_keys: list[str]) -> dict:
    from route_task import Policy, Task, route
    policy = Policy.of(cfg)
    out = {}
    for hk in history_keys:
        row = cfg["models"][hk]
        task = Task(task_class="IMPLEMENTATION", complexity=1, uncertainty=1, blast_radius=1,
                    reversibility=1, runtime=RUNTIME_OF_FAMILY.get(row["family"], "claude_code"),
                    prior_failures=1, prior_models=[row["id"]])
        r = route(task, cfg)
        code = 1 if r["terminal"] else policy.human_gate_exit_status \
            if r["requires_human_confirmation"] else 4 if r["human_confirmation_deferred"] else 0
        out[hk] = {"router_exit_now": code, "terminal": r["terminal"]}
    return out


def promote(*, repo: Path, key: str, price: str | None, state_path: Path) -> dict:
    """Move the applied overlay entry for `key` into the repository: the row's
    id and price, history rows from the generation's snapshots, ledger rows
    from the summaries, the id-succession chain. Refuses an effort change
    (1.16.0), a missing price, and an entry that does not apply. Never writes
    `prior_failure_exit` (hint only). Comments and every byte outside the row
    block, its appended history rows and the appended ledger rows stay."""
    import yaml
    from route_task import Policy
    price_rec = parse_price(price)
    repo = Path(repo)
    cfg_path, succ_path = repo / REPO_CONFIG, repo / REPO_SUCCESSION
    text = cfg_path.read_text()
    base = yaml.safe_load(text)
    base_sha = canonical_policy_sha256(base)
    with model_state.read_state(Path(state_path)) as st:
        if st.shape != "ok":
            raise SyncError(f"no committed generation to promote from ({st.shape})")
        gen = st.generation
        _, prov = model_state.effective_config(base, gen, apply_entries=True,
                                               summary=st.summary, base_sha=base_sha)
        if key not in prov.applied:
            why = next((f"noop:{n['reason']}" for n in prov.noop if n["key"] == key),
                       next((f"rejected:{r['reason']}" for r in prov.rejected
                             if r["key"] == key), "no entry"))
            raise SyncError(f"{key!r} has no applied overlay entry against this repo ({why})")
        e = gen["entries"][key]
        summary = st.summary(e["probe_summary_sha256"])
    row = base["models"][key]
    if dict(e["effort_map"]) != dict(row.get("effort_map") or {}) \
            or e["effort_ceiling"] != row.get("effort_ceiling"):
        raise SyncError(f"{key}: the overlay changes effort_map/effort_ceiling; promote in "
                        f"1.16.0 moves ids only (an effort change is a policy change)")
    chain = [e["from_id"], *e["superseded"]]
    missing = [m for m in chain if m not in gen["history"]]
    if missing:
        raise SyncError(f"{key}: no history snapshot for {missing}")

    lines = text.split("\n")
    head, end = _row_block(lines, key)
    block = lines[head:end]
    idl = next((i for i, l in enumerate(block) if re.match(r"^    id: ", l)), None)
    if idl is None:
        raise SyncError(f"{key}: row block has no id line")
    old_price = _sub_block(block, "price_per_mtok")
    old_price_lines = block[old_price[0]:old_price[1]] if old_price else None
    while old_price_lines and not old_price_lines[-1].strip():
        old_price_lines.pop()
    new_block = list(block)
    new_block[idl] = f"    id: {e['id']}"
    price_lines = ["    price_per_mtok:"] + [
        f"      {k}: {_q(price_rec[k])}" for k in ("verified_on", "max_age_days", "source",
                                                   *PRICE_RATES) if k in price_rec]
    if old_price:
        new_block[old_price[0]:old_price[0] + len(old_price_lines)] = price_lines
    else:
        new_block += price_lines
    history_keys, added = [], []
    for mid in chain:
        hk = f"{key}@{mid}"
        if hk in base["models"]:
            continue
        added += _history_row(key, mid, gen["history"][mid],
                              old_price_lines if mid == e["from_id"] else None)
        history_keys.append(hk)
    new_lines = lines[:head] + new_block + added + lines[end:]
    maker = _maker_summary(Path(state_path), key, e["id"])
    at = _ledger_end(new_lines)
    new_lines[at:at] = _ledger_rows(key, e, summary or {}, price_rec, maker, row,
                                 (base.get("verification_ledger") or {}).get("entries") or [])
    new_text = "\n".join(new_lines)

    new_cfg = yaml.safe_load(new_text)
    try:
        Policy.of(new_cfg)
    except Exception as exc:  # noqa: BLE001 — refuse, never write a config Policy rejects
        raise SyncError(f"the promoted config fails the Policy checks: {exc}") from None
    if new_cfg["models"][key]["id"] != e["id"]:
        raise SyncError("row rewrite did not take")
    succ = json.loads(succ_path.read_text())
    ids = succ.setdefault("chains", {}).setdefault(key, [])
    for mid in [*chain, e["id"]]:
        if mid not in ids:
            ids.append(mid)
    _atomic_write_text(cfg_path, new_text)
    _atomic_write_text(succ_path, dump_succession(succ))
    return {
        "status": "promoted", "key": key, "from_id": e["from_id"], "id": e["id"],
        "history_rows": history_keys, "maker_row": "verified" if maker and
        maker.get("outcome") == "pass" else "maker_not_reprobed",
        "prior_failure_exit_hint": _prior_failure_hint(new_cfg, history_keys),
        "checklist": [
            f"set prior_failure_exit for {', '.join(history_keys) or '(none)'} in "
            f"{REPO_SUCCESSION} (a person decides; the hint is today's router exit)",
            "add key_renames/test expectations the new history rows need",
            f"references/model-profiles.md: an 'inherited, not current' paragraph for {key}",
            "check context_window and served_model_caveats of the row for the new id",
            "CHANGELOG (en/ko), version sync (plugin.json x2, package.json)",
            "deep-suite marketplace pin (after the release; user approval)",
        ]}


def _add_promote_parser(sub) -> None:
    pr = sub.add_parser("promote", help="move an applied overlay entry into a repo checkout")
    pr.add_argument("--repo", required=True, help="repository root to edit")
    pr.add_argument("--key", required=True)
    pr.add_argument("--price", required=True,
                    help="input=…,output=…,cached_input=…,source=https://…,"
                         "verified_on=YYYY-MM-DD[,cache_write=…][,max_age_days=N]")


def _promote_cli(args, *, env, home, state_path) -> int:
    _print(promote(repo=Path(args.repo), key=args.key, price=args.price,
                   state_path=state_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
