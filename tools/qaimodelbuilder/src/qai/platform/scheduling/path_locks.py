# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-path file locks (serialise concurrent writes to the SAME file).

When one round's tool calls run in PARALLEL (parallel-tool-execution-design.md),
two ``write`` / ``edit`` / ``apply_patch`` calls hitting the SAME file would
lost-update / TOCTOU each other. This manager hands out one ``asyncio.Lock``
PER canonical file path so:

* concurrent writes to the SAME file serialise (correct, no lost update);
* writes to DIFFERENT files still run fully in parallel (no global lock).

It uses a per-file semaphore — fine-grained, by-resource,
NOT a coarse global mutex. Platform-neutral (sibling of ``tool_concurrency.py``
/ ``background_tasks.py``): it knows nothing about any bounded context.

The SAME manager instance must be shared by the main agent and every sub-agent
(DI wires one instance into both, see ``apps/api/_chat_di.py``) — otherwise two
agents writing the same file through separate lock tables would still collide.

Canonical key: ``normcase(normpath(abspath(path)))`` — a PURE string
normalisation (no disk touch). A write target may not exist yet, so ``realpath``
(which resolves symlinks via the filesystem) is unreliable here; the pure form
is deterministic and good enough to make "same file" judgements consistent
(State-Truth-First: the key never depends on whether the file currently exists).

To avoid the lock table growing without bound on a long-lived process, locks are
reference-counted and removed when no waiter/holder remains.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from qai.platform.logging import get_logger

_log = get_logger(__name__)


def canonical_path_key(path: str) -> str:
    """Return the canonical lock key for an ABSOLUTE file path.

    Pure string normalisation (no filesystem access, so it works for
    not-yet-created write targets): ``normpath`` (collapses ``..`` / redundant
    separators) + ``normcase`` (lower-cases + unifies separators on Windows so
    ``C:\\Foo`` == ``c:/foo``).

    Contract (State-Truth-First, AGENTS.md 铁律 4): callers MUST pass an
    already workspace-resolved ABSOLUTE path (the write-class tools do this via
    ``resolve_under_workspace`` before locking). A RELATIVE path is anchored to
    the process CWD by ``abspath`` — but CWD is mutable global state, so two
    spellings of the same file (or the same file under different CWDs) could
    yield DIFFERENT keys → two locks → concurrent writes NOT serialised
    (lost-update). We therefore log a loud warning on a relative path so the
    contract violation is visible rather than silently mis-locking, while still
    anchoring best-effort so the lock is never simply skipped.
    """
    try:
        if not os.path.isabs(path):
            _log.warning(
                "path_locks.relative_path_key",
                path=path,
                detail=(
                    "canonical_path_key got a RELATIVE path; lock correctness "
                    "depends on stable CWD. Callers must pass a "
                    "workspace-resolved absolute path."
                ),
            )
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except (ValueError, OSError):
        return os.path.normcase(path)


class PathLockManager:
    """Hands out one ``asyncio.Lock`` per canonical file path.

    Constructed once in DI and shared by the main agent + all sub-agents so
    every writer of a given file contends on the SAME lock.
    """

    __slots__ = ("_locks", "_refcount", "_guard")

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refcount: dict[str, int] = {}
        # Guards the lock-table bookkeeping itself (acquire/create/cleanup),
        # NOT the file — held only for the tiny dict operations.
        self._guard = asyncio.Lock()

    async def _acquire_lock_obj(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._refcount[key] = self._refcount.get(key, 0) + 1
            return lock

    async def _release_lock_obj(self, key: str, lock: asyncio.Lock) -> None:
        """Release the file lock AND decrement its refcount atomically.

        Releasing the ``asyncio.Lock`` and the refcount bookkeeping happen
        inside the SAME ``_guard`` critical section (no await between them) so
        a concurrent ``_acquire_lock_obj`` for the same key can never observe a
        half-updated state (lock released but still counted, or popped while a
        waiter holds a reference). The waiter's ``refcount += 1`` already ran
        under ``_guard`` BEFORE this decrement, so ``n`` here always accounts
        for every current holder/waiter and we only pop when truly idle.
        """
        async with self._guard:
            lock.release()
            n = self._refcount.get(key, 0) - 1
            if n <= 0:
                self._refcount.pop(key, None)
                # Drop the lock object only when nobody else references it and
                # it is not held (a waiter would have kept refcount > 0).
                if not lock.locked():
                    self._locks.pop(key, None)
            else:
                self._refcount[key] = n

    @asynccontextmanager
    async def lock(self, path: str) -> AsyncIterator[None]:
        """Acquire the lock for ONE file path (canonicalised)."""
        key = canonical_path_key(path)
        lock = await self._acquire_lock_obj(key)
        await lock.acquire()
        try:
            yield None
        finally:
            await self._release_lock_obj(key, lock)

    @asynccontextmanager
    async def lock_many(self, paths: Sequence[str]) -> AsyncIterator[None]:
        """Acquire locks for MULTIPLE paths (e.g. a multi-file apply_patch).

        Locks are acquired in SORTED canonical-key order so two concurrent
        multi-path operations can never deadlock (classic ordered-acquire).
        Released in reverse order.
        """
        keys = sorted({canonical_path_key(p) for p in paths})
        if not keys:
            yield None
            return
        acquired: list[tuple[str, asyncio.Lock]] = []
        try:
            for key in keys:
                lock = await self._acquire_lock_obj(key)
                await lock.acquire()
                acquired.append((key, lock))
            yield None
        finally:
            for key, lock in reversed(acquired):
                await self._release_lock_obj(key, lock)
