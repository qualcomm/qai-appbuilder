# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared Server-Sent-Events (SSE) helpers for the HTTP interface layer.

Lifted out of :mod:`interfaces.http.routes.model_catalog` once a second
context (App Builder weight downloads) needed the identical
``DownloadProgress`` → SSE translation. The model_catalog route's private
``_format_sse_event`` / ``_stream_progress_frames`` helpers carried an
explicit cross-PR note (S3-spec §10) to promote them here as soon as
``>= 2`` contexts require them — this module is that promotion.

Both the ``model_catalog`` download-progress route and the App Builder
``/weights/download/{job_id}/progress`` route now import these two public
functions so the SSE wire contract (frame names + payload keys) is
defined in exactly one place and cannot drift between contexts.

Wire contract (frozen — S3-spec §4.4)
-------------------------------------
* ``event: progress\\ndata: <progress-json>\\n\\n`` — one frame per
  :class:`~qai.platform.download.DownloadProgress` snapshot. Payload keys:
  ``bytes_downloaded`` / ``total_bytes`` / ``speed_bps`` / ``eta_seconds``
  / ``percent`` / ``is_complete``.
* ``event: done\\ndata: {}\\n\\n`` — end-of-stream (clean completion).
* ``event: error\\ndata: <QaiError.to_dict()>\\n\\n`` — sent before
  closing when the use case raises a :class:`qai.platform.errors.QaiError`
  mid-stream; the stream closes immediately afterwards.

All frames are UTF-8 bytes; ``data:`` is a single line (no embedded
newlines). Heartbeats are NOT emitted here — the use case is the source
of truth for stream cadence.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from qai.platform.errors import QaiError

__all__ = [
    "format_sse_event",
    "stream_progress_frames",
]


def format_sse_event(name: str, data: dict[str, Any]) -> bytes:
    """Encode a single SSE frame as UTF-8 bytes.

    Output shape::

        event: <name>\\ndata: <json>\\n\\n

    ``data`` is serialised with ``json.dumps`` (no trailing newline, no
    pretty-printing) so the whole frame fits on three logical lines per
    the SSE spec.
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {payload}\n\n".encode("utf-8")


async def stream_progress_frames(
    iterator: AsyncIterator[Any],
) -> AsyncIterator[bytes]:
    """Translate a use-case progress iterator to SSE byte frames.

    Translates each ``DownloadProgress`` snapshot to ``event: progress``,
    emits ``event: done`` at end-of-stream, and ``event: error`` for any
    :class:`qai.platform.errors.QaiError` raised mid-stream. Non-``QaiError``
    exceptions propagate so the global error handler can produce a 500
    envelope on the pre-stream HTTP frame (NB: once the response body has
    begun, the server can no longer change the status code; we surface the
    error via an SSE ``error`` frame instead).
    """
    try:
        async for snapshot in iterator:
            payload = {
                "bytes_downloaded": snapshot.bytes_downloaded,
                "total_bytes": snapshot.total_bytes,
                "speed_bps": snapshot.speed_bps,
                "eta_seconds": snapshot.eta_seconds,
                "percent": snapshot.percent,
                "is_complete": snapshot.is_complete,
            }
            yield format_sse_event("progress", payload)
    except QaiError as exc:
        yield format_sse_event("error", exc.to_dict())
        return
    yield format_sse_event("done", {})
