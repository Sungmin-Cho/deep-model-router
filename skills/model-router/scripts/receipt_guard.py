"""Explicit Darwin protection for a dispatch child's receipt store.

This is a local kernel boundary for the launched process tree, not cryptographic
receipt authentication or protection from unrelated unsandboxed same-UID actors.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path

GUARD_NAME = "darwin-sandbox-v1"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# macOS SDK sys/fcntl.h: F_GETPATH = 50, MAXPATHLEN = 1024.
F_GETPATH = 50
MAX_ENTRIES = 10000
BASE_POLICY = """(version 1)
(allow default)
(deny file-read* file-write* (subpath (param "ROOT")))
(allow file-write-data file-read-metadata (literal (param "STDOUT")) (literal (param "STDERR")))
(deny file-link)
(deny signal)
(allow signal (target same-sandbox))
(deny mach-priv-task-port)
(deny appleevent-send)
(deny job-creation)
"""
PROBE = """import errno,os,sys
try:
    fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
except OSError as e:
    sys.exit(0 if e.errno in (errno.EACCES,errno.EPERM) else 2)
else:
    os.close(fd)
    sys.exit(3)
"""


class GuardError(ValueError):
    pass


def _kernel_path(fd: int) -> str:
    if sys.platform != "darwin":
        raise GuardError("Darwin receipt guard is unavailable on this platform")
    try:
        raw = fcntl.fcntl(fd, F_GETPATH, b"\0" * 1024)
        return raw.split(b"\0", 1)[0].decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise GuardError(f"cannot resolve kernel receipt path: {exc}") from exc


def canonical_directory(root: Path) -> Path:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return Path(_kernel_path(fd))
    finally:
        os.close(fd)


def _check_store(root: Path) -> None:
    """New links are denied by Seatbelt; reject pre-existing aliases as well."""
    pending = [root]
    count = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ENTRIES:
                    raise GuardError("receipt store exceeds guard inspection limit; use a fresh store")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise GuardError("receipt store contains a symlink, special file, or pre-existing hard link")


def _recipe(root: Path, stdout_fd: int, stderr_fd: int):
    if sys.platform != "darwin" or not os.path.isfile(SANDBOX_EXEC):
        raise GuardError("Darwin receipt guard is unavailable on this platform")
    canonical = canonical_directory(root)
    outputs = [_kernel_path(stdout_fd), _kernel_path(stderr_fd)]
    if any(Path(output).parent != canonical for output in outputs):
        raise GuardError("output descriptors do not belong to the canonical receipt root")
    for fd in (stdout_fd, stderr_fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise GuardError("output descriptor is not a single-linked regular file")
    parameters = {"ROOT": str(canonical), "STDOUT": outputs[0], "STDERR": outputs[1]}
    policy = BASE_POLICY
    # Protect exact ancestor nodes, without denying ordinary workspace children.
    for index, ancestor in enumerate(canonical.parents):
        name = f"ANCESTOR_{index}"
        parameters[name] = str(ancestor)
        policy += f'(deny file-write* (literal (param "{name}")))\n'
    digest = hashlib.sha256(json.dumps({"policy": policy, "parameters": parameters},
        sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    prefix = [SANDBOX_EXEC]
    for key, value in parameters.items():
        prefix.extend(["-D", f"{key}={value}"])
    prefix.extend(["-p", policy, "--"])
    metadata = dict(mechanism=GUARD_NAME, profile_sha256=digest,
                    protected_root=str(canonical), phase="prepared")
    return prefix, metadata


def reject_protected_input(root: Path, fd: int) -> None:
    source = Path(_kernel_path(fd))
    canonical = canonical_directory(root)
    if source == canonical or canonical in source.parents:
        raise GuardError("prompt input descriptor belongs to the protected receipt store")


def prepare(root: Path, stdout_fd: int, stderr_fd: int, timeout: float):
    prefix, metadata = _recipe(root, stdout_fd, stderr_fd)
    canonical = Path(metadata["protected_root"])
    _check_store(canonical)
    probe = canonical / (".receipt-guard-probe-" + uuid.uuid4().hex)
    try:
        result = subprocess.run([*prefix, sys.executable, "-I", "-S", "-c", PROBE, str(probe)],
            stdin=subprocess.DEVNULL, capture_output=True, close_fds=True, timeout=min(3.0, timeout))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError(f"receipt guard probe unavailable: {exc}") from exc
    if result.returncode == 3:
        probe.unlink(missing_ok=True)  # O_EXCL proves this probe created it.
    if result.returncode != 0:
        raise GuardError(f"receipt guard probe failed (exit {result.returncode})")
    return prefix, metadata


def verified_metadata(metadata, root: Path, attempt_id: str) -> bool:
    """Check recipe binding, not the authenticity of an arbitrary supplied file."""
    if not isinstance(metadata, dict) or metadata.get("phase") != "launched":
        return False
    fds = []
    try:
        for suffix in ("stdout", "stderr"):
            fds.append(os.open(root / f"{attempt_id}.{suffix}",
                               os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK))
        _prefix, expected = _recipe(root, *fds)
        expected["phase"] = "launched"
        return metadata == expected
    except (OSError, ValueError, TypeError):
        return False
    finally:
        for fd in fds:
            os.close(fd)
