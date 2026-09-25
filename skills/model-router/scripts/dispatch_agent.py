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
  not) before the crash is re-raised as exit 9 (exit5 takes precedence when
  termination is unconfirmed). A supervisor crash must
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
7 CANCELLED (cancel: confirmed); 8 receipt publication failed; 9 internal error — a crash, never an
attempt outcome (the same lesson route_task.py's exit 5 encodes). status
exits 0 and prints the receipt. verify-evidence exits 0 iff the evidence
set is exactly valid.

POSIX only: process-group control uses start_new_session and os.killpg.
"""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import receipt_guard
from secure_io import NotARegularFile, open_regular as _open_regular

STATES = (
    "STARTING", "RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT",
    "CANCELLED", "START_FAILED", "TERMINATION_UNCONFIRMED", "INVALID_OUTPUT",
)
# CLAIMED is deliberately NOT a member: it is `status`'s report-only label
# for "a claim sentinel exists but no receipt does yet" (see cmd_status) —
# there is no receipt in that window to hold a `result.state`, so it is not
# a receipt state and never appears in a receipt's `result.state` field.

PUBLICATION_FAILED = 8


class UnconfirmedPublicationError(ValueError):
    """Publication error with a stronger possibly-live-writer hold."""


EXIT_BY_STATE = {
    "SUCCEEDED": 0, "FAILED": 1, "TIMED_OUT": 3, "START_FAILED": 4,
    "TERMINATION_UNCONFIRMED": 5, "INVALID_OUTPUT": 6, "CANCELLED": 7,
}

# Candidate tokens are confined to one line; `_verdict_of` then validates
# whether they belong to the final section or the supported run-in recovery.
VERDICT_ANYWHERE_RE = re.compile(r"verdict:[ \t]*(PASS_WITH_CHANGES|PASS|FAIL)\b")
# The schema's second field, on the line immediately after the verdict, and in
# range. `confidence: 1.9` is not a confidence, and a `confidence:` paragraphs
# away is not this verdict's.
CONFIDENCE_NEXT_LINE_RE = re.compile(
    r"[ \t]*\r?\n[ \t]*confidence:\s*(?:0(?:\.\d+)?|1(?:\.0+)?)(?![\d.])")

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
GROK_ENVELOPE_FORMAT = "grok-headless-json-v1"
# 2026-09-25 DD-B9. Codex has no single-document output, so its two formats
# are read line by line rather than through the JSON-document decoder below.
# `codex-exec-text-v1` is plain mode: stdout carries the final answer, stderr a
# banner whose `model:` line is the only place codex names the model (header-
# reported, never `served_models`), and a `tokens used` footer that EXCLUDES
# cached input. `codex-exec-json-v1` is `--json` mode: JSONL events whose
# `turn.completed.usage` carries the total `input_tokens` — and no model name
# anywhere (measured 2026-09-25, codex-cli 0.157.0).
CODEX_TEXT_FORMAT = "codex-exec-text-v1"
CODEX_JSON_FORMAT = "codex-exec-json-v1"
CODEX_FORMATS = (CODEX_TEXT_FORMAT, CODEX_JSON_FORMAT)
ENVELOPE_FORMATS = (GROK_ENVELOPE_FORMAT, "claude-print-json-v1", *CODEX_FORMATS)

# Which document key carries each envelope field, per format. The shapes are
# analogous, not identical: grok discriminates a failure with a top-level
# `type` ("error"), while a Claude `--output-format json` document is always
# `type: "result"` and discriminates with `subtype` ("success", "error_*").
# Both name the finishing reason and the served models the same way once
# mapped, which is why one gate serves both.
ENVELOPE_FIELDS = {
    "grok-headless-json-v1": {"stop_reason": "stopReason",
                              "session_id": "sessionId",
                              "text": "text", "error_type": "type"},
    "claude-print-json-v1": {"stop_reason": "stop_reason",
                             "session_id": "session_id",
                             "text": "result", "error_type": "subtype"},
    # The codex formats are not key-mapped documents; they are decoded by
    # `_decode_codex_text` / `_decode_codex_json`. Present so the import-time
    # agreement check below still covers every declared format.
    CODEX_TEXT_FORMAT: {},
    CODEX_JSON_FORMAT: {},
}
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
# The finishing reason each format must show, None where the format has none
# (plain codex output carries no turn-completion record at all). For
# `codex-exec-json-v1` the "stop reason" is the last `turn.*` event's type.
ENVELOPE_OK_STOP_REASONS = {
    GROK_ENVELOPE_FORMAT: ENVELOPE_OK_STOP_REASON,
    "claude-print-json-v1": ENVELOPE_OK_STOP_REASON,
    CODEX_TEXT_FORMAT: None,
    CODEX_JSON_FORMAT: "turn.completed",
}
# Codex plain-mode stderr reads: the banner and any metadata warning are at
# the head, the `tokens used` footer at the tail. Both are bounded.
CODEX_STDERR_HEAD_BYTES = 64 * 1024
CODEX_STDERR_TAIL_BYTES = 4 * 1024
CODEX_METADATA_WARNING_PREFIX = "warning: Model metadata for "
# DD-B9: the one declared exception to the guard/codex-sandbox refusal.
NESTED_SANDBOX_OPT_INS = ("no-file-access",)
CLAUDE_RESULT_DOC_TYPE = "result"
CLAUDE_OK_SUBTYPE = "success"

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

# --- Session evidence (DD-3) ------------------------------------------
#
# The effective agent identity and the effective sandbox profile appear
# nowhere on stdout — only in the session directory grok writes as it runs.
# That is what lets a receipt carry REQUESTED and EFFECTIVE side by side,
# which is the whole of G2: issue #14's "requested acceptEdits, effective
# grok-build-plan" becomes visible on one page instead of being inferred
# from an absence.
#
# The two surfaces are graded differently on purpose. `summary.json` and
# its `agent_name`/`info` fields are officially documented [UG-17], so they
# GATE. `events.jsonl` is not in the documented layout at all, so it is
# recorded when available and never gates — including on the size axis.
SUMMARY_MAX_BYTES = 1024 * 1024
EVENTS_TAIL_BYTES = 256 * 1024

# --- The `result.invalid_reasons` vocabulary (DD-5) --------------------
#
# No new terminal state: a cancelled turn, a violated envelope, missing
# artifacts and unbindable session evidence all converge on the existing
# INVALID_OUTPUT (exit 6) — "an attempt that finished inside its deadline
# with exit 0 but did not produce the contracted output", which is what
# INVALID_OUTPUT already meant. `STATES`, `EXIT_BY_STATE` and the
# documented state machine stay invariant; the CAUSE is what gains
# resolution, and it lives here.
#
# That distinction is operationally load-bearing. An INVALID_OUTPUT whose
# reasons name a cancellation or unreadable evidence is a seat RECIPE or
# transport defect, not a model capability failure — re-dispatch the same
# model once after fixing the recipe, and do not report it through
# `--prior-failures` as if the model had failed. `references/
# review-policy.md` carries that guidance for orchestrators.
#
# A reason is either an exact member of the first tuple, or
# `"<prefix>:<detail>"` for a prefix in the second. `_reason` is the only
# constructor for the parameterized form and REFUSES an unregistered
# prefix, so the vocabulary cannot drift by someone f-stringing a new one
# in at a call site.
INVALID_REASON_FLAGS = (
    "receipt_guard_unavailable",   # requested guard could not be admitted
    "deadline_expired_before_launch",  # target was never started
    "envelope_unparseable",         # stdout was not one JSON object
    "evidence_oversized",           # a gate surface exceeded its budget
    "schema_invalid",               # --output-schema was not satisfied
    "session_evidence_unreadable",  # declared, but summary.json is not there
    "session_evidence_unbound",     # evidence belongs to some other attempt
    "sandbox_event_missing",        # --expect-sandbox-enforced, no ProfileApplied
    "sandbox_event_disagreement",   # two reserved locations, two different records
    "envelope_reported_error",      # the document says so itself (is_error)
    "envelope_invalid_error_discriminator",  # Claude requires a boolean is_error
)
INVALID_REASON_PREFIXES = (
    "envelope_stop_reason",         # :<the non-end_turn value>
    "envelope_document_type",       # :<observed top-level `type`>
    "envelope_subtype",             # :<observed non-success `subtype`>
    "artifact_missing",             # :<path>
    "artifact_empty",               # :<path>
    "artifact_not_regular_file",    # :<path> — symlink, FIFO, device
    "artifact_escaped_root",        # :<path>
    "artifact_unchanged",           # :<path> — a prior attempt's leftover
    "artifact_multiply_linked",     # :<path> — a second name for the inode
    "artifact_identity_replaced",   # :<path> — not the inode pinned pre-spawn
    "sandbox_event_identity_replaced",  # :<path> — the ProfileApplied log is
                                    # not the inode reserved pre-spawn
    "artifact_reservation_cleanup_failed",  # :<path> — the supervisor's own
                                    # pre-spawn reservation is still on disk
                                    # because withdrawing it failed
    "artifact_sha256_mismatch",     # :<path>
    "effective_agent_mismatch",     # :<observed agent_name>
    "effective_sandbox_mismatch",   # :<observed sandbox_profile>
    "session_terminal_event",       # :<non-completed turn_ended outcome>
    "sandbox_profile_not_enforced",  # :<observed enforced value>
    "sandbox_profile_event_mismatch",  # :<observed profile name>
)

# A format in the choices with no field map is a KeyError inside the grading
# region, which the post-spawn handler reports as CANCELLED — the attempt's
# real outcome lost to a typo. Fail at import instead.
assert set(ENVELOPE_FIELDS) == set(ENVELOPE_FORMATS), (
    "ENVELOPE_FIELDS and ENVELOPE_FORMATS disagree: "
    f"{set(ENVELOPE_FORMATS) ^ set(ENVELOPE_FIELDS)}")

MAKER_SEAT_PROFILE = "grok-maker-v1"
MAKER_SANDBOX_PROFILE = "dmr-maker-v1"
# Every `$GROK_HOME`-relative location a grok build has written its
# ProfileApplied log to, current first. 1.0.40 moved it under `sessions/`;
# 1.0.13 wrote it at the home root. All of them are reserved before spawn,
# all of them are read at grading, and the shipped `mechanism_maker` denies
# the child Write/Edit on each — a location that is read but not denied is a
# forgery channel, so the two lists are the same list.
SANDBOX_EVENT_RELPATHS = (("sessions", "sandbox-events.jsonl"),
                          ("sandbox-events.jsonl",))
MAKER_SEAT_REQUIRED = (
    ("--child-cwd", "child_cwd"),
    ("--require-single-linked-cwd", "require_single_linked_cwd"),
    ("--grok-home", "grok_home"),
    ("--grok-auth-seed", "grok_auth_seed"),
    ("--expect-sandbox-enforced", "expect_sandbox_enforced"),
    ("--output-envelope", "output_envelope"),
    ("--session-evidence", "session_evidence"),
    ("--expect-effective-agent", "expect_effective_agent"),
    ("--expect-sandbox-profile", "expect_sandbox_profile"),
)


def _reason(prefix: str, detail: object) -> str:
    """Build one parameterized `invalid_reasons` member.

    Raises on an unregistered prefix rather than accepting it: a vocabulary
    that anyone can extend at a call site is not a vocabulary, and the
    consumers of these strings (retry policy, review policy) key off the
    prefix.
    """
    if prefix not in INVALID_REASON_PREFIXES:
        raise ValueError(f"undocumented invalid_reason prefix {prefix!r}")
    text = "" if detail is None else str(detail)
    return f"{prefix}:{text or '<absent>'}"


def _is_documented_reason(reason: str) -> bool:
    if reason in INVALID_REASON_FLAGS:
        return True
    prefix, sep, _ = reason.partition(":")
    return bool(sep) and prefix in INVALID_REASON_PREFIXES


def _audit_single_linked_tree(root: Path) -> str | None:
    """Refuse any second name for a file in `root` — hard link or symlink.

    Walks without following symlinks. Any stat error is a refusal: a tree
    we cannot inspect is not a tree we can claim is single-linked. This is
    the supervisor-side prevention for the grok maker hard-link escape —
    path-scoped Write/Edit and Seatbelt both see the inside path, so the
    only real prevention is not launching if a second name is already there.

    A symlink is such a name and used to walk through: it is neither a
    regular file nor a directory, so the loop below skipped it. Whether the
    CLI's permission rules match an alias or its target is not something this
    repository has measured, and the hard-link finding says a path-scoped
    rule cannot tell the two apart. Refusing the alias does not need the
    answer.
    """
    try:
        root_st = os.lstat(root)
    except OSError as exc:
        return (f"--child-cwd {str(root)!r} could not be read: "
                f"{os.strerror(exc.errno)}")
    if stat.S_ISLNK(root_st.st_mode):
        return f"--child-cwd {str(root)!r} is a symlink"
    if not stat.S_ISDIR(root_st.st_mode):
        return f"--child-cwd {str(root)!r} is not a directory"
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        return (f"--child-cwd contains an unreadable path "
                                f"{entry.path}: {os.strerror(exc.errno)}")
                    if stat.S_ISLNK(st.st_mode):
                        return (f"--child-cwd {str(root)!r} contains the "
                                f"symlink {entry.path}; a symlink is a second "
                                f"name for a file outside the tree this audit "
                                f"just walked, which the path-scoped write "
                                f"rules cannot distinguish either")
                    if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
                        return (f"--child-cwd {str(root)!r} contains "
                                f"{entry.path} with {st.st_nlink} links; "
                                f"a hard link inside the cwd aliases an "
                                f"inode the path-scoped write rules cannot "
                                f"distinguish")
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(Path(entry.path))
        except OSError as exc:
            return (f"--child-cwd {str(current)!r} could not be scanned: "
                    f"{os.strerror(exc.errno)}")
    return None


def _prepare_grok_home(home: Path, auth_seed: Path | None,
                       sandbox_profile: str | None,
                       pins: dict) -> str | None:
    """Create an attempt-private GROK_HOME. Never follows a symlink.

    `pins` is filled with `{relpath_tuple: (st_dev, st_ino)}` for every
    reserved ProfileApplied location, and is what grading compares against.
    """
    if home.exists() or os.path.lexists(home):
        try:
            st = os.lstat(home)
        except OSError as exc:
            return (f"--grok-home {str(home)!r} could not be read: "
                    f"{os.strerror(exc.errno)}")
        if stat.S_ISLNK(st.st_mode):
            return f"--grok-home {str(home)!r} is a symlink"
        return (f"--grok-home {str(home)!r} already exists; reuse of an "
                "attempt home is refused")
    try:
        home.mkdir(mode=0o700, parents=True)
    except OSError as exc:
        return (f"--grok-home {str(home)!r} could not be created: "
                f"{os.strerror(exc.errno)}")
    if auth_seed is not None:
        dest = home / "auth.json"
        if dest.exists() or os.path.lexists(dest):
            return (f"--grok-home {str(home)!r} already contains auth.json; "
                    "reuse of an attempt home is refused")
        try:
            src_st = os.lstat(auth_seed)
        except OSError as exc:
            return (f"--grok-auth-seed {str(auth_seed)!r} could not be read: "
                    f"{os.strerror(exc.errno)}")
        if not stat.S_ISREG(src_st.st_mode) or stat.S_ISLNK(src_st.st_mode):
            return f"--grok-auth-seed {str(auth_seed)!r} is not a regular file"
        try:
            shutil.copyfile(auth_seed, dest)
            os.chmod(dest, 0o600)
        except OSError as exc:
            return (f"--grok-auth-seed could not be copied: "
                    f"{os.strerror(exc.errno)}")
    if sandbox_profile:
        toml = home / "sandbox.toml"
        try:
            toml.write_text(
                f"[profiles.{sandbox_profile}]\nextends = \"workspace\"\n")
        except OSError as exc:
            return (f"--grok-home sandbox.toml could not be written: "
                    f"{os.strerror(exc.errno)}")
    for parts in SANDBOX_EVENT_RELPATHS:
        events = home.joinpath(*parts)
        try:
            events.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(events, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.set_inheritable(fd, False)
            st = os.fstat(fd)
        except OSError as exc:
            return (f"--grok-home {'/'.join(parts)} could not be reserved: "
                    f"{os.strerror(exc.errno)}")
        # Pin the inode this supervisor created, the same way --require-artifact
        # pins its reservations. Reserving a NAME only proves something is there
        # at grading; a child that unlinks the reservation and writes a fresh
        # file of its own gets a forged record read as the logger's. Measured on
        # grok 1.0.40: the logger APPENDS to this inode (the reservation's 0600
        # survives the run), so pinning costs nothing a real run needs.
        #
        # The descriptor is HELD, not closed, for the same reason the artifact
        # pin holds one: an unlinked inode is freed, and a comparison against a
        # bare (dev, ino) would rest on the filesystem never recycling that
        # number. An open descriptor keeps the inode alive for the attempt, so
        # a recycled number cannot be the reserved one. It is non-inheritable,
        # so the child never sees it, and it goes back with the process.
        pins[parts] = {"identity": (st.st_dev, st.st_ino), "fd": fd}
    return None


def _last_profile_applied(path: Path, pin: tuple | None
                          ) -> tuple[dict | None, str]:
    """`(record, status)` for one reserved events file.

    `status` is `"ok"` when the file is the reservation this supervisor made
    and was read; `"absent"` when it is gone; `"untrusted"` when something is
    there but is not that reservation — not a regular file, more than one link
    to its inode, not the pinned (dev, ino), or an unreadable fd. Absence and
    substitution are separate words because a caller triaging a failed write
    seat has to tell a CLI that moved its log from a child that swapped it.

    The identity check is the half that absence alone does not cover. Making
    a missing reservation fail stopped a child from DELETING the record that
    disagreed with it; without the pin it could unlink and recreate the same
    name around a record of its own, and a fresh single-linked regular file
    reads exactly like the logger's.

    ABSENCE IS TAMPERING, not emptiness. The supervisor creates every
    candidate location before spawn and nothing legitimate removes one, so a
    path that is gone at grading was unlinked by the child. Reading it as an
    empty file let a child suppress the location whose record disagreed and
    keep the one that passed — the mismatch gate below cannot fire on a
    record that is no longer there. A location grok never wrote is still
    PRESENT — the supervisor's own empty reservation — which is the normal
    one-writer case and not a failure.
    """
    # O_NOFOLLOW guards the final element only, and this log now lives one
    # directory down. A `sessions` swapped for a symlink between reservation
    # and grading would be followed silently, so the containing directory is
    # checked in its own right before the file is opened. It runs for the
    # root-level location too, where it checks $GROK_HOME itself: narrowing
    # it to the nested path would be a special case earning nothing.
    if path.parent != path.parent.parent:
        try:
            parent = os.lstat(path.parent)
        except OSError:
            return None, "untrusted"
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
            return None, "untrusted"
    try:
        fd, st = _open_regular(path)
    except FileNotFoundError:
        return None, "absent"
    except (OSError, NotARegularFile):
        return None, "untrusted"
    try:
        if st.st_nlink != 1 or (pin is not None
                                and (st.st_dev, st.st_ino) != pin):
            os.close(fd)
            return None, "untrusted"
        with os.fdopen(fd, "rb") as f:
            data = f.read()
    except OSError:
        return None, "untrusted"
    applied = None
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("event_type") == "ProfileApplied":
            applied = obj
    return applied, "ok"


def _grade_sandbox_events(home: Path, expected_profile: str, pins: dict
                          ) -> tuple[dict, list[str]]:
    """Read the reserved `ProfileApplied` log for `enforced`.

    grok moved this file: 1.0.13 wrote `$GROK_HOME/sandbox-events.jsonl` and
    1.0.40 writes `$GROK_HOME/sessions/sandbox-events.jsonl`. Reading one
    hard-coded location made an enforced sandbox indistinguishable from an
    absent one — the attempt failed closed, which is safe, but the seat was
    unusable on the shipped CLI. Every candidate location is reserved before
    spawn and every one is read here, so a CLI that moves the file again
    degrades to `sandbox_event_missing` rather than to a forged pass.

    Two records that disagree are a failure, not a vote: the supervisor has
    no way to tell which run wrote which file, and picking either one would
    let a stale or planted record answer for this attempt.
    """
    view: dict = {"path": None, "enforced": None, "profile": None,
                  "paths_read": [str(home.joinpath(*parts))
                                 for parts in SANDBOX_EVENT_RELPATHS]}
    found: list[tuple[Path, dict]] = []
    for parts in SANDBOX_EVENT_RELPATHS:
        path = home.joinpath(*parts)
        reserved = pins.get(parts)
        applied, status = _last_profile_applied(
            path, reserved["identity"] if reserved else None)
        if status != "ok":
            view["path"] = str(path)
            return view, ["sandbox_event_missing" if status == "absent"
                          else _reason("sandbox_event_identity_replaced", path)]
        if applied is not None:
            found.append((path, applied))
    if not found:
        return view, ["sandbox_event_missing"]
    path, applied = found[0]
    view["path"] = str(path)
    view["enforced"] = applied.get("enforced")
    view["profile"] = applied.get("profile")
    reasons: list[str] = []
    for other_path, other in found[1:]:
        if (other.get("enforced"), other.get("profile")) != (
                view["enforced"], view["profile"]):
            # Both sides, each beside its own path. Recording one file's path
            # with another file's values describes a state no file was in.
            view["path"] = None
            view["disagreement"] = [
                {"path": str(where), "profile": rec.get("profile"),
                 "enforced": rec.get("enforced")} for where, rec in found]
            view["enforced"] = None
            view["profile"] = None
            # Its own reason. `sandbox_profile_event_mismatch:<observed>` means
            # the sandbox that ran was not the one asked for; two locations that
            # disagree may both name the right profile and differ only on
            # `enforced`, and reporting that as a profile mismatch tells a
            # control loop keying off the prefix the wrong thing. The differing
            # records are in `view["disagreement"]`.
            return view, ["sandbox_event_disagreement"]
    if view["profile"] != expected_profile:
        reasons.append(_reason("sandbox_profile_event_mismatch",
                               view["profile"]))
    if view["enforced"] is not True:
        reasons.append(_reason("sandbox_profile_not_enforced",
                               view["enforced"]))
    return view, reasons


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
    fd, _ = _open_regular(_receipt_path(receipt_dir, attempt_id))
    with os.fdopen(fd, "rb") as f:
        data = f.read(ENVELOPE_MAX_BYTES + 1)
    if len(data) > ENVELOPE_MAX_BYTES:
        raise OSError(errno.EFBIG, "receipt exceeds read limit")
    return json.loads(data)


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


def _validate_output(stdout_path: Path,
                     output_schema: str) -> tuple[bool, str | None, str | None, bool]:
    """`(ok, digest, verdict, recovered)`.

    The verdict travels with the grade on this path too: an orchestrator that
    has to re-parse the stdout file to learn what the seat said is one grammar
    change away from disagreeing with the receipt.
    """
    data, _ = _capped_bytes(stdout_path, ENVELOPE_MAX_BYTES)
    if data is None or not data.strip():
        return False, None, None, False
    digest = hashlib.sha256(data).hexdigest()
    if output_schema != "review":
        return True, digest, None, False
    verdict, recovered = _verdict_of(data.decode(errors="replace"))
    return verdict is not None, digest, verdict, recovered


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


# --- Artifact IDENTITY: the pin (DD-2, R2-C1) ---------------------------
#
# `st_nlink` is sampled at exactly two instants — the pre-spawn baseline and
# grading — and the child owns everything in between. That gap is a whole
# attack, not an edge case: hard-link an outside inode at the required path,
# write through it, unlink that name, drop a fresh single-linked decoy, and
# both samples read 1 while an external file was overwritten under a
# `contained: true` / `changed: true` SUCCEEDED receipt.
#
# The fix is an IDENTITY the supervisor holds across the gap rather than two
# samples of a property. Before spawn every required path is pinned to one
# `(st_dev, st_ino)`: an existing artifact to its own inode, an ABSENT one to
# a reservation this supervisor creates with O_CREAT|O_EXCL. Grading requires
# the required path to still name that inode.
#
# The parent holds an open descriptor on the pinned inode for the whole
# attempt, and that is not bookkeeping: an unlinked inode's NUMBER is free
# for the next file created, so a pin that had already been closed would let
# a recycled number read as "the same artifact". Holding the descriptor keeps
# the inode allocated, which is what makes the comparison mean anything. The
# descriptor is non-inheritable (Python's `os.open`/`os.dup` default, asserted
# by a test rather than assumed) so the child never receives a handle to the
# very inode the pin exists to protect, and `cmd_run` releases every pin on
# every exit path, including the one that leaves through the crash handler.
#
# What this does NOT claim: a supervisor cannot stop an unconfined child from
# writing anywhere it likes. The contract is about PROOF — an attempt whose
# required path stopped naming the pinned inode never receives a successful
# receipt.


def _reservation_body(attempt_id: str) -> bytes:
    """The bytes a reservation holds until the child overwrites them.

    Non-empty on purpose. An empty reservation would make "the child never
    produced the artifact" and "the child truncated it to nothing"
    indistinguishable, and those are `artifact_missing` and `artifact_empty`
    — two different diagnoses with two different remedies. The text says what
    the file is and what the contract requires, because an agent that opens
    it deserves to read that rather than guess.
    """
    return (f"deep-model-router reserved this path for attempt "
            f"{attempt_id} before spawn. Write it IN PLACE; renaming a "
            f"different file over this one replaces the inode and voids "
            f"the proof.\n").encode()


def _reserve_artifact(entry: dict, attempt_id: str) -> str | None:
    """Pin an ABSENT required path by creating it. Returns an error string.

    O_CREAT|O_EXCL, so this either creates the inode or refuses — it never
    adopts something that appeared between the ENOENT and here. Missing
    parent directories are created first, and they are necessarily inside
    `--artifact-root`: the path was resolved and contained before this ran.
    """
    path = entry["path"]
    body = _reservation_body(attempt_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR
                     | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        return (f"--require-artifact {entry['declared']!r} is absent and "
                f"could not be reserved pre-spawn: {os.strerror(exc.errno)} "
                f"({errno.errorcode.get(exc.errno, exc.errno)}); an absent "
                f"required path must be pinned before the child runs")
    # A successful `write` is allowed to consume FEWER bytes than it was
    # handed, so one call is not a file. The digest and size recorded below
    # describe the WHOLE body; if the disk holds less than that, grading
    # hashes a truncated marker, fails to recognise it as the reservation,
    # and reports the supervisor's own leftover bytes as a `changed: true`
    # artifact — successful proof handed to a child that did nothing. So
    # the write is a loop, and anything short of the whole body is a
    # PREFLIGHT failure: the descriptor closes, the path this function
    # created goes away again, and the caller returns exit 2 before any
    # claim or receipt exists. Zero progress ends the loop rather than
    # spinning on it — a `write` that keeps succeeding without moving is a
    # failure that never raises.
    written = 0
    try:
        while written < len(body):
            sent = os.write(fd, body[written:])
            if sent <= 0:
                break
            written += sent
        st = os.fstat(fd)
    except OSError as exc:
        written, st = -1, None
        detail = os.strerror(exc.errno)
    if st is None or written != len(body):
        if st is not None:
            detail = f"wrote {written} of {len(body)} bytes"
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        return (f"--require-artifact {entry['declared']!r} reservation could "
                f"not be written: {detail}")
    os.set_inheritable(fd, False)
    entry["pin_fd"] = fd
    entry["pin_dev"] = st.st_dev
    entry["pin_ino"] = st.st_ino
    entry["reservation_sha256"] = hashlib.sha256(body).hexdigest()
    entry["reservation_size"] = len(body)
    return None


def _withdraw_reservation(entry: dict) -> bool:
    """Remove a reservation the child never wrote, restoring absence.

    A supervisor that reserved a path and then crashed must not leave a file
    standing where the caller declared there was none. Withdrawal is refused
    unless the path STILL names the pinned inode, still has exactly one name,
    and still holds exactly the reservation bytes — so a file the child
    actually produced is never deleted by the supervisor that asked for it.
    Called while the pin is still open, so the inode number under test cannot
    have been recycled underneath the check.
    """
    if entry.get("reservation_sha256") is None:
        return False
    path = entry["path"]
    try:
        fd, st = _open_regular(path)
    except (NotARegularFile, OSError):
        return False
    if (st.st_dev, st.st_ino) != (entry["pin_dev"], entry["pin_ino"]) \
            or st.st_nlink != 1 or st.st_size != entry["reservation_size"]:
        os.close(fd)
        return False
    if _hash_artifact(fd, None) != entry["reservation_sha256"]:
        return False  # _hash_artifact consumed the fd
    try:
        os.unlink(path)
    except OSError:
        return False
    # Cleared only now: while the file is still there, the digest is the one
    # fact that tells a reservation apart from a child's output, and dropping
    # it over a file that survived the unlink would leave the entry claiming
    # it holds no reservation while holding one.
    entry["reservation_sha256"] = None
    return True


def _release_artifact_pins(entries: list[dict]) -> None:
    """Give back every descriptor this attempt pinned. Idempotent.

    Withdrawal runs BEFORE the close, so the identity it checks is still
    anchored by the open descriptor. `cmd_run` calls this from a `finally`
    that wraps everything after the pins are taken, which is what makes
    "every exit path" true of the crash path as well as the terminal one.

    Cleanup RECORDS; it never decides. Withdrawal reads the reserved file to
    prove its identity, and a read can fail — EIO on a failing disk, ESTALE
    on a yanked network mount. That exception used to escape this loop, which
    left every remaining pin open AND reached `main`'s crash guard, so a
    command returned exit 9 after a FAILED or TIMED_OUT receipt with a
    different exit mapping was already on disk. So each entry is withdrawn
    inside its own guard and closed in its own `finally`: one entry's failure
    costs that entry's withdrawal and nothing else — not the descriptor, not
    the entries after it, and not the attempt's already-persisted outcome.
    `Exception`, not a hand-picked errno list, for the same reason the
    terminal evidence tail uses it (R2-W1): the point is that NOTHING here
    can become the result.
    """
    for entry in entries:
        try:
            try:
                _withdraw_reservation(entry)
            except Exception:
                pass
        finally:
            fd = entry.get("pin_fd")
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                entry["pin_fd"] = None


def _capture_artifact_baselines(args, entries: list[dict] | None = None
                                ) -> tuple[Path, list[dict]] | str:
    """Validate the artifact declarations, pin their identities, capture
    their baselines.

    Runs entirely PRE-SPAWN, so every failure here is a usage error: exit 2
    with no receipt and no attempt-id consumed. Returns `(pinned_root,
    entries)` on success, or an error string to print.

    `entries` is the CALLER's list, appended to as each declaration is
    accepted, so a failure partway through still leaves every pin already
    taken visible to the caller's `finally`. A function that built the list
    privately and then returned a string would leak exactly the descriptors
    the error path most needs to release.

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

    entries = [] if entries is None else entries
    by_path: dict[Path, dict] = {}
    for declared in args.require_artifact:
        resolved = Path(declared).resolve()
        if not _contained(resolved, root):
            return (f"--require-artifact {declared!r} resolves to {resolved} "
                    f"which is outside --artifact-root {root}")
        if resolved in by_path:
            return f"--require-artifact {declared!r} is declared twice"
        entry = {"declared": declared, "path": resolved,
                 "expected_sha256": None, "baseline_sha256": None,
                 # The pin (R2-C1): one `(st_dev, st_ino)` held open from
                 # here to grading. `reservation_*` is set only for a path
                 # this supervisor had to create because it was absent.
                 "pin_fd": None, "pin_dev": None, "pin_ino": None,
                 "reservation_sha256": None, "reservation_size": None}
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
            if exc.errno != errno.ENOENT:
                # Only a CONFIRMED nonexistence is absence. Every other
                # errno — EACCES, EIO, ESTALE, ENOTDIR, EMFILE — leaves
                # baseline_sha256 at None, which is the SAME value absence
                # produces, and grading reads that None as "changed: true".
                # An artifact that merely could not be read right now would
                # then be proof of freshness it never earned. ENOTDIR is
                # refused with the rest rather than folded into absence:
                # a regular file standing where a path component wants a
                # directory is an environment defect the caller must see,
                # and guessing on the caller's behalf is what this whole
                # gate exists to stop.
                return (f"--require-artifact {entry['declared']!r} could not "
                        f"be read pre-spawn: {os.strerror(exc.errno)} "
                        f"({errno.errorcode.get(exc.errno, exc.errno)}); "
                        f"only a confirmed ENOENT counts as absence")
            # Absent pre-spawn: `baseline_sha256` stays None, which is the
            # strongest freshness evidence there is — and the path is
            # RESERVED so the attempt has an identity to be graded against.
            # Without a reservation this is the one case with no pinned
            # inode at all, which is precisely the case the transient
            # hard-link laundering sequence lived in.
            failure = _reserve_artifact(entry, args.attempt_id)
            if failure is not None:
                return failure
            continue
        try:
            if st.st_size > args.require_artifact_baseline_max_bytes:
                return (f"--require-artifact {entry['declared']!r} baseline "
                        f"too large: {st.st_size} bytes over the limit of "
                        f"{args.require_artifact_baseline_max_bytes}")
            if st.st_nlink != 1:
                return (f"--require-artifact {entry['declared']!r} has "
                        f"{st.st_nlink} links, so its inode has more than "
                        f"one name; containment fences a PATH but a write "
                        f"lands on an INODE, so a second name outside "
                        f"--artifact-root would be overwritten through a "
                        f"contained path")
            budget += st.st_size
            if budget > ARTIFACT_BASELINE_BUDGET_BYTES:
                return (f"--require-artifact baselines exceed the aggregate "
                        f"budget of {ARTIFACT_BASELINE_BUDGET_BYTES} bytes")
            # Pin from the SAME descriptor the baseline is taken from, and
            # dup rather than reopen: a second open of the pathname would be
            # a fresh TOCTOU window, and `_hash_artifact` consumes the fd it
            # is handed. The dup outlives the hash and is what keeps this
            # inode number from being recycled if the child unlinks the path.
            entry["pin_dev"] = st.st_dev
            entry["pin_ino"] = st.st_ino
            entry["pin_fd"] = os.dup(fd)
            os.set_inheritable(entry["pin_fd"], False)
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
                  "nlink": None, "sha256": None, "contained": None,
                  # R2-C1: True when the graded inode is the one pinned
                  # pre-spawn, False when the path was made to name a
                  # different one, null where grading never got that far.
                  "identity_pinned": None,
                  "baseline_sha256": entry["baseline_sha256"],
                  "changed": None, "expected_sha256_match": None}
        records.append(record)
        try:
            current = path.resolve()
        except OSError:
            current = path
        record["contained"] = _contained(current, root)
        if not record["contained"]:
            reasons.append(_reason("artifact_escaped_root", path))
            continue
        try:
            fd, st = _open_regular(path)
        except NotARegularFile:
            reasons.append(_reason("artifact_not_regular_file", path))
            continue
        except OSError as exc:
            # ELOOP is O_NOFOLLOW refusing a symlink planted at the
            # artifact path — reporting that as "missing" would send an
            # operator looking for a file that is right there. The two
            # get different reasons because they have different remedies.
            reasons.append(_reason(
                "artifact_not_regular_file" if exc.errno == errno.ELOOP
                else "artifact_missing", path))
            continue
        record["exists"] = True
        record["size"] = st.st_size
        # From the SAME fd the digest would be taken from, so the link count
        # and the bytes describe one inode with no stat/read race between
        # them. `contained` above is a fact about the PATH; this is the fact
        # about the INODE that path names, and only both together are
        # containment. A second name for the inode means the child's write
        # also landed wherever that other name lives — possibly outside the
        # root — so no digest is recorded: a hash here would read as proof
        # that a contained file holds this content.
        record["nlink"] = st.st_nlink
        # Recorded before either refusal below, so the receipt shows BOTH
        # facts about the inode found here even when only one of them is
        # the reason the attempt is refused.
        record["identity_pinned"] = (
            (st.st_dev, st.st_ino) == (entry["pin_dev"], entry["pin_ino"]))
        if st.st_nlink != 1:
            os.close(fd)
            reasons.append(_reason("artifact_multiply_linked", path))
            continue
        if not record["identity_pinned"]:
            # The path no longer names the inode this supervisor pinned
            # before spawn. Whatever put a different inode here — a rename,
            # a fresh create after an unlink, or the tail of a hard-link
            # laundering sequence whose transient second name is already
            # gone — the supervisor cannot say what the vanished inode's
            # other names were, so it cannot certify that this attempt's
            # writes stayed inside the root. No digest is recorded, for the
            # same reason `artifact_multiply_linked` records none: a hash
            # here would read as proof about a file nobody can vouch for.
            os.close(fd)
            reasons.append(_reason("artifact_identity_replaced", path))
            continue
        if st.st_size == 0:
            os.close(fd)
            reasons.append(_reason("artifact_empty", path))
            continue
        digest = _hash_artifact(fd, deadline_monotonic)
        if digest is None:
            # Out of budget mid-hash. No partial digest is recorded, and
            # the attempt is a TIMED_OUT rather than a verdict on contents
            # nobody finished reading.
            record["hash_aborted"] = True
            aborted = True
            continue
        if entry["reservation_sha256"] is not None \
                and digest == entry["reservation_sha256"]:
            # The pinned inode still holds exactly the bytes the supervisor
            # put there to hold the path: the child never produced this
            # artifact. `artifact_missing` is that answer either way — it is
            # a fact about the CHILD, and no cleanup outcome changes it — so
            # it is appended before the withdrawal is even attempted. The
            # record shape is the one an unreserved absent path has always
            # produced only if the withdrawal SUCCEEDS; see below.
            reasons.append(_reason("artifact_missing", path))
            try:
                os.unlink(path)
            except OSError:
                # The withdrawal FAILED, so the reservation is still there.
                # Rewriting the record to `exists: false` here would put a
                # receipt on disk that contradicts the disk — and the next
                # attempt, told the path was absent, would find a file. The
                # record therefore keeps what grading actually observed (an
                # existing, singly-linked, pinned inode, with no digest,
                # because these are the supervisor's bytes and not the
                # child's) and the leftover gets its own reason. The
                # diagnosis for the CHILD is unchanged: `artifact_missing`.
                # `reservation_sha256` is deliberately NOT cleared, so the
                # release loop gets one more chance to withdraw it.
                reasons.append(
                    _reason("artifact_reservation_cleanup_failed", path))
                continue
            entry["reservation_sha256"] = None
            record.update({"exists": False, "size": None, "nlink": None,
                           "identity_pinned": None})
            continue
        record["sha256"] = digest
        baseline = entry["baseline_sha256"]
        record["changed"] = baseline is None or digest != baseline
        if not record["changed"]:
            if allow_unchanged:
                record["artifact_unchanged_accepted"] = True
            else:
                reasons.append(_reason("artifact_unchanged", path))
        if entry["expected_sha256"] is not None:
            record["expected_sha256_match"] = digest == entry["expected_sha256"]
            if not record["expected_sha256_match"]:
                reasons.append(_reason("artifact_sha256_mismatch", path))
    return records, reasons, aborted


def _read_session_evidence(evidence_dir: Path) -> dict:
    """Read what the session directory can prove about this attempt.

    Always returns the same shape. `summary` is None when the file is
    missing or unreadable and `summary_oversized` says which of the two it
    was; `terminal_event` is None whenever the tail scan cannot produce a
    complete one, for ANY reason — absent file, unparseable line, or a
    terminal event that sits outside the tail window. That last case is
    why the window is a read budget rather than a contract: an
    `events.jsonl` larger than the tail must not turn a clean attempt into
    a failure, because this surface is undocumented and gating on it would
    make an internal file load-bearing.
    """
    evidence = {"format": None, "dir": str(evidence_dir), "summary": None,
                "summary_oversized": False, "unreadable": False,
                "session_id": None, "created_at": None,
                "terminal_event": None}
    # R2-W1: each surface is read inside its OWN guard, and the guard is
    # `Exception`, not a hand-picked errno/parse list.
    #
    # Two concrete escapes proved why. `_tail_bytes` opens inside a try and
    # then seeks and reads OUTSIDE it, so an EIO on a rotating or truncated
    # `events.jsonl` propagates. And `json.loads` raises things that are
    # neither `JSONDecodeError` nor `UnicodeDecodeError` — a bare
    # `ValueError` on an over-long integer literal, `RecursionError` on a
    # deeply nested document. Either one escaping this function reaches
    # `cmd_run`'s post-spawn crash handler, which rewrites an ALREADY
    # SELECTED terminal state to CANCELLED and exits 9: a FAILED attempt
    # reported as a supervisor crash because a log file could not be read.
    #
    # Failing here is therefore not an error condition, it is an absence of
    # evidence, and it produces the same always-present shape with nulls in
    # it. `unreadable` records that the absence was a failure rather than a
    # file that was never written — the two are worth telling apart in an
    # audit trail, and neither is worth an attempt's receipt.
    try:
        data, oversized = _capped_bytes(evidence_dir / "summary.json",
                                        SUMMARY_MAX_BYTES)
        if oversized:
            evidence["summary_oversized"] = True
        elif data is not None:
            try:
                summary = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                summary = None
            if isinstance(summary, dict):
                evidence["summary"] = {
                    key: summary.get(key) for key in
                    ("agent_name", "current_model_id", "reasoning_effort",
                     "sandbox_profile")}
                info = summary.get("info")
                if isinstance(info, dict) and isinstance(info.get("id"), str):
                    evidence["session_id"] = info["id"]
                if isinstance(summary.get("created_at"), str):
                    evidence["created_at"] = summary["created_at"]
    except Exception:  # noqa: BLE001 — see the block comment above
        evidence["unreadable"] = True
        evidence["summary"] = None
        evidence["session_id"] = None
        evidence["created_at"] = None

    try:
        events, _ = _tail_bytes(evidence_dir / "events.jsonl",
                                EVENTS_TAIL_BYTES)
        if events:
            # Reverse scan: the last complete `turn_ended` in the window
            # wins. The first line of a tail read is usually a fragment, so
            # every line that does not parse is simply skipped rather than
            # treated as evidence of anything.
            for line in reversed(events.split(b"\n")):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict) and \
                        event.get("type") == "turn_ended":
                    evidence["terminal_event"] = {
                        "outcome": event.get("outcome"),
                        "cancellation_category": event.get(
                            "cancellation_category")}
                    break
    except Exception:  # noqa: BLE001 — see the block comment above
        evidence["unreadable"] = True
        evidence["terminal_event"] = None
    return evidence


def _session_evidence_view(declaration: str) -> tuple[dict, dict]:
    """Read the declared session directory once, for BOTH of its jobs.

    Returns `(evidence, receipt_view)`: the first is what the success gate
    grades, the second is what the receipt records. Splitting them here is
    what lets a terminal state that is never graded still be RECORDED —
    `summary_oversized` is the gate's own bookkeeping and stays out of the
    receipt, exactly as it always has.
    """
    evidence_format, _, evidence_dir = declaration.partition(":")
    try:
        evidence = _read_session_evidence(Path(evidence_dir))
    except Exception:  # noqa: BLE001 — belt and braces over R2-W1
        # `_read_session_evidence` guards both of its reads, so reaching
        # here means something outside them failed (an unrepresentable path,
        # for one). The rule is the same either way and is stated once: no
        # terminal state a supervisor has already chosen is ever relabeled
        # by the best-effort recording of evidence ABOUT it.
        evidence = {"format": None, "dir": evidence_dir, "summary": None,
                    "summary_oversized": False, "unreadable": True,
                    "session_id": None, "created_at": None,
                    "terminal_event": None}
    evidence["format"] = evidence_format
    return evidence, {k: v for k, v in evidence.items()
                      if k != "summary_oversized"}


def _tail_bytes(path: Path, limit: int) -> tuple[bytes | None, bool]:
    """Read at most the last `limit` bytes of a circumstantial surface."""
    try:
        fd, st = _open_regular(path)
    except OSError:
        return None, False
    with os.fdopen(fd, "rb") as f:
        if st.st_size > limit:
            f.seek(st.st_size - limit)
            return f.read(limit), True
        return f.read(), False


def _parse_grok_timestamp(value: str) -> float | None:
    """grok stamps `created_at` with microseconds and a `Z` suffix."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _grade_session_evidence(evidence: dict, *, session_id: str,
                            envelope: dict | None, launch_anchor: float,
                            graded_at: float, expect_agent: str | None,
                            expect_sandbox: str | None) -> list[str]:
    """The success gate over session evidence. Returns the reasons it fails.

    Failure tightens the SUCCESS direction only — the caller never applies
    these to a FAILED, TIMED_OUT or TERMINATION_UNCONFIRMED attempt, whose
    state is already decided by something that outranks evidence.
    """
    if evidence["summary_oversized"]:
        return ["evidence_oversized"]
    summary = evidence["summary"]
    if summary is None:
        return ["session_evidence_unreadable"]

    reasons: list[str] = []
    # (a) the session directory names the session this attempt declared.
    if evidence["session_id"] != session_id:
        reasons.append("session_evidence_unbound")
    # (b) stdout and the session directory name the SAME session. Without
    # this cross-proof a correct directory could be paired with a stdout
    # document from some other run. Exact equality is UNCONDITIONAL once an
    # envelope is declared: `_read_envelope` keeps `session_id` only when
    # the document supplies a string, so an absent or non-string `sessionId`
    # arrives here as None — and None is not the declared session. Skipping
    # the check for it would let the one document that proves nothing about
    # binding be the one document exempt from proving it.
    elif envelope is not None and envelope["session_id"] != session_id:
        reasons.append("session_evidence_unbound")
    # (c) freshness. The child creates the session at launch, so
    # `created_at` must fall inside this attempt's window. Both bounds are
    # untruncated internal values: the receipt's own timestamps are
    # truncated to whole seconds, and comparing those against a
    # microsecond-precision `created_at` misjudges every attempt that
    # finishes inside one second.
    else:
        created_at = _parse_grok_timestamp(evidence["created_at"] or "")
        if created_at is None or not (launch_anchor <= created_at <= graded_at):
            reasons.append("session_evidence_unbound")

    if expect_agent is not None and summary.get("agent_name") != expect_agent:
        reasons.append(_reason("effective_agent_mismatch",
                               summary.get("agent_name")))
    if expect_sandbox is not None and \
            summary.get("sandbox_profile") != expect_sandbox:
        # Absent counts as a mismatch: a version that stopped recording the
        # field is precisely the fail-open case this expectation exists to
        # catch, and treating absence as "fine" would disarm it silently.
        reasons.append(_reason("effective_sandbox_mismatch",
                               summary.get("sandbox_profile")))

    terminal_event = evidence["terminal_event"]
    if terminal_event is not None and terminal_event["outcome"] != "completed":
        # An opportunistic second line of defence, independent of DD-1: it
        # only works when the undocumented file is readable, and that limit
        # is documented rather than papered over.
        reasons.append(_reason("session_terminal_event",
                               terminal_event["outcome"]))
    return reasons


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


def _read_envelope(stdout_path: Path, fmt: str,
                   stderr_path: Path | None = None) -> dict:
    """Read one headless JSON document off a finished attempt's stdout.

    `fmt` selects the key names (ENVELOPE_FIELDS); everything below is shape,
    not vendor. Always returns the same eight keys, whatever went wrong.
    `usage` is the child's own token accounting where the format carries it
    (Claude does, grok does not) — recorded because the alternative is
    scraping it back out of the stdout file, which is what every probe in the
    2026-09-02 tranche had to do. `text` is an
    INTERNAL field — it feeds the verdict check and nothing else, and is
    dropped by `_receipt_envelope` before the receipt is written: the raw
    output already lives in the stdout file this read came from, and a
    receipt carries abbreviated evidence, not a second copy of the payload.

    `error_type` carries the document's own `type` discriminator for grok's
    `{"type": "error", ...}` failure object, and doubles as the read-failure
    channel (`evidence_oversized` / `envelope_unreadable`) so a caller can
    tell an over-budget stdout from a merely malformed one.
    """
    data, oversized = _capped_bytes(stdout_path, ENVELOPE_MAX_BYTES)
    envelope = _decode_envelope(data, fmt, oversized=oversized)
    if fmt == CODEX_TEXT_FORMAT:
        envelope.update(_codex_stderr_view(
            stderr_path if stderr_path is not None
            else stdout_path.with_suffix(".stderr")))
    return envelope


def _finite_counts(counts: dict) -> dict:
    # Finite and non-negative only. A child's document is untrusted input,
    # Python parses `NaN`/`Infinity` happily, and json.dumps would then
    # write a receipt no strict JSON reader (deep-loop among them) can
    # parse — a malformed count must not cost the attempt its receipt.
    return {k: v for k, v in counts.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0}


def _read_head_tail(path: Path, head: int, tail: int) -> tuple[bytes, bytes] | None:
    """Bounded head and tail of a regular file, refused like any gate read."""
    try:
        fd, st = _open_regular(path)
        with os.fdopen(fd, "rb") as f:
            first = f.read(head)
            size = os.fstat(f.fileno()).st_size
            if size <= head:
                return first, first[-tail:]
            f.seek(max(0, size - tail))
            return first, f.read(tail)
    except OSError:
        return None


def _codex_stderr_view(stderr_path: Path) -> dict:
    """Plain-mode codex evidence, read from stderr. Records only; never gates.

    The banner is the FIRST `OpenAI Codex v<version>` line followed by a block
    between two `--------` lines; only that block's `model:` line counts, so a
    `model:` line the conversation echoes later is not a claim. The metadata
    warning is scanned for anywhere in the bounded head, at line start —
    where codex prints it relative to the banner was not captured, and a
    false positive only defers a probe (fail closed). The footer is the last
    two non-empty lines: `tokens used` and a comma-grouped count.
    """
    view = {"header_model": None, "header_model_basis": "header-reported",
            "metadata_warning": False, "footer_tokens_uncached": None,
            "cli_version": None}
    read = _read_head_tail(stderr_path, CODEX_STDERR_HEAD_BYTES,
                           CODEX_STDERR_TAIL_BYTES)
    if read is None:
        return view
    head = read[0].decode("utf-8", errors="replace").splitlines()
    tail = read[1].decode("utf-8", errors="replace").splitlines()
    view["metadata_warning"] = any(
        line.startswith(CODEX_METADATA_WARNING_PREFIX) for line in head)
    for i, line in enumerate(head):
        match = re.fullmatch(r"OpenAI Codex v(\S+)", line.strip())
        if not match:
            continue
        if i + 1 < len(head) and head[i + 1].strip() == "--------":
            view["cli_version"] = match.group(1)
            for entry in head[i + 2:]:
                if entry.strip() == "--------":
                    break
                key, sep, value = entry.partition(": ")
                if sep and key == "model" and value.strip() \
                        and not any(c.isspace() for c in value.strip()):
                    view["header_model"] = value.strip()
                    break
        break
    lines = [line.strip() for line in tail if line.strip()]
    if len(lines) >= 2 and lines[-2] == "tokens used" \
            and re.fullmatch(r"\d{1,3}(?:,\d{3})*|\d+", lines[-1]):
        view["footer_tokens_uncached"] = int(lines[-1].replace(",", ""))
    return view


def _decode_codex_json(envelope: dict, text: str) -> dict:
    """`--json` JSONL, parsed line by line and by event NAME.

    Every non-empty line must be a JSON object with a string `type`, or the
    whole stream is unparseable. The finishing record is the last `turn.*`
    terminal event; `turn.failed` and `error` events are the stream's own
    failure report. The answer is the last completed `agent_message`.
    """
    events = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            return envelope
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return envelope
        events.append(event)
    envelope["parse_ok"] = True
    for event in events:
        kind = event["type"]
        if kind in ("turn.completed", "turn.failed"):
            envelope["stop_reason"] = kind
            usage = event.get("usage")
            if kind == "turn.completed" and isinstance(usage, dict):
                envelope["usage"] = _finite_counts(usage)
        if kind in ("turn.failed", "error"):
            envelope["reported_error"] = True
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            envelope["session_id"] = event["thread_id"]
        item = event.get("item")
        if kind == "item.completed" and isinstance(item, dict) \
                and item.get("type") == "agent_message" \
                and isinstance(item.get("text"), str):
            envelope["text"] = item["text"]
    return envelope


def _decode_envelope(data: bytes | None, fmt: str, *, oversized: bool = False) -> dict:
    """Decode the exact bytes also used for a success reader's digest check."""
    envelope = {"parse_ok": False, "stop_reason": None, "session_id": None,
                "served_models": None, "text": None, "error_type": None,
                "usage": None, "reported_error": False,
                "doc_type": None, "fmt": fmt}
    if oversized:
        envelope["error_type"] = "evidence_oversized"
        return envelope
    if data is None:
        envelope["error_type"] = "envelope_unreadable"
        return envelope
    if fmt in CODEX_FORMATS:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return envelope
        if fmt == CODEX_JSON_FORMAT:
            return _decode_codex_json(envelope, text)
        # Plain mode: stdout IS the answer. Its evidence lives on stderr and
        # is merged in by `_read_envelope`, never graded.
        envelope["parse_ok"] = True
        envelope["text"] = text
        return envelope
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return envelope
    if not isinstance(doc, dict):
        # A bare array or scalar parses as JSON but is not an envelope.
        return envelope
    envelope["parse_ok"] = True
    for field, key in ENVELOPE_FIELDS[fmt].items():
        value = doc.get(key)
        if isinstance(value, str):
            envelope[field] = value
    # A document that declares its own failure is not a success candidate
    # whatever its finishing reason says; `is_error` is Claude's channel for
    # that and absent from grok's, where `error_type` already carries it.
    envelope["reported_error"] = doc.get("is_error") is True
    envelope["error_discriminator_valid"] = type(doc.get("is_error")) is bool
    doc_type = doc.get("type")
    if isinstance(doc_type, str):
        envelope["doc_type"] = doc_type
    counts = doc.get("usage")
    if isinstance(counts, dict):
        envelope["usage"] = _finite_counts(counts)
    usage = doc.get("modelUsage")
    if isinstance(usage, dict):
        # DD-7: the SERVED model identifiers, recorded as observed and
        # never normalized against the declared `--model-id` — the
        # supervisor does not cross-check a caller's declaration.
        envelope["served_models"] = sorted(usage)
    return envelope


CODEX_TEXT_RECEIPT_KEYS = ("header_model", "header_model_basis",
                           "metadata_warning", "footer_tokens_uncached",
                           "cli_version")


def _receipt_envelope(envelope: dict) -> dict:
    """The six keys an envelope contributes to a receipt. `session_id` is
    here as the audit evidence for what the attempt-binding cross-proof
    (DD-3) actually compared; `text` is deliberately absent. Plain-mode codex
    adds its stderr evidence (DD-B9) and nothing else."""
    keys = ("parse_ok", "stop_reason", "session_id", "served_models",
            "error_type", "usage")
    if envelope.get("fmt") == CODEX_TEXT_FORMAT:
        keys = keys + CODEX_TEXT_RECEIPT_KEYS
    return {k: envelope.get(k) for k in keys}


def _grade_envelope(envelope: dict) -> list[str]:
    """Fail-closed gate on a declared envelope. Returns the reasons it
    failed, empty when it is a success candidate."""
    if envelope["error_type"] == "evidence_oversized":
        return ["evidence_oversized"]
    if not envelope["parse_ok"]:
        return ["envelope_unparseable"]
    reasons: list[str] = []
    if envelope["fmt"] == "claude-print-json-v1":
        # Fail closed on the discriminators the format actually carries. A
        # document is only a finished turn when it says so three ways; reading
        # `stop_reason` alone lets a foreign or malformed object through.
        if envelope["doc_type"] != CLAUDE_RESULT_DOC_TYPE:
            reasons.append(_reason("envelope_document_type",
                                   envelope["doc_type"]))
        if envelope["error_type"] != CLAUDE_OK_SUBTYPE:
            reasons.append(_reason("envelope_subtype", envelope["error_type"]))
        if envelope.get("error_discriminator_valid") is not True:
            reasons.append("envelope_invalid_error_discriminator")
    if envelope.get("reported_error"):
        reasons.append("envelope_reported_error")
    if envelope["fmt"] == CODEX_TEXT_FORMAT:
        # Plain codex output has no finishing record to grade.
        return reasons
    stop_reason = envelope["stop_reason"]
    if stop_reason != ENVELOPE_OK_STOP_REASONS[envelope["fmt"]]:
        # Appended, never returned early: a turn that both declared an error
        # and ended on a non-end_turn reason must report both, because the
        # stop reason is what tells a recipe defect from a model failure
        # (review-policy.md, "A silence caused by the recipe").
        reasons.append(_reason("envelope_stop_reason", stop_reason))
    return reasons


def _is_spec_echo(text: str, pos: int) -> bool:
    """`verdict: PASS | PASS_WITH_CHANGES | FAIL` is the format the review
    prompt quotes, not an answer. The primary grammar accepted it at line
    start for as long as it has existed — a seat that echoed the instructions
    and reviewed nothing graded as having reviewed — so the guard belongs on
    BOTH paths, not just the recovery."""
    return text[pos:pos + 8].lstrip().startswith("|")


def _verdict_of(text: str) -> tuple[str | None, bool]:
    """Parse a final review, refusing quoted or conflicting verdicts.

    An explicit final marker owns its section; earlier progress/history is
    excluded. Otherwise accept a leading verdict, or the documented run-in
    recovery with an adjacent confidence line. Markdown fences and blockquotes
    are evidence quoted by the reviewer, never the reviewer's own verdict.
    """
    lines = []
    fence = None
    quote = False
    for line in (text or "").splitlines(keepends=True):
        stripped = line.lstrip()
        match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence is not None:
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not stripped[match.end():].strip()):
                fence = None
            lines.append("\n")
        elif quote:
            # Markdown permits unmarked continuation lines in a quoted
            # paragraph. Require a blank line before returning to prose.
            if not stripped.strip():
                quote = False
            lines.append("\n")
        elif stripped.startswith(">"):
            quote = True
            lines.append("\n")
        elif match:
            fence = match[1]
            lines.append("\n")
        else:
            lines.append(line)
    visible = "".join(lines)
    # Grok can concatenate progress and the marker itself. Its trailing
    # newline still separates the final answer from that progress.
    markers = list(re.finditer(r"=== REVIEW ===[ \t]*\r?$", visible, re.M))
    if markers:
        visible = visible[markers[-1].end():]
    visible = visible.strip()
    candidates = [m for m in VERDICT_ANYWHERE_RE.finditer(visible)
                  if not _is_spec_echo(visible, m.end())]
    if len(candidates) != 1:
        return None, False
    match = candidates[0]
    # A token embedded in a sentence is not an anchored verdict line.
    line_end = visible.find("\n", match.end())
    remainder = visible[match.end():line_end if line_end >= 0 else len(visible)]
    if remainder.strip():
        return None, False
    anchored = match.start() == 0 or visible[match.start() - 1] == "\n"
    if anchored:
        return (match[1], False) if match.start() == 0 or markers else (None, False)
    if CONFIDENCE_NEXT_LINE_RE.match(visible, match.end()):
        return match[1], True
    return None, False


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
        "receipt_guard": (None if getattr(args, "receipt_guard", "none") == "none" else
                          {"mechanism": args.receipt_guard, "phase": "requested"}),
        "prompt_sha256": None,
        "output_schema": args.output_schema,
        # DD-1: the DECLARED envelope contract, null when none was
        # declared. Every key this tranche adds is present on every
        # receipt and null on the undeclared path — that is what keeps
        # the exact-field contract one key set instead of one per
        # declaration combination.
        "output_envelope": args.output_envelope,
        # DD-3: what actually took effect, read from the session directory
        # the caller declared. Null when none was declared.
        "session_evidence": None,
        # Issue #19 maker-seat prevention. Always present, null when the
        # caller did not declare a child cwd / attempt-private GROK_HOME.
        "child_cwd": args.child_cwd,
        "grok_home": args.grok_home,
        "seat_profile": args.seat_profile,
        "require_single_linked_cwd": bool(args.require_single_linked_cwd),
        # 2026-09-25 DD-B9: the declared nested-sandbox opt-in, null when
        # undeclared. Recorded so a guarded codex receipt says the caller
        # vouched that its child reads no files.
        "allow_nested_sandbox": args.allow_nested_sandbox,
        "process": {"pid": None, "process_group_id": None,
                    "supervisor_pid": os.getpid()},
        "timing": {"started_at": None, "deadline_at": None,
                   "finished_at": None, "launch_anchor_at": None},
        "result": {
            "state": "STARTING", "exit_status": None,
            "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
            "output_sha256": None, "schema_valid": None,
            # Always present, so a consumer never has to ask whether this
            # receipt's grader knew about verdicts. `verdict_recovered` says
            # the seat's output needed repair to parse at all — the recipe
            # that produced it should be fixed (adapters.md, "Output is a
            # contract").
            "verdict": None, "verdict_recovered": False,
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
            # Issue #19: ProfileApplied.enforced evidence. Null when
            # --expect-sandbox-enforced was not declared.
            "sandbox_events": None,
        },
    }


def _success_evidence_problems(receipt_dir: Path, receipt: dict, attempt_id: str) -> list[str]:
    """Consistency checks, not authentication of child-writable storage.

    Every public reader of SUCCEEDED applies the same minimum proof. A claim
    means the supervisor has not published its final result yet. The expected
    stdout path comes from the attempt id, never a path supplied by the file.
    """
    result = receipt.get("result")
    if not isinstance(result, dict):
        return ["result is not an object"]
    problems = []
    if receipt.get("attempt_id") != attempt_id:
        problems.append("receipt attempt_id does not match the requested attempt")
    if not isinstance(receipt.get("seat"), str) or not receipt["seat"].strip():
        problems.append("seat is missing or invalid")
    timing = receipt.get("timing")
    if (not isinstance(timing, dict)
            or any(not isinstance(timing.get(k), str) or not timing[k].strip()
                   for k in ("started_at", "finished_at"))):
        problems.append("completion timing is missing or invalid")
    if os.path.lexists(receipt_dir / f"{attempt_id}.claim"):
        problems.append("attempt is still claimed")
    if type(result.get("exit_status")) is not int or result["exit_status"] != 0:
        problems.append("exit_status is not integer zero")
    if result.get("termination_confirmed") is not True:
        problems.append("termination is not confirmed")
    if result.get("schema_valid") is not True:
        problems.append("schema_valid is not true")
    if result.get("invalid_reasons"):
        problems.append("success carries invalid reasons")
    if receipt.get("output_schema") not in ("none", "review"):
        problems.append("output_schema is missing or invalid")
    data, _ = _capped_bytes(receipt_dir / f"{attempt_id}.stdout", ENVELOPE_MAX_BYTES)
    digest = result.get("output_sha256")
    if not isinstance(digest, str) or not HEX64_RE.fullmatch(digest):
        problems.append("output digest is missing or malformed")
    elif data is None or digest != hashlib.sha256(data).hexdigest():
        problems.append("output digest does not match readable stdout")
    if data is not None:
        fmt = receipt.get("output_envelope")
        text = data.decode(errors="replace")
        if fmt is not None:
            if not isinstance(fmt, str) or fmt not in ENVELOPE_FIELDS:
                problems.append("unknown output envelope")
                text = ""
            else:
                envelope = _decode_envelope(data, fmt)
                if _grade_envelope(envelope):
                    problems.append("stdout envelope is not valid completion")
                text = envelope.get("text") or ""
        elif not data.strip():
            problems.append("plain stdout is empty")
        if receipt.get("output_schema") == "review":
            verdict, _ = _verdict_of(text)
            if verdict is None or verdict != result.get("verdict"):
                problems.append("review verdict does not match stdout")
    return problems


def _same_attempt_identity(local: dict, disk: dict) -> bool:
    """Compare declarations and launch identity, including JSON value types."""
    # Session/served-model observations can be collected after RUNNING. They
    # are results, not dispatch identity; including them loses a real cancel
    # whenever terminal evidence backfill fills a formerly-null field.
    keys = set(local) - {"result", "timing", "session_evidence",
                         "observed_model_id", "observed_model_source"}
    if not keys <= set(disk):
        return False
    def identity(receipt):
        return {
            "declarations": {k: receipt[k] for k in keys},
            "launch": {k: receipt["timing"][k] for k in
                       ("started_at", "deadline_at", "launch_anchor_at")},
            "output_paths": {k: receipt["result"][k] for k in
                             ("stdout_path", "stderr_path")},
        }
    try:
        return json.dumps(identity(local), sort_keys=True) == json.dumps(identity(disk), sort_keys=True)
    except (KeyError, TypeError):
        return False


@contextmanager
def _terminal_lock(receipt_dir: Path, attempt_id: str):
    """A stable inode serializes cooperating writers; never unlink this file."""
    path = receipt_dir / f"{attempt_id}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("publication lock is not a single-linked regular file")
        deadline = time.monotonic() + 2.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("publication lock deadline exceeded")
                time.sleep(.01)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _merge_terminal(receipt: dict, current: dict, owned_starting: dict | None = None) -> None:
    own_unpublished_launch = (owned_starting is not None
        and current.get("result", {}).get("state") == "STARTING"
        and json.dumps(current, sort_keys=True) == json.dumps(owned_starting, sort_keys=True))
    if not _same_attempt_identity(receipt, current) and not own_unpublished_launch:
        raise ValueError("receipt identity changed during finalization")
    local, disk = receipt["result"], current["result"]
    if disk.get("state") not in STATES:
        raise ValueError("unknown native receipt state")
    if disk.get("state") == "TERMINATION_UNCONFIRMED" and disk.get("termination_confirmed") is not False:
        local.update(state="TERMINATION_UNCONFIRMED", termination_confirmed=False)
        raise ValueError("unconfirmed receipt has malformed confirmation")
    if disk.get("state") == "CANCELLED" and disk.get("termination_confirmed") is not True:
        if disk.get("termination_confirmed") is False:
            local.update(state="TERMINATION_UNCONFIRMED", termination_confirmed=False)
        raise ValueError("cancelled receipt lacks confirmed termination")
    intent = disk.get("cancel_requested_at")
    if isinstance(intent, str) and intent:
        local["cancel_requested_at"] = intent
    if (local.get("termination_confirmed") is False
            or (disk.get("state") == "TERMINATION_UNCONFIRMED"
                and disk.get("termination_confirmed") is False)):
        local.update(state="TERMINATION_UNCONFIRMED", termination_confirmed=False)
    elif local.get("termination_confirmed") is True and (
            (isinstance(intent, str) and bool(intent))
            or (disk.get("state") == "CANCELLED" and disk.get("termination_confirmed") is True)):
        local["state"] = "CANCELLED"


def _commit_terminal(receipt_dir: Path, receipt: dict, claim_path: Path,
                     owned_starting: dict | None = None, *, preserve_published: bool = False) -> int:
    """Publish under one lock; failure retains the claim and cannot return green.

    This serializes cooperating supervisors/cancelers. Filesystem authority
    against a child requires a separately enforced receipt-store guard.
    """
    try:
        with _terminal_lock(receipt_dir, receipt["attempt_id"]):
            current = read_receipt(receipt_dir, receipt["attempt_id"])
            _merge_terminal(receipt, current, owned_starting)
            state = current["result"]["state"]
            claimed = os.path.lexists(claim_path)
            if state in EXIT_BY_STATE and not claimed and not current["result"].get("cancel_requested_at"):
                if not preserve_published:
                    raise ValueError("attempt already published without an active cancel intent")
                # A stale cancel snapshot is not authority to reopen a finished
                # attempt. An already accepted durable intent remains distinct.
                if state == "SUCCEEDED" and _success_evidence_problems(receipt_dir, current, receipt["attempt_id"]):
                    raise ValueError("published success has invalid evidence")
                return EXIT_BY_STATE[state]
            if state in ("STARTING", "RUNNING") and not claimed:
                raise ValueError("active attempt lost publication claim")
            write_receipt(receipt_dir, receipt)
            claim_path.unlink(missing_ok=True)
        return EXIT_BY_STATE[receipt["result"]["state"]]
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        print(f"receipt publication failed: {exc}", file=sys.stderr)
        # Missing storage proof must not erase a known live-process hazard.
        if receipt.get("result", {}).get("termination_confirmed") is False:
            return EXIT_BY_STATE["TERMINATION_UNCONFIRMED"]
        return PUBLICATION_FAILED


def _request_cancellation(receipt_dir: Path, receipt: dict) -> bool:
    """Linearize cancellation before signaling, surviving requester death."""
    with _terminal_lock(receipt_dir, receipt["attempt_id"]):
        current = read_receipt(receipt_dir, receipt["attempt_id"])
        if not _same_attempt_identity(receipt, current):
            raise ValueError("receipt identity changed before cancellation")
        state = current["result"].get("state")
        if state not in STATES:
            raise ValueError("unknown native receipt state")
        if state == "TERMINATION_UNCONFIRMED":
            raise UnconfirmedPublicationError("termination unconfirmed during cancellation publication")
        if state in EXIT_BY_STATE and os.path.lexists(receipt_dir / f"{receipt['attempt_id']}.claim"):
            raise ValueError("terminal receipt publication is incomplete")
        if state != "RUNNING" or current["result"].get("cancel_requested_at"):
            return False
        if not (receipt_dir / f"{receipt['attempt_id']}.claim").exists():
            raise ValueError("active attempt lost its claim")
        current["result"]["cancel_requested_at"] = _utcnow()
        write_receipt(receipt_dir, current)
        receipt["result"]["cancel_requested_at"] = current["result"]["cancel_requested_at"]
        return True


class CancellationRequested(Exception):
    pass


def _wait_with_cancellation(proc, receipt_dir, receipt, deadline):
    while True:
        try:
            current = read_receipt(receipt_dir, receipt["attempt_id"])
            intent = current.get("result", {}).get("cancel_requested_at")
            if isinstance(intent, str) and intent and _same_attempt_identity(receipt, current):
                receipt["result"]["cancel_requested_at"] = intent
                raise CancellationRequested
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            # Unreadable storage cannot request a signal. Final publication
            # still requires readable matching authority and fails closed.
            pass
        remaining = max(0.0, deadline - time.monotonic())
        try:
            return proc.wait(timeout=min(.2, remaining))
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                raise


def _backfill_terminal_evidence(args, receipt: dict,
                                stdout_path: Path) -> None:
    """Record, at an already-decided terminal state, the evidence that was
    never graded.

    Everything here is best-effort by construction and RECORDS only; the
    caller wraps it so that a failure inside it cannot become the attempt's
    outcome (R2-W1).
    """
    if args.session_evidence is not None \
            and receipt["session_evidence"] is None:
        # Recording is unconditional where gating is not, for the same
        # reason the envelope backfill below is: a FAILED, TIMED_OUT or
        # TERMINATION_UNCONFIRMED attempt is where the effective agent, the
        # effective sandbox profile and the turn's cancellation category are
        # MOST worth having, and grading is the one thing that never runs
        # there. The state is already decided; nothing collected here is
        # graded, so nothing collected here can relabel it. Both reads are
        # bounded (SUMMARY_MAX_BYTES, EVENTS_TAIL_BYTES) and best-effort — a
        # session directory that was never written leaves the same
        # always-present shape full of nulls rather than costing the attempt
        # its receipt.
        _, receipt["session_evidence"] = \
            _session_evidence_view(args.session_evidence)

    if args.output_envelope is not None \
            and receipt["result"]["envelope"] is None:
        # Recording is unconditional even where gating is not: a TIMED_OUT,
        # FAILED or TERMINATION_UNCONFIRMED attempt still leaves its
        # envelope in the receipt as evidence, and none of those states is
        # relabeled by what it says.
        receipt["result"]["envelope"] = _receipt_envelope(
            _read_envelope(stdout_path, args.output_envelope))


# Launchers whose first operand is the real program (i1r2 opus F1), with the
# options of each that consume the following word as their value.
_CODEX_WRAPPERS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-P", "-S",
                      "--split-string"}),
    "npx": frozenset({"-p", "--package", "-c", "--call"}),
    "node": frozenset({"-r", "--require", "--import", "--loader",
                       "--experimental-loader"}),
}


def _launched_program(argv: list[str]) -> str | None:
    """Basename of the program argv actually runs, looking through wrappers.

    argv[0] is the program unless it is a known wrapper (env, npx, node), in
    which case the program is the wrapper's first operand: the first word
    that is neither an option (nor an option's value) nor, for env, a
    VAR=val assignment. Wrappers may chain (`env A=1 npx codex`).
    """
    i = 0
    while i < len(argv):
        name = os.path.basename(argv[i])
        takes_value = _CODEX_WRAPPERS.get(name)
        if takes_value is None:
            return name
        i += 1
        while i < len(argv):
            word = argv[i]
            if word == "--":
                i += 1
                break
            if word.startswith("-"):
                i += 2 if word in takes_value else 1
                continue
            if name == "env" and "=" in word:
                i += 1
                continue
            break
    return None


def _launches_codex(argv: list[str]) -> bool:
    """True when the executable argv launches is codex.

    The one place this supervisor reads argv, and only to REFUSE (DD-B9):
    codex applies its own Seatbelt whether or not argv says so — an explicit
    `-s`/`--sandbox`, its `exec` default, a `--full-auto` preset, a `-c
    sandbox_mode=…` override or config.toml — so matching sandbox flags would
    miss every implicit case (i1r1 opus F5). Only the launched program counts
    (i1r2 opus F1): an argument that merely names a path ending in `codex`
    (`--add-dir ~/src/codex`) is not a codex child.
    """
    program = _launched_program(argv)
    if program is None:
        return False
    stem, ext = os.path.splitext(program)
    return program == "codex" or (ext in (".js", ".mjs", ".cjs")
                                  and stem == "codex")


def cmd_run(args) -> int:
    """Own the artifact pins' lifetime, and nothing else.

    The pins are descriptors this process holds on required-artifact inodes
    from before the child starts until after grading (R2-C1). They have to be
    given back on EVERY way out of `_run_attempt` — the terminal return, the
    preflight `return 2`s that happen after some pins were already taken, and
    the crash that leaves through `main`'s guard as exit 9. A `finally` around
    one call is the only shape that is true of all three; a release written at
    each return site is a list that grows a hole the first time someone adds a
    branch.
    """
    pins: list[dict] = []
    try:
        return _run_attempt(args, pins)
    finally:
        _release_artifact_pins(pins)


def _run_attempt(args, pins: list[dict]) -> int:
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
    if args.session_evidence is not None and args.session_id is None:
        # Attempt binding has nothing to bind to without the id the child
        # was handed: a session directory that named no particular attempt
        # would let any leftover directory stand in for this one.
        print("--session-evidence requires --session-id: the evidence is "
              "bound to this attempt by the session id, or not at all",
              file=sys.stderr)
        return 2
    if args.expect_sandbox_profile is not None:
        if not args.expect_sandbox_profile.strip():
            print("--expect-sandbox-profile must not be empty", file=sys.stderr)
            return 2
        if args.session_evidence is None:
            print("--expect-sandbox-profile requires --session-evidence: the "
                  "effective sandbox profile is only observable in the "
                  "session summary", file=sys.stderr)
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
        if args.output_envelope not in (None, GROK_ENVELOPE_FORMAT):
            print(f"--transport-id {args.transport_id!r} dispatches into grok, "
                  f"so its envelope must be {GROK_ENVELOPE_FORMAT!r}, not "
                  f"{args.output_envelope!r}: a declaration that names another "
                  f"format grades another vendor's document shape and would "
                  f"pass a cancelled grok turn on null fields.",
                  file=sys.stderr)
            return 2
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

    # --- Guard vs a nested codex sandbox (2026-09-25 DD-B9) -------------
    # Seatbelt does not nest: a codex child inside the receipt guard runs its
    # own sandbox (explicit -s, its exec default, a preset or config.toml),
    # cannot read the files it was asked to review and still exits 0 — a
    # silent non-review that grades SUCCEEDED. Every guarded codex child is
    # refused before spawn unless the caller declares it reads no files.
    guarded = getattr(args, "receipt_guard", "none") != "none"
    if guarded and _launches_codex(args.argv) \
            and args.allow_nested_sandbox is None:
        print("--receipt-guard with a codex child is refused: codex runs its "
              "own sandbox (-s/--sandbox, its exec default, --full-auto or "
              "config.toml), the nested Seatbelt cannot read files and still "
              "exits 0, so a review would succeed without reading anything. "
              "Drop the guard, or declare --allow-nested-sandbox "
              "no-file-access when the child reads no files "
              "(references/adapters.md).", file=sys.stderr)
        return 2
    if args.allow_nested_sandbox is not None and not guarded:
        print("--allow-nested-sandbox without --receipt-guard constrains "
              "nothing", file=sys.stderr)
        return 2

    if args.seat_profile == MAKER_SEAT_PROFILE:
        missing = [name for name, attr in MAKER_SEAT_REQUIRED
                   if not getattr(args, attr)]
        if missing:
            print(f"--seat-profile {MAKER_SEAT_PROFILE} requires "
                  f"{', '.join(missing)}", file=sys.stderr)
            return 2
        if args.expect_sandbox_enforced != MAKER_SANDBOX_PROFILE:
            print(f"--seat-profile {MAKER_SEAT_PROFILE} requires "
                  f"--expect-sandbox-enforced {MAKER_SANDBOX_PROFILE}",
                  file=sys.stderr)
            return 2
        if args.expect_sandbox_profile not in (None, MAKER_SANDBOX_PROFILE):
            print(f"--seat-profile {MAKER_SEAT_PROFILE} requires "
                  f"--expect-sandbox-profile {MAKER_SANDBOX_PROFILE} "
                  f"(or omitted)", file=sys.stderr)
            return 2
    if args.require_single_linked_cwd and not args.child_cwd:
        print("--require-single-linked-cwd requires --child-cwd: the "
              "supervisor will not audit its own ambient cwd",
              file=sys.stderr)
        return 2
    if args.grok_auth_seed and not args.grok_home:
        print("--grok-auth-seed requires --grok-home", file=sys.stderr)
        return 2
    if args.expect_sandbox_enforced is not None:
        if not args.expect_sandbox_enforced.strip():
            print("--expect-sandbox-enforced must not be empty",
                  file=sys.stderr)
            return 2
        if not args.grok_home:
            print("--expect-sandbox-enforced requires --grok-home: "
                  "ProfileApplied is recorded under $GROK_HOME ("
                  + ", ".join("/".join(parts)
                              for parts in SANDBOX_EVENT_RELPATHS) + ")",
                  file=sys.stderr)
            return 2

    if args.child_cwd is not None:
        child = Path(args.child_cwd)
        try:
            # Resolve the named path but do not follow a final symlink:
            # the audit refuses a linked cwd, and realpath would hide it.
            child = child if child.is_absolute() else Path.cwd() / child
            err = _audit_single_linked_tree(child) if args.require_single_linked_cwd \
                else None
            if err is None and not args.require_single_linked_cwd:
                st = os.lstat(child)
                if stat.S_ISLNK(st.st_mode):
                    err = f"--child-cwd {str(child)!r} is a symlink"
                elif not stat.S_ISDIR(st.st_mode):
                    err = f"--child-cwd {str(child)!r} is not a directory"
            if err is not None:
                print(f"error: {err}", file=sys.stderr)
                return 2
            if args.require_single_linked_cwd:
                try:
                    os.chmod(child, 0o700)
                except OSError as exc:
                    print(f"error: --child-cwd {str(child)!r} could not be "
                          f"mode 0700: {os.strerror(exc.errno)}",
                          file=sys.stderr)
                    return 2
        except OSError as exc:
            print(f"error: --child-cwd {args.child_cwd!r} could not be "
                  f"read: {os.strerror(exc.errno)}", file=sys.stderr)
            return 2
        args.child_cwd = str(child.resolve())

    sandbox_event_pins: dict = {}
    if args.grok_home is not None:
        home = Path(args.grok_home)
        home = home if home.is_absolute() else Path.cwd() / home
        seed = Path(args.grok_auth_seed) if args.grok_auth_seed else None
        if seed is not None and not seed.is_absolute():
            seed = Path.cwd() / seed
        err = _prepare_grok_home(home, seed, args.expect_sandbox_enforced,
                                 sandbox_event_pins)
        if err is not None:
            print(f"error: {err}", file=sys.stderr)
            return 2
        args.grok_home = str(home.resolve())

    artifact_root = None
    artifact_entries: list[dict] = []
    if args.require_artifact:
        if not _finite_positive(args.require_artifact_baseline_max_bytes):
            print("--require-artifact-baseline-max-bytes must be a finite "
                  "number > 0", file=sys.stderr)
            return 2
        captured = _capture_artifact_baselines(args, pins)
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
    # 0700 excludes other UIDs, not a child running under this same UID.
    # Trusted deployments must deny the child write access to this directory;
    # path placement and permission-mode declarations alone do not enforce it.
    try:
        if getattr(args, "receipt_guard", "none") != "none" and receipt_dir.is_symlink():
            raise receipt_guard.GuardError("guarded receipt root must not be a final symlink")
        receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if getattr(args, "receipt_guard", "none") != "none":
            receipt_dir = (receipt_guard.canonical_directory(receipt_dir) if sys.platform == "darwin"
                           else receipt_dir.resolve())
    except (OSError, receipt_guard.GuardError) as exc:
        if stdin_f is not subprocess.DEVNULL:
            stdin_f.close()
        print(f"invalid receipt root: {exc}", file=sys.stderr)
        return 2
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
    starting_snapshot = copy.deepcopy(receipt)
    with _terminal_lock(receipt_dir, args.attempt_id):
        write_receipt(receipt_dir, receipt)

    # Attempt output files are new objects. O_EXCL refuses every pre-existing
    # path (including FIFO, symlink and hardlink) without opening or truncating
    # it. This does not depend on 0700 providing same-UID process isolation.
    stdout_fd = None
    try:
        try:
            stdout_fd = os.open(
                stdout_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            stderr_fd = os.open(
                stderr_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
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
            print(f"start failed: cannot open output file: {exc}",
                  file=sys.stderr)
            return _commit_terminal(receipt_dir, receipt, claim_path)

        # stdout/stderr go straight to files: no pipe buffer to fill, no
        # drain thread to forget, no deadlock when the child floods.
        with os.fdopen(stdout_fd, "wb") as out_f, os.fdopen(stderr_fd, "wb") as err_f:
            # The lower bound of DD-3's session freshness window, captured
            # HERE rather than after Popen: the child creates its session
            # directory at launch, and `started_at` is stamped once Popen
            # has already returned — a bound stamped after the event it is
            # supposed to precede is not a bound. Kept untruncated for the
            # comparison; the receipt shows the human-readable form.
            launch_argv = args.argv
            if getattr(args, "receipt_guard", "none") != "none":
                try:
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        raise receipt_guard.GuardError("deadline expired before guard preparation")
                    if stdin_f is not subprocess.DEVNULL:
                        receipt_guard.reject_protected_input(receipt_dir, stdin_f.fileno())
                    prefix, metadata = receipt_guard.prepare(receipt_dir, out_f.fileno(), err_f.fileno(), remaining)
                    if time.monotonic() >= deadline_monotonic:
                        raise receipt_guard.GuardError("deadline expired during guard preparation")
                except (OSError, receipt_guard.GuardError) as exc:
                    receipt["result"]["state"] = "START_FAILED"
                    receipt["result"]["invalid_reasons"] = ["receipt_guard_unavailable"]
                    receipt["timing"]["finished_at"] = _utcnow()
                    print(f"receipt guard unavailable: {exc}", file=sys.stderr)
                    return _commit_terminal(receipt_dir, receipt, claim_path)
                receipt["receipt_guard"] = metadata
                starting_snapshot = copy.deepcopy(receipt)
                with _terminal_lock(receipt_dir, args.attempt_id):
                    write_receipt(receipt_dir, receipt)
                launch_argv = [*prefix, *args.argv]
            launch_anchor = time.time()
            try:
                popen_kw = dict(
                    stdin=stdin_f, stdout=out_f, stderr=err_f,
                    start_new_session=True, close_fds=True)  # only declared stdio is inherited
                if args.child_cwd is not None:
                    popen_kw["cwd"] = args.child_cwd
                if args.grok_home is not None:
                    child_env = os.environ.copy()
                    child_env["GROK_HOME"] = args.grok_home
                    popen_kw["env"] = child_env
                if time.monotonic() >= deadline_monotonic:
                    receipt["result"]["invalid_reasons"] = ["deadline_expired_before_launch"]
                    raise OSError(errno.ETIMEDOUT, "deadline expired before target launch")
                proc = subprocess.Popen(launch_argv, **popen_kw)
            except OSError as exc:
                receipt["result"]["state"] = "START_FAILED"
                receipt["timing"]["finished_at"] = _utcnow()
                err_f.write(f"start failed: {exc}\n".encode())
                return _commit_terminal(receipt_dir, receipt, claim_path)

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
            running_published = False
            try:
                if receipt.get("receipt_guard") is not None:
                    receipt["receipt_guard"]["phase"] = "launched"
                receipt["process"] = {"pid": proc.pid, "process_group_id": pgid,
                                      "supervisor_pid": os.getpid()}
                receipt["timing"]["started_at"] = _utcnow()
                receipt["timing"]["launch_anchor_at"] = datetime.fromtimestamp(
                    launch_anchor, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                receipt["timing"]["deadline_at"] = deadline_at
                receipt["result"]["state"] = "RUNNING"
                write_receipt(receipt_dir, receipt)
                running_published = True

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
                    exit_status = _wait_with_cancellation(proc, receipt_dir, receipt, deadline_monotonic)
                except CancellationRequested:
                    confirmed = terminate_group(proc, pgid, args.grace_seconds)
                    receipt["result"].update(termination_confirmed=confirmed,
                        state="CANCELLED" if confirmed else "TERMINATION_UNCONFIRMED")
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
                    elif (args.output_envelope is not None
                          or args.session_evidence is not None
                          or artifact_entries
                          or args.expect_sandbox_enforced is not None):
                        # The ladder inside grading, in order: envelope
                        # (DD-1) -> output schema -> artifacts (DD-2) ->
                        # a final deadline re-check. The envelope comes
                        # before the schema so a cancelled turn that
                        # happens to have emitted a well-formed verdict is
                        # still a cancelled turn.
                        reasons: list[str] = []
                        timed_out = False
                        envelope = None
                        if args.output_envelope is not None:
                            envelope = _read_envelope(
                                stdout_path, args.output_envelope, stderr_path)
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
                                # the verdict-line grammar. An empty `text`
                                # is fine under schema `none`: DD-2's
                                # artifact contract, not stdout length, is
                                # what proves that seat finished.
                                verdict, recovered = _verdict_of(
                                    envelope["text"] or "")
                                schema_ok = (args.output_schema != "review"
                                             or verdict is not None)
                                receipt["result"]["verdict"] = verdict
                                receipt["result"]["verdict_recovered"] = recovered
                                if not schema_ok:
                                    reasons = ["schema_invalid"]
                            else:
                                schema_ok = False
                            receipt["result"]["schema_valid"] = schema_ok
                        else:
                            ok, digest, verdict, recovered = _validate_output(
                                stdout_path, args.output_schema)
                            receipt["result"]["output_sha256"] = digest
                            receipt["result"]["schema_valid"] = ok
                            receipt["result"]["verdict"] = verdict
                            receipt["result"]["verdict_recovered"] = recovered
                            if not ok:
                                reasons = ["schema_invalid"]
                        if args.session_evidence is not None:
                            # Ladder step 5, alongside the output schema:
                            # recorded whatever the terminal state, gating
                            # only where success is still on the table.
                            evidence, receipt["session_evidence"] = \
                                _session_evidence_view(args.session_evidence)
                            reasons = reasons + _grade_session_evidence(
                                evidence, session_id=args.session_id,
                                envelope=envelope, launch_anchor=launch_anchor,
                                graded_at=time.time(),
                                expect_agent=args.expect_effective_agent,
                                expect_sandbox=args.expect_sandbox_profile)
                        if args.expect_sandbox_enforced is not None:
                            view, event_reasons = _grade_sandbox_events(
                                Path(args.grok_home),
                                args.expect_sandbox_enforced,
                                sandbox_event_pins)
                            receipt["result"]["sandbox_events"] = view
                            reasons = reasons + event_reasons
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
                        ok, digest, verdict, recovered = _validate_output(
                            stdout_path, args.output_schema)
                        receipt["result"]["output_sha256"] = digest
                        receipt["result"]["schema_valid"] = ok
                        receipt["result"]["verdict"] = verdict
                        receipt["result"]["verdict_recovered"] = recovered
                        if ok and _deadline_expired(deadline_monotonic):
                            receipt["result"]["state"] = "TIMED_OUT"
                        else:
                            receipt["result"]["state"] = (
                                "SUCCEEDED" if ok else "INVALID_OUTPUT")

                # R2-W1: the whole backfill is one guarded region. Both
                # readers below are already exception-safe on their own; this
                # is the outer statement of the invariant they serve, in the
                # one place a violation of it would do the damage. Above this
                # point a terminal state has ALREADY been selected — FAILED
                # from a non-zero exit, TIMED_OUT from the deadline,
                # TERMINATION_UNCONFIRMED from a group that would not die.
                # Everything here only RECORDS. An exception escaping into
                # the post-spawn `except Exception` below would re-run the
                # termination ladder and overwrite that state with CANCELLED
                # and exit 9, which is a supervisor crash reported in place
                # of an attempt outcome that was already known.
                try:
                    _backfill_terminal_evidence(args, receipt, stdout_path)
                except Exception:  # noqa: BLE001 — see above
                    pass

                receipt["timing"]["finished_at"] = _utcnow()
                return _commit_terminal(receipt_dir, receipt, claim_path)
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
                # A known possibly-live writer outranks generic crash status,
                # including when its receipt could not be published.
                publication = _commit_terminal(receipt_dir, receipt, claim_path,
                    owned_starting=starting_snapshot if not running_published else None)
                if publication == EXIT_BY_STATE["TERMINATION_UNCONFIRMED"]:
                    traceback.print_exc()
                    return publication
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
    if state in EXIT_BY_STATE and os.path.lexists(claim_path):
        print("receipt publication is incomplete (claim retained): " + state, file=sys.stderr)
        return 5 if state == "TERMINATION_UNCONFIRMED" else PUBLICATION_FAILED
    if state == "SUCCEEDED":
        problems = _success_evidence_problems(receipt_dir, receipt, args.attempt_id)
        if problems:
            print("invalid completion receipt: " + "; ".join(problems), file=sys.stderr)
            return 2
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
    if state in EXIT_BY_STATE and os.path.lexists(claim_path):
        print("receipt publication is incomplete (claim retained): " + state, file=sys.stderr)
        return 5 if state == "TERMINATION_UNCONFIRMED" else PUBLICATION_FAILED
    if state == "SUCCEEDED":
        problems = _success_evidence_problems(receipt_dir, receipt, args.attempt_id)
        if problems:
            print("invalid completion receipt: " + "; ".join(problems), file=sys.stderr)
            return 2
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
        return _commit_terminal(receipt_dir, receipt, claim_path, preserve_published=True)
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
    try:
        if not _request_cancellation(receipt_dir, receipt):
            print("cancellation already requested or attempt no longer RUNNING", file=sys.stderr)
            return 2
    except UnconfirmedPublicationError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BY_STATE["TERMINATION_UNCONFIRMED"]
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        print(f"cancel request publication failed: {exc}", file=sys.stderr)
        return PUBLICATION_FAILED
    confirmed = terminate_group(None, pgid, args.grace_seconds)
    receipt["result"]["termination_confirmed"] = confirmed
    receipt["result"]["state"] = (
        "CANCELLED" if confirmed else "TERMINATION_UNCONFIRMED")
    receipt["timing"]["finished_at"] = _utcnow()
    return _commit_terminal(receipt_dir, receipt, claim_path)


def cmd_verify_evidence(args) -> int:
    """Exit 0 iff the ids are exactly the valid evidence set: the expected
    count, each with a readable receipt for a supervised, completed review
    attempt (state SUCCEEDED, output_schema "review", schema_valid true),
    and each from a distinct seat (as recorded by the dispatcher). This
    proves a completed reviewer attempt per seat — not that the transport
    opened distinct real model sessions, which no receipt field can show.
    This is the producer-side check behind route_task.py's exact-count
    rule — run it BEFORE typing --isolation-evidence.

    Compatibility: a 1.4.x `to_xai` receipt that survived the upgrade was
    valid under its own contract but fails the envelope checks below. That
    is an intended, narrow, fail-closed window — verify an older evidence
    set with the older `verify-evidence`."""
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
        if getattr(args, "require_receipt_guard", False) and not receipt_guard.verified_metadata(
                receipt.get("receipt_guard"), receipt_dir, attempt_id):
            problems.append(f"{attempt_id}: receipt guard is missing or invalid")
        state = receipt["result"]["state"]
        if state == "SUCCEEDED":
            problems.extend(f"{attempt_id}: {reason}" for reason in
                            _success_evidence_problems(receipt_dir, receipt, attempt_id))
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
        # DD-6. Two checks, both about grok's exit-0 cancellation.
        #
        # The first is near-tautological beside SUCCEEDED, and that is what
        # it is for: it catches a hand-assembled evidence set, where the
        # state word was chosen rather than earned.
        envelope = receipt.get("result", {}).get("envelope")
        expected_stop = ENVELOPE_OK_STOP_REASONS.get(
            receipt.get("output_envelope"), ENVELOPE_OK_STOP_REASON)
        if envelope is not None and receipt.get("output_envelope") \
                != CODEX_TEXT_FORMAT and \
                envelope.get("stop_reason") != expected_stop:
            problems.append(
                f"{attempt_id}: envelope stop_reason is "
                f"{envelope.get('stop_reason')!r}, not "
                f"{expected_stop!r}")
        # The second is the second net behind the pre-spawn preflight. That
        # preflight refuses a PARTIAL declaration before the attempt starts;
        # this refuses a COMPLETE absence at the moment such a receipt is
        # promoted to review evidence. `transport_id` is a caller
        # declaration like every other linkage field, so a dispatch that
        # declares nothing at all is silent to both — that residue belongs
        # to the Layer B recipe, and adapters.md says so rather than
        # leaving it implied.
        transport_id = receipt.get("transport_id") or ""
        if transport_id.endswith(XAI_TRANSPORT_SUFFIX):
            for label, value in (("result.envelope", envelope),
                                 ("session_evidence",
                                  receipt.get("session_evidence"))):
                if value is None:
                    problems.append(
                        f"{attempt_id}: transport_id {transport_id!r} "
                        f"dispatches into grok but the receipt carries no "
                        f"{label} — a cancelled grok turn exits 0, so this "
                        f"receipt cannot show the turn finished")
            declared_format = receipt.get("output_envelope")
            if declared_format not in (None, GROK_ENVELOPE_FORMAT):
                problems.append(
                    f"{attempt_id}: transport_id {transport_id!r} dispatches "
                    f"into grok but the receipt declares envelope format "
                    f"{declared_format!r} — another vendor's document shape "
                    f"was graded, so this receipt says nothing about a "
                    f"cancelled grok turn")
        if receipt.get("result", {}).get("verdict_recovered") is True:
            # Not a problem: the verdict WAS recovered and the review did run.
            # It is a signal, and the one person who can fix the recipe is the
            # one reading this — so it goes to stderr rather than into a
            # receipt field nobody opens.
            print(f"note: {attempt_id}: the verdict was recovered from a "
                  f"run-in line; the seat's output needed repair to parse and "
                  f"its recipe should be fixed (references/adapters.md, "
                  f"\"Output is a contract\")", file=sys.stderr)
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
    run.add_argument("--allow-nested-sandbox", choices=list(NESTED_SANDBOX_OPT_INS),
                     default=None,
                     help="with --receipt-guard, allow a codex child that runs its "
                          "own -s/--sandbox; the caller vouches the child reads "
                          "no files (DD-B9). Recorded in the receipt")
    run.add_argument("--receipt-guard", choices=["none", receipt_guard.GUARD_NAME], default="none",
                     help="explicit kernel protection for this child's receipt store; no silent fallback")
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
                     help="version-named stdout contract to grade against, "
                          "read with that format's own key names. A cancelled "
                          "grok turn exits 0, so its stopReason is the only "
                          "machine-readable finish evidence; a Claude document "
                          "additionally carries type/subtype/is_error "
                          "discriminators and its own token counts")
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
                          "changed) IN PLACE before it can be SUCCEEDED; "
                          "repeatable. The path is pinned to one inode "
                          "before spawn (an absent one is reserved) and must "
                          "still name that inode at grading, so a rename "
                          "over it is refused; it must be the only name for "
                          "that inode; and a pre-spawn baseline that cannot "
                          "be read is absence only on a confirmed ENOENT")
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
    run.add_argument("--expect-sandbox-profile", default=None,
                     help="require the session summary's sandbox_profile to "
                          "equal this exactly, so a --sandbox flag that "
                          "quietly did nothing cannot pass as success; "
                          "requires --session-evidence")
    run.add_argument("--child-cwd", default=None, metavar="DIR",
                     help="directory the child is launched in; when "
                          "--require-single-linked-cwd is also set, the "
                          "supervisor walks this tree before spawn and "
                          "refuses any regular file with st_nlink != 1")
    run.add_argument("--require-single-linked-cwd", action="store_true",
                     help="refuse to spawn if --child-cwd contains a "
                          "regular file whose inode has more than one name; "
                          "requires --child-cwd")
    run.add_argument("--grok-home", default=None, metavar="DIR",
                     help="attempt-private GROK_HOME injected into the "
                          "child environment only; created 0700 if absent; "
                          "a symlink is refused")
    run.add_argument("--grok-auth-seed", default=None, metavar="FILE",
                     help="regular file copied onto a NEW inode at "
                          "$GROK_HOME/auth.json; requires --grok-home")
    run.add_argument("--expect-sandbox-enforced", default=None, metavar="PROFILE",
                     help="require the reserved ProfileApplied log under "
                          "$GROK_HOME (" + ", ".join(
                              "/".join(parts)
                              for parts in SANDBOX_EVENT_RELPATHS)
                          + ") to name this profile with enforced=true; every "
                            "location is read and two that disagree fail; "
                            "requires --grok-home")
    run.add_argument("--seat-profile", default=None,
                     choices=[MAKER_SEAT_PROFILE],
                     help="typed maker-seat declaration; grok-maker-v1 "
                          "requires the workspace audit, per-attempt home, "
                          "auth seed, envelope, session evidence, and "
                          "enforced-sandbox gates together")
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
    verify.add_argument("--require-receipt-guard", action="store_true",
                        help="require a matching launched Darwin receipt-guard recipe")
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
