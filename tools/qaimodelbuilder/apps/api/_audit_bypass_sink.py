# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Zero-IO audit bypass for the in-process protected-paths hook (P-17 §6.3).

The main-process protected-paths guard (:mod:`qai.platform.main_process_audit_sentinel`)
is a PEP-578 ``sys.addaudithook``: when an in-process write into a protected prefix
is detected it raises ``PermissionError`` from the audited call site to abort
the write. Historically it recorded NOTHING — an observability gap (P-17).

That gap cannot be closed by writing an audit row from the hook directly: the
hook runs synchronously on the audited call's thread, and ANY direct IO (a DB
write, a log-file append) re-triggers PEP-578 audit events → unbounded
recursion. And the canonical funnel — :class:`SecurityAuditFacade.record` — is
``async`` and lives on the API event loop, unreachable from the arbitrary
thread the hook fires on.

:class:`AuditBypassSink` bridges the two worlds WITHOUT IO on the hook thread:

    hook thread (sync, PEP-578)          dedicated loop thread (asyncio)
    ───────────────────────────          ───────────────────────────────
    enqueue(event, path)  ── SimpleQueue.put ──▶  _drain() coroutine
        │  (pure in-memory, zero-IO)                 │  await facade.record(DENY)
        ▼                                            ▼
      returns immediately                       security_audit_entry row

``enqueue`` is a pure ``queue.SimpleQueue.put`` (in-memory, lock-free, never
touches the filesystem / DB) guarded by a ``threading.local`` re-entrancy flag,
so even if enqueue's own machinery somehow emitted an audit event it would not
recurse. A DEDICATED daemon loop thread (mirroring
``apps.api._native_hook_bridge.NativeFileInterceptorBridge.start_dedicated_loop``)
owns its own asyncio loop; a background drain task pops queued items and
``await``\\s :class:`SecurityAuditFacade.record` on THAT loop, so the DB write
runs fully decoupled from both the hook thread and the API main loop.

Layering: this lives under ``apps/api`` — the one layer allowed to depend on
multiple bounded contexts — so it may reference ``qai.security`` (the Facade)
while ``qai.platform.main_process_audit_sentinel`` stays pure (its ``on_violation`` callback is
stdlib-typed and this module supplies the security coupling).
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.security.application.security_audit_facade import (
        SecurityAuditFacade,
    )

__all__ = ["AuditBypassSink"]

_log = get_logger(__name__)

#: The system subject attributed to every in-process protected-path deny.
_SUBJECT_KIND = "system"
_SUBJECT_IDENTIFIER = "protected_paths.main_process_audit_sentinel"

#: Audit ``source`` tag distinguishing these rows from the native subprocess
#: guard (``native.file_guard``) and the tool-layer checks.
_SOURCE = "protected_paths_main_hook"

#: P-08 #6 — the child-process (isolated-interpreter) sentinel's subject/source,
#: distinguishing its cross-process denies (relayed via the exec stderr marker
#: protocol) from the in-process main-hook rows above.
_CHILD_SUBJECT_IDENTIFIER = "protected_paths.child_process_audit_sentinel"
_CHILD_SOURCE = "protected_paths_child_hook"

#: Human-readable reason recorded on every row.
_REASON = "protected path in-process write blocked"

#: Bound on how long ``close()`` waits for the final drain to flush the queue
#: before it stops the loop regardless (never hang shutdown — 铁律5).
_FINAL_DRAIN_TIMEOUT_SEC = 5.0


def _op_from_event(event: str) -> str:
    """Map a PEP-578 audit ``event`` name (or a pre-normalised op) to an op.

    Reuses the SAME op vocabulary as
    ``NativeFileInterceptorBridge._event_to_op`` (``read`` / ``write`` /
    ``exec`` / ``delete``) — no new op strings are minted. Every protected
    event is a write-intent op; removals/unlinks are recorded as ``delete``,
    everything else (open/os.open/rename/replace/mkdir/makedirs/rmdir/
    truncate/copyfile) normalises to ``write``.

    P-08 #6: the child-process relay already hands us a normalised op
    (``write`` / ``delete``) rather than a PEP-578 event name, so those pass
    through unchanged instead of collapsing to ``write``.
    """
    if event in ("os.remove", "os.unlink", "delete"):
        return "delete"
    return "write"


class AuditBypassSink:
    """Sync enqueue → dedicated-loop async ``facade.record`` audit bypass.

    Parameters
    ----------
    facade:
        The :class:`SecurityAuditFacade` (its ``record`` is awaited on the
        dedicated loop). When ``None`` the sink is INERT: :meth:`enqueue`
        drops items and :meth:`start` / :meth:`close` are no-ops — so a
        hand-rolled test container without a wired facade never crashes.
    """

    def __init__(
        self,
        facade: SecurityAuditFacade | None,
    ) -> None:
        self._facade = facade
        # Queue item: (event, path, subject_identifier, source). The two tag
        # fields let one sink serve BOTH the in-process main hook (default tags)
        # and the child-process hook (child tags) without a second loop/thread.
        self._queue: queue.SimpleQueue[tuple[str, str, str, str]] = (
            queue.SimpleQueue()
        )
        self._local = threading.local()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._drain_task: asyncio.Task | None = None
        self._stopping = threading.Event()

    # -- sync enqueue (runs on the arbitrary audit-hook thread) --------
    def enqueue(self, event: str, path: str) -> None:
        """Zero-IO, non-blocking enqueue of a detected protected-write deny.

        This is the ``on_violation`` callback handed to
        ``install_protected_paths_audit_hook``. It runs on whatever thread
        triggered the audited write, so it MUST NOT perform IO (would recurse
        through PEP-578). It only ``put``\\s a tuple onto an in-memory
        ``SimpleQueue`` and returns. A ``threading.local`` re-entrancy guard
        makes a nested call (should enqueue itself ever emit an audit event) a
        no-op instead of recursing. Never raises — the deny path must not break.

        Rows are tagged with the MAIN-process subject/source. The child-process
        variant is :meth:`enqueue_child` (same queue/loop, different tags).
        """
        self._enqueue(event, path, _SUBJECT_IDENTIFIER, _SOURCE)

    def enqueue_child(self, event: str, path: str) -> None:
        """Enqueue a CHILD-process protected-deny relayed via the exec marker.

        P-08 #6 callback wired (by the apps layer) into
        ``qai.platform.child_process_deny_audit.set_on_child_protected_deny``.
        The parent ``exec`` handler parses the child sentinel's stderr marker
        and calls this once per deny. Identical zero-IO/non-blocking/never-raise
        contract as :meth:`enqueue`; only the recorded subject/source differ
        (``protected_paths.child_process_audit_sentinel`` /
        ``protected_paths_child_hook``) so child denies are distinguishable from
        the in-process main-hook rows. ``event`` here is the child ``op``
        (``write`` / ``delete``); it is normalised the same way downstream.
        """
        self._enqueue(event, path, _CHILD_SUBJECT_IDENTIFIER, _CHILD_SOURCE)

    def _enqueue(
        self, event: str, path: str, subject_identifier: str, source: str
    ) -> None:
        if self._facade is None:
            return
        if getattr(self._local, "in_enqueue", False):
            return
        self._local.in_enqueue = True
        try:
            # SimpleQueue.put is lock-free, unbounded, and never blocks / does
            # IO — the only work done on the hook thread.
            self._queue.put((event, path, subject_identifier, source))
        except Exception:  # noqa: BLE001,S110 — enqueue must never break deny
            pass
        finally:
            self._local.in_enqueue = False

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        """Spin the dedicated daemon loop thread + start the drain task.

        Idempotent. No-op when inert (facade is None). Mirrors
        ``NativeFileInterceptorBridge.start_dedicated_loop``.
        """
        if self._facade is None:
            return
        if self._loop is not None and not self._loop.is_closed():
            return
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._drain_task = loop.create_task(self._drain())
            ready.set()
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run, name="protected-paths-audit-sink", daemon=True
        )
        self._loop_thread.start()
        ready.wait(timeout=5.0)

    async def _drain(self) -> None:
        """Background: pop queued denies and await ``facade.record`` per item.

        Runs on the dedicated loop. Blocks in a short-slice poll (the queue is
        drained via ``get_nowait`` because ``SimpleQueue`` has no async API);
        exits when :meth:`close` sets the stop flag AND the queue is empty.
        """
        while True:
            drained_any = False
            while True:
                try:
                    event, path, subject_id, source = self._queue.get_nowait()
                except queue.Empty:
                    break
                drained_any = True
                await self._record_one(event, path, subject_id, source)
            if self._stopping.is_set() and self._queue.empty():
                return
            if not drained_any:
                # Nothing to do — yield the loop briefly so we don't busy-spin.
                await asyncio.sleep(0.05)

    async def _record_one(
        self, event: str, path: str, subject_id: str, source: str
    ) -> None:
        """Await ``facade.record`` for one deny (best-effort, never raises)."""
        facade = self._facade
        if facade is None:
            return
        try:
            from qai.security.domain.value_objects import (  # noqa: PLC0415
                PolicyAction,
                Resource,
                Subject,
            )

            await facade.record(
                subject=Subject(
                    kind=_SUBJECT_KIND, identifier=subject_id
                ),
                resource=Resource(kind="path", identifier=path),
                decision=PolicyAction.DENY,
                op=_op_from_event(event),
                reason=_REASON,
                source=source,
            )
        except Exception:  # noqa: BLE001 — audit is best-effort
            _log.warning(
                "audit_bypass_sink.record_failed", path=path, exc_info=True
            )

    def close(self) -> None:
        """Drain remaining queued denies (bounded), then stop the loop.

        Idempotent. Called on shutdown BEFORE ``Database.close()`` so the final
        ``facade.record`` writes still have a live DB. Mirrors
        ``NativeFileInterceptorBridge.close`` but adds a bounded FINAL DRAIN:
        we schedule a coroutine on the dedicated loop that empties whatever is
        still queued, wait up to :data:`_FINAL_DRAIN_TIMEOUT_SEC` for it, then
        stop the loop REGARDLESS (never hang shutdown — 铁律5). A DB already
        being torn down just makes each ``record`` a swallowed best-effort miss.
        """
        loop = self._loop
        self._stopping.set()
        if loop is not None and not loop.is_closed():
            # (1) bounded final drain on the dedicated loop.
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._final_drain(), loop
                )
                try:
                    fut.result(timeout=_FINAL_DRAIN_TIMEOUT_SEC)
                except Exception:  # noqa: BLE001 — timeout / drain fault
                    _log.warning(
                        "audit_bypass_sink.final_drain_incomplete",
                        exc_info=True,
                    )
            except Exception:  # noqa: BLE001,S110 — loop not running / stopping
                pass
            # (2) stop the loop (idempotent — a closed loop is skipped above).
            try:  # noqa: SIM105 — explicit swallow, no contextlib dependency
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001,S110
                pass
        self._loop = None
        self._drain_task = None

    async def _final_drain(self) -> None:
        """Empty the queue once (bounded by the outer ``result`` timeout)."""
        while True:
            try:
                event, path, subject_id, source = self._queue.get_nowait()
            except queue.Empty:
                return
            await self._record_one(event, path, subject_id, source)

    # -- state truth (真实探测, not optimistic flags) ------------------
    def is_running(self) -> bool:
        """True iff the dedicated loop thread is alive and its loop is open.

        State-Truth-First (AGENTS §5): probes the real thread / loop rather
        than a cached "started" flag.
        """
        loop = self._loop
        thread = self._loop_thread
        return bool(
            loop is not None
            and not loop.is_closed()
            and thread is not None
            and thread.is_alive()
        )
