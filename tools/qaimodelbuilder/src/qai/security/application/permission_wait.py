# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-process async ASK suspend/resume registry (P0 — V1 ASK popup restore).

V1 anchor
---------
``backend/security/policy.py`` kept a thread-keyed ``self._pending`` map of
``_PendingRequest`` objects, each carrying a ``threading.Event``. When a
synchronous tool hit ``Decision.ASK`` it called :meth:`PolicyCenter.ask_user`
(``policy.py:1336-1530``) which pushed an SSE ``permission_request`` event and
then blocked on ``pending.event.wait(timeout=60)``; the front-end ``POST
/api/security/permission`` handler called :meth:`resolve_permission`
(``policy.py:1658-1739``) which popped the pending entry, wrote the chosen grant
(``once`` / ``session`` / ``process`` / ``permanent``) and called
``pending.event.set()`` to wake the blocked thread, returning ALLOW/DENY. A
timeout resolved to DENY.

V2 design
---------
V2 runs the FileGuard gate on the asyncio event loop (the普通聊天 tool path is
``async def enforce_read/write/exec``), so the registry is implemented with
:class:`asyncio.Future` instead of ``threading.Event`` — single process, no
threads (per task brief). The registry is a thin, dependency-free application
service (no I/O, no FastAPI, no cross-context imports) so it can be shared by
the apps-layer FileGuard bridge and woken by the approve / reject use cases.

Lifecycle for one ASK:

1. FileGuard creates a :class:`PermissionRequest` (via
   ``RequestPermissionUseCase``) → gets a ``request_id``.
2. FileGuard calls :meth:`register` (or :meth:`register_or_dedupe` — Phase 2)
   then :meth:`wait` (awaits with an optional timeout).
3. The operator clicks approve/reject in the UI → the corresponding use case
   calls :meth:`resolve` with the chosen grant scope / decision.
4. :meth:`wait` returns the :class:`PermissionResolution`; FileGuard writes the
   grant (already done by the approve use case) and allows / denies.
5. On timeout (legacy call sites only), :meth:`wait` returns a DENY resolution
   with ``timed_out=True`` (V1 60s → DENY parity).

Phase 2 (2026-07-06) additions
------------------------------
* **Dedupe** — a reverse-index keyed by ``(pid, path, event)`` folds concurrent
  duplicate ASKs from the same subprocess+resource+op onto a SINGLE pending
  future. :meth:`register_or_dedupe` returns ``(future, is_dedupe)`` so the
  bridge can persist / broadcast only once per unique triple while still
  awaiting the shared future for every native filter callback. When the
  shared future resolves, ALL callbacks wake with the same ALLOW/DENY.
* **No-timeout wait** — :meth:`wait` now accepts ``timeout=None`` (default),
  which awaits the future INFINITELY (until an approve/reject/cancel, or
  the loop is torn down at shutdown). The legacy ``timeout=<float>`` path
  is kept for pre-Phase-2 call sites and tests, still returning
  ``PermissionResolution(allow=False, scope="deny", timed_out=True)`` on
  elapse. The native-hook bridge (``apps.api._native_hook_bridge``) passes
  ``timeout=None`` so users may be away for days without an auto-DENY —
  the DLL Phase 2 pipe wait is INFINITE too (see plan §2 N9).

The registry is intentionally idempotent and race-safe: a late
:meth:`resolve` after a timeout / cancel is a silent no-op, and a double
resolve only honours the first (mirrors V1 ``pending.event.is_set()`` guard).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

__all__ = [
    "PermissionGrantScope",
    "PermissionResolution",
    "PermissionWaitRegistry",
]


# Grant scopes mirror V1 ``resolve_permission`` ``grant`` vocabulary
# (``policy.py:1670``): deny / once / session / process / permanent.
_VALID_SCOPES = frozenset({"deny", "once", "session", "process", "permanent"})

#: Type alias documenting the accepted scope strings.
PermissionGrantScope = str


@dataclass(frozen=True, slots=True)
class PermissionResolution:
    """Terminal outcome of an ASK wait.

    ``allow`` is the binary ALLOW/DENY the FileGuard acts on; ``scope`` is the
    V1 grant vocabulary (``deny`` / ``once`` / ``session`` / ``process`` /
    ``permanent``) so callers / audit can record *how* it was resolved.
    ``timed_out`` marks the V1 "60s elapsed → DENY" path so the caller can log
    a distinct reason. Phase 2 no-timeout waits never set this flag (there is
    no elapse); a subprocess-gone cleanup surfaces as ``allow=False /
    scope='deny'`` with ``timed_out=False`` (the caller distinguishes the
    cause via the persistent store's ``resolution`` column).
    """

    allow: bool
    scope: PermissionGrantScope
    timed_out: bool = False


class PermissionWaitRegistry:
    """Async map of in-flight ASK waits keyed by ``request_id`` string.

    Single-process, asyncio-only (no threads). All public methods are cheap
    and safe to call from the event loop; no external locking is required
    because asyncio is cooperatively scheduled on a single thread.

    Phase 2 (2026-07-06) — the registry gained a ``(pid, path, event)`` reverse
    index to fold concurrent duplicate ASKs onto a single future
    (:meth:`register_or_dedupe`), and :meth:`wait` accepts ``timeout=None``
    for an infinite wait (users may take days to click — no auto-DENY).
    All Phase 1 APIs (``register`` / ``wait(timeout=X)`` / ``resolve`` /
    ``cancel`` / ``has_pending``) are byte-for-byte preserved so existing
    call sites / tests are unaffected.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[PermissionResolution]] = {}
        # Phase 2 dedupe reverse-index: (pid, path, event_int) -> request_id.
        # Same triple → callers share the ONE future in ``_waiters``. Cleaned
        # up on every resolve / cancel path so a fresh ASK for the same
        # triple (after the previous one resolved) always gets a NEW future.
        self._dedupe_index: dict[tuple[int, str, int], str] = {}
        # Forward index (request_id -> dedupe key) so we can scrub in O(1)
        # instead of walking ``_dedupe_index.values()`` on every resolve.
        self._request_to_dedupe: dict[str, tuple[int, str, int]] = {}

    def register(self, request_id: str) -> "asyncio.Future[PermissionResolution]":
        """Create (or return the existing) future for ``request_id``.

        Returns the future the caller will await in :meth:`wait`. Re-
        registering an id that is already pending returns the same future
        (idempotent) so a retried ASK never spawns two waiters.
        """
        existing = self._waiters.get(request_id)
        if existing is not None and not existing.done():
            return existing
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[PermissionResolution]" = loop.create_future()
        self._waiters[request_id] = fut
        return fut

    def register_or_dedupe(
        self,
        *,
        pid: int,
        path: str,
        event: int,
        request_id: str,
    ) -> tuple["asyncio.Future[PermissionResolution]", bool]:
        """Phase 2 — register with a ``(pid, path, event)`` dedupe key.

        Returns ``(future, is_dedupe)``:

        * ``is_dedupe=True`` → an ASK for the same ``(pid, path, event)``
          triple is already pending; ``future`` is the SHARED future the
          caller should await. ``request_id`` is ignored (the callers all
          wait on the pre-existing request's future). The caller MUST NOT
          persist / broadcast a new PermissionRequest — the original one
          already covers this triple.
        * ``is_dedupe=False`` → this is a brand-new triple; a new future
          was created for ``request_id`` and registered in the dedupe
          index. The caller SHOULD persist / broadcast a fresh
          PermissionRequest and await the returned future.

        The triple ``(pid, path, event)`` matches the native ``FilterEventV2``
        fields (``pid``, ``file_path``, ``event`` bitfield) — see plan §2.5.
        Empty / falsy ``pid`` disables dedupe for that call (returns
        ``is_dedupe=False`` without touching the index) so events that
        arrive without a valid subprocess id (rare, DLL fallback) still get
        their own waiter — safer than folding onto an unrelated triple.
        """
        key = (int(pid), str(path), int(event))
        if pid and key in self._dedupe_index:
            existing_rid = self._dedupe_index[key]
            existing_fut = self._waiters.get(existing_rid)
            if existing_fut is not None and not existing_fut.done():
                return (existing_fut, True)
            # Stale index entry (the previous request already resolved but
            # cleanup missed it — belt-and-braces). Scrub and fall through
            # so this call registers a fresh future.
            self._scrub_dedupe_by_request(existing_rid)

        fut = self.register(request_id)
        if pid:
            self._dedupe_index[key] = request_id
            self._request_to_dedupe[request_id] = key
        return (fut, False)

    async def wait(
        self,
        request_id: str,
        *,
        timeout: float | None = None,
    ) -> PermissionResolution:
        """Block until ``request_id`` is resolved.

        Phase 2 default (``timeout=None``): wait INFINITELY until the future
        is resolved (approve / reject / cancel / subprocess-gone cleanup /
        loop teardown). No auto-DENY on elapse — users may be away for days.

        Phase 1 back-compat (``timeout=<float>``): behave as before — on
        elapse, drop the waiter and return
        ``PermissionResolution(allow=False, scope="deny", timed_out=True)``.

        Loop-teardown safety: when the loop is closing / the future is
        cancelled externally, this re-raises ``asyncio.CancelledError`` so
        the caller (native-hook bridge) applies its fail-closed policy.
        """
        fut = self.register(request_id)
        if timeout is None:
            try:
                return await fut
            finally:
                # Belt-and-braces: if the future resolved, ensure the entry
                # is popped (usually already done in :meth:`resolve`).
                current = self._waiters.get(request_id)
                if current is fut and fut.done():
                    self._waiters.pop(request_id, None)
                    self._scrub_dedupe_by_request(request_id)

        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            # Legacy parity: timeout → DENY. Drop the waiter so a late resolve
            # is a no-op.
            self._waiters.pop(request_id, None)
            self._scrub_dedupe_by_request(request_id)
            if not fut.done():
                fut.cancel()
            return PermissionResolution(
                allow=False, scope="deny", timed_out=True
            )
        finally:
            # Clean up only our own entry if it's the resolved one.
            current = self._waiters.get(request_id)
            if current is fut and fut.done():
                self._waiters.pop(request_id, None)
                self._scrub_dedupe_by_request(request_id)

    def resolve(
        self,
        request_id: str,
        *,
        allow: bool,
        scope: PermissionGrantScope = "once",
    ) -> bool:
        """Wake the waiter for ``request_id``; return ``True`` on success.

        Idempotent: an unknown / already-resolved id returns ``False`` (V1
        ``resolve_permission`` returns ``False`` on a missing / already-set
        pending). ``scope`` is normalised; an unknown scope falls back to
        ``deny`` (mirrors V1 rejecting unknown grants).

        Phase 2 — the ``(pid, path, event)`` dedupe entry (if any) is scrubbed
        so a subsequent ASK for the same triple after this one resolves
        gets a FRESH future rather than the resolved one.

        Cross-loop wake safety (2026-07-06 bugfix): the native-hook ASK path
        (:mod:`apps.api._native_hook_bridge`) creates + awaits the future on a
        DEDICATED asyncio loop thread (``native-guard-ask-loop``), whereas the
        approve / reject / cancel HTTP routes call :meth:`resolve` on the API's
        MAIN uvicorn loop — a DIFFERENT thread + loop. ``asyncio.Future`` is
        NOT thread-safe: a bare ``fut.set_result(...)`` from the main loop
        schedules the future's done-callbacks via the OWNING loop's
        ``call_soon`` (not ``call_soon_threadsafe``), which does NOT wake that
        loop's selector — so the ``await fut`` on the dedicated loop never
        resumes and the native ``filter()`` outer ``fut.result(timeout)``
        eventually fires ``decide_timeout`` (fail-closed DENY) EVEN THOUGH the
        user clicked "Allow". We therefore marshal the terminal ``set_result``
        (and the dict/dedupe cleanup) onto the future's OWNING loop via
        ``call_soon_threadsafe`` whenever the caller is on a different loop.
        Same-loop callers keep the original synchronous fast-path.
        """
        fut = self._waiters.get(request_id)
        if fut is None or fut.done():
            return False
        scope_n = (scope or "").strip().lower()
        if scope_n not in _VALID_SCOPES:
            scope_n = "deny"
            allow = False
        resolution = PermissionResolution(allow=allow, scope=scope_n)

        fut_loop = fut.get_loop()
        try:
            caller_loop: asyncio.AbstractEventLoop | None = (
                asyncio.get_running_loop()
            )
        except RuntimeError:
            caller_loop = None

        if caller_loop is fut_loop and not fut_loop.is_closed():
            # Same loop as the awaiter — the original synchronous fast-path is
            # already correct (the awaiter's callbacks run on THIS loop).
            self._settle(request_id, fut, resolution)
            return True

        # Different loop (or no running loop, e.g. a worker thread) — the
        # future belongs to another loop. Marshal the set_result + cleanup
        # onto its OWNING loop so its selector is woken and ``await fut``
        # resumes. ``call_soon_threadsafe`` is the ONLY thread-safe way to
        # touch a future / its loop from a foreign thread.
        if fut_loop.is_closed():
            return False
        try:
            fut_loop.call_soon_threadsafe(
                self._settle, request_id, fut, resolution
            )
        except RuntimeError:
            # Owning loop is shutting down; nothing we can safely do.
            return False
        return True

    def _settle(
        self,
        request_id: str,
        fut: "asyncio.Future[PermissionResolution]",
        resolution: PermissionResolution,
    ) -> None:
        """Terminal set_result + registry cleanup, run on the future's loop.

        Always executed on the future's OWNING loop (directly on the fast
        path, or via ``call_soon_threadsafe`` from a foreign loop) so the
        ``set_result`` correctly wakes the awaiter and the dict mutations
        happen on a single, consistent loop thread. A late double-resolve is
        a silent no-op (mirrors the V1 ``pending.event.is_set()`` guard).
        """
        if fut.done():
            return
        fut.set_result(resolution)
        self._waiters.pop(request_id, None)
        self._scrub_dedupe_by_request(request_id)

    def cancel(self, request_id: str) -> bool:
        """Resolve ``request_id`` as DENY (used by session-cancel paths).

        Mirrors V1 ``cancel_pending_for_session`` resolving outstanding asks
        as DENY when the chat session is interrupted. Returns ``True`` iff a
        live waiter was woken. Phase 2 — also scrubs the dedupe entry.
        """
        return self.resolve(request_id, allow=False, scope="deny")

    def has_pending(self, request_id: str) -> bool:
        """Return ``True`` iff a live (unresolved) waiter exists."""
        fut = self._waiters.get(request_id)
        return fut is not None and not fut.done()

    def list_pending(self) -> list[str]:
        """Return every request_id with a live (unresolved) waiter.

        Phase 2 — consumed by :class:`PendingCleanupService` (scans every
        10s and resolves pids that have died) and by the ``cancel_all``
        route branch. Ordering is insertion order (Python 3.7+ dict);
        callers should treat it as a snapshot (a concurrent resolve may
        prune an id between listing and touching it).
        """
        return [
            rid
            for rid, fut in self._waiters.items()
            if not fut.done()
        ]

    def pending_pids(self) -> set[int]:
        """Return the set of actor pids with a LIVE (unresolved) native ASK.

        2026-07-08 — consumed (via an apps-layer probe) by the exec tool's
        timeout: while a native FileGuard dialog is pending on an exec child
        process, the exec deadline must PAUSE instead of killing the child
        mid-decision. The pid comes from the ``(pid, path, event)`` dedupe key
        native requests register under (see :meth:`register_or_dedupe`), so
        this only reflects native-subprocess ASKs (in-process/exec-command
        ASKs use :meth:`register` without a pid key and are irrelevant to the
        child-process timeout). Snapshot semantics like :meth:`list_pending`.
        """
        alive: set[int] = set()
        for (pid, _path, _event), rid in self._dedupe_index.items():
            fut = self._waiters.get(rid)
            if fut is not None and not fut.done():
                alive.add(pid)
        return alive

    def pending_request_ids_by_pid(self) -> dict[int, list[str]]:
        """Return ``{pid: [request_id, ...]}`` for every LIVE native ASK.

        Companion to :meth:`pending_pids` (same ``_dedupe_index`` walk +
        live-future guard) but keyed the other way so a caller that already
        knows a pid (or its descendant pids) can recover the request_ids to
        RESOLVE. Introduced for the chat-Stop directed flush
        (``apps.api._guard_token.build_ask_flush_for_pid``): when the exec
        task is cancelled the flush must wake the queued ASK futures for the
        killed child's pid tree instead of leaving them for the 10s
        subprocess-gone backstop. Only reflects native-subprocess ASKs
        (those registered with a pid key via :meth:`register_or_dedupe`);
        in-process ASKs carry no pid and are irrelevant here. Snapshot
        semantics like :meth:`list_pending` (a concurrent resolve may prune
        an id between listing and touching it).
        """
        by_pid: dict[int, list[str]] = {}
        for (pid, _path, _event), rid in self._dedupe_index.items():
            fut = self._waiters.get(rid)
            if fut is not None and not fut.done():
                by_pid.setdefault(pid, []).append(rid)
        return by_pid

    # -- internal ------------------------------------------------------
    def _scrub_dedupe_by_request(self, request_id: str) -> None:
        """Remove the ``(pid, path, event)`` entry for ``request_id``.

        Uses the O(1) forward index so we don't walk ``_dedupe_index.values()``
        on every resolve. A missing entry is a silent no-op (request never
        had a dedupe key — e.g. registered via :meth:`register` directly).
        """
        key = self._request_to_dedupe.pop(request_id, None)
        if key is not None:
            # Only remove if the reverse map still points at THIS request
            # (a rare race where a stale index survived shouldn't clobber
            # a fresh entry for the same triple).
            if self._dedupe_index.get(key) == request_id:
                self._dedupe_index.pop(key, None)
