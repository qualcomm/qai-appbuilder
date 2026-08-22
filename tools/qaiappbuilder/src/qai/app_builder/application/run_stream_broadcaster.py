# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-process run-frame broadcast + SSE replay state machine (R17).

This module hosts the App Builder run-streaming orchestration that used
to live as module-level mutable state inside
``interfaces/http/routes/app_builder.py``:

* the per-run broadcast registry (a ``dict`` keyed by ``str(RunId)``)
  with lazy TTL eviction of terminal entries;
* the ``register`` / ``publish`` / ``mark_terminal`` mutators;
* the background drainer that tees every :class:`RunFrame` yielded by
  the :class:`RunAppUseCase` iterator into the registry;
* the SSE replay state machine that a subscriber drains (backfilled
  history + live frames + ``state`` transitions + terminal ``done`` /
  ``error``).

Clean-Architecture rationale (R17): run orchestration + frame fan-out
is application-layer responsibility, not a route concern. Hoisting it
here removes the module-level mutable ``dict`` from the route module
(``§3.6`` route-thinness advisory) and lets the route handler stay thin
— it only translates HTTP ⇄ broadcaster calls and encodes the yielded
event tuples to SSE wire bytes.

Wire-shape invariance (R17 — pure refactor): the broadcaster yields
abstract ``(event_name, payload_dict)`` tuples; the route encodes them
verbatim with the existing ``_sse_event`` helper. The ``state`` /
``frame`` / ``error`` / ``done`` payloads, the TTL value, the replay
semantics and the ``create_run`` behaviour are reproduced byte-for-byte
from the pre-refactor route module — no client-observable change.

Concurrency note: the registry is read/written exclusively from inside
the asyncio event loop driving the FastAPI app, so no extra lock is
required (asyncio is single-threaded per loop). This matches the
pre-refactor module-level registry's threading model exactly.

Multi-subscriber wake-up (R-6 / D7 — per-subscriber Event): each
``GET /runs/{run_id}/stream`` subscriber registers its **own**
``asyncio.Event`` in :attr:`RunStreamEntry.subscribers`. ``publish`` /
``mark_terminal`` set *every* registered event, and each subscriber only
``clear()``s its own — so one subscriber can no longer swallow another's
wake-up signal (the bug a single shared ``Event`` had). The SSE wire
shape is unchanged; only the in-process wake-up signal carrier changed.

Background drain ownership (R-3): the broadcaster holds a
:class:`~qai.platform.tasks.TaskRegistry` so the fire-and-forget drain
tasks keep a strong ref (no GC mid-flight), surface their exceptions,
and get cancelled on :meth:`aclose` at app shutdown.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.app_builder.domain.run import RunFrame, RunStatus
from qai.app_builder.domain.value_objects import RunId
from qai.platform.errors import QaiError
from qai.platform.logging import get_logger
from qai.platform.tasks import TaskRegistry

if TYPE_CHECKING:  # pragma: no cover
    from qai.app_builder.application.ports import RunRepositoryPort

__all__ = ["RunStreamBroadcaster", "RunStreamEntry"]

logger = get_logger(__name__)


#: Time-to-live for terminal run entries in the broadcast registry.
#: After ``done=True`` is set, the entry stays around for at least this
#: many seconds so a late-arriving SSE subscriber can still replay the
#: full frame list. Older entries are GC'd lazily on the next
#: :meth:`RunStreamBroadcaster.register` call. Reproduced verbatim from
#: the pre-R17 route module (``_RUN_BUFFER_TTL_S``).
_RUN_BUFFER_TTL_S: float = 600.0  # 10 min — generous; entries are tiny


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunStreamEntry:
    """Per-run broadcast state shared between the drainer and the
    GET-stream subscribers.

    Mirrors the pre-R17 route-module ``_RunStreamEntry`` verbatim.
    """

    frames: list[RunFrame] = field(default_factory=list)
    # R-6 / D7 — per-subscriber wake-up events. Each SSE subscriber
    # registers its own ``asyncio.Event`` here; ``publish`` /
    # ``mark_terminal`` set them all, each subscriber clears only its
    # own. Replaces the single shared ``asyncio.Event`` whose
    # cross-subscriber ``clear()`` could swallow another reader's wake-up.
    subscribers: set[asyncio.Event] = field(default_factory=set)
    done: bool = False
    failed: bool = False
    error_message: str | None = None
    # PR-F1 (F-15) — append-only structured failure code, mirrored from
    # ``Run.error_code``. Used by ``replay`` to surface the runner's
    # ``WEIGHTS_NOT_INSTALLED`` / ``AUDIO_DECODE_ERROR`` etc. inside the
    # SSE ``error`` frame's ``details.error_code`` (so the frontend can
    # dispatch to i18n-friendly toasts).
    error_code: str | None = None
    terminal_at: float | None = None  # monotonic ts when ``done`` flipped
    # R-cancel — the background drain task driving the runner iterator for
    # this run. Held so a user-initiated cancel can cancel THIS task, which
    # closes the runner iterator and (for the one-shot subprocess runner)
    # triggers ``SubprocessProcessRunner``'s ``finally`` ``proc.kill()`` +
    # tree-kill — without a one-shot run there is no other way to terminate
    # the spawned subprocess (V1 ``runner.py:322`` ``proc.terminate()`` parity).
    drain_task: asyncio.Task[None] | None = None


class RunStreamBroadcaster:
    """Application-layer run-frame broadcast registry + SSE replay.

    A single instance is held by the DI container
    (``container.app_builder.run_stream_broadcaster``); it lives for the
    process lifetime so the ``POST /runs`` drainer and the
    ``GET /runs/{run_id}/stream`` subscribers share the same in-process
    frame buffers.

    The buffer is intentionally simple — no LRU, no size cap — because:

    * SSE consumers connect within seconds of the run starting (the
      happy path is "POST /runs → 201 → open stream"); even slow
      clients hold the buffer for at most the run timeout;
    * a terminal run's entry is purged ``_RUN_BUFFER_TTL_S`` after the
      terminal mark, so abandoned runs don't accumulate;
    * frames are small dicts; even a 30-min run with 10/s frames
      occupies < 100 MB before terminal cleanup.
    """

    def __init__(self, *, buffer_ttl_s: float = _RUN_BUFFER_TTL_S) -> None:
        self._streams: dict[str, RunStreamEntry] = {}
        self._buffer_ttl_s = buffer_ttl_s
        # R-3 — hold strong refs to background drain tasks so they are
        # not GC'd mid-flight and are cancelled on ``aclose``.
        self._tasks = TaskRegistry()

    # ---- registry mutators -------------------------------------------

    def register(self, run_id: str) -> RunStreamEntry:
        """Allocate and store a fresh broadcast entry for ``run_id``.

        Lazily evicts terminal entries older than the configured TTL so
        abandoned runs do not accumulate indefinitely.
        """
        # Lazy eviction — runs in O(N) where N is the number of
        # currently tracked runs (typically < 100). Cheaper than
        # spinning a periodic cleanup task and good enough until the run
        # rate justifies one.
        now = time.monotonic()
        expired = [
            rid
            for rid, entry in self._streams.items()
            if entry.done
            and entry.terminal_at is not None
            and now - entry.terminal_at > self._buffer_ttl_s
        ]
        for rid in expired:
            self._streams.pop(rid, None)

        entry = RunStreamEntry()
        self._streams[run_id] = entry
        return entry

    def publish(self, run_id: str, frame: RunFrame) -> None:
        """Append ``frame`` to the run's buffer and wake subscribers."""
        entry = self._streams.get(run_id)
        if entry is None:
            return
        entry.frames.append(frame)
        for ev in set(entry.subscribers):
            ev.set()

    def mark_terminal(
        self,
        run_id: str,
        *,
        failed: bool,
        error_message: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Mark the run's broadcast entry as terminal and wake subscribers.

        ``error_code`` (PR-F1, append-only) is the structured failure
        code from the runner subprocess (``"WEIGHTS_NOT_INSTALLED"`` /
        ``"AUDIO_DECODE_ERROR"`` / etc.); pre-PR-F1 callers omit it and
        the entry stores ``None``.
        """
        entry = self._streams.get(run_id)
        logger.info(
            "RunStreamBroadcaster.mark_terminal: run_id=%s failed=%s subscriber_count=%d",
            run_id,
            failed,
            len(entry.subscribers) if entry else 0,
        )
        if entry is None:
            return
        entry.done = True
        entry.failed = failed
        entry.error_message = error_message
        entry.error_code = error_code
        entry.terminal_at = time.monotonic()
        for ev in set(entry.subscribers):
            ev.set()

    def get(self, run_id: str) -> RunStreamEntry | None:
        """Return the broadcast entry for ``run_id`` (or ``None``)."""
        return self._streams.get(run_id)

    async def aclose(self) -> None:
        """Cancel all outstanding background drain tasks (R-3).

        Called from the app ``lifespan`` shutdown path so abandoned
        run-drain coroutines do not leak past process shutdown.
        Idempotent.
        """
        await self._tasks.cancel_all()

    # ---- background drainer ------------------------------------------

    def schedule_drain(
        self, run_id: str, iterator: AsyncIterator[RunFrame]
    ) -> "asyncio.Task[None]":
        """Register ``run_id`` and start draining ``iterator`` in the
        background, tee'ing each frame into the broadcast registry.

        The caller is expected to have already obtained the iterator
        from :meth:`RunAppUseCase.execute`. The use case still owns the
        Run state machine; this drainer is purely a frame-fan-out
        mechanism. Errors during streaming are caught and surfaced via
        the entry's ``failed`` flag (the use case has already persisted
        the Run as FAILED before re-raising).
        """
        self.register(run_id)
        task = self._tasks.spawn(
            self._drain(run_id, iterator), name=f"run-drain-{run_id}"
        )
        # R-cancel — remember the drain task on the entry so
        # :meth:`cancel_drain` can terminate THIS run's runner (the one-shot
        # subprocess path has no other kill handle).
        entry = self._streams.get(run_id)
        if entry is not None:
            entry.drain_task = task
        return task

    def cancel_drain(self, run_id: str) -> bool:
        """Cancel the background drain task for ``run_id`` (if running).

        Cancelling the drain task closes the runner iterator it is consuming;
        for the one-shot subprocess runner that triggers
        ``SubprocessProcessRunner``'s ``finally`` ``proc.kill()`` +
        ``_best_effort_tree_kill`` (and the Windows Job Object teardown), so a
        user-initiated cancel actually terminates the spawned subprocess
        instead of leaving it running while the DB says CANCELLED (§🔴
        State-Truth-First). The sticky-worker path is cancelled separately via
        the worker's ``op:cancel`` (``RunCancellationPort``); this covers the
        one-shot path V1 killed with ``proc.terminate()``.

        Returns ``True`` when a live drain task was found and cancel()-ed,
        ``False`` otherwise (no entry / already finished). Best-effort and
        idempotent — the drain task's own ``mark_terminal`` still runs.
        """
        entry = self._streams.get(run_id)
        if entry is None or entry.drain_task is None:
            return False
        task = entry.drain_task
        if task.done():
            return False
        task.cancel()
        return True

    async def _drain(
        self, run_id: str, iterator: AsyncIterator[RunFrame]
    ) -> None:
        try:
            async for frame in iterator:
                self.publish(run_id, frame)
            self.mark_terminal(run_id, failed=False)
        except asyncio.CancelledError:
            # R-cancel — a user-initiated cancel (``cancel_drain``) cancelled
            # this task. Closing the iterator triggered the runner's
            # ``finally`` subprocess kill. Mark the broadcast entry terminal
            # so live SSE subscribers get a closing frame; the Run aggregate
            # was already transitioned to CANCELLED by ``CancelRunUseCase``,
            # whose ``fresh.status`` the replay loop surfaces as ``done``
            # (status=cancelled). Re-raise so the task is recorded as
            # cancelled (TaskRegistry treats CancelledError as expected).
            self.mark_terminal(run_id, failed=False)
            raise
        except Exception as exc:  # noqa: BLE001 — already persisted as FAILED
            # The use case has already transitioned the Run to FAILED
            # and persisted ``error_message`` before re-raising, so the
            # SSE handler can pick up the error via either the broadcast
            # entry's ``failed`` flag OR the repository status — we set
            # both for robustness.
            logger.warning("RunStreamBroadcaster._drain: run_id=%s failed with exception: %s", run_id, exc)
            self.mark_terminal(
                run_id,
                failed=True,
                error_message=str(exc) or type(exc).__name__,
            )
            logger.info("app_builder.background_run_drain_failed")

    # ---- SSE replay state machine ------------------------------------

    async def replay(
        self,
        run_id: RunId,
        *,
        run_repository: "RunRepositoryPort",
        initial_status: RunStatus,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(event_name, payload)`` tuples for an SSE subscriber.

        Reproduces the pre-R17 route generator's behaviour exactly. The
        caller (route layer) encodes each tuple to SSE wire bytes via
        ``_sse_event``. ``initial_status`` is the Run status the route
        read up-front (so it can surface a 404 before opening the
        stream).

        New subscribers see the full frame history (the registry is
        cumulative). When the registry has no entry for the run (server
        restarted between POST and GET, or the TTL expired) the
        generator falls back to a single repository snapshot of the
        terminal status so a polling client still learns whether the
        run finished.
        """
        current_status = initial_status
        run_id_str = str(run_id)
        yield (
            "state",
            {
                "status": current_status.value,
                "run_id": run_id_str,
                "ts": _now_iso(),
            },
        )

        entry = self._streams.get(run_id_str)
        # Hold the delegate generator explicitly so we can ``aclose`` it
        # deterministically when this outer generator is closed (client
        # disconnect). ``async for`` alone would leave the delegate's
        # ``finally`` (which discards the per-subscriber event, R-6) to
        # run only on GC — leaking the subscriber registration until
        # then. Explicit close keeps ``entry.subscribers`` bounded by
        # live subscribers.
        inner = (
            self._replay_without_entry(
                run_id, run_repository, current_status
            )
            if entry is None
            else self._replay_with_entry(
                run_id, run_repository, entry, current_status
            )
        )
        try:
            async for ev in inner:
                yield ev
        except QaiError as exc:
            yield ("error", exc.to_dict())
        except Exception as exc:  # noqa: BLE001 — convert to error frame
            yield (
                "error",
                {
                    "type": "InternalServerError",
                    "code": "internal.unexpected",
                    "message": str(exc) or type(exc).__name__,
                },
            )
        finally:
            await inner.aclose()

    async def _replay_without_entry(
        self,
        run_id: RunId,
        run_repository: "RunRepositoryPort",
        current_status: RunStatus,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Fallback path: no broadcast entry → single repo snapshot.

        The historical frame stream is unrecoverable in that case, but
        at least the client sees the final state.
        """
        fresh = await run_repository.get(run_id)
        if fresh.status != current_status:
            current_status = fresh.status
            yield (
                "state",
                {
                    "status": current_status.value,
                    "run_id": str(fresh.id),
                    "ts": _now_iso(),
                },
            )
        if fresh.status.is_terminal:
            if fresh.status == RunStatus.FAILED:
                # PR-F1 (F-15) — surface the runner's structured failure
                # code (e.g. ``WEIGHTS_NOT_INSTALLED``) inside ``details``
                # so the frontend can dispatch to i18n keys without
                # depending on free-text ``message`` parsing. Append-only
                # under v2.7 §3.1 — the existing top-level ``code`` keeps
                # its semantic (``"app_builder.run_failed"`` per
                # ``api-contract.md`` §2.1).
                details: dict[str, Any] = {"run_id": str(fresh.id)}
                if fresh.error_code is not None:
                    details["error_code"] = fresh.error_code
                yield (
                    "error",
                    {
                        "type": "DomainError",
                        "code": "app_builder.run_failed",
                        "message": fresh.error_message or "run failed",
                        "details": details,
                    },
                )
            else:
                yield (
                    "done",
                    {
                        "status": fresh.status.value,
                        "run_id": str(fresh.id),
                    },
                )
        else:
            # Non-terminal AND no broadcast entry — frames are
            # unrecoverable. Surface a structured error so the client
            # can fall back to polling ``GET /runs/{run_id}``.
            yield (
                "error",
                {
                    "type": "QaiError",
                    "code": "app_builder.run_stream_unavailable",
                    "message": (
                        "Frame stream is no longer available "
                        "for this run; query "
                        "GET /runs/{run_id} for status."
                    ),
                    "details": {"run_id": str(run_id)},
                },
            )

    async def _replay_with_entry(
        self,
        run_id: RunId,
        run_repository: "RunRepositoryPort",
        entry: RunStreamEntry,
        current_status: RunStatus,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Live + backfilled replay loop bound to a broadcast entry.

        Loop terminates when the registry flips to ``done`` AND the
        cursor has caught up with the buffer.

        ── Backfill marker (wire append-only) ────────────────────────────
        Frames already buffered when this subscriber attaches are the
        run's transcript-so-far (NOT live deltas the subscriber can watch
        appear). We tag them ``backfill: true`` in the payload so any future
        consumer that pre-renders the transcript via a snapshot path can
        suppress per-frame UI commits for those frames — mirroring the
        chat / sub-agent broadcasters' wire contract. Frames published
        AFTER the initial drain (when ``live`` flips to True on the first
        ``await my_event.wait()``) omit the flag — they are real deltas.
        This is append-only under §3.1 (consumers ignoring the key keep
        their behaviour unchanged); the App Builder route is the only
        current consumer and it forwards the dict verbatim to SSE bytes.
        """
        # R-6 / D7 — this subscriber's own wake-up event. Registered in
        # the entry so ``publish`` / ``mark_terminal`` wake it; only this
        # coroutine ever ``clear()``s it, so concurrent subscribers can
        # no longer swallow each other's wake-ups. Always discarded in
        # ``finally`` so the set stays bounded by live subscribers.
        my_event = asyncio.Event()
        entry.subscribers.add(my_event)
        try:
            cursor = 0
            live = False
            while True:
                # Yield any new frames since the last cursor (backfill +
                # live).
                while cursor < len(entry.frames):
                    frame = entry.frames[cursor]
                    cursor += 1
                    payload: dict[str, Any] = {
                        "sequence": frame.sequence,
                        "payload": frame.payload,
                    }
                    if not live:
                        # Buffered-at-attach frames are backfill (transcript
                        # the late subscriber missed). Tag so consumers that
                        # pre-rendered via a snapshot can suppress逐 frame
                        # UI commits — see chat/sub-agent broadcasters.
                        payload["backfill"] = True
                    yield ("frame", payload)

                # Re-poll the repository so ``state`` events mirror Run
                # aggregate transitions (PENDING → RUNNING → STREAMING →
                # terminal). The entry's ``frames`` track the runner's
                # NDJSON v3.1 events; the repository tracks the Run state
                # machine — both are needed for parity with the legacy
                # contract.
                fresh = await run_repository.get(run_id)
                if fresh.status != current_status:
                    current_status = fresh.status
                    yield (
                        "state",
                        {
                            "status": current_status.value,
                            "run_id": str(fresh.id),
                            "ts": _now_iso(),
                        },
                    )

                # Terminal? Emit the closing frame and exit.
                if entry.done and cursor >= len(entry.frames):
                    if entry.failed or fresh.status == RunStatus.FAILED:
                        error_message = (
                            entry.error_message
                            or fresh.error_message
                            or "run failed"
                        )
                        # PR-F1 (F-15) — append the runner's structured
                        # ``error_code`` to ``details``. Prefer the
                        # entry's value (drainer-level cache) and fall
                        # back to the fresh repo aggregate (which the use
                        # case set when promoting the run to FAILED on an
                        # ``error`` frame or a runner exception).
                        error_code = entry.error_code or fresh.error_code
                        details: dict[str, Any] = {"run_id": str(fresh.id)}
                        if error_code is not None:
                            details["error_code"] = error_code
                        yield (
                            "error",
                            {
                                "type": "DomainError",
                                "code": "app_builder.run_failed",
                                "message": error_message,
                                "details": details,
                            },
                        )
                    else:
                        # ``cancelled`` is reflected in ``fresh.status``;
                        # ``completed`` is the normal happy path.
                        yield (
                            "done",
                            {
                                "status": fresh.status.value,
                                "run_id": str(fresh.id),
                            },
                        )
                    return

                # Wait for the next frame OR the terminal flag.
                # ``my_event`` is woken by :meth:`publish` AND
                # :meth:`mark_terminal` (which set every subscriber's
                # event). The timeout is a safety belt so a stalled
                # drainer cannot wedge the SSE stream — on each tick we
                # re-poll the repo so ``state`` transitions still surface.
                # First entry into ``await``: from here on every frame is
                # LIVE (no longer tagged ``backfill: true``).
                live = True
                try:
                    await asyncio.wait_for(my_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                finally:
                    # Clear so the next ``wait`` blocks until a new frame
                    # / terminal mark arrives. Any frames published
                    # between ``clear()`` and the next loop iteration are
                    # still picked up via ``cursor < len(entry.frames)``.
                    my_event.clear()
        finally:
            entry.subscribers.discard(my_event)
