# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Service logs SSE route — live tail of inference daemon stdout/stderr.

PR-095 / S9 audit §3.3 A-26.  The legacy
``backend/service_manager.py`` (lines 264-321) exposed a long-lived
``stream_logs`` endpoint so the frontend "Service" panel could
display the GenieAPIService output in real time.  After the L6
refactor (PR-604) only the snapshot route ``GET /api/service/logs``
remained; this module restores the SSE form.

Wire shape
----------

* URL: ``GET /api/service/{service_id}/logs/sse``
* Response media type: ``text/event-stream``
* Frame format::

      event: log
      data: {"service_id":"...","line":"...","seq":N,"ts":"2026-05-31T01:23:45.678Z"}

  followed by a terminal ``event: end`` frame when the stream is
  cancelled / the daemon stops.

The polling / buffer-diff / roll-replay orchestration that used to live
here has been pushed down to the ``model_runtime`` application layer
(:class:`qai.model_runtime.application.use_cases.StreamLogFramesUseCase`,
which reuses the adapter's monotonically-sequenced ``stream_logs``). This
route is now a thin encoder: it maps each :class:`LogFrame` onto an
``event: log`` SSE frame and emits a terminal ``event: end`` frame.

Currently the underlying ``model_runtime`` BC manages a single
inference service so ``service_id`` is treated as a free-form opaque
token; future work that supports multiple services may use it as a
real lookup key.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

    from qai.model_runtime.application.use_cases import LogFrame


__all__ = ["build_router"]


def _encode_event(event: str, data: dict[str, object]) -> bytes:
    """Encode an SSE frame as bytes."""
    return (
        f"event: {event}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


def build_router(*, container: "Container") -> APIRouter:
    """Build the service-logs SSE router bound to ``container``."""
    router = APIRouter(prefix="/api/service", tags=["model_runtime"])

    @router.get(
        "/{service_id}/logs/sse",
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def stream_service_logs(service_id: str) -> StreamingResponse:
        use_case = container.model_runtime.stream_log_frames_use_case

        async def event_stream() -> AsyncIterator[bytes]:
            last_seq = 0
            try:
                frame: "LogFrame"
                async for frame in use_case.execute(service_id=service_id):
                    last_seq = frame.seq
                    yield _encode_event(
                        "log",
                        {
                            "service_id": frame.service_id,
                            "line": frame.line,
                            "seq": frame.seq,
                            "ts": frame.ts,
                        },
                    )
            except Exception as exc:  # noqa: BLE001 — defensive
                yield _encode_event(
                    "error",
                    {"message": f"failed to read logs: {exc}"},
                )
            finally:
                # Best-effort terminal frame so well-behaved clients can
                # stop reconnecting. If the connection is already closed,
                # the write is a no-op.
                try:
                    yield _encode_event(
                        "end", {"service_id": service_id, "seq": last_seq}
                    )
                except Exception:  # noqa: BLE001
                    pass

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
