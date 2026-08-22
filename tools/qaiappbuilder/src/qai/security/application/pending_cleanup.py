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
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from qai.platform.logging import get_logger
from qai.platform.time import Clock

if TYPE_CHECKING:  # pragma: no cover
    from qai.security.application.permission_wait import PermissionWaitRegistry
    from qai.security.application.ports import (
        PermissionPendingStorePort,
        PermissionRequestRepositoryPort,
    )

#: Optional resolved-notification callback: ``on_resolved(request_id,
#: resolution)`` — invoked (best-effort) after the sweep wakes a stale ASK
#: future so an apps-layer publisher can push a UI-close SSE frame
#: (``PermissionResolvedEvent``). May be sync or async; either is awaited /
#: called defensively. ``None`` keeps the sweep byte-for-byte unchanged.
OnResolvedCallback = Callable[[str, str], "Awaitable[None] | None"]


@runtime_checkable
class ConversationLivenessPort(Protocol):
    """Minimal port answering "which conversation ids are still live?".

    M-Sec-3 (fileguard-audit-2026-07-26): pid-only liveness misses the case
    where the shell subprocess survives its owning conversation (e.g. the
    conversation was closed / cancelled but the child persists). Adding this
    port lets :class:`PendingCleanupService` also resolve pending ASKs whose
    conversation is gone.

    Kept as a LOCAL Protocol (not added to
    ``qai.security.application.ports``) so this module owns its contract
    end-to-end and other slices of the audit fix (which touch
    ``ports.py``) do not collide.

    ``list_live_conversation_ids`` returns the set of conversation ids
    currently owning a real client session. Implementers MUST return an
    empty set (not None) when there are no live conversations. May be sync
    or async — :meth:`PendingCleanupService._sweep_once` awaits an
    awaitable return; a plain set is used directly.
    """

    def list_live_conversation_ids(
        self,
    ) -> "frozenset[str] | set[str] | Awaitable[frozenset[str] | set[str]]":
        ...


__all__ = ["ConversationLivenessPort", "PendingCleanupService"]

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
        "_conversation_liveness",
        "_requests",
    )

    def __init__(
        self,
        *,
        wait_registry: "PermissionWaitRegistry",
        pending_store: "PermissionPendingStorePort | None" = None,
        clock: Clock,
        scan_interval_seconds: float = 10.0,
        on_resolved: "OnResolvedCallback | None" = None,
        conversation_liveness: "ConversationLivenessPort | None" = None,
        request_repository: "PermissionRequestRepositoryPort | None" = None,
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
        # M-Sec-3 (fileguard-audit-2026-07-26): optional
        # ``ConversationLivenessPort``. When wired, the sweep additionally
        # resolves pending rows whose ``conversation_id`` is not in the
        # live set (resolution='conversation_gone'). Independent of the
        # pid-alive branch — pid-alive rows can still be dropped this way.
        # Backward compatible: ``None`` keeps the sweep pid-only.
        self._conversation_liveness = conversation_liveness
        # 2026-08-09 — ``security_permission_request`` mirror. The sweep used
        # to resolve ONLY the in-memory future + the ``pending_permission``
        # row, leaving the REQUEST aggregate at ``state='pending'`` forever:
        # the UI's "pending requests" list reads THAT table, so every ASK
        # abandoned by a short-lived subprocess (e.g. onnxruntime writing
        # ``C:\Windows\INF\cpu.PNF`` during provider init, which never waits
        # for the answer) accumulated as a permanent ghost entry whose dialog
        # had already closed. Wiring the repository here lets the sweep close
        # the aggregate too, carrying the real reason
        # (``subprocess_gone`` / ``conversation_gone``) in
        # ``resolution_reason`` — ``state`` itself is CHECK-constrained to
        # ``pending|approved|rejected|expired|cancelled`` (migration 001), so
        # the reason cannot live there. ``None`` keeps the sweep aggregate-free.
        self._requests = request_repository

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
        """Single scan — resolve stale pending ids.

        Resolution reasons, in order of precedence per row:

        * ``subprocess_gone`` — pid is dead. (Original Phase 2 behaviour.)
        * ``conversation_gone`` — pid is still alive but the row carries a
          ``conversation_id`` and that conversation is no longer live
          (M-Sec-3, only when a :class:`ConversationLivenessPort` is
          wired AND the underlying store row exposes a
          ``conversation_id`` field). Rows without a conv id are ignored
          by this branch — the pid branch still gates them.
        """
        pending_ids = self._registry.list_pending()
        if not pending_ids:
            return
        # Build a request_id → row lookup from the durable store (source of
        # truth for the pid — the in-memory registry does not carry it, and
        # for the optional conversation_id used by the M-Sec-3 branch below).
        # Falls back to an empty map when persistence is off; in that case
        # the sweep can't check liveness and the entries stay pending
        # (in-memory-only deployments accept this; they typically run
        # inside tests where subprocesses die predictably).
        row_lookup: dict[str, dict] = {}
        if self._store is not None:
            try:
                for row in await self._store.list_unresolved():
                    rid = row.get("request_id")
                    if isinstance(rid, str):
                        row_lookup[rid] = row
            except Exception:  # noqa: BLE001 — read fail → skip cleanup
                _log.warning(
                    "security.pending_cleanup.store_read_failed",
                    exc_info=True,
                )
                return
        if not row_lookup:
            return

        # M-Sec-3: query live conversation ids ONCE per sweep so a slow /
        # remote port never fans out into N sync calls. Only fetch when
        # both the port is wired AND at least one row carries a conv id;
        # empty set means "resolve everything with a conv id" (the
        # simplest fail-DENY posture consistent with the audit intent).
        live_conv_ids: frozenset[str] | None = None
        if self._conversation_liveness is not None and any(
            isinstance(r.get("conversation_id"), str) and r.get("conversation_id")
            for r in row_lookup.values()
        ):
            try:
                outcome = self._conversation_liveness.list_live_conversation_ids()
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                live_conv_ids = frozenset(outcome or ())
            except Exception:  # noqa: BLE001 — port fail → skip conv branch
                _log.warning(
                    "security.pending_cleanup.conversation_liveness_failed",
                    exc_info=True,
                )
                live_conv_ids = None

        now = self._clock.now()
        for rid in pending_ids:
            row = row_lookup.get(rid)
            if row is None:
                continue
            pid = row.get("pid")
            if not isinstance(pid, int) or pid <= 0:
                continue
            resolution: str | None = None
            if not _pid_is_alive(pid):
                resolution = "subprocess_gone"
            elif live_conv_ids is not None:
                conv = row.get("conversation_id")
                if isinstance(conv, str) and conv and conv not in live_conv_ids:
                    resolution = "conversation_gone"
            if resolution is None:
                continue
            # 2026-08-09 attribution-order fix — persist the REAL reason
            # BEFORE waking the waiter. ``mark_resolved`` is first-write-wins
            # (``WHERE resolved_at IS NULL``), and ``resolve()`` wakes the
            # native-hook bridge on its own dedicated loop, whose
            # ``_ask_user`` immediately writes ``resolution='deny'`` for the
            # ``allow=False`` it receives. With the old order (resolve first,
            # then mark) that bridge write raced in during the ``await`` on
            # the notify callback and WON, so every sweep-abandoned ASK was
            # audited as a plain ``deny`` — indistinguishable from a genuine
            # user rejection, defeating the whole point of the distinct
            # ``subprocess_gone`` / ``conversation_gone`` reasons (see module
            # docstring). Marking first makes the bridge's later write the
            # no-op instead.
            #
            # The persistence steps run inside ``try`` with the wake in
            # ``finally``, because moving them ahead of ``resolve()`` put two
            # ``await``s between the decision and the wake: a
            # ``CancelledError`` there (``stop()`` cancels this task on
            # shutdown, and it is a ``BaseException`` that the helpers'
            # ``except Exception`` cannot catch) would otherwise skip the wake
            # entirely and leave the ASK future hanging forever — the exact
            # failure this whole service exists to prevent. ``finally``
            # restores the old guarantee: the waiter is freed on every path,
            # including cancellation, while attribution still lands first
            # whenever the awaits complete normally.
            woke = False
            try:
                if self._store is not None:
                    try:
                        await self._store.mark_resolved(
                            request_id=rid,
                            resolved_at=now,
                            resolution=resolution,
                        )
                    except Exception:  # noqa: BLE001
                        _log.warning(
                            "security.pending_cleanup.mark_failed",
                            request_id=rid,
                            exc_info=True,
                        )
                # Close the REQUEST aggregate too — same ordering rationale:
                # do it before the wake so the UI-facing projection carries
                # the real reason. Without this the row stays
                # ``state='pending'`` forever and the UI's pending list (which
                # reads ``security_permission_request``, NOT the pending
                # mirror above) shows a ghost entry whose dialog already
                # closed.
                await self._close_request_aggregate(rid, resolution, now)
            finally:
                woke = self._registry.resolve(rid, allow=False, scope="deny")
            # Problem ② backstop-honesty — tell the UI to close the dialog for
            # this now-resolved ASK (a silent subprocess death has no local
            # user response and no exec-cancel flush, so this sweep is the only
            # thing that can close it). Best-effort: a notify glitch must never
            # break the sweep. Only fire when we actually woke a live waiter.
            if woke and self._on_resolved is not None:
                await self._notify_resolved(rid, resolution)
            _log.info(
                "security.permission." + resolution,
                request_id=rid,
                pid=pid,
                woke=woke,
            )

    async def _close_request_aggregate(
        self, request_id: str, resolution: str, now: datetime
    ) -> None:
        """Mark the ``PermissionRequest`` aggregate resolved (best-effort).

        The sweep's own truth source is the wait registry + pending mirror;
        this is the UI-facing projection (``security_permission_request``,
        read by the "pending requests" list). Left unresolved, an ASK whose
        subprocess exited before the user clicked stays ``pending`` forever
        even though its dialog is long closed.

        Uses ``reject`` rather than ``cancel``: the waiter really did receive
        a DENY (``resolve(allow=False)`` above), and ``reject`` is the only
        transition that carries a ``reason`` — ``cancel``/``expire`` drop it,
        and ``state`` cannot hold ``subprocess_gone`` anyway (CHECK-constrained
        to ``pending|approved|rejected|expired|cancelled``, migration 001:127).
        So the machine-readable reason lands in ``resolution_reason``, keeping
        a sweep-abandoned ASK distinguishable from a genuine user rejection.

        NEVER raises: the waiter is freed by the ``finally`` in the caller, so
        a projection glitch must not break the sweep.

        Concurrency: written through ``resolve_if_pending`` (compare-and-set on
        ``state='pending'``), NOT ``save``. ``PermissionRequest`` is a frozen
        snapshot, so the domain's ``_ensure_pending`` only validates the copy
        THIS sweep read — it cannot see a user who clicked Approve during the
        awaits in between, and ``save`` is an unconditional upsert that would
        then overwrite that approval with ``rejected``. The read-then-check
        below is kept as a cheap early-out; the CAS is what actually makes it
        safe. A ``False`` return means someone else resolved it first, which is
        exactly the outcome this projection wanted anyway.
        """
        repo = self._requests
        if repo is None:
            return
        try:
            from qai.security.domain.value_objects import RequestId

            existing = await repo.get(RequestId(value=request_id))
            if existing is None or not existing.is_pending:
                return
            updated = await repo.resolve_if_pending(
                existing.reject(now=now, reason=resolution)
            )
            if not updated:
                # Raced with a user click / a sibling writer — the aggregate is
                # closed either way, and their decision is the one that counts.
                _log.debug(
                    "security.pending_cleanup.aggregate_already_resolved",
                    request_id=request_id,
                )
        except Exception:  # noqa: BLE001 — projection must never break sweep
            _log.warning(
                "security.pending_cleanup.aggregate_close_failed",
                request_id=request_id,
                resolution=resolution,
                exc_info=True,
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
    # Review fix (2026-07-28): declare restype/argtypes BEFORE the call.
    # ctypes defaults restype to c_int (32-bit signed); OpenProcess returns
    # a 64-bit HANDLE on Win64/ARM64, so without this a handle that does not
    # fit a signed 32-bit int is truncated — GetExitCodeProcess then fails
    # on the mangled value (mapped to "assume alive"), so a genuinely dead
    # subprocess is never swept and its real kernel handle leaks (CloseHandle
    # gets the wrong value). Mirrors qai.platform.process.kill_group.
    wt = ctypes.wintypes
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.GetExitCodeProcess.restype = wt.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
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
