# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Native FileGuard → asyncio ASK bridge (apps/api wiring root).

2026-07-04 native-hook integration — PR-3.

The native ``guard64.dll`` invokes its filter callback on an internal
**native pipe thread** (synchronous, blocking — the DLL waits for the
callback's ``bool`` return before allowing / denying the intercepted
file event). The security decision path (:class:`CheckPermissionUseCase`
+ the ASK ``PermissionWaitRegistry`` round-trip) is ``async`` and lives
on the API process's asyncio event loop. This bridge marshals between
the two worlds:

    native thread (sync)              asyncio loop (API)
    ────────────────────              ──────────────────
    filter_v2(evt)  ── run_coroutine_threadsafe ──▶  _decide(evt)
        │                                               │
        │   fut.result(timeout) ◀───────────────────────┘
        ▼
      True / False  (ALLOW / DENY back to the DLL)

Like ``_file_guard_bridge.py`` this lives under ``apps/api`` — the one
layer allowed to depend on multiple bounded contexts — so
``qai.security`` internals are consumed without any cross-context import
leak.

Event mapping (``Event`` → :class:`AceMask` action):

* ``Event.READ``    → ``read``
* ``Event.WRITE``   → ``write``
* ``Event.DELETE``  → ``write`` (a delete mutates the tree — gated as a
  write; V1 protected-paths treated remove/truncate as write ops).
* ``Event.EXECUTE`` → ``execute``

fail-closed semantics (``native_file_guard_fail_closed``, default True):
any bridge fault — no loop, coroutine exception, ASK request error, or
the dedicated ASK loop being torn down (service shutdown) — resolves to
DENY when fail-closed, ALLOW when fail-open. Phase 2 (2026-07-06): the
bridge NO LONGER auto-denies on any elapsed wall-clock timeout. It waits
INDEFINITELY for the operator's ALLOW/DENY (the inner ASK wait is
``timeout=None``); the ONLY non-decision breakout is real loop teardown,
which :meth:`filter` detects by polling the loop-liveness truth source in
short slices (never a cached flag). ``callback_timeout_ms`` is retained on
the constructor for the DLL-side ABI / diagnostics only.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
from typing import TYPE_CHECKING, Callable

from qai.platform.logging import get_logger
from qai.security.adapters.native_hook import Event, FilterEventV2

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.time import Clock
    from qai.security.application.permission_wait import PermissionWaitRegistry
    from qai.security.application.ports import PermissionPendingStorePort

__all__ = ["NativeFileInterceptorBridge", "build_native_hook_filter"]

_log = get_logger(__name__)

#: The native subject identity attributed to every intercepted OS file
#: event — these come from LLM-spawned subprocesses, not the in-process
#: ai_coding tool layer (which uses ``ai_coding.tool``).
_SUBJECT_IDENTIFIER = "native.file_guard"

#: 60s ASK ceiling (mirrors V1 ``PolicyCenter.ask_user`` + the in-process
#: FileGuard bridge). Overridable per-construction.
_ASK_TIMEOUT_SEC = 60.0


class NativeFileInterceptorBridge:
    """Marshals native-thread filter callbacks onto the asyncio loop.

    Parameters
    ----------
    check_permission_use_case:
        The security ``CheckPermissionUseCase`` (ALLOW / DENY / would-ask).
    fail_closed:
        DENY on any fault / timeout when True (default); ALLOW when False.
    callback_timeout_ms:
        The DLL-side callback ceiling. The bridge waits slightly less than
        this for the coroutine so the Python side resolves before the DLL
        times out. 0 falls back to :data:`_ASK_TIMEOUT_SEC`.
    request_permission_use_case / wait_registry:
        Optional ASK collaborators. When both are wired, a would-ask miss
        pops the authorization dialog (SSE ``permission_request``) and
        blocks for the operator's decision; otherwise a would-ask miss
        stays a fail-closed DENY.
    ask_timeout_sec:
        Legacy Phase-1 ceiling for the ASK dialog wait (default 60s). Phase 2
        (2026-07-06) — the value is retained on the constructor for back-
        compat and diagnostics ONLY; it is no longer forwarded to the
        registry's ``wait`` call. The Phase 2 semantic is INFINITE wait
        (no auto-DENY on elapse) so users may be away for days; see
        :meth:`_ask_user` and plan §2.3 / §2 N9.
    boot_id_provider:
        Optional 0-arg callable returning THIS backend process's boot id
        (minted in ``lifespan.py`` as ``container.boot_id``). Threaded into
        the ``CheckPermissionUseCase.execute`` call as ``scope_boot_id`` so
        ``process``-scoped grants match this process's native sub-process
        events. Native events carry NO conversation, so
        ``scope_conversation_id`` is always ``""`` — only ``permanent`` and
        ``process`` grants can match at the native layer (SEC true-scoping:
        native = process granularity). ``None`` → "" → only permanent grants
        match (fail-safe).
    """

    def __init__(
        self,
        *,
        check_permission_use_case: object,
        fail_closed: bool = True,
        callback_timeout_ms: int = 60000,
        request_permission_use_case: object | None = None,
        wait_registry: "PermissionWaitRegistry | None" = None,
        ask_timeout_sec: float = _ASK_TIMEOUT_SEC,
        boot_id_provider: "Callable[[], str] | None" = None,
        pending_store: "PermissionPendingStorePort | None" = None,
        clock: "Clock | None" = None,
    ) -> None:
        self._check = check_permission_use_case
        self._fail_closed = bool(fail_closed)
        self._request_permission = request_permission_use_case
        self._wait_registry = wait_registry
        self._ask_timeout_sec = float(ask_timeout_sec)
        self._boot_id_provider = boot_id_provider
        # Phase 2 (2026-07-06) — durable pending-ASK mirror + clock. When
        # both are wired, ``_ask_user`` writes a row on ASK-open and marks
        # it resolved when the future wakes. Either being None disables
        # the durable side; the in-memory registry stays authoritative.
        self._pending_store = pending_store
        self._clock = clock
        # Reserve a little headroom so the Python future resolves before the
        # DLL's own callback_timeout_ms fires (avoids a double-timeout race).
        if callback_timeout_ms and callback_timeout_ms > 0:
            self._result_timeout_sec = max(1.0, callback_timeout_ms / 1000.0)
        else:
            self._result_timeout_sec = self._ask_timeout_sec
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: "threading.Thread | None" = None
        self._owns_loop = False

    # -- loop binding --------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the asyncio loop the filter marshals coroutines onto.

        Used by tests / callers that already run a suitable loop. Prefer
        :meth:`start_dedicated_loop` in production so the native filter never
        marshals onto the API's MAIN event loop — a native child file event
        can arrive while the main loop is busy, and blocking the pipe thread
        on a coroutine the main loop can't service in time causes the ASK to
        time out (or, if the main loop thread itself triggered the hooked I/O,
        deadlock). A dedicated loop thread has no such coupling.
        """
        self._loop = loop

    def start_dedicated_loop(self) -> None:
        """Spin a private daemon thread running its own asyncio loop.

        The native filter marshals :meth:`_decide` onto THIS loop (not the
        API main loop), so ASK round-trips run independently of API request
        handling — no main-loop contention / deadlock. Idempotent.
        """
        if self._loop is not None and not self._loop.is_closed():
            return
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._owns_loop = True
            ready.set()
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run, name="native-guard-ask-loop", daemon=True
        )
        self._loop_thread.start()
        ready.wait(timeout=5.0)

    def close(self) -> None:
        """Stop the dedicated loop thread (if we own one). Idempotent."""
        loop = self._loop
        if self._owns_loop and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        self._loop = None
        self._owns_loop = False

    # -- sync entry point (native thread) ------------------------------
    def filter(self, evt: FilterEventV2) -> bool:
        """Synchronous filter — invoked on the native DLL pipe thread.

        Marshals :meth:`_decide` onto the bound loop and blocks INDEFINITELY
        for the ALLOW/DENY answer (Phase 2 — no wall-clock auto-DENY). The
        only non-decision breakout is loop teardown (service shutdown),
        detected by polling the loop-liveness truth source in short slices;
        that (and any decide fault) applies the ``fail_closed`` policy.
        Returns ``True`` = ALLOW, ``False`` = DENY.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            _log.warning("native_hook_bridge.no_loop", path=evt.file_path)
            return not self._fail_closed
        try:
            fut = asyncio.run_coroutine_threadsafe(self._decide(evt), loop)
        except RuntimeError:
            # loop not running / shutting down
            _log.warning("native_hook_bridge.loop_not_running")
            return not self._fail_closed
        # P-10: wait INDEFINITELY for the user (Phase 2 inner wait is
        # timeout=None), but bail to fail-closed DENY on loop teardown so
        # shutdown can never hang the native pipe thread. Poll in short
        # slices and re-check the loop-liveness truth source each slice.
        poll_slice = 0.5
        while True:
            try:
                return bool(fut.result(timeout=poll_slice))
            except concurrent.futures.TimeoutError:
                cur = self._loop
                thread = self._loop_thread
                if (
                    cur is None
                    or cur.is_closed()
                    or (thread is not None and not thread.is_alive())
                ):
                    fut.cancel()
                    _log.warning(
                        "native_hook_bridge.decide_shutdown",
                        path=evt.file_path,
                    )
                    return not self._fail_closed
                # loop still alive -> user simply hasn't answered; keep waiting.
                continue
            except Exception:  # noqa: BLE001 — any decide fault => fail policy
                _log.warning(
                    "native_hook_bridge.decide_error",
                    path=evt.file_path,
                    exc_info=True,
                )
                return not self._fail_closed

    # -- async decision (asyncio loop) ---------------------------------
    async def _decide(self, evt: FilterEventV2) -> bool:
        from qai.security.domain.value_objects import (
            AceMask,
            PolicyAction,
            Resource,
            Subject,
        )

        mask = self._event_to_mask(evt.event)
        if mask is None or mask.is_empty():
            # Unknown / NONE event — nothing to gate; allow (do not block
            # unrelated events). This is not a security-relevant op.
            return True
        resource_kind = "exec" if evt.event == int(Event.EXECUTE) else "path"
        subject = Subject(kind="system", identifier=_SUBJECT_IDENTIFIER)
        resource = Resource(kind=resource_kind, identifier=evt.file_path)

        # SEC-ENHANCE-AUDITUX-1: enrich the audit row with the native event
        # metadata (op name from Event kind + subprocess process_path /
        # command_line / pid / parent_pid). These kwargs are audit-only —
        # they NEVER influence the decision — so a missing / empty value is
        # safe and never blocks. The V1-shaped filter callback (READ/WRITE/
        # DELETE/EXECUTE only) doesn't carry a rich command_line for every
        # event; the pid->cmdline fallback below fills that gap when
        # possible on Windows without changing the decision path.
        op_name = self._event_to_op(evt.event)
        process_path = evt.process_path or ""
        command_line = evt.command_line or ""
        if not command_line and evt.pid:
            command_line = self._reverse_lookup_cmdline(evt.pid)

        # P-01 diagnostics — the native CreateProcessW hook is the ONLY point
        # every child-process EXECUTE event passes through (the "mystery"
        # startup ``cmd.exe /c ver`` is spawned by a child of ours — an MCP
        # server / OpenCode CLI / worker probing the Windows version — never by
        # our own Python code, so it is invisible to the subprocess.Popen audit
        # hook). Logging the EXECUTE event with its ``parent_pid`` here makes
        # that otherwise-invisible spawner observable: reverse-look the
        # parent_pid to see which of our children issued it. Audit-only, never
        # influences the decision; guarded to EXECUTE so it adds no per-file-op
        # log noise.
        if evt.event == int(Event.EXECUTE):
            _log.info(
                "native_spawn_probe",
                op=op_name,
                command_line=command_line,
                process_path=process_path,
                pid=evt.pid,
                parent_pid=(evt.parent_pid if evt.parent_pid else None),
            )

        # SEC true-scoping — native sub-process file events have NO
        # conversation context (the DLL only sees OS file events from
        # LLM-spawned children, never a conversation id), so
        # ``scope_conversation_id`` is ALWAYS "". Only ``permanent`` and
        # ``process`` grants can therefore match at the native layer — this is
        # the intended "native = process granularity" layering (a session
        # grant's ``scope_key`` = a conversation id, which "" never matches).
        # ``scope_boot_id`` is THIS process's boot id so ``process``-scoped
        # grants (whose ``scope_key`` = the boot id) match this process's
        # native events. Missing provider → "" → only permanent grants match.
        boot_id = self._boot_id_provider() if self._boot_id_provider else ""

        try:
            result = await self._check.execute(  # type: ignore[attr-defined]
                subject=subject,
                resource=resource,
                requested_mask=mask,
                op=op_name,
                process_path=process_path,
                command_line=command_line,
                actor_pid=evt.pid if evt.pid else None,
                actor_parent_pid=(
                    evt.parent_pid if evt.parent_pid else None
                ),
                scope_conversation_id="",
                scope_boot_id=boot_id,
            )
        except Exception:  # noqa: BLE001 — evaluation error => fail policy
            _log.warning(
                "native_hook_bridge.evaluate_error",
                path=evt.file_path,
                exc_info=True,
            )
            return not self._fail_closed

        if result.decision is PolicyAction.ALLOW:
            return True

        # would-ask miss → pop the dialog + block for the operator's answer
        if (
            getattr(result, "would_ask", False)
            and self._request_permission is not None
            and self._wait_registry is not None
        ):
            allowed = await self._ask_user(
                subject=subject,
                resource=resource,
                requested_mask=mask,
                evt=evt,
                process_path=process_path,
                command_line=command_line,
            )
            return allowed
        # explicit DENY / ASK not wired
        return False

    async def _ask_user(
        self,
        *,
        subject: object,
        resource: object,
        requested_mask: object,
        evt: "FilterEventV2 | None" = None,
        process_path: str = "",
        command_line: str = "",
    ) -> bool:
        """Create a PENDING request + block for the operator's decision.

        Mirrors ``apps.api._file_guard_bridge.FileGuardFacade._ask_user``:
        the request use case publishes the SSE ``permission_request`` event
        and may auto-resolve via smart-approval; a still-PENDING request is
        awaited on the shared :class:`PermissionWaitRegistry` (woken by the
        approve / reject / cancel routes).

        Phase 2 (2026-07-06) additions:

        * **Dedup** — before creating a fresh PermissionRequest we consult
          :meth:`PermissionWaitRegistry.register_or_dedupe` on the
          ``(pid, path, event)`` triple. A concurrent duplicate ASK for
          the same subprocess+resource+op folds onto the ORIGINAL future
          (no second SSE broadcast, no second durable row) and simply
          awaits the shared future. All callbacks wake with the same
          ALLOW/DENY.
        * **Infinite wait** — ``timeout=None`` (see class docstring).
        * **Durable mirror** — the native event context is persisted on
          the FIRST (non-dedupe) ASK and marked resolved when the
          future wakes.

        Returns True on ALLOW, False on DENY / reject / cancel.
        """
        registry = self._wait_registry
        pid = int(getattr(evt, "pid", 0) or 0) if evt is not None else 0
        event_bits = (
            int(getattr(evt, "event", 0) or 0) if evt is not None else 0
        )
        path_str = (
            str(getattr(evt, "file_path", "") or "")
            if evt is not None
            else ""
        )

        # Phase 2 dedupe — probe first. When ``pid`` is 0 (missing) the
        # dedupe probe returns is_dedupe=False without touching the index
        # (safer than folding onto an unrelated triple). We can only dedupe
        # AFTER the first ASK has registered its request_id; probing here
        # requires knowing the id, so we do it in two steps: (a) if there's
        # already a live dedupe entry we grab its future directly; (b)
        # otherwise we mint a fresh PermissionRequest and register it.
        if (
            registry is not None
            and pid
            and path_str
            and event_bits
        ):
            existing_rid = None
            existing_key = (pid, path_str, event_bits)
            # Peek at the reverse index without mutation. We intentionally
            # touch the private attribute rather than exposing a public
            # ``lookup_dedupe`` — a public API would tempt callers to
            # bypass ``register_or_dedupe`` and introduce a TOCTOU race.
            reverse_map = getattr(registry, "_dedupe_index", None)
            if isinstance(reverse_map, dict):
                existing_rid = reverse_map.get(existing_key)
            if existing_rid is not None and registry.has_pending(existing_rid):
                # DEDUPE HIT — share the pending future; no new
                # PermissionRequest, no new durable row, no second SSE.
                try:
                    resolution = await registry.wait(
                        existing_rid, timeout=None
                    )
                except asyncio.CancelledError:
                    return not self._fail_closed
                return bool(resolution.allow)

        # No dedupe hit — mint a fresh PermissionRequest via the domain
        # use case (which publishes SSE / consults smart-approval / etc.).
        try:
            request = await self._request_permission.execute(  # type: ignore[attr-defined]
                subject=subject,
                resource=resource,
                requested_mask=requested_mask,
            )
        except Exception:  # noqa: BLE001 — request error => deny
            _log.warning("native_hook_bridge.ask_request_error", exc_info=True)
            return False

        request_id = request.request_id.value
        state = getattr(request, "state", None)
        state_value = getattr(state, "value", state)
        if state_value == "approved":
            return True
        if state_value in ("rejected", "cancelled", "expired"):
            return False

        # Register with the dedupe key so a concurrent duplicate ASK
        # for the same triple can fold onto THIS future.
        if registry is not None and pid and path_str and event_bits:
            registry.register_or_dedupe(
                pid=pid,
                path=path_str,
                event=event_bits,
                request_id=request_id,
            )

        # Phase 2 — persist a durable pending row (best-effort). The
        # in-memory registry is authoritative; a persistence failure
        # never blocks the ASK.
        store = self._pending_store
        clock = self._clock
        boot_id = (
            self._boot_id_provider() if self._boot_id_provider else ""
        )
        actor_parent_pid = (
            int(getattr(evt, "parent_pid", 0) or 0) if evt is not None else 0
        )
        if store is not None and clock is not None and path_str:
            try:
                await store.insert_pending(
                    request_id=request_id,
                    pid=pid,
                    process_path=process_path,
                    command_line=command_line,
                    path=path_str,
                    event=event_bits,
                    boot_id=boot_id,
                    created_at=clock.now(),
                    actor_parent_pid=(
                        actor_parent_pid if actor_parent_pid > 0 else None
                    ),
                )
            except Exception:  # noqa: BLE001 — best-effort persist
                _log.warning(
                    "native_hook_bridge.pending_persist_failed",
                    request_id=request_id,
                    exc_info=True,
                )

        # Phase 2 (2026-07-06) no-timeout wait — users may take days to
        # click and there is NO auto-DENY on elapse (see plan §2.3 / §2 N9).
        # The DLL Phase 2 pipe wait is INFINITE too, so both sides agree on
        # "wait forever until the user acts (or the loop / DLL tears down)".
        # ``_ask_timeout_sec`` is kept as a diagnostics-only field for
        # back-compat; it is intentionally NOT forwarded to the registry.
        resolution = await self._wait_registry.wait(  # type: ignore[union-attr]
            request_id, timeout=None
        )

        # Phase 2 — mark the durable row resolved. The registry has already
        # woken us; the store side is a best-effort audit trail.
        if store is not None and clock is not None:
            try:
                await store.mark_resolved(
                    request_id=request_id,
                    resolved_at=clock.now(),
                    resolution="allow" if resolution.allow else "deny",
                )
            except Exception:  # noqa: BLE001
                _log.warning(
                    "native_hook_bridge.pending_resolve_persist_failed",
                    request_id=request_id,
                    exc_info=True,
                )
        return bool(resolution.allow)

    # -- event mapping -------------------------------------------------
    @staticmethod
    def _event_to_mask(event: int):
        """Map an ``Event`` value to an :class:`AceMask` (or None)."""
        from qai.security.domain.value_objects import AceMask

        if event == int(Event.READ):
            return AceMask(read=True, write=False, execute=False)
        if event == int(Event.WRITE):
            return AceMask(read=False, write=True, execute=False)
        if event == int(Event.DELETE):
            # A delete mutates the tree — gate as a write (V1 parity).
            # SEC-ENHANCE-AUDITUX-1: the AceMask stays write-based (the
            # security decision is identical), but the audit ``op`` string
            # emitted by :meth:`_event_to_op` distinguishes delete from an
            # in-place write. The mask itself does NOT set ``delete=True``:
            # doing so would ask the policy engine for a delete-specific
            # grant that V1 never had, changing decisions (regression). The
            # audit ``op`` string is the granularity the audit query needs.
            return AceMask(read=False, write=True, execute=False)
        if event == int(Event.EXECUTE):
            return AceMask(read=False, write=False, execute=True)
        return None

    @staticmethod
    def _event_to_op(event: int) -> str:
        """Map an ``Event`` value to an audit ``op`` string.

        SEC-ENHANCE-AUDITUX-1 — the audit row's ``op`` field distinguishes
        native events at ``op`` granularity even when the underlying
        :class:`AceMask` shares a decision (e.g. delete + write both use
        write-grant). Returns ``""`` for unknown events (the audit column
        is nullable / defaults to empty).
        """
        if event == int(Event.READ):
            return "read"
        if event == int(Event.WRITE):
            return "write"
        if event == int(Event.DELETE):
            return "delete"
        if event == int(Event.EXECUTE):
            return "exec"
        return ""

    @staticmethod
    def _reverse_lookup_cmdline(pid: int) -> str:
        """Best-effort pid → command-line reverse lookup (Windows only).

        SEC-ENHANCE-AUDITUX-1 — the V1-shaped filter callback (READ /
        WRITE / DELETE / EXECUTE) does NOT carry ``command_line`` for
        every event: a child process's WRITE event may arrive with an
        empty ``command_line`` even though the process itself was
        launched with a real one. When the ``FilterEventV2`` supplies an
        empty ``command_line`` but a live ``pid``, we do a defensive
        ``psutil.Process(pid).cmdline()`` to fill the audit row.

        Design constraints (never break the decision path):

        * **Windows only** — the DLL is Windows-only; on other platforms
          this returns ``""`` immediately. Guards on ``sys.platform`` so
          the code stays domain-purity safe (this is apps/api layer, so
          platform branching is allowed).
        * **psutil is already a runtime dep** (``pyproject.toml`` L104
          ``psutil>=6.0,<8.0``) — no new dependency introduced. Any
          import / lookup failure is swallowed and returns ``""``.
        * **Never raises** — a lookup failure (dead pid, access denied,
          any OS error) returns ``""``. The audit stays valid; only the
          reverse-lookup enrichment is best-effort.
        * **No side effects** — pure read. Safe to call from the
          bridge's asyncio loop even though it is a sync call (psutil's
          ``cmdline()`` is a fast NtQueryInformationProcess round-trip).
        """
        if sys.platform != "win32":
            return ""
        if not pid or pid <= 0:
            return ""
        try:
            import psutil  # local import so the bridge never fails to load
        except Exception:  # noqa: BLE001 — psutil unavailable → skip
            return ""
        try:
            proc = psutil.Process(pid)
            cmd = proc.cmdline()
            if not cmd:
                return ""
            # Join with a single space so the audit row carries a scannable
            # single-string command line (matches the V2 event shape).
            return " ".join(str(part) for part in cmd)
        except Exception:  # noqa: BLE001 — dead pid / access denied → skip
            return ""


def build_native_hook_filter(
    container: object,
) -> tuple["NativeFileInterceptorBridge | None", Callable[[FilterEventV2], bool] | None]:
    """Compose the native-hook ASK bridge from the container.

    Returns ``(bridge, filter_callable)``. Returns ``(None, None)`` when
    the security namespace / ``check_permission_use_case`` is unavailable
    (hand-rolled test containers) so lifespan simply skips wiring the
    filter and the guard falls back to its built-in fail-closed default.
    """
    security = getattr(container, "security", None)
    check = (
        getattr(security, "check_permission_use_case", None)
        if security is not None
        else None
    )
    if check is None:
        return (None, None)

    settings = getattr(container, "settings", None)
    security_settings = getattr(settings, "security", None) if settings else None
    fail_closed = bool(
        getattr(security_settings, "native_file_guard_fail_closed", True)
    )
    callback_timeout_ms = int(
        getattr(security_settings, "native_file_guard_callback_timeout_ms", 60000)
    )

    request_permission = getattr(security, "request_permission_use_case", None)
    wait_registry = getattr(security, "permission_wait_registry", None)
    # Phase 2 (2026-07-06) — durable pending mirror. When persistence is off
    # DI wires a null store (no-op); the bridge still writes to it so the
    # code path stays identical either way. ``container.clock`` is the same
    # domain clock the rest of security uses (frozen in tests).
    pending_store = getattr(security, "permission_pending_store", None)
    clock = getattr(container, "clock", None)

    bridge = NativeFileInterceptorBridge(
        check_permission_use_case=check,
        fail_closed=fail_closed,
        callback_timeout_ms=callback_timeout_ms,
        request_permission_use_case=request_permission,
        wait_registry=wait_registry,
        # SEC true-scoping — this process's boot id (minted in lifespan as
        # ``container.boot_id``) so ``process``-scoped grants match this
        # process's native sub-process events. Read live per event so a
        # container rebuilt on restart yields the new boot id.
        boot_id_provider=lambda: getattr(container, "boot_id", ""),
        pending_store=pending_store,
        clock=clock,
    )
    # Run the ASK decision on a DEDICATED loop thread, NOT the API main loop:
    # a native child file event can arrive on the DLL pipe thread at any time,
    # and marshalling onto the main loop risks timeout/deadlock when it is busy
    # (or is itself the thread that triggered the hooked I/O). The dedicated
    # loop is fully decoupled.
    bridge.start_dedicated_loop()
    return (bridge, bridge.filter)
