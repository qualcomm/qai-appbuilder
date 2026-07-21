# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP transport seam + SSE event parser for ai_coding providers (PR-102).

The :class:`HttpTransportPort` lets us inject a fake httpx-style
client into :class:`ClaudeCodeProvider` and :class:`OpenCodeProvider`
without monkey-patching :mod:`httpx` globally.  In production the
default adapter (:class:`HttpxTransport`) calls
:func:`httpx.AsyncClient.stream` exactly as Anthropic / OpenCode
expect it; tests register an :class:`InMemorySseTransport` that
replays canned SSE bytes.

SSE protocol
------------
The wire format consumed by :func:`parse_sse_lines` is the standard
EventSource-style stream used by both Anthropic Messages and the
local OpenCode HTTP server:

::

    event: content_block_delta
    data: {"type":"content_block_delta","delta":{"text":"hi"}}

    event: message_stop
    data: {}

Empty-line separators end an event.  Comments (``: keepalive``) are
ignored.  Multi-line ``data:`` fields are concatenated with ``\n``
(EventSource spec §9.2.6).

The parser is deliberately a pure function over an async iterator of
bytes so the same code path serves both real httpx streaming and the
in-memory test fixture.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "HttpTransportPort",
    "HttpxTransport",
    "InMemorySseTransport",
    "SseEvent",
    "parse_sse_bytes",
]


@dataclass(frozen=True, slots=True)
class SseEvent:
    """A single Server-Sent-Events frame."""

    event: str
    data: dict[str, Any]
    raw_data: str


@runtime_checkable
class HttpTransportPort(Protocol):
    """Minimal streaming-POST interface used by both providers.

    Adapters return an async iterator of raw bytes — the SSE event
    parser sits one layer above so the transport stays format-agnostic.
    """

    async def stream_post(
        self,
        *,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any],
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> AsyncIterator[bytes]:
        """POST ``json_body`` to ``url`` and yield response bytes.

        The implementation must raise :class:`HttpStreamError` (or any
        ``Exception`` subclass) on transport failure; the provider
        wraps that into a :class:`StreamFrameKind.ERROR` frame for
        downstream consumers.
        """
        ...


class HttpStreamError(Exception):
    """Raised by :class:`HttpTransportPort` when the upstream call fails."""


class HttpxTransport:
    """Default :class:`HttpTransportPort` backed by :mod:`httpx`.

    Imports httpx lazily so the test suite can run on machines without
    the package — though it ships in :mod:`pyproject` and ARM64 venv
    so this is mostly defensive.
    """

    __slots__ = ()

    async def stream_post(
        self,
        *,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any],
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> AsyncIterator[bytes]:
        try:
            import httpx  # type: ignore[import-untyped]
        except ImportError as e:
            raise HttpStreamError(
                f"httpx is required for HttpxTransport: {e}"
            ) from e

        timeout = httpx.Timeout(
            connect=connect_timeout_s, read=read_timeout_s, write=10.0, pool=5.0
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=json_body
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise HttpStreamError(
                            f"HTTP {response.status_code}: "
                            f"{body[:512].decode('utf-8', errors='replace')}"
                        )
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as e:
            raise HttpStreamError(f"httpx error: {e}") from e


class InMemorySseTransport:
    """Test transport that replays a pre-baked byte sequence.

    Construct with the SSE wire bytes you want the provider to consume.
    The transport ignores ``url`` / ``headers`` / ``json_body`` and
    yields the bytes in chunks of ``chunk_size`` so tests can exercise
    the parser's multi-chunk re-assembly path.
    """

    __slots__ = ("_chunks", "captured_calls", "chunk_size")

    def __init__(
        self, *, payload: bytes, chunk_size: int = 64
    ) -> None:
        self._chunks: bytes = payload
        self.chunk_size = max(1, chunk_size)
        self.captured_calls: list[dict[str, Any]] = []

    async def stream_post(
        self,
        *,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any],
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> AsyncIterator[bytes]:
        self.captured_calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "json_body": dict(json_body),
                "connect_timeout_s": connect_timeout_s,
                "read_timeout_s": read_timeout_s,
            }
        )
        for i in range(0, len(self._chunks), self.chunk_size):
            yield self._chunks[i : i + self.chunk_size]


# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------


async def parse_sse_bytes(
    byte_stream: AsyncIterator[bytes],
) -> AsyncIterator[SseEvent]:
    """Parse a raw byte stream into :class:`SseEvent` frames.

    Buffers across chunk boundaries; emits one :class:`SseEvent` per
    EventSource event delimited by a blank line.  Lines that don't
    parse as JSON are returned with ``data={}`` and ``raw_data`` set
    to the unparsed text — callers can inspect ``raw_data`` for
    diagnostics.
    """
    buffer = b""
    pending_event: str | None = None
    pending_data_lines: list[str] = []

    def _flush() -> SseEvent | None:
        nonlocal pending_event, pending_data_lines
        if not pending_data_lines and pending_event is None:
            return None
        raw_data = "\n".join(pending_data_lines)
        try:
            data = json.loads(raw_data) if raw_data else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {"value": data}
        ev = SseEvent(
            event=pending_event or "message",
            data=data,
            raw_data=raw_data,
        )
        pending_event = None
        pending_data_lines = []
        return ev

    def _has_only_event_name() -> bool:
        """True when a blank line arrives after an ``event:`` line but
        before any ``data:`` line.

        Some OpenAI/Anthropic-compatible gateways emit a *non-standard*
        framing that inserts a blank line BETWEEN the ``event:`` line and
        its ``data:`` line::

            event: content_block_delta\\n\\ndata: {...}\\n\\n

        A strict SSE parser would dispatch the lone ``event:`` block as an
        empty event (losing the event name) and then surface the ``data:``
        block under the default ``message`` name (losing the ``type``).
        To stay robust to these gateways (V1's Claude Code SDK tolerates
        the same upstream), we carry the pending event name forward across
        such a blank line instead of dispatching an empty event.  This is
        a strict superset of the spec: a well-formed stream that puts
        ``event:`` + ``data:`` in one block always has accumulated
        ``data`` at its blank line, so it is unaffected.
        """
        return pending_event is not None and not pending_data_lines

    async for chunk in byte_stream:
        buffer += chunk
        # Process complete lines; keep the trailing partial line in buffer.
        while True:
            # SSE spec accepts \r\n, \r, and \n line terminators.
            for sep in (b"\r\n", b"\n", b"\r"):
                idx = buffer.find(sep)
                if idx >= 0:
                    line = buffer[:idx]
                    buffer = buffer[idx + len(sep) :]
                    break
            else:
                # No newline yet — wait for more bytes.
                line = None  # type: ignore[assignment]
            if line is None:
                break
            if not line:
                # Blank line → dispatch pending event UNLESS only an
                # ``event:`` name has accumulated (no ``data:`` yet): some
                # gateways insert a blank line between the ``event:`` and
                # ``data:`` lines, so carry the event name forward rather
                # than emit an empty event (see :func:`_has_only_event_name`).
                if _has_only_event_name():
                    continue
                ev = _flush()
                if ev is not None:
                    yield ev
                continue
            if line.startswith(b":"):
                # Comment / heartbeat — ignore.
                continue
            text = line.decode("utf-8", errors="replace")
            if ":" in text:
                field, _, value = text.partition(":")
                # Per spec, strip exactly one leading space from value.
                if value.startswith(" "):
                    value = value[1:]
            else:
                field, value = text, ""
            if field == "event":
                pending_event = value
            elif field == "data":
                pending_data_lines.append(value)
            # ``id`` / ``retry`` are accepted by the spec but unused here.

    # Flush any trailing event without a closing blank line.
    ev = _flush()
    if ev is not None:
        yield ev
