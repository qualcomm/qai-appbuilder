# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""File-system-backed :class:`WorkspaceLockPort` (PR-046).

Replaces the in-memory ``_FakeWorkspaceLock`` from S3 with a
cross-process mutex.  Each acquired workspace is represented by a
sentinel file under ``<lock_root>/<sha256(path)>.lock`` (or, when no
root is configured, the legacy ``<workspace>/.qai_workspace.lock``
location).  The platform-appropriate primitive holds an exclusive
flock for as long as the handle lives.

Design choices
--------------
* The default lock-file location is the workspace itself which is
  **not** suitable for tests that hard-code real paths (e.g.
  ``C:/work/cc-sse``) because the OS lock then leaks across test
  invocations.  Production wiring therefore passes
  :class:`pathlib.Path` ``lock_root`` so all lock files for a given
  container live under the per-data-paths sandbox.
* Filenames are derived from a SHA-256 of the workspace path so two
  visually-similar paths (e.g. ``C:/work`` vs ``C:/Work``) map to
  distinct sentinel files without depending on case-folding.

Platform support
----------------
* POSIX (Linux/macOS): ``fcntl.flock`` with ``LOCK_EX | LOCK_NB``.
* Windows: ``msvcrt.locking`` with ``LK_NBLCK`` over the first byte
  of the sentinel file.

If the underlying primitive returns "already held"
(``BlockingIOError`` on POSIX, ``OSError`` with errno 11/13/33/36 on
Windows) the adapter raises
:class:`qai.ai_coding.domain.WorkspaceLockedError` so the
application layer can map it to a clean ``409``.

The two ``async`` operations (:meth:`acquire`, :meth:`release`) wrap
synchronous file I/O via :func:`asyncio.to_thread` so the FastAPI
event loop is never blocked by the OS call.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import sys
from pathlib import Path
from typing import IO

from qai.ai_coding.application.ports import WorkspaceLockHandle
from qai.ai_coding.domain import Workspace, WorkspaceLockedError

__all__ = [
    "WORKSPACE_LOCK_FILENAME",
    "FileSystemWorkspaceLock",
    "FileSystemWorkspaceLockHandle",
]


WORKSPACE_LOCK_FILENAME = ".qai_workspace.lock"

_IS_WINDOWS = sys.platform.startswith("win")
# Windows error codes that mean "already locked" — see
# `_is_already_locked_error` for context.
_WIN_LOCK_ERRNOS = frozenset({11, 13, 36})
_WIN_LOCK_VIOLATION = 33


def _lock_file_path(workspace: Workspace, lock_root: Path | None) -> Path:
    """Return the sentinel-file path for ``workspace``.

    When ``lock_root`` is ``None`` the lock lives next to the workspace
    (legacy behaviour).  Production deployments pass a per-container
    root so test isolation works without depending on filesystem
    cleanup of real workspace paths.
    """
    if lock_root is None:
        return Path(workspace.path) / WORKSPACE_LOCK_FILENAME
    digest = hashlib.sha256(workspace.path.encode("utf-8")).hexdigest()
    return lock_root / f"{digest}.lock"


# ---------------------------------------------------------------------------
# Sync primitives (run in worker thread)
# ---------------------------------------------------------------------------
def _acquire_sync(path: Path) -> IO[bytes]:
    """Open ``path`` and acquire an exclusive non-blocking lock.

    Raises :class:`BlockingIOError` (POSIX) or :class:`OSError`
    (Windows) when the file is already locked by another holder.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``a+b`` keeps the file content (zero bytes by default) and lets us
    # both write the sentinel byte and read the existing one.
    fp: IO[bytes] = path.open("a+b")
    try:
        if _IS_WINDOWS:
            import msvcrt

            # Need at least one byte to lock on Windows.
            if path.stat().st_size == 0:
                fp.write(b"\x00")
                fp.flush()
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        fp.close()
        raise
    return fp


def _release_sync(fp: IO[bytes]) -> None:
    try:
        if _IS_WINDOWS:
            import msvcrt

            with contextlib.suppress(OSError):
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            fp.close()


def _is_already_locked_error(exc: BaseException) -> bool:
    """Return ``True`` iff ``exc`` indicates the lock is already held."""
    if isinstance(exc, BlockingIOError):
        return True
    if _IS_WINDOWS and isinstance(exc, OSError):
        if exc.errno in _WIN_LOCK_ERRNOS:
            return True
        if getattr(exc, "winerror", None) == _WIN_LOCK_VIOLATION:
            return True
    return False


# ---------------------------------------------------------------------------
# Handle
# ---------------------------------------------------------------------------
class FileSystemWorkspaceLockHandle:
    """Concrete :class:`WorkspaceLockHandle` returned by the adapter."""

    __slots__ = ("_fp", "_parent", "_released", "_workspace")

    def __init__(
        self,
        *,
        workspace: Workspace,
        fp: IO[bytes],
        parent: FileSystemWorkspaceLock,
    ) -> None:
        self._workspace = workspace
        self._fp = fp
        self._released = False
        self._parent = parent

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await asyncio.to_thread(_release_sync, self._fp)
        self._parent._on_release(self._workspace.path)


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------
class FileSystemWorkspaceLock:
    """Cross-process :class:`WorkspaceLockPort` adapter.

    Tracks an in-process registry of held paths so a second
    :meth:`acquire` call from the same process surfaces
    :class:`WorkspaceLockedError` *without* going through the OS lock
    — POSIX ``flock`` is per-FD on the same process which would
    otherwise succeed and silently allow a second use case to enter.
    """

    __slots__ = ("_held", "_lock_root")

    def __init__(self, *, lock_root: Path | None = None) -> None:
        self._held: dict[str, IO[bytes]] = {}
        self._lock_root = lock_root

    @property
    def lock_root(self) -> Path | None:
        return self._lock_root

    async def acquire(self, workspace: Workspace) -> WorkspaceLockHandle:
        path = workspace.path
        if path in self._held:
            raise WorkspaceLockedError(
                message=f"workspace {path} already locked",
                details={"workspace": path},
            )
        lock_path = _lock_file_path(workspace, self._lock_root)
        try:
            fp = await asyncio.to_thread(_acquire_sync, lock_path)
        except Exception as exc:
            if _is_already_locked_error(exc):
                raise WorkspaceLockedError(
                    message=f"workspace {path} already locked",
                    details={"workspace": path},
                ) from exc
            raise
        self._held[path] = fp
        return FileSystemWorkspaceLockHandle(
            workspace=workspace, fp=fp, parent=self
        )

    async def release(self, workspace: Workspace) -> None:
        fp = self._held.pop(workspace.path, None)
        if fp is None:
            return
        await asyncio.to_thread(_release_sync, fp)

    # Internal callback used by handles.
    def _on_release(self, path: str) -> None:
        self._held.pop(path, None)

    # Best-effort cleanup if the process exits without releasing.
    def __del__(self) -> None:  # pragma: no cover — defensive
        for fp in list(self._held.values()):
            with contextlib.suppress(OSError):
                _release_sync(fp)
        self._held.clear()
