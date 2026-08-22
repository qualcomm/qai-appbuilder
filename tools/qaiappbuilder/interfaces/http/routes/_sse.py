# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Tiny SSE frame helpers shared by streaming routes.

V1 download-center wire format: each frame is ``data: <json>\\n\\n`` and
the stream terminates with ``data: [DONE]\\n\\n`` (NOT the named-event
format used by ``model_catalog``'s per-job progress stream). The frontend
``useDownloadCenter`` parser (ported 1:1 from V1) splits on ``\\n`` and
reads ``data: `` lines, treating ``[DONE]`` as end-of-stream.
"""

from __future__ import annotations

import json
from typing import Any


def sse_data(payload: dict[str, Any]) -> bytes:
    """Encode one ``data: <json>\\n\\n`` frame as UTF-8 bytes."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {body}\n\n".encode("utf-8")


def sse_done() -> bytes:
    """Terminal ``data: [DONE]\\n\\n`` frame."""
    return b"data: [DONE]\n\n"


__all__ = ["sse_data", "sse_done"]
