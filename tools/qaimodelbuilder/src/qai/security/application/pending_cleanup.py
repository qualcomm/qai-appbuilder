# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Phase 2 (2026-07-06) — subprocess-gone cleanup service.

Scans the in-memory :class:`PermissionWaitRegistry` (and the durable
:class:`PermissionPendingStorePort` mirror when persistence is enabled)
on a fixed interval (default 10s) and RESOLVES any pending ASK whose
subprocess is no longer alive as ``subprocess_gone``:

* the in-memory registry wakes the awaiting future with ``allow=False /
  scope='deny'`` (the native-hook bridge translates that to DENY);
* the durable store records ``resolution='subprocess_gone'`` so a
  restart / audit query can distinguish this from a genuine user-DENY.

Why this is needed
------------------
Phase 2 shipped an INFINITE wait on the ASK future (see permission_wait.py
docstring). If a native subprocess triggered an ASK, then died / was killed
before the user clicked, the ASK future would hang forever with nothing to
wake it. This service is the "the pid is gone, stop waiting" fallback.

The DLL pipe on the native side ALSO tears down when the process dies
(see plan §2 N9 — subprocess-gone teardown), so there's no risk of double-
resolve: by the time the pid is dead the DLL is no longer holding a pipe
open, and the filter callback for that event has already returned (with
whatever the DLL's own teardown policy chose).

Windows-only pid liveness — uses ``ctypes.windll.kernel32`` because psutil
is a heavy dep for a 10s poll of a tiny id list. Non-Windows platforms
(no native FileGuard) skip the pid check silently; the service becomes a
no-op there (there are no pending ASKs from native subprocesses).

State-Truth-First (AGENTS.md §5): the truth source is the OS ("does this
pid still exist and is it in a non-terminated state") — NOT any cached
"we spawned it recently" flag. A pid that was reused by an unrelated
process still counts as "alive" and stays pending; the user is expected
to click within the reuse window (which is orders of magnitude larger
than the 10s scan cadence).
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import inspect
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.logging import get_logger
from qai.platform.time import Clock

if TYPE_CHECKING:  # pragma: no cover
    from qai.security.application.permission_wait import PermissionWaitRegistry
    from qai.security.application.ports import PermissionPendingStorePort

#: Optional resolved-notification callback: ``on_resolved(request_id,
#: resolution)`` — invoked (best-effort) after the sweep wakes a stale ASK
#: future so an apps-layer publisher can push a UI-close SSE frame
#: (``PermissionResolvedEvent``). May be sync or async; either is awaited /
#: called defensively. ``None`` keeps the sweep byte-for-byte unchanged.
OnResolvedCallback = Callable[[str, str], "Awaitable[None] | None"]

__all__ = ["PendingCleanupService"]

_log = get_logger(__name__)


# Windows PROCESS_QUERY_LIMITED_INFORMATION = 0x1000. We use the "limited"
# variant so we can query even elevated / protected processes without
# holding SeDebugPrivilege (matches what psutil.pid_exists uses on Win8+).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259  # STATUS_PENDING — the sentinel Windows returns while
# a process is still running. Any OTHER exit code means the process has
# exited (with that code). ``GetExitCodeProcess`` never fails for a valid
# HANDLE, so the STILL_ACTIVE check is the definitive liveness signal.


class PendingCleanupService:
    """Periodically resolve stale ASKs whose subprocess is dead.

    Parameters
    ----------
    wait_registry:
        The process-wide :class:`PermissionWaitRegistry`. Its
        ``list_pending()`` is the primary source of truth; the service
        wakes futures via ``resolve(request_id, allow=False, scope='deny')``.
    pending_store:
        Optional durable mirror (``PermissionPendingStorePort``). When
        provided, the service also marks the row as
        ``resolution='subprocess_gone'`` so a restart / audit query can
        distinguish it from user-DENY. Also used to look up the pid for
        request_ids that came from a prior boot (rehydrate flow — Phase
        2.5). ``None`` skips the durable side (in-memory-only).
    clock:
        Domain clock used for ``resolved_at`` timestamps.
    scan_interval_seconds:
        How often to sweep (default 10s). The Phase 2 plan (§2.3 P4)
        defines this as the cadence; operators may tune via a future
        setting but the default is fine for a foreground desktop tool.
    """

    __slots__ = (
        "_registry",
        "_store",
        "_clock",
        "_interval",
        "_task",
        "_stopping",
        "_on_resolved",
    )

    def __init__(
        self,
        *,
        wait_registry: "PermissionWaitRegistry",
        pending_store: "PermissionPendingStorePort | None" = None,
        clock: Clock,
        scan_interval_seconds: float = 10.0,
        on_resolved: "OnResolvedCallback | None" = None,
    ) -> None:
        self._registry = wait_registry
        self._store = pending_store
        self._clock = clock
        self._interval = float(scan_interval_seconds)
        self._task: "asyncio.Task[None] | None" = None
        self._stopping = False
        # Problem ② backstop-honesty — optional apps-layer notification called
        # after a stale ASK is resolved subprocess_gone, so a dialog left open
        # after a SILENT subprocess death still closes in the UI (the sweep is
        # the only path that resolves it in that case). ``None`` keeps the
        # sweep byte-for-byte unchanged (additive, backward-compatible).
        self._on_resolved = on_resolved

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "asyncio.Task[None]":
        """Spawn the background sweep task (idempotent).

        Returns the running :class:`asyncio.Task` so the lifespan can
        keep a handle for the shutdown ``cancel()`` call.
        """
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name="security-pending-cleanup"
        )
        return self._task

    async def stop(self) -> None:
        """Stop the sweep task cleanly. Idempotent."""
        self._stopping = True
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    # -- internals -----------------------------------------------------
    async def _run(self) -> None:
        """Main sweep loop — runs until :meth:`stop` is called."""
        _log.info(
            "security.pending_cleanup.started",
            interval_seconds=self._interval,
        )
        try:
            while not self._stopping:
                try:
                    await self._sweep_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — a sweep must never crash the loop
                    _log.warning(
                        "security.pending_cleanup.sweep_failed",
                        exc_info=True,
                    )
                try:
                    await asyncio.sleep(self._interval)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            _log.info("security.pending_cleanup.cancelled")
            raise

    async def _sweep_once(self) -> None:
        """Single scan — resolve any pending id whose pid is dead."""
        pending_ids = self._registry.list_pending()
        if not pending_ids:
            return
        # Build a request_id → pid lookup from the durable store (source of
        # truth for the pid — the in-memory registry does not carry it).
        # Falls back to an empty map when persistence is off; in that case
        # the sweep can't check liveness and the entries stay pending
        # (in-memory-only deployments accept this; they typically run
        # inside tests where subprocesses die predictably).
        pid_lookup: dict[str, int] = {}
        if self._store is not None:
            try:
                for row in await self._store.list_unresolved():
                    rid = row.get("request_id")
                    pid = row.get("pid")
                    if isinstance(rid, str) and isinstance(pid, int) and pid > 0:
                        pid_lookup[rid] = pid
            except Exception:  # noqa: BLE001 — read fail → skip cleanup
                _log.warning(
                    "security.pending_cleanup.store_read_failed",
                    exc_info=True,
                )
                return
        if not pid_lookup:
            return

        now = self._clock.now()
        for rid in pending_ids:
            pid = pid_lookup.get(rid)
            if pid is None:
                continue
            if _pid_is_alive(pid):
                continue
            # Subprocess is gone — resolve as DENY + mark the durable row.
            woke = self._registry.resolve(rid, allow=False, scope="deny")
            # Problem ② backstop-honesty — tell the UI to close the dialog for
            # this now-resolved ASK (a silent subprocess death has no local
            # user response and no exec-cancel flush, so this sweep is the only
            # thing that can close it). Best-effort: a notify glitch must never
            # break the sweep. Only fire when we actually woke a live waiter.
            if woke and self._on_resolved is not None:
                await self._notify_resolved(rid, "subprocess_gone")
            if self._store is not None:
                try:
                    await self._store.mark_resolved(
                        request_id=rid,
                        resolved_at=now,
                        resolution="subprocess_gone",
                    )
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "security.pending_cleanup.mark_failed",
                        request_id=rid,
                        exc_info=True,
                    )
            _log.info(
                "security.permission.subprocess_gone",
                request_id=rid,
                pid=pid,
                woke=woke,
            )

    async def _notify_resolved(self, request_id: str, resolution: str) -> None:
        """Fire the optional resolved-notification callback (best-effort).

        Supports a sync OR async ``on_resolved``; an async return is awaited.
        NEVER raises — a UI-close notification glitch must not break or crash
        the periodic sweep (the resolve + durable mark already happened).
        """
        cb = self._on_resolved
        if cb is None:
            return
        try:
            outcome = cb(request_id, resolution)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:  # noqa: BLE001 — notify must never break the sweep
            _log.warning(
                "security.pending_cleanup.notify_failed",
                request_id=request_id,
                exc_info=True,
            )


# -----------------------------------------------------------------------
# Windows pid liveness via kernel32 — cheap enough for a 10s poll of a
# handful of ids (typical ASK queue is <5). psutil is intentionally NOT
# used here: the additional import cost + PROCESS_QUERY_INFORMATION calls
# it does under the hood are overkill for a boolean "alive?" check.
# -----------------------------------------------------------------------
def _pid_is_alive(pid: int) -> bool:
    """Return True iff ``pid`` refers to a running process (Windows only).

    Non-Windows platforms return True unconditionally — there is no native
    FileGuard there, so the ASK queue is empty and this branch is never
    hit in practice; returning True keeps the entries pending (safer than
    a bogus subprocess-gone resolution).

    Windows: ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` returns
    NULL for an unknown / already-reaped pid → dead.
    ``GetExitCodeProcess`` != STILL_ACTIVE also means dead. Any other API
    fault (access-denied on a protected process, etc.) is treated as
    "alive" so we DON'T falsely resolve a live ASK.
    """
    if sys.platform != "win32":
        return True
    if not pid or pid <= 0:
        return False
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — no kernel32 → can't check
        return True
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )
    if not handle:
        # Windows returns NULL for unknown pids (ERROR_INVALID_PARAMETER)
        # AND for pids the caller lacks rights on. We treat the former as
        # dead and the latter as alive; distinguish via GetLastError.
        try:
            err = kernel32.GetLastError()
        except Exception:  # noqa: BLE001
            err = 0
        # ERROR_INVALID_PARAMETER = 87; ERROR_ACCESS_DENIED = 5.
        if err == 87:
            return False
        # Any other error (5, 6, ...) → treat as alive to be safe.
        return True
    try:
        exit_code = ctypes.wintypes.DWORD(0)
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        if not ok:
            return True  # can't tell → assume alive
        return int(exit_code.value) == _STILL_ACTIVE
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
