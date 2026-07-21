# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Aggregate a streamed dispatch frame iterator into one final reply (R16).

Background — the HTTP ``POST /api/{kind}/dispatch`` route previously
inlined the business rule for turning the *streamed* outbound frames
produced by the apps-layer dispatch pipeline into the single
``reply_text`` / ``coding_session_id`` pair that the route's response
shape exposes.  That orchestration rule (skip partial markers, stop on
the first ERROR frame, concatenate the remaining text in order) is a
piece of application-level business logic, not transport plumbing, so
it belongs in the application layer rather than the interface layer.

This module hosts that rule as a single pure async function so the
route layer becomes a thin caller.  To keep the Clean-Architecture
``context-isolation`` / layering contracts intact the function never
imports the apps-layer ``OutboundFrame`` concretely — it consumes any
object that *structurally* matches :class:`DispatchFrameLike` (the
``OutboundFrame`` dataclass already does).  The ERROR-kind discriminator
is passed in by the caller so this module stays free of the apps-layer
:class:`OutboundFrameKind` string-constants holder too.

The aggregation rules are intentionally byte-for-byte identical to the
inline route logic they replace (v2.7 §3 immutability — dispatch reply
behaviour is unchanged):

1. The first frame whose ``kind`` equals ``error_kind`` is appended to
   the text and aggregation stops immediately (``break``) — the caller
   surfaces it as the reply so the HTTP caller sees the same UX a
   channel user would.
2. Frames carrying ``("partial", "true")`` metadata are skipped — the
   realtime delivery service has already pushed them to the channel
   user via Layer-1 / Layer-2; the HTTP ``/dispatch`` contract wants
   only the final consolidated text.
3. Any other frame's non-empty ``text`` is concatenated in arrival
   order; the last non-empty ``coding_session_id`` seen wins.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class DispatchFrameLike(Protocol):
    """Structural shape of an outbound dispatch frame.

    The apps-layer ``OutboundFrame`` dataclass satisfies this Protocol
    without importing it here, preserving the layering boundary
    (application code must not depend on ``apps.*``).
    """

    kind: str
    text: str
    coding_session_id: str | None
    metadata: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class AggregatedDispatchReply:
    """Consolidated result of folding a dispatch frame stream.

    Mirrors the two fields the ``POST /api/{kind}/dispatch`` response
    exposes (``reply_text`` / ``coding_session_id``).
    """

    reply_text: str
    coding_session_id: str | None


async def aggregate_dispatch_frames(
    frames: AsyncIterator[DispatchFrameLike],
    *,
    error_kind: str,
) -> AggregatedDispatchReply:
    """Fold a streamed dispatch frame iterator into one final reply.

    See the module docstring for the (immutable) aggregation rules.

    Parameters
    ----------
    frames:
        Async iterator of frame-like objects produced by the apps-layer
        dispatch pipeline.
    error_kind:
        The ``kind`` value that marks a terminal error frame (the caller
        passes the apps-layer ``OutboundFrameKind.ERROR`` constant so
        this module stays decoupled from it).
    """
    text_parts: list[str] = []
    coding_session_id: str | None = None
    async for frame in frames:
        if frame.kind == error_kind:
            # Surface the first error frame as the reply text so the
            # caller sees the same UX a channel user would.
            text_parts.append(frame.text)
            break
        # Skip partial markers — the realtime delivery service has
        # already pushed them via Layer-1/Layer-2 to the channel user.
        # For the HTTP /dispatch contract we want the final consolidated
        # text so callers can verify dispatch worked.
        is_partial = any(
            k == "partial" and v == "true" for k, v in frame.metadata
        )
        if is_partial:
            continue
        if frame.text:
            text_parts.append(frame.text)
        if frame.coding_session_id:
            coding_session_id = frame.coding_session_id
    return AggregatedDispatchReply(
        reply_text="".join(text_parts),
        coding_session_id=coding_session_id,
    )


__all__ = [
    "DispatchFrameLike",
    "AggregatedDispatchReply",
    "aggregate_dispatch_frames",
]
