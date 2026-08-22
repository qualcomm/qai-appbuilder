# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L-Sec-4 (fileguard-audit-2026-07-26) — WaitWatchdogService.

The Phase 2 (2026-07-06) ASK wait semantic is ``timeout=None`` — a
:class:`~qai.security.application.permission_wait.PermissionWaitRegistry`
waiter blocks INDEFINITELY, so an operator can be away from the console
for hours or days without an auto-DENY. That is the intended behaviour
for the security decision, but operations still needs a signal when a
row goes UNANSWERED for far longer than a human normally would — a
stuck popup on a headless deployment, a dropped SSE stream that hid the
dialog, or a runaway integration test that never resolves.

This module adds a lightweight background sweep that periodically
inspects the wait registry + durable pending store, computes ``age =
now - created_at`` for each unresolved row and emits a structlog
``WARN`` (and optionally increments an in-process counter) for rows
past ``stale_after_seconds``. It NEVER resolves the wait — the
Phase 2 no-timeout guarantee is preserved. This is purely observation.

The service is deliberately kept minimal — no cross-context imports of
grant / policy state, no persistence — so it can be composed into any
DI wiring without pulling extra collaborators. Assembly into
``build_security_services`` is a follow-up commit; this module owns the
class + its unit test and is safe to leave dormant until then.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping

from qai.platform.logging import get_logger
from qai.platform.time import Clock

if TYPE_CHECKING:  # pragma: no cover
    from qai.security.application.permission_wait import PermissionWaitRegistry

__all__ = ["WaitWatchdogService"]

_log = get_logger(__name__)


#: Default cadence — 5 minutes. Long enough that a human dialog answered
#: within a minute is never spuriously warned; short enough that a truly
#: stuck row surfaces before the operator's shift ends.
_DEFAULT_SCAN_INTERVAL_SECONDS = 300.0

#: Default "stale" threshold — 15 minutes. Anything older than this
#: without a resolve triggers a warning. Overridable per-construction so
#: an integration test can dial it down to seconds without a monkeypatch.
_DEFAULT_STALE_AFTER_SECONDS = 900.0


class WaitWatchdogService:
    """Periodically warn about ASKs that have been unresolved too long.

    Parameters
    ----------
    wait_registry:
        The process-wide
        :class:`~qai.security.application.permission_wait.PermissionWaitRegistry`.
        Used as the LIVE set of pending ids so a row already resolved in
        memory is never warned (the durable row is the timestamp source).
    pending_store:
        A durable pending-permission store with an
        ``async list_unresolved()`` returning dict rows carrying
        ``request_id`` (str) and ``created_at`` (ISO-8601 str OR
        :class:`datetime.datetime`). Any row shaped otherwise is
        silently skipped. When ``None`` the sweep is a no-op — the
        in-memory registry alone does not carry an age.
    clock:
        Domain clock providing ``now() -> datetime``.
    scan_interval_seconds:
        Cadence between sweeps. Default 5 minutes.
    stale_after_seconds:
        Rows whose age exceeds this threshold are warned.
        Default 15 minutes.
    counter:
        Optional 1-arg callable invoked once per stale row (per sweep)
        with the row's ``request_id``. Wire e.g. a Prometheus counter
        ``security.permission.ask_stale``. Absent → log-only.

    Notes
    -----
    * NEVER resolves the wait — Phase 2's no-auto-DENY invariant holds.
    * NEVER raises — a sweep hiccup is logged and swallowed so the loop
      keeps running (mirrors
      :class:`~qai.security.application.pending_cleanup.PendingCleanupService`).
    """

    __slots__ = (
        "_registry",
        "_store",
        "_clock",
        "_interval",
        "_stale_after",
        "_counter",
        "_task",
        "_stopping",
    )

    def __init__(
        self,
        *,
        wait_registry: "PermissionWaitRegistry",
        pending_store: Any = None,
        clock: Clock,
        scan_interval_seconds: float = _DEFAULT_SCAN_INTERVAL_SECONDS,
        stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
        counter: "Callable[[str], None] | None" = None,
    ) -> None:
        self._registry = wait_registry
        self._store = pending_store
        self._clock = clock
        self._interval = float(scan_interval_seconds)
        self._stale_after = float(stale_after_seconds)
        self._counter = counter
        self._task: "asyncio.Task[None] | None" = None
        self._stopping = False

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "asyncio.Task[None]":
        """Spawn the background sweep task (idempotent)."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name="security-wait-watchdog"
        )
        return self._task

    async def stop(self) -> None:
        """Stop the sweep task cleanly (idempotent)."""
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
        _log.info(
            "security.wait_watchdog.started",
            interval_seconds=self._interval,
            stale_after_seconds=self._stale_after,
        )
        try:
            while not self._stopping:
                try:
                    await self.sweep_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — never crash the loop
                    _log.warning(
                        "security.wait_watchdog.sweep_failed", exc_info=True
                    )
                try:
                    await asyncio.sleep(self._interval)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            _log.info("security.wait_watchdog.cancelled")
            raise

    async def sweep_once(self) -> int:
        """Emit a WARN for every unresolved row past the staleness ceiling.

        Returns the number of rows warned in this sweep — handy for tests.
        Rows already resolved in the in-memory registry (or whose
        ``created_at`` is missing / unparseable) are skipped.
        """
        if self._store is None:
            return 0
        pending_ids: set[str] = set(self._registry.list_pending())
        if not pending_ids:
            return 0
        rows: Iterable[Mapping[str, Any]]
        try:
            rows = await self._store.list_unresolved()
        except Exception:  # noqa: BLE001 — read fail → skip
            _log.warning(
                "security.wait_watchdog.store_read_failed", exc_info=True
            )
            return 0
        now = self._clock.now()
        warned = 0
        for row in rows:
            rid = row.get("request_id")
            if not isinstance(rid, str) or rid not in pending_ids:
                continue
            created_at = _parse_created_at(row.get("created_at"))
            if created_at is None:
                continue
            try:
                age_seconds = (now - created_at).total_seconds()
            except (TypeError, ValueError):
                continue
            if age_seconds < self._stale_after:
                continue
            _log.warning(
                "security.permission.ask_stale",
                request_id=rid,
                age_seconds=int(age_seconds),
                threshold_seconds=int(self._stale_after),
            )
            if self._counter is not None:
                try:
                    self._counter(rid)
                except Exception:  # noqa: BLE001 — counter must never break
                    _log.warning(
                        "security.wait_watchdog.counter_failed",
                        request_id=rid,
                        exc_info=True,
                    )
            warned += 1
        return warned


def _parse_created_at(value: Any) -> "datetime | None":
    """Best-effort ISO-8601 / datetime parse. Any failure → ``None``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
