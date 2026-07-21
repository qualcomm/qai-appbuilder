# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: stream messages from a running :class:`CodingSession`.

The use case is an async generator — callers ``async for frame in
use_case.execute(...)`` to receive frames as the provider produces
them.  On each frame:

1. The aggregate's state machine is advanced via
   :meth:`CodingSession.record_stream_frame`.
2. The frame is yielded to the caller.
3. Pending domain events are flushed to the :class:`EventBus`.

The use case takes care of moving the session into ``STREAMING`` on
the first frame and back to ``ACTIVE`` when the iterator drains.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

from qai.ai_coding.application.ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    CodingSessionStreamFrameEvent,
    CodingStreamFrame,
    PermissionRequestId,
    SessionStatus,
    StreamFrameKind,
    ToolName,
)
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)

# V1 parity (``backend/ai_coding/session_manager.py:307-308,378-386`` +
# ``api_routes.py:815-820``): a stream request that lands on a session
# already mid-turn must yield a clean ``session_busy`` error frame and
# return, never crash the SSE response with a state-machine exception.
SESSION_BUSY_ERROR_CODE = "session_busy"

# 2-H13 (send_message / turn overall timeout).  V1 wrapped each CC/OC
# streaming turn in ``async with asyncio.timeout(message_timeout_seconds)``
# (``session_manager.py:408`` /
# ``_DEFAULT_MESSAGE_TIMEOUT_SECONDS = 3600.0`` at line 98); on expiry the
# session was reset to ``idle`` (retryable) and a ``{"code": "timeout"}``
# error frame was emitted (lines 426-437).  V2 mirrors the default and the
# error code so the WebUI's "处理超时（已等待 N 秒）" surface is unchanged.
DEFAULT_TURN_TIMEOUT_SECONDS: float = 3600.0
TURN_TIMEOUT_ERROR_CODE = "timeout"

# 2-H12 (turn_warning — over-turn-count reminder).  V1
# (``session_manager.py:107-130``) warned the user at an escalating
# threshold sequence — 20, 25, 30, 35, … (start 20, step 5) — so a long
# session is nudged to start fresh.  The algorithm is replicated here as
# an application-layer PURE function (NOT imported from ``qai.chat`` —
# the cross-context isolation contract forbids it); it is the same
# arithmetic V1 used so the user-perceived cadence is identical.
_TURN_WARNING_START = 20
_TURN_WARNING_STEP = 5


def compute_turn_warning_threshold(turn_count: int) -> int:
    """Return the over-turn warning threshold for ``turn_count`` (0 = none).

    2-H12.  Pure replica of V1 ``_compute_turn_warning_threshold``
    (``session_manager.py:111-130``): below the floor (20) returns 0; at
    or above it returns the highest crossed tier (20 / 25 / 30 / …).

    Examples: 19→0, 20→20, 24→20, 25→25, 31→30.
    """
    if turn_count < _TURN_WARNING_START:
        return 0
    steps = (turn_count - _TURN_WARNING_START) // _TURN_WARNING_STEP
    return _TURN_WARNING_START + steps * _TURN_WARNING_STEP


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamCodingSessionCommand:
    """Input for :class:`StreamCodingSessionUseCase`."""

    session_id: CodingSessionId


class StreamCodingSessionUseCase:
    """Application service for streaming a session's frames."""

    def __init__(
        self,
        *,
        provider_port: CodingProviderPort,
        repository: CodingSessionRepositoryPort,
        event_bus: EventBus,
        clock=None,
        turn_timeout_s: float | None = DEFAULT_TURN_TIMEOUT_SECONDS,
        permission_ttl_s: float = 0.0,
    ) -> None:
        self._provider_port = provider_port
        self._repository = repository
        self._event_bus = event_bus
        # Optional clock — when present, a PERMISSION_REQUEST frame is
        # registered on the aggregate (so a subsequent ``decide`` finds
        # the request).  Falls back to a UTC ``datetime.now`` so the DI
        # wiring that omits it keeps working.
        self._clock = clock
        # 2-H13: overall wall-clock budget for one streaming turn.  V1
        # default 3600s (``_DEFAULT_MESSAGE_TIMEOUT_SECONDS``); ``None``
        # or a non-positive value disables the timeout (V1 ``message_
        # timeout_seconds <= 0`` → ``None`` "无超时限制" semantics).
        self._turn_timeout_s = (
            turn_timeout_s
            if (turn_timeout_s is not None and turn_timeout_s > 0)
            else None
        )
        # 2-H14: pending-permission TTL applied at turn start so a stale
        # approval gate (un-answered past the TTL) is auto-rejected and
        # the session unblocked before a new turn streams (V1 reset the
        # session for retry).  0 / negative disables the sweep.
        self._permission_ttl_s = permission_ttl_s

    async def execute(
        self,
        command: StreamCodingSessionCommand,
    ) -> AsyncIterator[CodingStreamFrame]:
        return self._iterate(command)

    async def _iterate(
        self,
        command: StreamCodingSessionCommand,
    ) -> AsyncIterator[CodingStreamFrame]:
        session = await self._repository.get(command.session_id)
        # 2-H14: sweep stale pending permission requests before anything
        # else.  A gate un-answered past the TTL is auto-rejected and the
        # session unblocked (V1 reset for retry), so a session that would
        # otherwise read as PERMISSION_REQUESTED below can stream a new
        # turn instead of bouncing on the busy guard.
        if self._permission_ttl_s and self._permission_ttl_s > 0:
            now = self._clock.now() if self._clock is not None else None
            if now is None:
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)
            expired = session.expire_stale_permissions(
                now=now, ttl_seconds=self._permission_ttl_s
            )
            if expired:
                await self._repository.save(session)
                await self._drain_and_publish(session)
        # V1 parity (``session_manager.py:307-308,378-386``): if a turn
        # is already in flight (the session is STREAMING or paused on a
        # PERMISSION_REQUESTED gate), a second stream request must NOT
        # crash the SSE response by forcing an illegal
        # ``streaming -> streaming`` transition.  Instead surface a clean
        # ``session_busy`` ERROR frame (mirroring V1's ``session_busy``
        # error event / HTTP 409) and return without consuming the
        # provider — the in-flight turn keeps running untouched.  The
        # domain state machine stays strict (no STREAMING->STREAMING
        # short-circuit); busy semantics live in the application layer,
        # the same separation V1 used (lock pre-check in the route /
        # manager, not the aggregate).
        if session.status in (
            SessionStatus.STREAMING,
            SessionStatus.PERMISSION_REQUESTED,
        ):
            logger.info(
                "ai_coding.stream_coding_session.busy",
                session_id=str(command.session_id),
                status=session.status.value,
            )
            yield CodingStreamFrame(
                kind=StreamFrameKind.ERROR,
                payload={
                    "type": "ai_coding.session_busy",
                    "code": SESSION_BUSY_ERROR_CODE,
                    "message": (
                        "Session is busy, please wait for the current "
                        "message to complete."
                    ),
                    "details": {
                        "session_id": str(command.session_id),
                        "status": session.status.value,
                        "retryable": False,
                    },
                },
                sequence=session.last_stream_sequence + 1,
            )
            yield CodingStreamFrame(
                kind=StreamFrameKind.END,
                payload={},
                sequence=session.last_stream_sequence + 2,
            )
            return
        # V1 parity (``backend/ai_coding/session_manager.py:2138-2140 +
        # 2401-2416``): every CC/OC streaming turn was timed via
        # ``_time.monotonic()`` and the elapsed seconds were emitted on
        # the ``done`` frame as ``duration_s`` (rounded to 1 decimal).
        # We do the same here, before the ``mark_streaming`` save, so a
        # provider that raises immediately still records the elapsed
        # time spent attempting the turn.
        started_ns = self._monotonic_ns()
        # Move the aggregate to STREAMING before consuming the provider
        # iterator; if the provider raises immediately we still flush
        # the status-change event so subscribers see the attempt.
        session.mark_streaming()
        await self._repository.save(session)
        await self._drain_and_publish(session)

        try:
            iterator = await self._provider_port.stream(
                session_id=command.session_id,
            )
            # 2-H13: enforce an overall turn timeout.  V1 wrapped the
            # whole turn in ``async with asyncio.timeout(...)``; here we
            # drive the provider iterator manually so the deadline spans
            # the entire turn (all frames), and on expiry we emit a
            # ``timeout`` ERROR + END and stop consuming — the ``finally``
            # below flips the session back to ACTIVE (V1 reset to idle →
            # retryable).  ``None`` budget disables the timeout.
            deadline = (
                asyncio.get_event_loop().time() + self._turn_timeout_s
                if self._turn_timeout_s is not None
                else None
            )
            ait = iterator.__aiter__()
            while True:
                try:
                    if deadline is not None:
                        remaining = deadline - asyncio.get_event_loop().time()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        frame = await asyncio.wait_for(
                            ait.__anext__(), timeout=remaining
                        )
                    else:
                        frame = await ait.__anext__()
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    timeout_s = self._turn_timeout_s or 0.0
                    logger.warning(
                        "ai_coding.stream_coding_session.turn_timeout",
                        session_id=str(command.session_id),
                        timeout_s=timeout_s,
                    )
                    yield CodingStreamFrame(
                        kind=StreamFrameKind.ERROR,
                        payload={
                            "type": "ai_coding.turn_timeout",
                            "code": TURN_TIMEOUT_ERROR_CODE,
                            "message": (
                                f"处理超时（已等待 {timeout_s:.0f} 秒）。"
                                "会话已重置，可重新发送消息。"
                            ),
                            "details": {
                                "session_id": str(command.session_id),
                                "timeout_s": timeout_s,
                                "retryable": True,
                            },
                        },
                        sequence=session.last_stream_sequence + 1,
                    )
                    yield CodingStreamFrame(
                        kind=StreamFrameKind.END,
                        payload={},
                        sequence=session.last_stream_sequence + 2,
                    )
                    break
                # 缺陷修复（frame sequence not strictly increasing）：
                # the provider's ``_next_sequence`` counter is per-provider-
                # instance and starts at 0 for any session NOT already in its
                # in-memory map (``base.py:751-755``), while the aggregate's
                # ``last_stream_sequence`` is PERSISTED + monotonic + never
                # reset (``entities.py:241,452-480``).  So re-streaming a
                # session that already accumulated ``last_stream_sequence >= 0``
                # (daemon restart / ``/cc <n>`` 切回旧会话 / WebUI 重开旧会话)
                # delivered a provider first-frame ``sequence=0`` <= persisted
                # ``last`` → ``record_stream_frame`` raised
                # ``not strictly increasing``.  We RE-NUMBER every provider
                # frame off the aggregate's running ``last_stream_sequence``
                # (use-case side; provider untouched) so each frame is the
                # next monotonic value: first frame → ``last+1``,
                # ``record_stream_frame`` then advances ``last`` to that value,
                # the next frame becomes ``last+1`` again, etc.  For a fresh
                # session (``last == -1``) the first frame becomes 0 — exactly
                # what the provider produced before, so new-session streaming
                # is unchanged.  ``CodingStreamFrame`` is frozen, hence
                # ``dataclasses.replace``.  The synthesised error / turn_warning
                # / timeout frames below already base off
                # ``session.last_stream_sequence + 1/+2`` and stay correct
                # because ``last`` is kept in lockstep here.
                frame = replace(
                    frame, sequence=session.last_stream_sequence + 1
                )
                session.record_stream_frame(frame)
                # U-010 / 2-H2 (token + context full chain): fold any
                # provider ``usage`` payload into the aggregate's
                # cumulative counters before persisting.  V1 parity
                # (``session_manager.py:2108-2135`` /
                # ``opencode_session_manager.py:744,928``): every CC/OC
                # turn surfaced ``input_tokens`` / ``output_tokens`` /
                # ``context_window`` (and OC ``cost``) which the manager
                # accumulated onto the session.  In V2 the provider may
                # attach a ``usage`` dict on any frame's payload (typically
                # the terminal frame); we read it here so the write-back
                # is provider-agnostic and stays in the application layer
                # (the domain mutator enforces the accumulate / replace
                # rules).  A TOOL_CALL frame also bumps the tool-call
                # counter (V1 ``total_tool_calls`` per ToolUseBlock).
                self._record_frame_usage(session, frame)
                await self._repository.save(session)
                await self._drain_and_publish(session)
                yield frame
                # 2-H12: a terminal END frame marks one completed turn.
                # Bump the turn counter and, when a fresh over-turn
                # threshold tier (20 / 25 / 30 / …) is reached, emit a
                # ``turn_warning`` frame right after the END — mirroring
                # V1 ``session_manager.py:2141-2155`` which yielded the
                # warning after the ``done`` frame.  The warning rides a
                # TEXT-kind frame carrying a ``turn_warning`` payload (the
                # 13-enum SSE frame contract is preserved — §3.1 — no new
                # StreamFrameKind).  The channel-side ``turn_warning_sync``
                # bridge wiring belongs to P2-B (not touched here).
                if frame.kind is StreamFrameKind.END:
                    threshold = compute_turn_warning_threshold(
                        session.turn_count + 1
                    )
                    should_warn = session.record_turn_completed(
                        threshold=threshold
                    )
                    # RE-OC-7: persist the OpenCode-native message ids the
                    # adapter learned this turn so a later revert / rewind
                    # can call OpenCode's native revert by ``messageID``
                    # even after a daemon restart (V1 parity).  OC-only +
                    # best-effort (CC / stubs lack the hook → no-op).
                    self._persist_oc_message_ids(session)
                    await self._repository.save(session)
                    await self._drain_and_publish(session)
                    if should_warn:
                        yield CodingStreamFrame(
                            kind=StreamFrameKind.TEXT,
                            payload={
                                "turn_warning": {
                                    "turn_count": session.turn_count,
                                    "threshold": threshold,
                                    "message": (
                                        f"⚠️ 当前会话已达到 "
                                        f"{session.turn_count} 轮对话。\n"
                                        "为避免上下文过长影响回复质量，"
                                        "建议尽快创建新会话。"
                                    ),
                                }
                            },
                            sequence=session.last_stream_sequence + 1,
                        )
                    continue
                # A PERMISSION_REQUEST frame pauses the session: register
                # the request on the aggregate so a later
                # ``POST /permissions/{request_id}/decide`` resolves it,
                # then stop consuming the provider iterator (the upstream
                # stream is blocked awaiting the operator's decision).
                if frame.kind is StreamFrameKind.PERMISSION_REQUEST:
                    self._register_permission(session, frame)
                    await self._repository.save(session)
                    await self._drain_and_publish(session)
                    break
        finally:
            # Always return to ACTIVE so the next user message lands on
            # a session in the right state, even when the provider
            # raises mid-stream or the consumer cancels. Also stamp the
            # turn duration on the aggregate for V1 parity.
            try:
                duration_s = (self._monotonic_ns() - started_ns) / 1_000_000_000
                session.record_stream_duration(duration_s)
                session.mark_active()
                await self._repository.save(session)
                await self._drain_and_publish(session)
            except Exception as exc:  # noqa: BLE001 — finally must not mask original
                logger.warning(
                    "ai_coding.stream_coding_session.cleanup_failed",
                    session_id=str(command.session_id),
                    error=repr(exc),
                )

    def _persist_oc_message_ids(self, session) -> None:  # noqa: ANN001
        """Copy the OpenCode adapter's learned message ids onto the aggregate.

        RE-OC-7 (restart-safe native revert).  The OpenCode adapter caches
        each turn's native ``messageID`` in-process; this lifts the ordered
        list onto the aggregate (persisted via migration 033) so a later
        :class:`RevertMessageUseCase` / rewind can resolve a marker index to
        the right native id even after a daemon restart.  Best-effort: the
        provider hook is duck-typed (CC / stubs lack it → no-op) and a
        failure never aborts the turn's END handling.
        """
        getter = getattr(self._provider_port, "get_oc_message_ids", None)
        if not callable(getter):
            return
        try:
            ids = getter(session.session_id)
        except Exception:  # noqa: BLE001 — never break the turn.
            return
        if ids:
            session.record_oc_message_ids(ids)

    def _monotonic_ns(self) -> int:
        """Read a monotonic timestamp in ns from the injected clock.

        Falls back to ``time.monotonic_ns`` when the clock has no
        ``monotonic_ns`` method (older test fakes); this preserves
        determinism for production while keeping unit tests that pass
        a minimal clock fake working unchanged.
        """
        clock = self._clock
        if clock is not None:
            mono = getattr(clock, "monotonic_ns", None)
            if callable(mono):
                return int(mono())
        import time as _time

        return _time.monotonic_ns()

    async def _drain_and_publish(self, session) -> None:
        """Publish a session's pending domain events, except per-frame ones.

        ``CodingSession.record_stream_frame`` appends one
        :class:`CodingSessionStreamFrameEvent` PER provider frame (per token /
        tool chunk) — the AI Coding equivalent of chat's per-token event. Those
        per-frame events are NOT published to the in-process event bus: no
        production subscriber consumes them. The coding stream reaches the
        front-end over the dedicated per-message / per-session SSE (which
        consumes this use case's async iterator directly); channel mirroring
        and audit do not subscribe to per-frame events either. The only bus
        subscriber that ever matched ``ai_coding.emitted_stream_frame`` was the
        global ``/api/events`` notification SSE, which dropped it — so
        forwarding per frame only floods that bounded queue (the historical
        ``events.backpressure`` log-spam).

        All OTHER drained events (session lifecycle, permission requested /
        decided, tool invoked / completed / failed, status changes, ...) are
        low-frequency notifications and ARE published normally — this is why we
        filter per event type here rather than skipping ``drain_events()``
        wholesale.
        """
        for event in session.drain_events():
            if isinstance(event, CodingSessionStreamFrameEvent):
                continue
            await self._event_bus.publish(event)

    @staticmethod
    def _record_frame_usage(session, frame: CodingStreamFrame) -> None:
        """Fold a frame's provider ``usage`` payload into the aggregate.

        U-010 / 2-H2.  Two independent signals are harvested:

        * a ``TOOL_CALL`` frame increments ``total_tool_calls`` by one
          (V1 counted one per ``ToolUseBlock``);
        * a ``usage`` dict on the frame payload (any frame, typically the
          terminal one) supplies token / context-window / cost counters.

        The ``usage`` dict is normalised to the V1 field names with the
        common Anthropic / OpenAI aliases collapsed:

        * input  = ``input_tokens`` | ``prompt_tokens`` (the provider is
          expected to have already folded cache-read / cache-write into
          this number — V1 ``chat_handler.py:1566-1579`` did the same);
        * output = ``output_tokens`` | ``completion_tokens``;
        * context window = ``context_window`` | ``contextWindow``;
        * cost = ``cost`` | ``total_cost``.

        Defensive: a missing / malformed ``usage`` value is silently
        ignored (telemetry must never abort a turn).  The domain mutator
        enforces accumulate-vs-replace semantics and clamping.
        """
        payload = frame.payload or {}
        tool_calls = 1 if frame.kind is StreamFrameKind.TOOL_CALL else 0

        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            # Still record a bare tool-call bump even without a usage dict.
            if tool_calls:
                session.record_token_usage(tool_calls=tool_calls)
            return

        def _as_int(*keys: str) -> int:
            for key in keys:
                val = usage.get(key)
                if isinstance(val, (int, float)):
                    return int(val)
            return 0

        def _as_float(*keys: str) -> float:
            for key in keys:
                val = usage.get(key)
                if isinstance(val, (int, float)):
                    return float(val)
            return 0.0

        input_tokens = _as_int("input_tokens", "prompt_tokens")
        output_tokens = _as_int("output_tokens", "completion_tokens")
        context_window_val = _as_int("context_window", "contextWindow")
        cost = _as_float("cost", "total_cost")
        session.record_token_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            context_window=context_window_val or None,
            cost=cost,
        )

    def _register_permission(self, session, frame: CodingStreamFrame) -> None:
        """Register a PERMISSION_REQUEST frame's request on the aggregate.

        Idempotent: a request id already present (e.g. when the provider
        re-emits the frame on a resume) is skipped so we never raise a
        duplicate-request error.  Missing / blank request ids are
        ignored — the frame still reached the client for display.
        """
        payload = frame.payload or {}
        raw_id = str(payload.get("request_id") or "").strip()
        if not raw_id:
            return
        request_id = PermissionRequestId(value=raw_id)
        if request_id in session.permission_requests:
            return
        from datetime import datetime, timezone

        now = self._clock.now() if self._clock is not None else datetime.now(
            timezone.utc
        )
        session.request_permission(
            request_id=request_id,
            tool_name=ToolName(value=str(payload.get("tool_name") or "unknown")),
            args=dict(payload.get("args") or {}),
            now=now,
        )


__all__ = [
    "StreamCodingSessionCommand",
    "StreamCodingSessionUseCase",
    "compute_turn_warning_threshold",
]
