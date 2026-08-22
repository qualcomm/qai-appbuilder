# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared LLM-stream guard helpers (abortable wrap + network auto-retry).

These two coroutines were originally private methods on
:class:`StreamChatUseCase` (``_abortable_frames`` /
``_network_retrying_stream``). The sub-agent loop in
:mod:`qai.chat.adapters.agent_tool` previously opened
``self._llm.stream(...)`` directly, so a user *Stop* could not interrupt
a half-open / wedged cloud SSE and a transient network glitch crashed
the whole sub-agent run instead of auto-retrying (report C-8 / P4).

This module is the single source of truth both paths now share —
:class:`StreamChatUseCase` delegates to these helpers (keeping its
public method names as thin shims to preserve the existing call sites /
docstrings); :class:`AgentToolHandler` wraps its per-round
``self._llm.stream(...)`` with the same two helpers when wired with the
required dependencies. AGENTS.md "复用 > 重造".

Design notes
------------

* **Free functions, not methods.** Both helpers take their dependencies
  (retry policy, stall budget, sleep callable, frame factory, error
  classifier) as explicit parameters so they have ZERO ``self``
  coupling and can be called from any layer that can satisfy the
  signature. The application use case binds them with its members; the
  adapter binds them with the same dependencies passed through the
  constructor.

* **Behaviour preserved byte-for-byte.** ``_abortable_frames`` keeps
  the same 50 ms poll cadence + per-frame stall budget + dangling
  ``__anext__`` cancellation. ``_network_retrying_stream`` keeps the
  first-frame inspection seam, the abortable backoff sleep, the
  policy-controlled escalating delay, and the transient
  ``network_retry`` progress frame emission.

* **Optional dependencies, no-op fallback.**
  ``_network_retrying_stream`` returns a transparent pass-through when
  ``retry_policy`` is ``None`` (matches the existing legacy / unit-stub
  path). ``_abortable_frames`` requires a handle but otherwise has no
  optional dependency.

Imports
-------

This module sits in ``qai.chat.application.use_cases`` and may not
import any adapter or interface module (Clean Architecture). All
dependencies are passed in.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from qai.chat.application.ports import (
    RetryCategory,
    RetryPolicyPort,
    StreamAbortHandle,
)
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.platform.logging import get_logger


__all__ = [
    "abortable_frames",
    "is_meaningful_stream_frame",
    "network_retrying_stream",
]


_log = get_logger(__name__)


def is_meaningful_stream_frame(frame: StreamFrame) -> bool:
    """True when *frame* carries real progress (resets the stall watchdog).

    Mirrors ``streaming._is_meaningful_stream_frame``: a CHUNK with
    non-blank text, a tool-call / tool-result frame, or any sub-agent
    progress frame. ERROR / END are terminal (the loop ends regardless),
    and a CHUNK whose text is blank (whitespace-only keep-alive) is NOT
    meaningful — otherwise a half-open upstream emitting only blank
    keep-alives would keep the stall budget pinned at zero forever.
    """
    ft = frame.frame_type
    if ft is StreamFrameType.CHUNK:
        text = frame.payload.get("text", "")
        return isinstance(text, str) and bool(text.strip())
    return ft in (
        StreamFrameType.TOOL_CALL,
        StreamFrameType.TOOL_RESULT,
        StreamFrameType.SUBAGENT_START,
        StreamFrameType.SUBAGENT_OUTPUT,
        StreamFrameType.SUBAGENT_TOOL,
        StreamFrameType.SUBAGENT_TOOL_RESULT,
        StreamFrameType.SUBAGENT_DONE,
        StreamFrameType.AGENT_SUMMARY,
    )


async def abortable_frames(
    stream_frames: AsyncIterator[StreamFrame],
    handle: StreamAbortHandle,
    *,
    frame_stall_budget_s: float = 600.0,
    poll_seconds: float = 0.05,
) -> AsyncIterator[StreamFrame]:
    """Yield from *stream_frames* but stop promptly when *handle* fires.

    Two failsafes:

    * **Cooperative abort** — each ``__anext__`` is raced against a tiny
      poll window (``poll_seconds``); if the handle is set while we are
      blocked on the upstream the wrapper abandons the wait and stops
      iteration so the caller's abort tail releases the tab. The
      dangling ``__anext__`` is cancelled in the ``finally``; the
      upstream HTTP stream is closed by the use case's surrounding
      ``async with`` on unwind.
    * **Inter-frame stall budget** — if no meaningful frame arrives
      within ``frame_stall_budget_s``, the wrapper bails the same way a
      user Stop would. Guards against a wedged upstream that holds the
      socket open with keep-alive bytes but never advances the
      application state (the "无输出卡死" failure mode). Set to ``0`` to
      disable (legacy callers); the default is generous enough that a
      slow-but-alive cloud turn is never cut off.
    """
    _since_last_frame = 0.0
    iterator = stream_frames.__aiter__()
    pending: asyncio.Task[StreamFrame] | None = None
    try:
        while True:
            if handle.is_set():
                return
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=poll_seconds)
            if not done:
                # Still blocked — accumulate stall time, re-check abort, keep
                # the SAME pending ``__anext__`` task alive so a mid-flight
                # frame is never dropped.
                _since_last_frame += poll_seconds
                if (
                    frame_stall_budget_s > 0
                    and _since_last_frame >= frame_stall_budget_s
                ):
                    _log.warning(
                        "chat.streaming.frame_stream_stalled",
                        stall_seconds=_since_last_frame,
                    )
                    return
                continue
            task = pending
            pending = None
            try:
                frame = task.result()
            except StopAsyncIteration:
                return
            if is_meaningful_stream_frame(frame):
                _since_last_frame = 0.0
            yield frame
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


async def _abortable_sleep(
    seconds: float,
    handle: StreamAbortHandle | None,
    *,
    sleep: Callable[[float], Awaitable[None]],
    slice_seconds: float = 0.1,
) -> bool:
    """Sleep up to *seconds*; return early on *handle* (returns True if aborted).

    Mirrors the use case's private ``_abortable_sleep``. Slice-poll because
    :class:`StreamAbortHandle` exposes only ``is_set()`` (no awaitable Event).

    Also honours a "retry now" request (the "立即重试" button): when the
    handle's ``consume_retry_now`` fires, the wait ends early and returns
    ``False`` (NOT aborted) so the caller re-opens the stream immediately —
    distinct from an abort, which returns ``True`` and stops the turn. Handles
    predating retry-now are probed via ``getattr`` (legacy-safe).
    """
    if handle is None:
        await sleep(seconds)
        return False
    _consume_retry = getattr(handle, "consume_retry_now", None)
    waited = 0.0
    while waited < seconds:
        if handle.is_set():
            return True
        if _consume_retry is not None and _consume_retry():
            # User clicked "立即重试" — stop waiting and re-open now (continue).
            return False
        await sleep(min(slice_seconds, seconds - waited))
        waited += slice_seconds
    return handle.is_set()


async def network_retrying_stream(
    open_stream: Callable[[], AsyncIterator[StreamFrame]],
    handle: StreamAbortHandle | None,
    *,
    retry_policy: RetryPolicyPort | None,
    classify_error_frame: Callable[[StreamFrame], RetryCategory | None],
    build_retry_frame: Callable[[int, float, Any], StreamFrame],
    sleep: Callable[[float], Awaitable[None]],
    scope: str = "followup_round",
) -> AsyncIterator[StreamFrame]:
    """Wrap *open_stream* with connectivity-error auto-retry.

    First-frame inspection seam: when the first frame is a retryable
    connectivity ERROR the round had not streamed any content yet, so it
    is safe to sleep (abortable backoff) and re-open without duplicating
    output. Any other first frame (normal content, terminal error, empty
    stream) is forwarded verbatim.

    Three retryable categories are handled here, all sharing the abortable
    backoff + ``network_retry`` progress banner:

    * :data:`RetryCategory.NETWORK` (``connect_error`` / ``timeout`` /
      ``read_error``) — escalating backoff, bounded by a WALL-CLOCK budget
      (``elapsed_s`` passed to the policy); once the policy returns
      ``should_retry=False`` the terminal error frame is forwarded.
    * :data:`RetryCategory.BOUNDED_FAST` (``dns_error`` /
      ``connection_refused`` / ``host_unreachable``) — a few fast attempts
      then terminal.
    * :data:`RetryCategory.BOUNDED_SERVER` (``server_error`` 5xx) — a few
      jittered attempts then terminal.

    When *retry_policy* is None this is a transparent pass-through
    (matches the use case's legacy fallback). *build_retry_frame* is
    called once per backoff to surface a transient ``network_retry``
    progress frame so the UI shows the "网络中断，正在等待恢复…" banner
    and the WS gets a positive keep-alive.

    *sleep* is the awaitable sleeper (the use case's clock-injected
    sleeper; the adapter's ``asyncio.sleep``) used inside
    :func:`_abortable_sleep`.
    """
    if retry_policy is None:
        async for frame in open_stream():
            yield frame
        return
    # Per-category attempt counters. NETWORK is bounded by wall-clock time
    # (``network_started_monotonic``), the two BOUNDED_* by attempt count in
    # the policy.
    attempt_by_category: dict[RetryCategory, int] = {
        RetryCategory.NETWORK: 0,
        RetryCategory.BOUNDED_FAST: 0,
        RetryCategory.BOUNDED_SERVER: 0,
    }
    network_started_monotonic: float | None = None
    _RETRYABLE = (
        RetryCategory.NETWORK,
        RetryCategory.BOUNDED_FAST,
        RetryCategory.BOUNDED_SERVER,
    )
    while True:
        if handle is not None and handle.is_set():
            return
        stream_iter = open_stream().__aiter__()
        try:
            first_frame = await stream_iter.__anext__()
        except StopAsyncIteration:
            return
        category = classify_error_frame(first_frame)
        if category not in _RETRYABLE:
            # Normal content / terminal (non-retryable) error: forward as-is.
            yield first_frame
            async for f in stream_iter:
                yield f
            return
        # Retryable connectivity error before any content — back off + re-open.
        attempt_by_category[category] += 1
        attempt_number = attempt_by_category[category]
        # Log the actual error content so the cause is diagnosable from
        # the log alone (previously the error frame was silently swallowed
        # and only the retry attempt number was logged, making SSL / connect
        # failures invisible).
        _log.warning(
            "chat.stream_retry.error_detail",
            attempt=attempt_number,
            category=category.value,
            code=first_frame.payload.get("code") if hasattr(first_frame, "payload") else None,
            message=first_frame.payload.get("message") if hasattr(first_frame, "payload") else str(first_frame),
            scope=scope,
        )
        # NETWORK: enforce a wall-clock budget by passing cumulative elapsed
        # time to the policy (which terminates once it exceeds the budget).
        elapsed_s: float | None = None
        if category is RetryCategory.NETWORK:
            if network_started_monotonic is None:
                network_started_monotonic = time.monotonic()
                elapsed_s = 0.0
            else:
                elapsed_s = max(
                    0.0, time.monotonic() - network_started_monotonic
                )
        decision = retry_policy.next_attempt(
            category=category,
            attempt_number=attempt_number,
            elapsed_s=elapsed_s,
        )
        _log.info(
            "chat.stream_retry",
            category=category.value,
            attempt=decision.attempt_number,
            delay_seconds=decision.delay_seconds,
            scope=scope,
        )
        if not decision.should_retry:
            yield first_frame
            async for f in stream_iter:
                yield f
            return
        yield build_retry_frame(
            decision.attempt_number,
            decision.delay_seconds,
            first_frame.payload.get("code"),
        )
        if decision.delay_seconds > 0:
            aborted = await _abortable_sleep(
                decision.delay_seconds, handle, sleep=sleep,
            )
            if aborted:
                return
        # Loop continues — re-open the round stream (same request/wire).
