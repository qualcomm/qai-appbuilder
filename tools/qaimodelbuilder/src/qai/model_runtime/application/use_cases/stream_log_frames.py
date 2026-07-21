# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``StreamLogFramesUseCase`` — structured log frames for the SSE endpoint.

The live ``GET /api/service/{service_id}/logs/sse`` endpoint previously
implemented its own 0.5s polling loop, buffer-diff and roll/replay state
machine inside the route module. That orchestration belongs in the
application layer: this use case reuses the adapter's
:meth:`InferenceServicePort.stream_logs` (which already does
buffered-then-live streaming with monotonic sequencing) and decorates each
line with the wire metadata the SSE frame needs (``seq`` / ``ts`` /
``service_id``).

The interfaces layer is then a thin encoder: it maps each
:class:`LogFrame` onto an ``event: log`` SSE frame and emits a terminal
``event: end`` frame when the stream completes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from qai.model_runtime.application.ports import InferenceServicePort
from qai.platform.time import utcnow as _utcnow

# Hard cap on stream duration so a forgotten browser tab does not pin a
# worker forever. Twelve hours mirrors the legacy default.
_MAX_STREAM_S = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class LogFrame:
    """A single log line plus its wire metadata."""

    service_id: str
    line: str
    seq: int
    ts: str


class StreamLogFramesUseCase:
    """Yield structured :class:`LogFrame`s for the live SSE log stream."""

    def __init__(
        self,
        *,
        service: InferenceServicePort,
        max_duration_s: float = _MAX_STREAM_S,
    ) -> None:
        self._service = service
        self._max_duration_s = max_duration_s

    async def execute(
        self, *, service_id: str, skip: int = 0
    ) -> AsyncIterator[LogFrame]:
        """Stream log frames for *service_id*.

        Reuses the adapter's :meth:`stream_logs` (buffered then live) so the
        polling / diff / roll-replay logic stays in one place (the adapter),
        not duplicated in the route. Each yielded line becomes one
        :class:`LogFrame` with a monotonically increasing ``seq`` (1-based)
        and an ISO-8601 ``ts``.
        """
        seq = 0
        started = asyncio.get_running_loop().time()
        async for line in self._service.stream_logs(skip=skip):
            now = asyncio.get_running_loop().time()
            if now - started > self._max_duration_s:
                break
            seq += 1
            yield LogFrame(
                service_id=service_id,
                line=line,
                seq=seq,
                ts=_utcnow().isoformat(),
            )


__all__ = ["LogFrame", "StreamLogFramesUseCase"]
