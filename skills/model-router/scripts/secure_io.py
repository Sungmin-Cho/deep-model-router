"""File access that cannot be walked out of its root (design 2026-09-25 DD-A0).

Two layers.

``open_regular(path)`` is the evidence-file primitive `dispatch_agent.py` has
always used: ``O_NOFOLLOW|O_NONBLOCK`` and a ``fstat`` of the SAME fd, so a
symlink or a FIFO planted at an artifact path is refused without blocking.

``StateRoot`` is the discipline for the router's local model state. The root
directory is opened ONCE (``O_DIRECTORY|O_NOFOLLOW``) and checked on that fd:
a directory, owned by this uid, mode 0700. Every subdirectory and file below
it is then opened with ``openat`` relative to a checked directory fd — never
by pathname — so renaming the root away, or planting a symlink at an
intermediate directory, cannot redirect a read or a write outside it.
Subdirectories get the same checks as the root; files are opened
``O_NOFOLLOW|O_NONBLOCK`` and must be regular, 0600, single-linked, owned by
this uid and at most 256 KiB, all read from the fd that is then read.

What this is NOT: authentication. The same uid can rewrite these files, the
plugin cache and the CLI binaries alike; the checks keep out accidental
damage, path substitution and other users — the honesty `receipt_guard.py`
states about itself.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from strict_json import ensure_json_value, loads as strict_json_loads

MAX_STATE_FILE_BYTES = 256 * 1024
DIR_MODE = 0o700
FILE_MODE = 0o600
LOCK_RELPATH = "work/publish.lock"


class NotARegularFile(OSError):
    """An evidence path resolved to something that is not a regular file.

    Distinct from a plain OSError so callers can tell "this is a FIFO,
    device or symlink" (a containment/blocking hazard) from "this is
    missing" (an ordinary absent artifact) — the two get different
    `invalid_reasons`.
    """


class StateError(OSError):
    """A state path failed admission: wrong type, mode, owner, link count,
    size, a symlink hop, or undecodable content. Always fail closed."""


def open_regular(path: Path) -> tuple[int, os.stat_result]:
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


def _current_uid() -> int:
    """Indirection so a test can stand in for "another user owns this"
    without root: the comparison, not the kernel, is what is under test."""
    return os.getuid()


def _check_dir(fd: int, label: str) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise StateError(f"{label} is not a directory")
    if st.st_uid != _current_uid():
        raise StateError(f"{label} is not owned by this user")
    if stat.S_IMODE(st.st_mode) != DIR_MODE:
        raise StateError(f"{label} has mode {stat.S_IMODE(st.st_mode):o}, want 700")


def _check_file(st: os.stat_result, label: str, max_bytes: int) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise StateError(f"{label} is not a regular file")
    if st.st_uid != _current_uid():
        raise StateError(f"{label} is not owned by this user")
    if stat.S_IMODE(st.st_mode) != FILE_MODE:
        raise StateError(f"{label} has mode {stat.S_IMODE(st.st_mode):o}, want 600")
    if st.st_nlink != 1:
        raise StateError(f"{label} has {st.st_nlink} links, want 1")
    if st.st_size > max_bytes:
        raise StateError(f"{label} exceeds {max_bytes} bytes")


def _components(relpath: str) -> list[str]:
    parts = relpath.split("/")
    if not relpath or any(p in ("", ".", "..") for p in parts):
        raise StateError(f"state path {relpath!r} is not a plain relative path")
    return parts


def canonical_json_bytes(obj: Any) -> bytes:
    """The one serialisation state files are written in (and hashed as)."""
    ensure_json_value(obj)
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("ascii")


class StateRoot:
    """A verified root directory fd; every access below it is fd-relative."""

    def __init__(self, fd: int, path: Path):
        self._fd = fd
        self.path = path

    @classmethod
    def open(cls, path: Path, *, create: bool = False) -> "StateRoot":
        """Open (optionally creating, 0700) and admit the root directory.
        Raises FileNotFoundError when it is absent and `create` is false."""
        path = Path(path)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(path, DIR_MODE)
            except FileExistsError:
                pass
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise
        except OSError as exc:     # ELOOP (symlink), ENOTDIR, EACCES
            raise StateError(f"state root {path} cannot be opened: {exc}") from None
        try:
            _check_dir(fd, "state root")
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, path)

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "StateRoot":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    # -- walking ------------------------------------------------------------
    def _open_dir(self, parent_fd: int, name: str, label: str, create: bool) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, DIR_MODE, dir_fd=parent_fd)
            except FileExistsError:
                pass
            fd = os.open(name, flags, dir_fd=parent_fd)
            # mkdir honours the umask; the admission rule is exact.
            if stat.S_IMODE(os.fstat(fd).st_mode) != DIR_MODE:
                os.fchmod(fd, DIR_MODE)
        except OSError as exc:
            raise StateError(f"{label} cannot be opened as a directory: {exc}") from None
        try:
            _check_dir(fd, label)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @contextmanager
    def _parent(self, relpath: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        """Yield (fd of the checked parent directory, final name)."""
        parts = _components(relpath)
        opened: list[int] = []
        cur = self._fd
        try:
            for i, name in enumerate(parts[:-1]):
                cur = self._open_dir(cur, name, "/".join(parts[: i + 1]), create)
                opened.append(cur)
            yield cur, parts[-1]
        finally:
            for fd in opened:
                os.close(fd)

    # -- queries ------------------------------------------------------------
    def lexists(self, relpath: str) -> bool:
        """Does anything (of any type, symlinks included) sit at `relpath`?
        An intermediate directory that fails admission raises StateError."""
        try:
            with self._parent(relpath) as (dfd, name):
                os.stat(name, dir_fd=dfd, follow_symlinks=False)
                return True
        except FileNotFoundError:
            return False

    def listdir(self, relpath: str) -> list[str]:
        with self._dir(relpath) as dfd:
            return sorted(os.listdir(dfd))

    @contextmanager
    def _dir(self, relpath: str, *, create: bool = False) -> Iterator[int]:
        with self._parent(relpath, create=create) as (dfd, name):
            fd = self._open_dir(dfd, name, relpath, create)
            try:
                yield fd
            finally:
                os.close(fd)

    # -- reads --------------------------------------------------------------
    def read_bytes(self, relpath: str, *, max_bytes: int = MAX_STATE_FILE_BYTES) -> bytes:
        with self._parent(relpath) as (dfd, name):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise StateError(f"{relpath} cannot be opened: {exc}") from None
            try:
                _check_file(os.fstat(fd), relpath, max_bytes)
                chunks, total = [], 0
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise StateError(f"{relpath} exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(fd)

    def read_json(self, relpath: str, *, max_bytes: int = MAX_STATE_FILE_BYTES) -> Any:
        return self.read_json_with_sha(relpath, max_bytes=max_bytes)[0]

    def read_json_with_sha(self, relpath: str, *,
                           max_bytes: int = MAX_STATE_FILE_BYTES) -> tuple[Any, str]:
        """(strict-decoded value, sha256 of the exact bytes read)."""
        data = self.read_bytes(relpath, max_bytes=max_bytes)
        try:
            value = strict_json_loads(data)
        except ValueError as exc:
            raise StateError(f"{relpath} is not strict JSON: {exc}") from None
        return value, hashlib.sha256(data).hexdigest()

    # -- writes -------------------------------------------------------------
    def write_bytes_atomic(self, relpath: str, data: bytes) -> str:
        """tmp (O_EXCL, 0600) -> fsync -> renameat -> fsync(dir). Returns the
        sha256 of `data`. Parent directories are created 0700 as needed."""
        with self._parent(relpath, create=True) as (dfd, name):
            tmp = f".{name}.tmp-{secrets.token_hex(8)}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         FILE_MODE, dir_fd=dfd)
            try:
                try:
                    os.fchmod(fd, FILE_MODE)
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except OSError:
                    pass
                raise
            os.fsync(dfd)
        return hashlib.sha256(data).hexdigest()

    def write_json_atomic(self, relpath: str, obj: Any) -> str:
        return self.write_bytes_atomic(relpath, canonical_json_bytes(obj))

    def mkdir(self, relpath: str) -> None:
        with self._dir(relpath, create=True):
            pass

    def rename(self, src_relpath: str, dst_relpath: str) -> None:
        """renameat between two entries under this root (e.g. the atomic
        `committed.tmp-*` -> `committed` bootstrap), then fsync both parents."""
        with self._parent(src_relpath) as (sfd, sname), \
                self._parent(dst_relpath, create=True) as (dfd, dname):
            os.rename(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd)
            os.fsync(dfd)
            if sfd != dfd:
                os.fsync(sfd)

    def fsync_dir(self, relpath: str | None = None) -> None:
        if relpath is None:
            os.fsync(self._fd)
            return
        with self._dir(relpath) as fd:
            os.fsync(fd)

    @contextmanager
    def lock(self, *, timeout: float = 10.0) -> Iterator[None]:
        """Exclusive publication lock on `work/publish.lock`. A stable inode
        serialises cooperating writers; the file is never unlinked."""
        with self._parent(LOCK_RELPATH, create=True) as (dfd, name):
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         FILE_MODE, dir_fd=dfd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != _current_uid()):
                raise StateError("publication lock is not a single-linked regular file of this user")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StateError("publication lock deadline exceeded") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
