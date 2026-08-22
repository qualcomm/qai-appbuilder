# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""ULID generation and validation.

A ULID is a 128-bit identifier rendered as a 26-character Crockford
base32 string.  Its leading 48 bits encode a Unix millisecond timestamp,
which makes ULIDs naturally sortable by creation time -- a property we
rely on for log correlation, event ordering, and primary keys in
append-only tables.

This module prefers the third-party ``ulid`` library when available, but
falls back to a pure-stdlib implementation so the platform layer never
hard-depends on an optional package.  The fallback uses
:func:`secrets.token_bytes` for entropy; we deliberately do **not** use
:mod:`random`, and we never cache state at module load time.
"""

from __future__ import annotations

import secrets
import time

# Crockford base32 alphabet -- excludes I, L, O, U to avoid visual
# ambiguity.  Defined as a constant (not a mutable global) so it can be
# safely shared.
_CROCKFORD_ALPHABET: str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_SET: frozenset[str] = frozenset(_CROCKFORD_ALPHABET)
_ULID_LENGTH: int = 26
_TIMESTAMP_CHARS: int = 10  # 48 bits / 5 bits per base32 char => 9.6, padded to 10
_RANDOM_CHARS: int = 16  # 80 bits / 5 bits per base32 char


def _encode_crockford(value: int, length: int) -> str:
    """Encode ``value`` as a fixed-length Crockford base32 string.

    The encoding is most-significant-character first, zero-padded on the
    left to ``length`` characters.  ``value`` must be non-negative and
    fit in ``length * 5`` bits.
    """

    if value < 0:
        raise ValueError("value must be non-negative")
    chars: list[str] = ["0"] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    if value != 0:
        raise ValueError("value too large for the requested length")
    return "".join(chars)


def _decode_crockford(text: str) -> int:
    """Decode a Crockford base32 string into an integer.

    Raises :class:`ValueError` if the string contains characters outside
    the alphabet.
    """

    result: int = 0
    for ch in text:
        idx = _CROCKFORD_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid Crockford base32 character: {ch!r}")
        result = (result << 5) | idx
    return result


def _new_ulid_fallback() -> str:
    """Stdlib-only ULID generator used when the ``ulid`` package is absent."""

    timestamp_ms: int = int(time.time() * 1000)
    if timestamp_ms < 0 or timestamp_ms >= (1 << 48):
        # 48-bit milliseconds covers ~10889 AD; this branch would only
        # trigger for clocks set to before 1970 or far in the future.
        raise ValueError("timestamp out of range for ULID")
    randomness: int = int.from_bytes(secrets.token_bytes(10), "big")  # 80 bits
    ts_part: str = _encode_crockford(timestamp_ms, _TIMESTAMP_CHARS)
    rand_part: str = _encode_crockford(randomness, _RANDOM_CHARS)
    return ts_part + rand_part


def new_ulid() -> str:
    """Return a fresh ULID as a 26-character Crockford base32 string.

    Uses the optional ``ulid`` third-party library when importable; falls
    back to a deterministic stdlib implementation otherwise.  The output
    is always upper-case and free of ambiguous characters (no I/L/O/U).
    """

    try:
        import ulid as _ulid_lib  # type: ignore[import-untyped]
    except ImportError:
        return _new_ulid_fallback()
    # ulid-py exposes ``new()``; we coerce to ``str`` to obtain the
    # canonical 26-char Crockford form.
    return str(_ulid_lib.new()).upper()


def is_valid_ulid(s: str) -> bool:
    """Return ``True`` iff ``s`` is a syntactically valid ULID.

    A valid ULID is exactly 26 characters drawn from the Crockford
    base32 alphabet.  The first character must additionally be ``0``-``7``
    so the encoded 130 bits fit into the 128-bit ULID space.
    """

    if not isinstance(s, str):
        return False
    if len(s) != _ULID_LENGTH:
        return False
    if any(ch not in _CROCKFORD_SET for ch in s):
        return False
    # First Crockford character contributes 5 bits, but a ULID only has
    # 128 bits total.  Therefore the leading char must be in 0..7.
    return s[0] in "01234567"


def ulid_to_timestamp_ms(s: str) -> int:
    """Extract the Unix-millisecond timestamp embedded in a ULID.

    Raises :class:`ValueError` when ``s`` is not a valid ULID.  This
    helper is intended for diagnostics and ordering checks; it is *not*
    a substitute for storing real timestamps.
    """

    if not is_valid_ulid(s):
        raise ValueError(f"not a valid ULID: {s!r}")
    return _decode_crockford(s[:_TIMESTAMP_CHARS])


__all__ = [
    "new_ulid",
    "is_valid_ulid",
    "ulid_to_timestamp_ms",
]
