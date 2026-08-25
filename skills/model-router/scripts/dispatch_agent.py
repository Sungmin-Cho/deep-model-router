#!/usr/bin/env python3
"""Bounded execution supervisor for one background dispatch attempt.

`route_task.py` decides who runs. This script owns the time axis after that
decision — the part of the Layer B contract `references/adapters.md` claims:
start confirmation, a wall-clock deadline, cancellation with escalation
(TERM, a grace period, then KILL against the whole process group),
termination confirmation, and a completion receipt. The receipt is
completion evidence; it is deliberately distinct from any isolation claim
passed to the router as `--isolation-evidence`, and neither substitutes for
the other.

Design rules this file enforces:

- A spawn handle is not a result. The receipt reaches a terminal state only
  when the process group is confirmed finished.
- Once the deadline has expired, no output can produce SUCCEEDED. A verdict
  written during the grace period stays TIMED_OUT — a late fragment is not
  a review.
- If the process group cannot be confirmed dead, the terminal state is
  TERMINATION_UNCONFIRMED, and no write-capable retry may follow it
  (`route_task.py --flags termination_unconfirmed` holds the route).
- A crash anywhere after Popen succeeds — writing the RUNNING receipt,
  waiting on the child, validating output, or writing the terminal receipt
  itself — still runs the termination ladder and leaves a terminal receipt
  (CANCELLED if the group's death is confirmed, TERMINATION_UNCONFIRMED if
  not) before the crash is re-raised as exit 9. A supervisor crash must
  never be a silent abandonment of a live process group behind a receipt
  stuck at RUNNING.
- Prompts travel by file into the child's stdin; with no prompt file, stdin
  is /dev/null, so waiting on stdin is structurally impossible. argv is
  executed without a shell.
- The supervisor never parses argv, and applying a DECLARED output contract
  is not parsing argv. "Never parses argv" means it never infers what the
  caller did not declare — not that it refuses knowledge the caller handed
  it explicitly. `--output-schema review` already knows verdict grammar on
  exactly that basis; `--output-envelope grok-headless-json-v1` knows a
  version-named stdout document the same way. What stays outside the
  boundary is the child's identity: a dispatch that declares nothing is a
  dispatch this supervisor cannot tell is grok, because that would require
  reading its argv. That residue belongs to the Layer B recipe and the
  evidence chain, and `references/adapters.md` documents it as a boundary
  rather than leaving it silent.
- `--attempt-id` must match a safe identifier grammar before any path is
  built from it, and every path derived from it must resolve inside
  `--receipt-dir` — no `../` escape; every subcommand (`run`, `status`,
  `cancel`, `verify-evidence`) validates through the same chokepoint before
  its first path, not just `run`. Exclusive attempt creation claims a
  separate sentinel file (`<attempt_id>.claim`), never the receipt path
  itself: the receipt file only ever appears via an atomic replace of a
  complete JSON document, so it is never observable half-written OR empty.
  `run` refuses to start a second attempt under an attempt-id whose claim
  or receipt already exists; it never overwrites a live attempt's receipt
  or output files.

Subcommands: run | status | cancel | verify-evidence

Exit status: 0 SUCCEEDED; 1 FAILED; 2 invalid usage; 3 TIMED_OUT;
4 START_FAILED; 5 TERMINATION_UNCONFIRMED; 6 INVALID_OUTPUT;
7 CANCELLED (cancel: confirmed); 9 internal error — a crash, never an
attempt outcome (the same lesson route_task.py's exit 5 encodes). status
exits 0 and prints the receipt. verify-evidence exits 0 iff the evidence
set is exactly valid.

POSIX only: process-group control uses start_new_session and os.killpg.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

STATES = (
    "STARTING", "RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT",
    "CANCELLED", "START_FAILED", "TERMINATION_UNCONFIRMED", "INVALID_OUTPUT",
)
# CLAIMED is deliberately NOT a member: it is `status`'s report-only label
# for "a claim sentinel exists but no receipt does yet" (see cmd_status) —
# there is no receipt in that window to hold a `result.state`, so it is not
# a receipt state and never appears in a receipt's `result.state` field.

EXIT_BY_STATE = {
    "SUCCEEDED": 0, "FAILED": 1, "TIMED_OUT": 3, "START_FAILED": 4,
    "TERMINATION_UNCONFIRMED": 5, "INVALID_OUTPUT": 6, "CANCELLED": 7,
}

VERDICT_RE = re.compile(r"^verdict:\s*(PASS|PASS_WITH_CHANGES|FAIL)\b", re.M)

# Safe identifier grammar for attempt-id: this string is interpolated
# directly into filesystem paths, so it must never contain a path
# separator or a traversal segment. `\A`/`\Z` for the same reason the
# digest grammar below uses them — `$` also matches just before a final
# newline, which put a newline in a receipt FILENAME.
ATTEMPT_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

# 64-hex digest grammar for the linkage args a route hands to a dispatch.
# `\A`/`\Z`, not `^`/`$`: without re.MULTILINE, `$` still matches just before
# a final newline, so `^[0-9a-f]{64}$` accepted a 65-char "<64 hex>\n" — a
# value a programmatic caller produces by reading a file without stripping,
# and one this gate then wrote verbatim into a permanent receipt.
HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# --- Grok seat integrity (2026-08-25 design, DD-1) ---------------------
#
# A headless grok turn that a permission prompt cancelled still exits 0
# (official semantics, [UG-14]), so the exit status alone cannot tell a
# finished turn from a killed one. The stdout document can: it carries a
# `stopReason` from a documented, version-named vocabulary. Declaring
# `--output-envelope` opts a dispatch into grading that document.
#
# This does not breach the supervisor's "never parse argv" rule. That rule
# says the supervisor never INFERS what the caller did not declare;
# applying a version-named output contract the caller explicitly declared
# is the same kind of knowledge `--output-schema review` already encodes
# about verdict grammar.
ENVELOPE_FORMATS = ("grok-headless-json-v1",)
# The envelope is a GATE surface, so its read is bounded: an unbounded
# JSON parse of a child-controlled file is a denial-of-service surface on
# the supervisor itself. Over budget is a typed refusal
# (`evidence_oversized`), never a best-effort partial parse.
ENVELOPE_MAX_BYTES = 4 * 1024 * 1024
# The only stop reason that leaves an attempt a success candidate. Every
# other member of the official vocabulary (`max_tokens`,
# `max_turn_requests`, `refusal`, `cancelled`), every unknown value, and
# an absent field all fail closed. The vocabulary is pinned to the
# envelope VERSION name: a grok upgrade that changes it gets a new
# envelope version, never a new meaning for this one.
ENVELOPE_OK_STOP_REASON = "end_turn"

SESSION_EVIDENCE_FORMATS = ("grok-session-v1",)

UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

# The sole trigger for the declaration-consistency preflight below.
# `--runtime` is the HOST throughout this repository (`runtimes:` in the
# config, SKILL.md, the observation record's RUNTIMES enum), so it says
# nothing about the child's identity: keying off `--runtime grok` would
# refuse grok-HOSTED dispatches out to claude/codex, which produce no
# envelope, while doing nothing for issue #14's actual path (a claude_code
# host dispatching INTO grok). The child's identity travels in the
# `to_<target>` half of `--transport-id`.
XAI_TRANSPORT_SUFFIX = ".to_xai"

# --- Required artifacts (DD-2) ----------------------------------------
#
# `output_schema: none` is not proof a seat finished — it is proof nothing
# was checked. A seat that produces files proves completion by naming them
# and letting this supervisor verify, before SUCCEEDED, that they exist,
# are contained, and are THIS attempt's work rather than a prior one's
# leftovers.
ARTIFACT_MAX_COUNT = 16
# Pre-spawn there is no deadline anchor to bound a hash against (the
# attempt has not started, so there is nothing to time out), so the
# baseline half is bounded by size instead — per file and in aggregate.
# The grading half, which does have an anchor, is bounded by the deadline
# itself (`_hash_artifact`).
ARTIFACT_BASELINE_MAX_BYTES = 64 * 1024 * 1024
ARTIFACT_BASELINE_BUDGET_BYTES = 256 * 1024 * 1024
ARTIFACT_HASH_CHUNK_BYTES = 8 * 1024 * 1024


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validated_attempt_id(value: str) -> str:
    """The single validation chokepoint every subcommand passes through
    before any path is built from a caller-supplied attempt id — `run`,
    `status`, `cancel`, and `verify-evidence` all call this before their
    first `_receipt_path`/`read_receipt`, not just `run`. Raises ValueError
    (never lets an id past the grammar reach a path); every caller maps
    that the same way, to exit 2 with a usage message, before touching the
    filesystem."""
    if not ATTEMPT_ID_RE.match(value):
        raise ValueError(
            f"invalid attempt-id {value!r}: must match {ATTEMPT_ID_RE.pattern}")
    return value


def _finite_positive(value: float) -> bool:
    """Shared duration validator: argparse's `type=float` passes `inf`/`nan`
    straight through unchanged, so every duration input — `run`'s
    --deadline-seconds and --grace-seconds, and `cancel`'s --grace-seconds
    — is checked through this one function, not reimplemented per
    subcommand."""
    return math.isfinite(value) and value > 0


def _receipt_path(receipt_dir: Path, attempt_id: str) -> Path:
    path = receipt_dir / f"{attempt_id}.json"
    resolved_dir = receipt_dir.resolve()
    resolved_path = path.resolve()
    if resolved_path.parent != resolved_dir:
        # A checked exception, never an assert: assertions are stripped
        # under `python -O`, and a containment check that can silently
        # vanish under an interpreter flag is not a safety check at all.
        # This is defense in depth behind _validated_attempt_id, which
        # every subcommand already runs first — this still fires if that
        # chokepoint is ever bypassed.
        raise ValueError(
            f"receipt path escaped receipt_dir: {resolved_path} not in {resolved_dir}")
    return path


def write_receipt(receipt_dir: Path, receipt: dict) -> None:
    """Atomic: a poller must never read a half-written receipt. The tmp path
    is unpredictable and created exclusively INSIDE receipt_dir via
    `tempfile.mkstemp` — O_CREAT|O_EXCL|0600 by construction — never a name
    derived from this writer's own pid. A `<receipt>.<pid>.tmp` name (the
    round-2 shape this replaces) is guessable the instant a caller reads
    supervisor_pid off any receipt this same process already wrote: a
    write-capable supervised child could plant a symlink at that predictable
    path ahead of time and have this process's own `os.replace` follow it
    onto an external file the child cannot otherwise touch. mkstemp's name
    cannot be predicted or pre-planted, and O_EXCL means it is never a
    symlink hop in the first place — the guessable pid-suffix name WAS the
    vulnerability, not a safeguard. The tmp is unlinked on any failure so a
    partial write never lingers next to the receipt."""
    path = _receipt_path(receipt_dir, receipt["attempt_id"])
    fd, tmp_name = tempfile.mkstemp(
        dir=receipt_dir, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(receipt, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_receipt(receipt_dir: Path, attempt_id: str) -> dict:
    return json.loads(_receipt_path(receipt_dir, attempt_id).read_text())


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but is not ours to signal — still alive


def _await_group_death(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)
    return not _group_alive(pgid)


def terminate_group(proc: subprocess.Popen | None, pgid: int,
                    grace: float) -> bool:
    """TERM -> grace -> KILL -> confirm, against the whole group.

    Returns True only when the group is confirmed gone. The leader must be
    reaped (`proc.wait`) or a zombie holds the group open and a dead tree
    reads as alive forever.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        if proc is not None:
            try:
                proc.wait(timeout=grace)
                proc = None  # leader reaped
            except subprocess.TimeoutExpired:
                continue  # leader ignored the signal; escalate
        if _await_group_death(pgid, grace):
            return True
    return _await_group_death(pgid, grace)


def _validate_output(stdout_path: Path, output_schema: str) -> tuple[bool, str | None]:
    data = stdout_path.read_bytes()
    if not data.strip():
        return False, None
    digest = hashlib.sha256(data).hexdigest()
    if output_schema == "review" and not VERDICT_RE.search(
            data.decode(errors="replace")):
        return False, digest
    return True, digest


class NotARegularFile(OSError):
    """An evidence path resolved to something that is not a regular file.

    Distinct from a plain OSError so callers can tell "this is a FIFO,
    device or symlink" (a containment/blocking hazard) from "this is
    missing" (an ordinary absent artifact) — the two get different
    `invalid_reasons`.
    """


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    """Open an evidence file for reading, refusing anything that is not a
    regular file — and refusing it WITHOUT blocking.

    Three flags carry the whole rule. O_NOFOLLOW refuses a symlink hop, so
    a link planted at an artifact path cannot make this process read (or
    attest to) a file outside the root. O_NONBLOCK means a FIFO planted
    there opens immediately instead of waiting for a writer that never
    comes — an ordinary open() would hold the terminal receipt past the
    deadline, which is exactly the hazard the deadline exists to prevent.
    The fstat is on the SAME fd that was opened, not a second stat of the
    pathname: a path checked and then reopened is a TOCTOU window, an fd
    checked and then read is not.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise NotARegularFile(f"{path} is not a regular file")
    return fd, st


def _contained(path: Path, root: Path) -> bool:
    """Containment against an already-resolved, PINNED root."""
    return path == root or root in path.parents


def _hash_artifact(fd: int, deadline_monotonic: float | None) -> str | None:
    """SHA-256 of an open regular file, in chunks, re-checking the remaining
    deadline budget between them. Returns None when the budget ran out.

    A digest is either complete or absent: a partial hash recorded as if it
    were the file's identity would be worse than no hash at all. Bounding
    this is what keeps an enormous or sparse artifact from delaying a
    terminal receipt indefinitely — DD-2's replacement for the draft's
    "the hash is contractually unbounded".
    """
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as f:
        while True:
            if deadline_monotonic is not None and \
                    time.monotonic() > deadline_monotonic:
                return None
            chunk = f.read(ARTIFACT_HASH_CHUNK_BYTES)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _deadline_expired(deadline_monotonic: float) -> bool:
    """The final deadline re-check, immediately before SUCCEEDED is written.

    Grading itself consumes time — several file hashes, JSON and JSONL
    parsing — so an attempt whose leader exited comfortably early can still
    cross its own deadline while being graded. DD-9's invariant is about
    when grading FINISHES, not only when the leader exited. A named helper
    rather than an inline comparison so a test can make the crossing
    deterministic instead of racing a real clock.
    """
    return time.monotonic() > deadline_monotonic


def _capture_artifact_baselines(args) -> tuple[Path, list[dict]] | str:
    """Validate the artifact declarations and capture their baselines.

    Runs entirely PRE-SPAWN, so every failure here is a usage error: exit 2
    with no receipt and no attempt-id consumed. Returns `(pinned_root,
    entries)` on success, or an error string to print.

    The root is resolved exactly once, here, and every later containment
    check compares against that pinned value — a child that replaces the
    root's pathname with a symlink to somewhere else afterwards does not
    get to move the fence.
    """
    if args.artifact_root is None:
        return ("--require-artifact requires --artifact-root: containment "
                "has no meaning without a fence")
    if len(args.require_artifact) > ARTIFACT_MAX_COUNT:
        return (f"--require-artifact declared {len(args.require_artifact)} "
                f"paths, over the limit of {ARTIFACT_MAX_COUNT}")
    root = Path(args.artifact_root).resolve()
    if not root.is_dir():
        return f"--artifact-root {args.artifact_root!r} is not a directory"

    entries: list[dict] = []
    by_path: dict[Path, dict] = {}
    for declared in args.require_artifact:
        resolved = Path(declared).resolve()
        if not _contained(resolved, root):
            return (f"--require-artifact {declared!r} resolves to {resolved} "
                    f"which is outside --artifact-root {root}")
        if resolved in by_path:
            return f"--require-artifact {declared!r} is declared twice"
        entry = {"declared": declared, "path": resolved,
                 "expected_sha256": None, "baseline_sha256": None}
        entries.append(entry)
        by_path[resolved] = entry

    for spec in args.require_artifact_sha256 or ():
        # rpartition, not split: an artifact path may legitimately contain
        # '=', and only the LAST one separates the digest.
        raw_path, sep, digest = spec.rpartition("=")
        if not sep or not HEX64_RE.match(digest):
            return (f"--require-artifact-sha256 {spec!r} must be "
                    f"PATH=<64 lowercase hex>")
        resolved = Path(raw_path).resolve()
        entry = by_path.get(resolved)
        if entry is None:
            return (f"--require-artifact-sha256 {spec!r} names a path that "
                    f"was never declared with --require-artifact")
        if entry["expected_sha256"] is not None:
            # Duplicate AND conflict are both refused: a repeated mapping
            # is a caller mistake whether or not the two digests agree, and
            # silently taking the last one hides which was intended.
            return (f"--require-artifact-sha256 maps {resolved} more than "
                    f"once")
        entry["expected_sha256"] = digest

    budget = 0
    for entry in entries:
        try:
            fd, st = _open_regular(entry["path"])
        except (NotARegularFile, OSError) as exc:
            if isinstance(exc, NotARegularFile) or exc.errno == errno.ELOOP:
                return (f"--require-artifact {entry['declared']!r} exists but "
                        f"is not a regular file")
            continue  # absent pre-spawn: baseline stays None, which is the
                      # strongest freshness evidence there is
        try:
            if st.st_size > args.require_artifact_baseline_max_bytes:
                return (f"--require-artifact {entry['declared']!r} baseline "
                        f"too large: {st.st_size} bytes over the limit of "
                        f"{args.require_artifact_baseline_max_bytes}")
            budget += st.st_size
            if budget > ARTIFACT_BASELINE_BUDGET_BYTES:
                return (f"--require-artifact baselines exceed the aggregate "
                        f"budget of {ARTIFACT_BASELINE_BUDGET_BYTES} bytes")
            entry["baseline_sha256"] = _hash_artifact(fd, None)
            fd = -1
        finally:
            if fd != -1:
                os.close(fd)
    return root, entries


def _grade_artifacts(entries: list[dict], root: Path,
                     deadline_monotonic: float,
                     allow_unchanged: bool) -> tuple[list[dict], list[str], bool]:
    """Prove each required artifact, or say precisely why it is not proof.

    Returns `(records, reasons, hash_aborted)`. The records go into the
    receipt on success too: existence, containment and the digest ARE the
    evidence G4 asks for, and a receipt that only carried them on failure
    would prove nothing on the path that matters.
    """
    records: list[dict] = []
    reasons: list[str] = []
    aborted = False
    for entry in entries:
        path = entry["path"]
        record = {"path": str(path), "exists": False, "size": None,
                  "sha256": None, "contained": None,
                  "baseline_sha256": entry["baseline_sha256"],
                  "changed": None, "expected_sha256_match": None}
        records.append(record)
        try:
            current = path.resolve()
        except OSError:
            current = path
        record["contained"] = _contained(current, root)
        if not record["contained"]:
            reasons.append(f"artifact_escaped_root:{path}")
            continue
        try:
            fd, st = _open_regular(path)
        except NotARegularFile:
            reasons.append(f"artifact_not_regular_file:{path}")
            continue
        except OSError as exc:
            # ELOOP is O_NOFOLLOW refusing a symlink planted at the
            # artifact path — reporting that as "missing" would send an
            # operator looking for a file that is right there. The two
            # get different reasons because they have different remedies.
            reasons.append(
                f"artifact_not_regular_file:{path}"
                if exc.errno == errno.ELOOP else f"artifact_missing:{path}")
            continue
        record["exists"] = True
        record["size"] = st.st_size
        if st.st_size == 0:
            os.close(fd)
            reasons.append(f"artifact_empty:{path}")
            continue
        digest = _hash_artifact(fd, deadline_monotonic)
        if digest is None:
            # Out of budget mid-hash. No partial digest is recorded, and
            # the attempt is a TIMED_OUT rather than a verdict on contents
            # nobody finished reading.
            record["hash_aborted"] = True
            aborted = True
            continue
        record["sha256"] = digest
        baseline = entry["baseline_sha256"]
        record["changed"] = baseline is None or digest != baseline
        if not record["changed"]:
            if allow_unchanged:
                record["artifact_unchanged_accepted"] = True
            else:
                reasons.append(f"artifact_unchanged:{path}")
        if entry["expected_sha256"] is not None:
            record["expected_sha256_match"] = digest == entry["expected_sha256"]
            if not record["expected_sha256_match"]:
                reasons.append(f"artifact_sha256_mismatch:{path}")
    return records, reasons, aborted


def _capped_bytes(path: Path, limit: int) -> tuple[bytes | None, bool]:
    """Read at most `limit` bytes from a gate surface.

    Returns `(data, oversized)`; `data` is None when the file could not be
    read at all or when it is over budget. Reading `limit + 1` is what
    makes "exactly at the limit" distinguishable from "over it" without a
    stat/read race. The open goes through `_open_regular`, so a symlink,
    FIFO or device planted at a gate surface is refused rather than
    followed or waited on.
    """
    try:
        fd, _ = _open_regular(path)
        with os.fdopen(fd, "rb") as f:
            data = f.read(limit + 1)
    except OSError:
        return None, False
    if len(data) > limit:
        return None, True
    return data, False


def _read_envelope(stdout_path: Path) -> dict:
    """Read one grok headless JSON document off a finished attempt's stdout.

    Always returns the same six keys, whatever went wrong. `text` is an
    INTERNAL field — it feeds the verdict check and nothing else, and is
    dropped by `_receipt_envelope` before the receipt is written: the raw
    output already lives in the stdout file this read came from, and a
    receipt carries abbreviated evidence, not a second copy of the payload.

    `error_type` carries the document's own `type` discriminator for grok's
    `{"type": "error", ...}` failure object, and doubles as the read-failure
    channel (`evidence_oversized` / `envelope_unreadable`) so a caller can
    tell an over-budget stdout from a merely malformed one.
    """
    envelope = {"parse_ok": False, "stop_reason": None, "session_id": None,
                "served_models": None, "text": None, "error_type": None}
    data, oversized = _capped_bytes(stdout_path, ENVELOPE_MAX_BYTES)
    if oversized:
        envelope["error_type"] = "evidence_oversized"
        return envelope
    if data is None:
        envelope["error_type"] = "envelope_unreadable"
        return envelope
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return envelope
    if not isinstance(doc, dict):
        # A bare array or scalar parses as JSON but is not an envelope.
        return envelope
    envelope["parse_ok"] = True
    for key, field in (("stopReason", "stop_reason"), ("sessionId", "session_id"),
                       ("text", "text"), ("type", "error_type")):
        value = doc.get(key)
        if isinstance(value, str):
            envelope[field] = value
    usage = doc.get("modelUsage")
    if isinstance(usage, dict):
        # DD-7: the SERVED model identifiers, recorded as observed and
        # never normalized against the declared `--model-id` — the
        # supervisor does not cross-check a caller's declaration.
        envelope["served_models"] = sorted(usage)
    return envelope


def _receipt_envelope(envelope: dict) -> dict:
    """The five keys an envelope contributes to a receipt. `session_id` is
    here as the audit evidence for what the attempt-binding cross-proof
    (DD-3) actually compared; `text` is deliberately absent."""
    return {k: envelope[k] for k in
            ("parse_ok", "stop_reason", "session_id", "served_models",
             "error_type")}


def _grade_envelope(envelope: dict) -> list[str]:
    """Fail-closed gate on a declared envelope. Returns the reasons it
    failed, empty when it is a success candidate."""
    if envelope["error_type"] == "evidence_oversized":
        return ["evidence_oversized"]
    if not envelope["parse_ok"]:
        return ["envelope_unparseable"]
    stop_reason = envelope["stop_reason"]
    if stop_reason != ENVELOPE_OK_STOP_REASON:
        return [f"envelope_stop_reason:{stop_reason or '<absent>'}"]
    return []


def _stdout_digest(stdout_path: Path) -> str | None:
    data, _ = _capped_bytes(stdout_path, ENVELOPE_MAX_BYTES)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _new_receipt(args, stdout_path: Path, stderr_path: Path) -> dict:
    return {
        "attempt_id": args.attempt_id,
        "seat": args.seat,
        "runtime": args.runtime,
        "model_id": args.model_id,
        "effort_native": args.effort_native,
        "permission_mode": args.permission_mode,
        # Linkage back to the route decision (design §4 B2). All four are
        # caller-supplied and null when absent — the supervisor probes
        # nothing. model_id above is likewise the caller's DECLARED value:
        # this supervisor never parses argv to cross-check it (transport-
        # specific knowledge); the raw argv in this receipt is what an
        # auditor checks it against.
        "decision_fingerprint": args.decision_fingerprint,
        "policy_sha256": args.policy_sha256,
        "transport_id": args.transport_id,
        "host_cli_version": args.host_cli_version,
        # Requested vs served: no transport can observe the served model
        # yet, so this pair stays at its defaults in this tranche. Contract
        # (documented, unenforced until a producer exists): observed_model_id
        # is null IFF observed_model_source == "unavailable"; the only
        # source value this version defines is "unavailable".
        "observed_model_id": None,
        "observed_model_source": "unavailable",
        "argv": args.argv,
        "prompt_sha256": None,
        "output_schema": args.output_schema,
        # DD-1: the DECLARED envelope contract, null when none was
        # declared. Every key this tranche adds is present on every
        # receipt and null on the undeclared path — that is what keeps
        # the exact-field contract one key set instead of one per
        # declaration combination.
        "output_envelope": args.output_envelope,
        "process": {"pid": None, "process_group_id": None,
                    "supervisor_pid": os.getpid()},
        "timing": {"started_at": None, "deadline_at": None, "finished_at": None},
        "result": {
            "state": "STARTING", "exit_status": None,
            "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
            "output_sha256": None, "schema_valid": None,
            "termination_confirmed": None,
            # DD-1 evidence and DD-5's shared cause vocabulary. `envelope`
            # is recorded on every terminal state once declared (error
            # objects and non-zero exits included) — recording is
            # unconditional, gating is not.
            "envelope": None,
            "invalid_reasons": None,
            # DD-2: the per-artifact proof set. Null when nothing was
            # required, and null on every termination that never reached
            # grading — a baseline is evidence about a comparison that was
            # actually made, not a field to fill in for its own sake.
            "artifacts": None,
        },
    }


def _commit_terminal(receipt_dir: Path, receipt: dict, claim_path: Path) -> int:
    """The single last-instant commit for a terminal receipt — shared by
    `cmd_run`'s normal tail and its post-spawn `except Exception` handler,
    so an external terminal write (typically `cancel`) is preserved no
    matter which of the two paths reaches this attempt's last write. The
    caller has already set `receipt["result"]["state"]` (and
    `finished_at`) to its own conclusion before calling this.

    This re-read is the LAST action before the atomic replace below — as
    close to the write as the language lets it get, to shrink (not
    eliminate) the read-replace gap that design doc DD-9 documents as a
    residual race. An external `cancel` (or, in principle, a second `run`
    sharing this attempt-id) may have reached the receipt first with a
    terminal state of its own; whichever terminal state is already on disk
    wins — the caller's own conclusion (typically FAILED or CANCELLED)
    never overwrites it. Preserve ANY state in EXIT_BY_STATE (every
    terminal state this file knows), not only CANCELLED/
    TERMINATION_UNCONFIRMED: a late overwrite of a cancelled or unconfirmed
    attempt must not relabel it FAILED, SUCCEEDED, or anything else,
    regardless of which of the two call sites (normal tail or crash
    handler) is the one doing the overwriting.

    The claim sentinel is unlinked in both branches: this `run` process is
    the only holder of the claim either way, so it is the one that
    releases it once ANY terminal state is confirmed on disk, whoever's
    conclusion that terminal state is."""
    try:
        on_disk = read_receipt(receipt_dir, receipt["attempt_id"])
        on_disk_state = on_disk["result"]["state"]
        if on_disk_state in EXIT_BY_STATE:
            # Someone else (typically `cancel`, racing this same attempt)
            # already landed a terminal state — this `run` is the only
            # holder of the claim sentinel either way, so it is the one
            # that removes it once ANY terminal state is confirmed on
            # disk, whoever's conclusion that terminal state is.
            claim_path.unlink(missing_ok=True)
            return EXIT_BY_STATE[on_disk_state]
    except (OSError, json.JSONDecodeError):
        pass
    write_receipt(receipt_dir, receipt)
    claim_path.unlink(missing_ok=True)  # receipt is terminal now
    return EXIT_BY_STATE[receipt["result"]["state"]]


def cmd_run(args) -> int:
    # Everything that can fail before spawn is validated before any receipt
    # exists — a preflight failure must never leave a permanent STARTING
    # receipt behind (status/cancel only know how to unwind RUNNING).
    try:
        args.attempt_id = _validated_attempt_id(args.attempt_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not _finite_positive(args.deadline_seconds):
        print(f"--deadline-seconds must be a finite number > 0, got "
              f"{args.deadline_seconds!r}", file=sys.stderr)
        return 2
    if not _finite_positive(args.grace_seconds):
        print(f"--grace-seconds must be a finite number > 0, got "
              f"{args.grace_seconds!r}", file=sys.stderr)
        return 2
    for name, value in (("--decision-fingerprint", args.decision_fingerprint),
                        ("--policy-sha256", args.policy_sha256)):
        if value is not None and not HEX64_RE.match(value):
            print(f"{name} must be 64 lowercase hex chars, got {value!r}",
                  file=sys.stderr)
            return 2

    # --- Grok seat integrity preflight (DD-1) -------------------------
    # Everything below refuses BEFORE spawn and before any receipt or
    # claim exists: a usage error must never burn an attempt-id or leave
    # a permanent STARTING receipt that only `cancel` knows how to unwind.
    if args.session_evidence is not None:
        evidence_format, sep, evidence_dir = args.session_evidence.partition(":")
        if not sep or evidence_format not in SESSION_EVIDENCE_FORMATS \
                or not evidence_dir:
            print(f"--session-evidence must be FORMAT:DIR with FORMAT in "
                  f"{list(SESSION_EVIDENCE_FORMATS)}, got "
                  f"{args.session_evidence!r}", file=sys.stderr)
            return 2
    if args.session_id is not None and not UUID_RE.match(args.session_id):
        print(f"--session-id must be a UUID, got {args.session_id!r}",
              file=sys.stderr)
        return 2
    if args.expect_effective_agent is not None:
        if not args.expect_effective_agent.strip():
            print("--expect-effective-agent must not be empty",
                  file=sys.stderr)
            return 2
        if args.session_evidence is None:
            # An expectation with nothing to compare against would be
            # silently ignored, and a gate that can be silently disarmed
            # by omitting an unrelated argument is not a gate. The name it
            # checks (`summary.agent_name`) only exists in the session
            # evidence.
            print("--expect-effective-agent requires --session-evidence: "
                  "the effective agent name is only observable in the "
                  "session summary", file=sys.stderr)
            return 2
    if args.transport_id and args.transport_id.endswith(XAI_TRANSPORT_SUFFIX):
        missing = [name for name, value in (
            ("--output-envelope", args.output_envelope),
            ("--session-evidence", args.session_evidence)) if value is None]
        if missing:
            print(f"--transport-id {args.transport_id!r} dispatches into "
                  f"grok, whose cancelled turns exit 0 — it requires "
                  f"{' and '.join(missing)}. Half a declaration is not a "
                  f"declaration; see references/adapters.md \"Dispatch "
                  f"contract\".", file=sys.stderr)
            return 2

    artifact_root = None
    artifact_entries: list[dict] = []
    if args.require_artifact:
        if not _finite_positive(args.require_artifact_baseline_max_bytes):
            print("--require-artifact-baseline-max-bytes must be a finite "
                  "number > 0", file=sys.stderr)
            return 2
        captured = _capture_artifact_baselines(args)
        if isinstance(captured, str):
            print(f"error: {captured}", file=sys.stderr)
            return 2
        artifact_root, artifact_entries = captured
    elif args.require_artifact_sha256:
        print("--require-artifact-sha256 without --require-artifact "
              "constrains nothing", file=sys.stderr)
        return 2

    prompt_sha256 = None
    stdin_f = subprocess.DEVNULL
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        # Read (and hash) the prompt before anything is claimed or written:
        # a missing/unreadable prompt file must fail with nothing on disk,
        # not after a STARTING receipt already exists.
        try:
            prompt_bytes = prompt_path.read_bytes()
        except OSError as exc:
            print(f"--prompt-file {args.prompt_file!r} is not readable: "
                  f"{exc}", file=sys.stderr)
            return 2
        prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
        stdin_f = open(prompt_path, "rb")

    receipt_dir = Path(args.receipt_dir)
    # 0o700: supervisor-owned, no other principal can read, write, or place
    # anything inside it. This is the out-of-band mitigation for the residual
    # O_TRUNC-on-hardlink risk noted at the stdout/stderr opens below —
    # O_NOFOLLOW refuses a symlink hop, but a hardlink is not a symlink and
    # still resolves to the external file's own inode, so O_TRUNC through it
    # still truncates that file. A 0700 directory this process exclusively
    # owns is what keeps anything else from placing a hardlink in here to
    # begin with.
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = _receipt_path(receipt_dir, args.attempt_id)
    claim_path = receipt_dir / f"{args.attempt_id}.claim"
    # Exclusive attempt creation is one atomic O_CREAT|O_EXCL claim on a
    # SEPARATE sentinel file (<attempt_id>.claim), never on the receipt path
    # itself. Claiming the receipt path directly would publish an empty
    # `.json` file at the final receipt pathname before the STARTING JSON
    # ever replaces it — a concurrent status/poller reading the receipt in
    # that window sees invalid JSON, and a supervisor crash in that same
    # window leaves a permanently unreadable "receipt". With the sentinel,
    # the receipt file itself only ever appears via write_receipt's atomic
    # tmp+os.replace of a COMPLETE JSON document — never half-written,
    # never empty, end to end. Two `run`s racing on the same attempt-id must
    # never both pass a check and then clobber each other's receipt/output
    # files; the loser of the claim gets FileExistsError as its refusal
    # signal, not a stale read.
    try:
        claim_f = open(claim_path, "x")
    except FileExistsError:
        print(f"attempt-id {args.attempt_id!r} is already claimed under "
              f"{receipt_dir} — another `run` is starting it, or a prior "
              f"claim was left behind by a crash (safe to remove once the "
              f"claiming supervisor is confirmed dead) — refusing to start "
              f"a second attempt with the same id", file=sys.stderr)
        if stdin_f is not subprocess.DEVNULL:
            stdin_f.close()
        return 2
    # The claim handle already owns the sentinel's filename; close it.
    claim_f.close()
    if receipt_path.exists():
        # A prior attempt under this id already ran to completion — its
        # own claim was already removed at its terminal write (below).
        # Attempt ids are one-shot: give up the claim just taken, which
        # belongs to no attempt, and refuse.
        print(f"attempt-id {args.attempt_id!r} already has a receipt under "
              f"{receipt_dir} — refusing to start a second attempt with the "
              f"same id", file=sys.stderr)
        claim_path.unlink(missing_ok=True)
        if stdin_f is not subprocess.DEVNULL:
            stdin_f.close()
        return 2

    stdout_path = receipt_dir / f"{args.attempt_id}.stdout"
    stderr_path = receipt_dir / f"{args.attempt_id}.stderr"
    receipt = _new_receipt(args, stdout_path, stderr_path)
    receipt["prompt_sha256"] = prompt_sha256
    # deadline_at is computed here, before Popen — no post-spawn arithmetic
    # (e.g. a non-finite deadline) can crash once the child is already
    # running; the finiteness check above already rules that out, but
    # computing it before spawn keeps the ordering honest either way.
    #
    # deadline_monotonic is the SAME instant, stamped as a monotonic clock
    # reading instead of wall-clock: deadline_at is for humans reading the
    # receipt, deadline_monotonic is what every later wait actually
    # enforces against (monotonic is immune to wall-clock adjustment, and
    # is the only clock `time.monotonic()`-based waits below can compare
    # against consistently). Every wait from here on consumes the
    # REMAINING budget against this one anchor — never the full
    # deadline_seconds duration a second time — so a wait started partway
    # through an attempt cannot let it run past deadline_at.
    deadline_at = datetime.fromtimestamp(
        time.time() + args.deadline_seconds, timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline_monotonic = time.monotonic() + args.deadline_seconds
    write_receipt(receipt_dir, receipt)

    # stdout/stderr open without following a symlink: a regular open() that
    # follows one would let a symlink prepared at either path (by whatever
    # placed the prompt/receipt directory) truncate an arbitrary external
    # file this process can write to. O_NOFOLLOW refuses instead of
    # following.
    #
    # Residual risk O_NOFOLLOW does NOT close: a hardlink is not a symlink,
    # so O_NOFOLLOW does not refuse one — a hardlink at either path still
    # resolves straight to the external file's own inode, and O_TRUNC
    # through it still truncates that file. Nothing in this open() call can
    # tell a hardlink from an ordinary regular file. The mitigation is the
    # out-of-band 0700 mode receipt_dir was created with above: nothing but
    # this supervisor can place anything — symlink or hardlink — inside a
    # directory it exclusively owns.
    stdout_fd = None
    try:
        try:
            stdout_fd = os.open(
                stdout_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            stderr_fd = os.open(
                stderr_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            # Either open can fail (ELOOP on a planted symlink, a
            # permission failure, ENOSPC, ...). This arm must exist for the
            # SAME reason the Popen-failure arm below does: a STARTING
            # receipt already exists (written above) and nothing may leave
            # it as this attempt's last word. If the FIRST open (stdout)
            # succeeded and the SECOND (stderr) then raised, neither of the
            # two `with os.fdopen(...)` context managers below is ever
            # entered — so nothing else owns that first fd, and it would
            # leak here if not closed explicitly. The reason cannot be
            # written into the stderr file's own text (that file is
            # exactly what failed to open), so it goes to the supervisor's
            # own stderr instead.
            if stdout_fd is not None:
                os.close(stdout_fd)
            receipt["result"]["state"] = "START_FAILED"
            receipt["timing"]["finished_at"] = _utcnow()
            write_receipt(receipt_dir, receipt)
            # The receipt is terminal now — same rule as every other
            # terminal path: release the claim sentinel so it does not sit
            # forever as a claim with no attempt behind it.
            claim_path.unlink(missing_ok=True)
            print(f"start failed: cannot open output file: {exc}",
                  file=sys.stderr)
            return EXIT_BY_STATE["START_FAILED"]

        # stdout/stderr go straight to files: no pipe buffer to fill, no
        # drain thread to forget, no deadlock when the child floods.
        with os.fdopen(stdout_fd, "wb") as out_f, os.fdopen(stderr_fd, "wb") as err_f:
            try:
                proc = subprocess.Popen(
                    args.argv, stdin=stdin_f, stdout=out_f, stderr=err_f,
                    start_new_session=True)  # child leads its own group
            except OSError as exc:
                receipt["result"]["state"] = "START_FAILED"
                receipt["timing"]["finished_at"] = _utcnow()
                err_f.write(f"start failed: {exc}\n".encode())
                write_receipt(receipt_dir, receipt)
                # The receipt is terminal now — the claim sentinel has done
                # its job (kept a racing `run` from clobbering this attempt
                # while it was still ambiguous) and would otherwise sit
                # forever as a claim with no attempt behind it.
                claim_path.unlink(missing_ok=True)
                return EXIT_BY_STATE["START_FAILED"]

            pgid = proc.pid  # start_new_session: pgid == pid
            # supervisor_pid is this `run` process's own pid — distinct from
            # the child's pid/pgid above. It lets `status` tell "supervisor
            # dead, child alive" (orphaned) apart from "both dead" (stale):
            # without it, a RUNNING receipt only proves a child pgid existed,
            # never who is still watching the deadline. It is already set
            # (to this same os.getpid()) on the STARTING receipt above, so
            # `status` can triage a stuck STARTING receipt the same way.
            #
            # Everything from here to the terminal write is one try: a spawn
            # handle is not a result, and nothing after Popen succeeds may
            # leave the group unsupervised or the receipt stuck non-terminal
            # (below, `except Exception`).
            try:
                receipt["process"] = {"pid": proc.pid, "process_group_id": pgid,
                                      "supervisor_pid": os.getpid()}
                receipt["timing"]["started_at"] = _utcnow()
                receipt["timing"]["deadline_at"] = deadline_at
                receipt["result"]["state"] = "RUNNING"
                write_receipt(receipt_dir, receipt)

                try:
                    # Consume the REMAINING budget against the one monotonic
                    # anchor stamped before spawn (deadline_monotonic, Task 8)
                    # — not args.deadline_seconds again. Waiting the full
                    # duration a second time here would let the leader run
                    # past its own recorded deadline_at before TimeoutExpired
                    # ever fires; with the remaining-budget expression,
                    # TimeoutExpired now fires at the absolute deadline
                    # instant, not deadline_seconds after this wait started.
                    remaining = max(0.0, deadline_monotonic - time.monotonic())
                    exit_status = proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    # Past the deadline nothing the attempt writes can matter:
                    # the state is decided by termination alone, and a late
                    # verdict on disk is deliberately never graded.
                    confirmed = terminate_group(proc, pgid, args.grace_seconds)
                    receipt["result"]["termination_confirmed"] = confirmed
                    receipt["result"]["state"] = (
                        "TIMED_OUT" if confirmed else "TERMINATION_UNCONFIRMED")
                else:
                    # The leader exited — but grandchildren may linger, and a
                    # lingering writer is the duplicate-writer hazard (F-02).
                    # Clean the group up and confirm before grading anything.
                    # Bound this confirmation wait by whatever deadline budget
                    # remains too, via min(args.grace_seconds, remaining) —
                    # grace is time to let a normal exit's stragglers finish
                    # dying, never free extra time before grading, so it must
                    # not let a normal exit outrun deadline_at. Grace keeps its
                    # full, un-shortened value only inside terminate_group's own
                    # TERM->grace->KILL escalation ladder below (that ladder
                    # already runs only once termination is being forced, past
                    # the point where "on time" still means anything).
                    remaining = max(0.0, deadline_monotonic - time.monotonic())
                    normal_exit_grace = min(args.grace_seconds, remaining)
                    confirmed = (_await_group_death(pgid, normal_exit_grace)
                                 or terminate_group(None, pgid, args.grace_seconds))
                    receipt["result"]["termination_confirmed"] = confirmed
                    receipt["result"]["exit_status"] = exit_status
                    if not confirmed:
                        receipt["result"]["state"] = "TERMINATION_UNCONFIRMED"
                    elif time.monotonic() > deadline_monotonic:
                        # Group confirmation itself can consume time —
                        # normal_exit_grace above, or the escalation ladder in
                        # terminate_group() when a grandchild lingers — and that
                        # wait can run past deadline_monotonic even though the
                        # leader exited 0 well before it. DD-9's invariant ("no
                        # output can produce SUCCEEDED once the deadline has
                        # expired") is about when GRADING happens, not just when
                        # the leader exited, so this re-check sits immediately
                        # before grading, not only in the TimeoutExpired branch
                        # above. exit_status is already recorded either way;
                        # schema_valid stays null — a post-deadline result is
                        # never graded, regardless of the leader's exit status.
                        receipt["result"]["state"] = "TIMED_OUT"
                    elif exit_status != 0:
                        receipt["result"]["state"] = "FAILED"
                    elif args.output_envelope is not None or artifact_entries:
                        # The ladder inside grading, in order: envelope
                        # (DD-1) -> output schema -> artifacts (DD-2) ->
                        # a final deadline re-check. The envelope comes
                        # before the schema so a cancelled turn that
                        # happens to have emitted a well-formed verdict is
                        # still a cancelled turn.
                        reasons: list[str] = []
                        timed_out = False
                        if args.output_envelope is not None:
                            envelope = _read_envelope(stdout_path)
                            receipt["result"]["envelope"] = _receipt_envelope(
                                envelope)
                            receipt["result"]["output_sha256"] = _stdout_digest(
                                stdout_path)
                            reasons = _grade_envelope(envelope)
                            if not reasons:
                                # With an envelope declared the verdict
                                # grammar applies to the envelope's `text`,
                                # not to the raw JSON document carrying it —
                                # the document's own bytes never match
                                # VERDICT_RE at line start. An empty `text`
                                # is fine under schema `none`: DD-2's
                                # artifact contract, not stdout length, is
                                # what proves that seat finished.
                                schema_ok = (
                                    args.output_schema != "review"
                                    or bool(VERDICT_RE.search(
                                        envelope["text"] or "")))
                                if not schema_ok:
                                    reasons = ["schema_invalid"]
                            else:
                                schema_ok = False
                            receipt["result"]["schema_valid"] = schema_ok
                        else:
                            ok, digest = _validate_output(
                                stdout_path, args.output_schema)
                            receipt["result"]["output_sha256"] = digest
                            receipt["result"]["schema_valid"] = ok
                            if not ok:
                                reasons = ["schema_invalid"]
                        if artifact_entries:
                            records, artifact_reasons, aborted = _grade_artifacts(
                                artifact_entries, artifact_root,
                                deadline_monotonic,
                                args.require_artifact_allow_unchanged)
                            receipt["result"]["artifacts"] = records
                            reasons = reasons + artifact_reasons
                            timed_out = aborted
                        if timed_out:
                            # A hash abandoned mid-file is a deadline
                            # outcome, not a content verdict.
                            receipt["result"]["state"] = "TIMED_OUT"
                        elif reasons:
                            receipt["result"]["state"] = "INVALID_OUTPUT"
                        elif _deadline_expired(deadline_monotonic):
                            receipt["result"]["state"] = "TIMED_OUT"
                        else:
                            receipt["result"]["state"] = "SUCCEEDED"
                        if reasons:
                            receipt["result"]["invalid_reasons"] = reasons
                    else:
                        ok, digest = _validate_output(stdout_path, args.output_schema)
                        receipt["result"]["output_sha256"] = digest
                        receipt["result"]["schema_valid"] = ok
                        if ok and _deadline_expired(deadline_monotonic):
                            receipt["result"]["state"] = "TIMED_OUT"
                        else:
                            receipt["result"]["state"] = (
                                "SUCCEEDED" if ok else "INVALID_OUTPUT")

                if args.output_envelope is not None \
                        and receipt["result"]["envelope"] is None:
                    # Recording is unconditional even where gating is not:
                    # a TIMED_OUT, FAILED or TERMINATION_UNCONFIRMED
                    # attempt still leaves its envelope in the receipt as
                    # evidence, and none of those states is relabeled by
                    # what it says.
                    receipt["result"]["envelope"] = _receipt_envelope(
                        _read_envelope(stdout_path))

                receipt["timing"]["finished_at"] = _utcnow()
                # DEFER-2: terminal persistence is best-effort. OSError here
                # must not skip claim release or borrow crash exit 9 — the
                # child is already reaped on this happy path. STARTING /
                # start-failed writes above stay strict. Both this tail
                # and the crash handler commit through `_commit_terminal`
                # so an already-terminal on-disk state is never relabeled.
                try:
                    return _commit_terminal(receipt_dir, receipt, claim_path)
                except OSError:
                    print("receipt write failed", file=sys.stderr)
                    claim_path.unlink(missing_ok=True)
                    return EXIT_BY_STATE[receipt["result"]["state"]]
            except Exception:
                # A crash writing the RUNNING receipt, waiting on the child,
                # validating output, or writing the terminal receipt above
                # must still confirm the group is gone and leave a terminal
                # receipt behind — a supervisor crash must never abandon a
                # live process group behind a receipt stuck at RUNNING
                # (that would defeat DD-9/DD-10's duplicate-writer
                # protection exactly where it matters). Run the same
                # TERM->grace->KILL ladder, record whatever it confirms,
                # write the terminal receipt, then let the crash propagate —
                # `main()`'s guard still turns it into exit 9, but by then
                # the receipt is already terminal.
                confirmed = terminate_group(proc, pgid, args.grace_seconds)
                receipt["result"]["termination_confirmed"] = confirmed
                receipt["result"]["state"] = (
                    "CANCELLED" if confirmed else "TERMINATION_UNCONFIRMED")
                receipt["timing"]["finished_at"] = _utcnow()
                # The return value is ignored here on purpose: whichever
                # terminal state ends up on disk (this handler's own
                # conclusion, or an already-terminal state
                # `_commit_terminal` preserved instead), `main()`'s crash
                # guard still turns THIS exception into exit 9 — the
                # receipt being terminal is what matters here, not what
                # `cmd_run` would have returned.
                _commit_terminal(receipt_dir, receipt, claim_path)
                raise
    finally:
        if stdin_f is not subprocess.DEVNULL:
            stdin_f.close()


def _pid_alive(pid: int) -> bool:
    """`os.kill(pid, 0)` semantics for a single pid — the supervisor's own
    pid, not a process group. Analogous to `_group_alive` but distinguishes
    "the child's group lives on" from "the process that was watching its
    deadline is still around"."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but is not ours to signal — still alive


def cmd_status(args) -> int:
    # Same chokepoint `run` uses — a raw caller-supplied id never reaches
    # _receipt_path unvalidated just because this is a read-only command.
    try:
        args.attempt_id = _validated_attempt_id(args.attempt_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    receipt_dir = Path(args.receipt_dir)
    claim_path = receipt_dir / f"{args.attempt_id}.claim"
    # Asked for, not tested for: an `exists()` pre-check answers a question
    # about a moment that has already passed by the time the read runs, and
    # when it guessed wrong the FileNotFoundError escaped to the crash guard —
    # a traceback and exit 9, the status reserved for "a crash, never an
    # attempt outcome". `cancel` has always read first and handled the absence;
    # both commands answer the same question the same way now.
    try:
        receipt = read_receipt(receipt_dir, args.attempt_id)
    except FileNotFoundError:
        if claim_path.exists():
            # A claim sentinel with no receipt yet: `run` has exclusively
            # claimed this attempt id but has not written even the STARTING
            # receipt (normally too brief a window to observe; durably, this
            # is what a crash between the claim and the first receipt write
            # leaves behind). CLAIMED is a report-only label, not a receipt
            # state — it is deliberately absent from STATES, since there is
            # no receipt yet to hold a `result.state` in. It is safe to
            # remove this claim file once the claiming supervisor
            # (recorded nowhere else, since no receipt exists yet — this is
            # the one case status cannot cross-check a supervisor_pid) is
            # otherwise confirmed dead; this command does not do that
            # removal itself (no automatic crashed-claim cleanup).
            print(json.dumps({"attempt_id": args.attempt_id,
                              "state": "CLAIMED"}, indent=2))
            return 0
        # Nothing was ever created under this id. That is a caller mistake,
        # not an attempt outcome — the same sentence and the same status
        # `cancel` gives it.
        print(f"attempt {args.attempt_id!r} is unknown under {receipt_dir} "
              f"— no receipt and no claim", file=sys.stderr)
        return 2
    state = receipt["result"]["state"]
    if state in ("STARTING", "RUNNING"):
        pgid = receipt["process"]["process_group_id"]
        # RUNNING with a dead group means the supervising `run` died before
        # its terminal write: the state is unknown, not failed — the caller
        # sees exactly that and escalates instead of guessing. STARTING has
        # no child pgid yet (it is None until Popen succeeds), so
        # child_alive is always False there — see the STARTING branch below.
        child_alive = bool(pgid) and _group_alive(pgid)
        receipt["process_alive"] = child_alive
        supervisor_pid = receipt["process"].get("supervisor_pid")
        supervisor_alive = bool(supervisor_pid) and _pid_alive(supervisor_pid)
        if state == "STARTING":
            # No child exists yet, so "child_alive" does not apply — the
            # only question is whether anything is still driving this
            # attempt toward RUNNING. A STARTING receipt whose supervisor
            # died is stuck forever otherwise: cancel is the only safe move,
            # the same reasoning as a stale/orphaned RUNNING receipt.
            receipt["supervision"] = "supervised" if supervisor_alive else "stale"
        elif child_alive and supervisor_alive:
            # Both watcher and child are up — the deadline has an owner.
            receipt["supervision"] = "supervised"
        elif child_alive and not supervisor_alive:
            # The child is still running and nothing owns its deadline —
            # cancel is the only safe move; a retry behind a possibly-live
            # writer is the duplicate-writer hazard (F-02).
            receipt["supervision"] = "orphaned"
        elif not child_alive and supervisor_alive:
            # The child already exited and the supervisor that spawned it
            # is still alive — most likely inside the ordinary window
            # between the child dying and the terminal receipt landing
            # (Task 10's group-confirmation wait, or output validation
            # right after `proc.wait` returns). The supervisor still owns
            # this attempt, so this is `supervised`, not `stale` — `stale`
            # is reserved for a receipt nothing is driving toward a
            # terminal state at all.
            receipt["supervision"] = "supervised"
        else:
            # Both dead but the receipt is still RUNNING: the supervisor
            # crashed before writing a terminal state. Same remedy as
            # orphaned — a stale/orphaned RUNNING receipt requires cancel
            # before any retry.
            receipt["supervision"] = "stale"
    print(json.dumps(receipt, indent=2))
    return 0


def cmd_cancel(args) -> int:
    # Same chokepoint `run`/`status` use, before any path is built.
    try:
        args.attempt_id = _validated_attempt_id(args.attempt_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not _finite_positive(args.grace_seconds):
        # cancel takes a duration input too — the same finiteness/sign
        # check `run` applies to --deadline-seconds/--grace-seconds applies
        # here, not just at spawn time.
        print(f"--grace-seconds must be a finite number > 0, got "
              f"{args.grace_seconds!r}", file=sys.stderr)
        return 2
    receipt_dir = Path(args.receipt_dir)
    claim_path = receipt_dir / f"{args.attempt_id}.claim"
    try:
        receipt = read_receipt(receipt_dir, args.attempt_id)
    except FileNotFoundError:
        if claim_path.exists():
            # A claim sentinel with no receipt yet: Task 8's narrow window
            # between a successful O_CREAT|O_EXCL claim and the first
            # STARTING receipt write, or a supervisor that crashed inside
            # it. There is nothing recorded yet to signal (no pgid, no
            # supervisor_pid to check liveness against), so cancel must
            # not guess — it refuses without touching the claim. The
            # documented remedy stays manual (DD-9's no-auto-delete rule
            # for a crashed claim; `status` reports this window as
            # CLAIMED).
            print(f"attempt {args.attempt_id!r} is claimed but never "
                  f"started — confirm the claimer is dead, then delete "
                  f"the claim manually per DD-9; refusing to signal "
                  f"anything", file=sys.stderr)
            return 2
        print(f"attempt {args.attempt_id!r} is unknown under {receipt_dir} "
              f"— no receipt and no claim", file=sys.stderr)
        return 2
    state = receipt["result"]["state"]
    if state not in ("STARTING", "RUNNING"):
        # A terminal state is already someone's final word — refuse without
        # touching the receipt.
        print(f"not RUNNING: {state}", file=sys.stderr)
        return EXIT_BY_STATE.get(state, 2)
    # STARTING may predate the RUNNING write, so process_group_id can still
    # be None here — there is no child yet for a STARTING receipt whose
    # supervisor never got past claiming the attempt.
    pgid = receipt["process"].get("process_group_id")
    supervisor_pid = receipt["process"].get("supervisor_pid")
    supervisor_alive = bool(supervisor_pid) and _pid_alive(supervisor_pid)
    if not supervisor_alive:
        # The receipt is stale: the supervisor that recorded this attempt is
        # dead. If a pgid was recorded, its *identity* can no longer be
        # trusted — an unrelated process may have been assigned the same
        # pgid since; if no pgid was recorded yet (STARTING), there is
        # nothing to signal in the first place. Either way POSIX offers no
        # portable birth-identity check, so refuse to signal rather than
        # risk killpg-ing an innocent process group. Fail closed: mark the
        # receipt unconfirmed and let a human (or the orchestrator's
        # termination_unconfirmed re-route) take over. No signal is sent.
        pgid_desc = pgid if pgid is not None else "none recorded (STARTING)"
        print(f"attempt {args.attempt_id!r} is stale (supervisor "
              f"{supervisor_pid} is dead) — refusing to signal pgid "
              f"{pgid_desc}: its identity cannot be verified after "
              f"supervisor death", file=sys.stderr)
        receipt["result"]["termination_confirmed"] = False
        receipt["result"]["state"] = "TERMINATION_UNCONFIRMED"
        receipt["timing"]["finished_at"] = _utcnow()
        write_receipt(receipt_dir, receipt)
        # The receipt is terminal now — same rule every terminal writer
        # follows (ITEM-V-5): release the claim sentinel so it does not
        # sit forever as a claim with a terminal receipt already behind
        # it.
        claim_path.unlink(missing_ok=True)
        return EXIT_BY_STATE["TERMINATION_UNCONFIRMED"]
    if state == "STARTING":
        # The supervisor is alive but the attempt has not reached RUNNING
        # yet — there is no child process group to signal, and the receipt
        # may still be about to change out from under us (to RUNNING or a
        # pre-spawn refusal). Refuse without touching the receipt; the
        # caller can retry cancel once the attempt reaches RUNNING (or a
        # terminal state on its own).
        print(f"attempt {args.attempt_id!r} is not yet RUNNING (state is "
              f"STARTING, supervisor {supervisor_pid} is alive) — nothing "
              f"to signal yet", file=sys.stderr)
        return 2
    confirmed = terminate_group(None, pgid, args.grace_seconds)
    receipt["result"]["termination_confirmed"] = confirmed
    receipt["result"]["state"] = (
        "CANCELLED" if confirmed else "TERMINATION_UNCONFIRMED")
    receipt["timing"]["finished_at"] = _utcnow()
    write_receipt(receipt_dir, receipt)
    claim_path.unlink(missing_ok=True)  # receipt is terminal now — same rule every terminal writer follows
    return EXIT_BY_STATE[receipt["result"]["state"]]


def cmd_verify_evidence(args) -> int:
    """Exit 0 iff the ids are exactly the valid evidence set: the expected
    count, each with a readable receipt for a supervised, completed review
    attempt (state SUCCEEDED, output_schema "review", schema_valid true),
    and each from a distinct seat (as recorded by the dispatcher). This
    proves a completed reviewer attempt per seat — not that the transport
    opened distinct real model sessions, which no receipt field can show.
    This is the producer-side check behind route_task.py's exact-count
    rule — run it BEFORE typing --isolation-evidence."""
    receipt_dir = Path(args.receipt_dir)
    ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    # Same chokepoint `run`/`status`/`cancel` use — every id is validated
    # BEFORE any path is built or any receipt is read, not merely trusted
    # because it happens to name an existing file. A malformed id (a `../`
    # segment, say) exits 2 here without a single filesystem access, the
    # same shape as every other subcommand's usage errors.
    try:
        ids = [_validated_attempt_id(x) for x in ids]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    expected_models = None
    if args.expect_models is not None:
        expected_models = [m.strip() for m in args.expect_models.split(",")]
        if any(not m for m in expected_models):
            print("error: --expect-models contains an empty token", file=sys.stderr)
            return 2
        if len(set(expected_models)) != len(expected_models):
            print("error: --expect-models contains duplicates — the route's "
                  "reviewer_models are distinct by construction, so a "
                  "duplicate expectation is always a caller mistake",
                  file=sys.stderr)
            return 2
        if len(expected_models) != args.expect_count:
            print(f"error: --expect-models names {len(expected_models)} "
                  f"model(s) but --expect-count is {args.expect_count}",
                  file=sys.stderr)
            return 2
    if args.expect_fingerprint is not None and not HEX64_RE.match(args.expect_fingerprint):
        print("error: --expect-fingerprint must be 64 lowercase hex chars",
              file=sys.stderr)
        return 2
    problems = []
    declared_models = []
    if len(set(ids)) != args.expect_count:
        problems.append(
            f"expected exactly {args.expect_count} distinct id(s), "
            f"got {len(set(ids))}")
    seats = []
    for attempt_id in sorted(set(ids)):
        try:
            receipt = read_receipt(receipt_dir, attempt_id)
        except (OSError, json.JSONDecodeError):
            # ITEM-V-5: a claim sentinel with no receipt yet (Task 8's
            # claim-then-STARTING window, or a supervisor that crashed
            # inside it) is not a completed reviewer attempt either way,
            # but the caller should be able to tell "claimed, maybe still
            # alive" apart from "nothing was ever claimed under this id".
            claim_path = receipt_dir / f"{attempt_id}.claim"
            if claim_path.exists():
                problems.append(f"{attempt_id}: no readable receipt (claim only)")
            else:
                problems.append(f"{attempt_id}: no readable receipt")
            continue
        state = receipt["result"]["state"]
        if state != "SUCCEEDED":
            problems.append(f"{attempt_id}: state is {state}, not SUCCEEDED")
        if receipt.get("output_schema") != "review":
            problems.append(
                f"{attempt_id}: output_schema is "
                f"{receipt.get('output_schema')!r}, not 'review'")
        if receipt.get("result", {}).get("schema_valid") is not True:
            problems.append(f"{attempt_id}: schema_valid is not true")
        if expected_models is not None and receipt.get("model_id") is None:
            problems.append(f"{attempt_id}: model_id is null — cannot match "
                            f"--expect-models")
        if args.expect_fingerprint is not None and \
                receipt.get("decision_fingerprint") != args.expect_fingerprint:
            problems.append(f"{attempt_id}: decision_fingerprint is "
                            f"{receipt.get('decision_fingerprint')!r}, not the "
                            f"expected value")
        declared_models.append(receipt.get("model_id"))
        seats.append(receipt.get("seat"))
    if len(seats) != len(set(seats)):
        problems.append(f"seats are not distinct: {sorted(seats)}")
    if expected_models is not None:
        got = sorted(m for m in declared_models if m is not None)
        # A null model_id is already reported per receipt above; comparing the
        # multiset again there would say the same thing twice with a confusing
        # "does not match" that names a shorter list than the caller supplied.
        if not any("cannot match --expect-models" in p for p in problems) \
                and got != sorted(expected_models):
            problems.append(f"declared models {got} do not match expected "
                            f"{sorted(expected_models)}")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="supervise one attempt to a terminal state")
    run.add_argument("--attempt-id", required=True)
    run.add_argument("--receipt-dir", required=True)
    run.add_argument("--deadline-seconds", type=float, required=True)
    run.add_argument("--grace-seconds", type=float, default=15.0)
    run.add_argument("--seat", required=True,
                     help="worker | reviewer-1 | reviewer-2 | judge")
    run.add_argument("--runtime", default=None)
    run.add_argument("--model-id", default=None)
    run.add_argument("--effort-native", default=None)
    run.add_argument("--permission-mode", default=None)
    run.add_argument("--decision-fingerprint", default=None,
                     help="route JSON's decision_fingerprint (64 lowercase hex)")
    run.add_argument("--policy-sha256", dest="policy_sha256", default=None,
                     help="route JSON's policy_sha256 (64 lowercase hex)")
    run.add_argument("--transport-id", default=None,
                     help="transports table path, e.g. claude_code.to_openai")
    run.add_argument("--host-cli-version", default=None,
                     help="caller-known CLI version; omit rather than probe")
    run.add_argument("--prompt-file", default=None,
                     help="fed to the child's stdin; omit for DEVNULL")
    run.add_argument("--output-schema", choices=["none", "review"],
                     default="none")
    # --- Grok seat integrity (2026-08-25 design) ----------------------
    # All four are declared here together so the parser has one shape; the
    # grading semantics of the last three belong to DD-3.
    run.add_argument("--output-envelope", choices=list(ENVELOPE_FORMATS),
                     default=None,
                     help="version-named stdout contract to grade against; "
                          "a cancelled grok turn exits 0, so its stopReason "
                          "is the only machine-readable finish evidence")
    run.add_argument("--session-evidence", default=None,
                     help="FORMAT:DIR, e.g. grok-session-v1:<session dir>. "
                          "The caller computes DIR; the supervisor derives "
                          "no path (see references/adapters.md)")
    run.add_argument("--session-id", default=None,
                     help="the session UUID this attempt declared to the "
                          "child (grok's -s); the axis attempt-binding "
                          "cross-proves stdout against the session dir")
    run.add_argument("--require-artifact", action="append", default=[],
                     metavar="PATH",
                     help="a file this attempt must have written (or "
                          "changed) before it can be SUCCEEDED; repeatable")
    run.add_argument("--artifact-root", default=None, metavar="DIR",
                     help="containment fence for every --require-artifact; "
                          "resolved and pinned before spawn")
    run.add_argument("--require-artifact-sha256", action="append", default=[],
                     metavar="PATH=HEX64",
                     help="fixed-content contract for one declared artifact; "
                          "must be 1:1 with --require-artifact")
    run.add_argument("--require-artifact-allow-unchanged", action="store_true",
                     help="accept an artifact whose content equals its "
                          "pre-spawn baseline (deterministic regeneration); "
                          "recorded in the receipt as an explicit opt-in")
    run.add_argument("--require-artifact-baseline-max-bytes", type=float,
                     default=ARTIFACT_BASELINE_MAX_BYTES,
                     help="per-file cap on the pre-spawn baseline hash, "
                          "which has no deadline to be bounded by")
    run.add_argument("--expect-effective-agent", default=None,
                     help="require the session summary's agent_name to "
                          "equal this exactly; requires --session-evidence")
    run.add_argument("argv", nargs="+",
                     help="command to execute, after `--`")

    status = sub.add_parser("status", help="print the receipt; liveness-check RUNNING")
    status.add_argument("--attempt-id", required=True)
    status.add_argument("--receipt-dir", required=True)

    cancel = sub.add_parser("cancel", help="terminate a RUNNING attempt and confirm")
    cancel.add_argument("--attempt-id", required=True)
    cancel.add_argument("--receipt-dir", required=True)
    cancel.add_argument("--grace-seconds", type=float, default=15.0)

    verify = sub.add_parser(
        "verify-evidence",
        help="check ids against receipts before --isolation-evidence")
    verify.add_argument("--receipt-dir", required=True)
    verify.add_argument("--ids", required=True,
                        help="comma-separated attempt ids")
    verify.add_argument("--expect-count", type=int, required=True,
                        help="the route's reviewer seat count — exactly")
    verify.add_argument("--expect-models", default=None,
                        help="comma-separated DECLARED model ids — a multiset "
                             "match against receipts' model_id (seat<->model "
                             "pairing is deliberately not checked: --seat is a "
                             "non-semantic label; independence needs N distinct "
                             "models completing, not a label assignment). This "
                             "checks the declared/requested identity, not the "
                             "served one.")
    verify.add_argument("--expect-fingerprint", default=None,
                        help="route decision_fingerprint (64 lowercase hex) "
                             "every receipt must carry")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"run": cmd_run, "status": cmd_status, "cancel": cmd_cancel,
                "verify-evidence": cmd_verify_evidence}
    try:
        return handlers[args.command](args)
    except Exception:  # noqa: BLE001 — a crash must not borrow an outcome code
        import traceback
        traceback.print_exc()
        return 9


if __name__ == "__main__":
    sys.exit(main())
