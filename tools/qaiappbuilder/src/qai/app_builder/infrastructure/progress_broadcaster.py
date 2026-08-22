# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Generic multi-subscriber progress broadcaster.

Used to allow both SSE and WebSocket transports to consume the same
progress iterator without the single-consumer limitation of raw
AsyncIterator. The pattern mirrors RunStreamBroadcaster but is simpler
(linear buffer, no state machine).

Usage:
    broadcaster = ProgressBroadcaster()
    # When a job starts:
    broadcaster.schedule_drain(job_id, async_iterator)
    # Multiple consumers can replay:
    async for event_name, payload in broadcaster.replay(job_id):
        ...  # yields ("progress", {...}), then ("done", {...}) or ("error", {...})
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from qai.platform.logging import get_logger

__all__ = ["ProgressBroadcaster", "ProgressEntry"]

logger = get_logger(__name__)

#: Time-to-live for terminal entries after all subscribers disconnect.
#: After ``done`` is set and no subscribers remain, the entry stays
#: around for at least this many seconds so a late-arriving consumer
#: can still replay the full frame list. Older entries are GC'd lazily
#: on the next :meth:`ProgressBroadcaster.register` call.
_ENTRY_TTL_S: float = 300.0  # 5 min


@dataclass
class ProgressEntry:
    """Per-job broadcast state shared between the drainer and consumers.

    Mirrors :class:`RunStreamEntry` from
    ``run_stream_broadcaster.py`` but is simpler (no run-state machine,
    no error_code/failed distinction — just buffer + done + terminal).
    """

    frames: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    subscribers: set[asyncio.Event] = field(default_factory=set)
    done: bool = False
    terminal_frame: tuple[str, dict[str, Any]] | None = None
    terminal_at: float | None = None  # monotonic ts when ``done`` flipped


class ProgressBroadcaster:
    """Application-layer progress-frame broadcast registry + replay.

    A single instance is held by the DI container; it lives for the
    process lifetime so the progress-start handler and SSE/WS
    subscribers share the same in-process frame buffers.

    Concurrency note: the registry is read/written exclusively from
    inside the asyncio event loop driving the FastAPI app, so no extra
    lock is required.
    """

    def __init__(self, *, entry_ttl_s: float = _ENTRY_TTL_S) -> None:
        self._entries: dict[str, ProgressEntry] = {}
        self._entry_ttl_s = entry_ttl_s
        self._drain_tasks: dict[str, asyncio.Task[None]] = {}

    # ---- registry mutators -------------------------------------------

    def register(self, job_id: str) -> ProgressEntry:
        """Allocate and store a fresh broadcast entry for ``job_id``.

        Lazily evicts terminal entries older than the configured TTL so
        abandoned jobs do not accumulate indefinitely.
        """
        now = time.monotonic()
        expired = [
            jid
            for jid, entry in self._entries.items()
            if entry.done
            and entry.terminal_at is not None
            and not entry.subscribers
            and now - entry.terminal_at > self._entry_ttl_s
        ]
        for jid in expired:
            self._entries.pop(jid, None)
            self._drain_tasks.pop(jid, None)

        entry = ProgressEntry()
        self._entries[job_id] = entry
        return entry

    def publish(self, job_id: str, event_name: str, payload: dict[str, Any]) -> None:
        """Append a frame and wake all subscribers."""
        entry = self._entries.get(job_id)
        if entry is None:
            return
        entry.frames.append((event_name, payload))
        for ev in set(entry.subscribers):
            ev.set()

    def mark_done(self, job_id: str, payload: dict[str, Any] | None = None) -> None:
        """Mark job as complete and wake all subscribers."""
        entry = self._entries.get(job_id)
        if entry is None:
            return
        entry.done = True
        entry.terminal_frame = ("done", payload or {})
        entry.terminal_at = time.monotonic()
        for ev in set(entry.subscribers):
            ev.set()

    def mark_error(self, job_id: str, error: dict[str, Any]) -> None:
        """Mark job as failed and wake all subscribers."""
        entry = self._entries.get(job_id)
        if entry is None:
            return
        entry.done = True
        entry.terminal_frame = ("error", error)
        entry.terminal_at = time.monotonic()
        for ev in set(entry.subscribers):
            ev.set()

    def get(self, job_id: str) -> ProgressEntry | None:
        """Return the broadcast entry for ``job_id`` (or ``None``)."""
        return self._entries.get(job_id)

    def cleanup(self, job_id: str) -> None:
        """Remove entry (call after all subscribers disconnect)."""
        self._entries.pop(job_id, None)
        task = self._drain_tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()

    # ---- background drainer ------------------------------------------

    def schedule_drain(
        self,
        job_id: str,
        iterator: AsyncIterator[Any],
    ) -> None:
        """Register ``job_id`` and start draining ``iterator`` in the
        background, publishing each snapshot as a ``progress`` frame.

        The iterator is expected to yield objects with attributes
        matching the progress payload shape (``bytes_downloaded``,
        ``total_bytes``, ``speed_bps``, ``eta_seconds``, ``percent``,
        ``is_complete``). Alternatively, it may yield raw dicts.

        On successful exhaustion, ``mark_done`` is called.
        On exception, ``mark_error`` is called with the error details.
        """
        self.register(job_id)
        task = asyncio.ensure_future(
            self._drain(job_id, iterator),
        )
        self._drain_tasks[job_id] = task

    async def _drain(
        self, job_id: str, iterator: AsyncIterator[Any]
    ) -> None:
        """Consume the iterator and publish progress frames."""
        try:
            async for snapshot in iterator:
                # Support both dict-like and attribute-access snapshots.
                if isinstance(snapshot, dict):
                    payload = snapshot
                else:
                    payload = {
                        "bytes_downloaded": getattr(snapshot, "bytes_downloaded", 0),
                        "total_bytes": getattr(snapshot, "total_bytes", None),
                        "speed_bps": getattr(snapshot, "speed_bps", 0.0),
                        "eta_seconds": getattr(snapshot, "eta_seconds", None),
                        "percent": getattr(snapshot, "percent", 0.0),
                        "is_complete": getattr(snapshot, "is_complete", False),
                    }
                self.publish(job_id, "progress", payload)
            self.mark_done(job_id)
        except asyncio.CancelledError:
            self.mark_done(job_id)
            raise
        except Exception as exc:  # noqa: BLE001 — surface via entry
            self.mark_error(
                job_id,
                {
                    "type": type(exc).__name__,
                    "code": getattr(exc, "code", "progress.drain_failed"),
                    "message": str(exc) or type(exc).__name__,
                },
            )
            logger.info("progress_broadcaster.drain_failed", job_id=job_id)

    # ---- replay (SSE / WS consumer) ----------------------------------

    async def replay(
        self, job_id: str
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(event_name, payload)`` tuples for a subscriber.

        Yields all buffered frames, waits for new ones until the entry
        is marked done, then yields the terminal frame.

        Raises ``KeyError`` if no entry exists for ``job_id``.
        """
        entry = self._entries.get(job_id)
        if entry is None:
            raise KeyError(f"No progress entry for job_id={job_id!r}")

        my_event = asyncio.Event()
        entry.subscribers.add(my_event)
        try:
            cursor = 0
            while True:
                # Yield any new frames since the last cursor.
                while cursor < len(entry.frames):
                    frame = entry.frames[cursor]
                    cursor += 1
                    yield frame

                # Terminal? Emit the terminal frame and exit.
                if entry.done and cursor >= len(entry.frames):
                    if entry.terminal_frame is not None:
                        yield entry.terminal_frame
                    return

                # Wait for the next frame or terminal mark.
                # 1s timeout acts as a heartbeat opportunity for the
                # transport layer (the caller can send a ping on timeout).
                try:
                    await asyncio.wait_for(my_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                finally:
                    my_event.clear()
        finally:
            entry.subscribers.discard(my_event)

    # ---- lifecycle ----------------------------------------------------

    async def aclose(self) -> None:
        """Cancel all outstanding background drain tasks.

        Called from the app ``lifespan`` shutdown path. Idempotent.
        """
        for task in self._drain_tasks.values():
            if not task.done():
                task.cancel()
        self._drain_tasks.clear()
