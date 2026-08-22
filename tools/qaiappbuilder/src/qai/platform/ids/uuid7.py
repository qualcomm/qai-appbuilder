# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""UUID v7 generation and generic UUID validation.

UUID v7 (RFC 9562) is a time-ordered 128-bit identifier whose leading
48 bits encode a Unix millisecond timestamp.  The Python standard
library does not yet ship a ``uuid7()`` constructor (3.12), so we
implement a minimal compliant generator here.

Layout of the 128 bits we produce::

    | 48 bits unix-ms |  4 bits ver=0x7 | 12 bits rand_a |
    | 2 bits var=0b10 | 62 bits rand_b                   |

Entropy comes from :func:`secrets.token_bytes`; we never use
:mod:`random` and never seed at import time.
"""

from __future__ import annotations

import secrets
import time
import uuid

_UUID_TEXT_LENGTH: int = 36  # 8-4-4-4-12 with four hyphens


def new_uuid7() -> str:
    """Return a freshly minted UUID v7 in canonical 8-4-4-4-12 form.

    The integer layout follows RFC 9562 §5.7: 48-bit big-endian Unix
    milliseconds, 4-bit version (0x7), 12 bits of random ``rand_a``,
    2-bit variant (0b10), and 62 bits of random ``rand_b``.
    """

    timestamp_ms: int = int(time.time() * 1000) & ((1 << 48) - 1)
    # 74 bits of randomness total: 12 for rand_a + 62 for rand_b.  We
    # draw 10 bytes (80 bits) and discard the high 6 bits.
    rand_bits: int = int.from_bytes(secrets.token_bytes(10), "big") & ((1 << 74) - 1)
    rand_a: int = (rand_bits >> 62) & 0xFFF  # top 12 bits
    rand_b: int = rand_bits & ((1 << 62) - 1)  # bottom 62 bits

    value: int = 0
    value |= timestamp_ms << 80
    value |= 0x7 << 76  # version nibble
    value |= rand_a << 64
    value |= 0b10 << 62  # RFC 4122 variant
    value |= rand_b

    return str(uuid.UUID(int=value))


def is_valid_uuid(s: str) -> bool:
    """Return ``True`` iff ``s`` parses as any RFC-compliant UUID.

    Accepts versions 1, 3, 4, 5, 6, 7 and 8.  We do not constrain the
    variant beyond what :class:`uuid.UUID` already enforces, because
    callers may legitimately pass through identifiers minted by other
    systems.
    """

    if not isinstance(s, str):
        return False
    if len(s) != _UUID_TEXT_LENGTH:
        return False
    try:
        uuid.UUID(s)
    except ValueError:
        return False
    return True


__all__ = [
    "new_uuid7",
    "is_valid_uuid",
]
