# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Output buffer with UTF-8-continuation-safe truncation.

The ring buffer in :class:`SubprocessBackgroundProcessManager` is a
plain ``bytes`` object kept at <= 200 KiB by :func:`clamp`.

Two design decisions worth noting:

1. **bytes, not str** - Python ``str`` is immutable and grows by full
   copy on every concat + slice. Holding raw ``bytes`` lets the manager
   do O(1) ``bytes[start:]`` slices and only decode on read
   (``Info.output`` / ``logs()``). UTF-8 decoding uses
   ``errors="replace"`` so partial multi-byte sequences at chunk
   boundaries become U+FFFD instead of raising.

2. **Lead-byte alignment after truncation** - when a chunk fills the
   buffer beyond ``cap``, the head is advanced past UTF-8 continuation
   bytes (``10xxxxxx`` -> ``b & 0xC0 == 0x80``) so the surviving slice
   always starts at a lead byte. Without this the buffer head could
   slice through a multi-byte CJK / emoji character and produce a
   permanent U+FFFD prefix on every subsequent decode. Aligned with
   AGENTS.md section 3.10 encoding rule.

This module is **stdlib-only** so it stays cheap to import from
anywhere in the package and carries no FastAPI / DI / SQL import chain.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_CAP_BYTES",
    "append_clamped",
    "clamp",
    "decode_lossy",
]


DEFAULT_CAP_BYTES: int = 200 * 1024
"""Output retention cap in bytes.

Chosen so behavioural tests have a stable, well-defined boundary.
"""


def clamp(data: bytes, cap: int = DEFAULT_CAP_BYTES) -> bytes:
    """Truncate ``data`` from the head so its length is <= ``cap``.

    Preserves the tail of ``data``. When the cut point would land on a
    UTF-8 continuation byte (``10xxxxxx``), the head is advanced
    further until the next lead byte is reached. This guarantees the
    returned ``bytes`` is itself a well-formed UTF-8 prefix when
    decoded with ``errors="replace"`` (no spurious U+FFFD at the
    boundary).

    Behavioural contract:

    >>> clamp(b'hello', cap=10)
    b'hello'
    >>> clamp(b'0123456789ABCDEF', cap=8)
    b'89ABCDEF'

    UTF-8 boundary example - 'a' + 4-byte emoji + 'b':

    >>> chunk = b'a' + bytes([0xF0, 0x9F, 0x98, 0x80]) + b'b'
    >>> len(chunk)
    6
    >>> # cap=3: head wants to start at byte 3, which is the third
    >>> # continuation byte of the emoji (0x98). The algorithm advances
    >>> # to byte 5 (the 'b' lead byte).
    >>> clamp(chunk, cap=3)
    b'b'

    Edge cases:

    - ``data`` shorter than or equal to ``cap`` is returned verbatim
      (no allocation).
    - ``cap == 0`` returns ``b''``.
    - ``cap < 0`` raises ``ValueError``.
    - All UTF-8 continuation bytes after the cut point are skipped;
      in pathological inputs (e.g. ``cap`` smaller than the largest
      multi-byte character) the result may be shorter than ``cap``.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"data must be bytes-like, got {type(data).__name__}")
    if not isinstance(cap, int) or isinstance(cap, bool):
        raise TypeError(f"cap must be int, got {type(cap).__name__}")
    if cap < 0:
        raise ValueError(f"cap must be non-negative, got {cap}")
    # Normalise to bytes view to allow caller passing bytearray.
    view = bytes(data) if not isinstance(data, bytes) else data
    if len(view) <= cap:
        return view
    start = len(view) - cap
    # Skip UTF-8 continuation bytes so the surviving slice starts on a
    # lead byte (or ASCII). 0xC0 mask isolates the top 2 bits;
    # 0x80 means 10xxxxxx (continuation). Stop at len(view) defensively
    # in case a malformed sequence runs off the end.
    while start < len(view) and (view[start] & 0xC0) == 0x80:
        start += 1
    return view[start:]


def append_clamped(
    current: bytes, chunk: bytes | bytearray | memoryview, *, cap: int = DEFAULT_CAP_BYTES
) -> bytes:
    """Concatenate ``current + chunk`` then ``clamp`` the result.

    Convenience helper for the manager's hot path (every stdout/stderr
    chunk triggers exactly one call). Returns a fresh ``bytes``
    object; callers should reassign ``active.output_bytes`` to the
    result.

    Empty ``chunk`` is a no-op fast path - returns ``current``
    unchanged without re-running ``clamp`` (since the size cannot
    have grown).
    """
    if not chunk:
        return current
    raw_chunk = bytes(chunk) if not isinstance(chunk, bytes) else chunk
    return clamp(current + raw_chunk, cap=cap)


def decode_lossy(data: bytes) -> str:
    """UTF-8 decode with replacement of invalid sequences.

    Used by :class:`Info` / :class:`Logs` construction sites; ``data``
    is already clamp-aligned to lead bytes so replacement should only
    occur for genuinely malformed input (binary content piped to
    stdout / partial chunk boundaries the OS handed us before the
    next write completed).
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"data must be bytes-like, got {type(data).__name__}")
    return bytes(data).decode("utf-8", errors="replace")
