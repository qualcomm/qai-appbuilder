# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Stateful NDJSON line decoder for runner_protocol byte streams.

The :class:`ProcessRunnerPort` produces :class:`ProcessStdoutFrame`
chunks of arbitrary size — one ``read(64KB)`` chunk may contain zero,
one, partial, or many newline-delimited JSON events. This module
buffers bytes and yields complete lines as they become available.

Why a class and not just ``str.splitlines()``?
----------------------------------------------

Two reasons:

1. **Partial line carry-over.** A 64 KB ``read()`` boundary may fall
   mid-line; we have to buffer the trailing fragment until the next
   chunk arrives.
2. **Stream EOF semantics.** When the child process closes stdout
   without a trailing newline, a final un-terminated fragment may
   still be a valid JSON object (some runners do this); the
   :meth:`flush` method emits that final piece.

Encoding
--------

The decoder is byte-oriented. It uses ``utf-8`` with
``errors="replace"`` so a malformed multi-byte sequence never crashes
the line stream — the offending bytes become ``U+FFFD`` and downstream
JSON parsing of that line will fall back to a stdout log event.

Thread-safety
-------------

Single-task only. Each subprocess has one decoder; the host's run loop
serialises feeds and reads.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["NdjsonDecoder"]


# Maximum buffered fragment size before we force-flush.  Defends against
# pathological producers that never emit a newline. The value is
# generous (1 MB) so legitimate 100KB-tile log lines still fit; runners
# emitting structured events much larger than this are violating the
# protocol contract anyway.
_MAX_BUFFER_BYTES = 1_048_576


class NdjsonDecoder:
    """Buffer bytes and yield complete UTF-8 lines.

    Usage::

        decoder = NdjsonDecoder()
        for line in decoder.feed(chunk_bytes):
            event = decode_event(line)
            ...
        for line in decoder.flush():
            event = decode_event(line)
            ...
    """

    __slots__ = ("_buffer", "_max_buffer_bytes")

    def __init__(self, *, max_buffer_bytes: int = _MAX_BUFFER_BYTES) -> None:
        if max_buffer_bytes <= 0:
            raise ValueError(
                f"max_buffer_bytes must be > 0, got {max_buffer_bytes}"
            )
        self._buffer = bytearray()
        self._max_buffer_bytes = max_buffer_bytes

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def feed(self, chunk: bytes) -> Iterable[str]:
        """Append ``chunk`` and yield any complete lines it produced.

        Empty / zero-byte ``chunk`` is a no-op (returns an empty
        iterable).
        """
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError(
                f"feed() requires bytes, got {type(chunk).__name__}"
            )
        if not chunk:
            return
        # Cap defends against runaway producers — at the cap we flush
        # whatever we have as one log line and reset.
        if len(self._buffer) + len(chunk) > self._max_buffer_bytes:
            yield from self._flush_oversize_then(chunk)
            return
        self._buffer.extend(chunk)
        # Pop complete lines (terminated by '\n' / '\r\n').
        while True:
            nl = self._buffer.find(b"\n")
            if nl < 0:
                break
            line_bytes = bytes(self._buffer[:nl])
            del self._buffer[: nl + 1]
            # Strip a trailing CR if present (Windows line endings).
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]
            yield line_bytes.decode("utf-8", errors="replace")

    def flush(self) -> Iterable[str]:
        """Emit any un-terminated trailing bytes as one final line.

        Called when the upstream stream closes (``read()`` returns
        ``b""``). If the buffer is empty / whitespace, returns nothing.
        """
        if not self._buffer:
            return
        line_bytes = bytes(self._buffer)
        self._buffer.clear()
        if line_bytes.endswith(b"\r"):
            line_bytes = line_bytes[:-1]
        text = line_bytes.decode("utf-8", errors="replace")
        if text.strip():
            yield text

    @property
    def buffered_bytes(self) -> int:
        """Number of bytes currently buffered (diagnostics)."""
        return len(self._buffer)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _flush_oversize_then(self, chunk: bytes) -> Iterable[str]:
        """Pathological-producer path: flush the buffer + chunk as logs."""
        # Yield the existing buffer (if any) as one synthetic line so
        # the caller sees the bytes; do the same for ``chunk``.
        if self._buffer:
            yield bytes(self._buffer).decode("utf-8", errors="replace")
            self._buffer.clear()
        yield bytes(chunk).decode("utf-8", errors="replace")
