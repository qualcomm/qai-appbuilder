# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""String and byte-level input assertions.

These helpers are deliberately *context-free*: they know nothing about
business entities, DTOs or HTTP request shapes.  Higher layers
(application use-cases) should keep using pydantic for full schema
validation -- this module only provides cheap, reusable building
blocks that can run before pydantic is invoked.

Every ``assert_*`` function:

* takes the value as its first positional argument,
* takes a keyword-only ``name`` used in error messages,
* returns the (possibly trimmed) value on success,
* raises :class:`qai.platform.errors.ValidationError` on failure.

No module-level mutable globals or pre-compiled regexes are kept; the
``functools.lru_cache``-backed compile helper provides equivalent
performance without leaking state.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Collection, Final

from ._errors import raise_validation

# ---------------------------------------------------------------------------
# Error codes (keep these stable; presentation layers may key off them)
# ---------------------------------------------------------------------------
_CODE_EMPTY: Final[str] = "validation.string.empty"
_CODE_MAX_LENGTH: Final[str] = "validation.string.max_length"
_CODE_PATTERN: Final[str] = "validation.string.pattern"
_CODE_NOT_ALLOWED: Final[str] = "validation.string.not_allowed"
_CODE_CONTROL_CHAR: Final[str] = "validation.string.control_char"
_CODE_BYTE_SIZE: Final[str] = "validation.bytes.size"
_CODE_NOT_UTF8: Final[str] = "validation.bytes.not_utf8"

# Allowed control characters: TAB (0x09), LF (0x0A), CR (0x0D).
_ALLOWED_CONTROL_ORDS: Final[frozenset[int]] = frozenset({0x09, 0x0A, 0x0D})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
@lru_cache(maxsize=128)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile-and-cache a regex.

    Cached at function scope so we avoid module-level mutable state
    while still amortising the cost of frequently used patterns.
    """

    return re.compile(pattern)


def _coerce_pattern(pattern: re.Pattern[str] | str) -> re.Pattern[str]:
    if isinstance(pattern, re.Pattern):
        return pattern
    return _compile_pattern(pattern)


# ---------------------------------------------------------------------------
# Public string assertions
# ---------------------------------------------------------------------------
def assert_non_empty(value: str, *, name: str) -> str:
    """Reject empty or whitespace-only strings.

    Returns the original (un-stripped) value on success so callers can
    decide whether to keep leading/trailing whitespace.
    """

    if not isinstance(value, str):  # defensive: callers may forget typing
        raise_validation(
            _CODE_EMPTY,
            f"{name} must be a string, got {type(value).__name__}",
        )
    if value == "" or value.strip() == "":
        raise_validation(_CODE_EMPTY, f"{name} must not be empty")
    return value


def assert_max_length(value: str, *, max_length: int, name: str) -> str:
    """Reject strings whose length (in characters) exceeds ``max_length``."""

    if max_length < 0:
        # Programmer error, not user input -- raise plain ValueError.
        raise ValueError(f"max_length must be >= 0, got {max_length}")
    if len(value) > max_length:
        raise_validation(
            _CODE_MAX_LENGTH,
            f"{name} length {len(value)} exceeds maximum {max_length}",
        )
    return value


def assert_matches(
    value: str,
    *,
    pattern: re.Pattern[str] | str,
    name: str,
) -> str:
    """Reject strings that do not fully match ``pattern``.

    Uses :func:`re.fullmatch` semantics so callers don't need to anchor
    the pattern with ``^...$`` themselves.
    """

    compiled = _coerce_pattern(pattern)
    if compiled.fullmatch(value) is None:
        raise_validation(
            _CODE_PATTERN,
            f"{name} does not match required pattern {compiled.pattern!r}",
        )
    return value


def assert_one_of(value: str, *, allowed: Collection[str], name: str) -> str:
    """Reject values that are not in ``allowed``."""

    if value not in allowed:
        # Sort to keep error messages deterministic (helpful for tests
        # and log diff'ing).  Limit to a sensible number to avoid blowing
        # up huge allow-lists into the message.
        sample = sorted(allowed)
        if len(sample) > 10:
            shown = ", ".join(sample[:10]) + ", ..."
        else:
            shown = ", ".join(sample)
        raise_validation(
            _CODE_NOT_ALLOWED,
            f"{name} must be one of [{shown}], got {value!r}",
        )
    return value


def assert_no_control_chars(value: str, *, name: str) -> str:
    """Reject strings containing C0 control characters.

    The C0 range is ``\\x00``..``\\x1f``; we permit the three
    ubiquitous whitespace controls (TAB, LF, CR) since they appear in
    legitimate user input (multi-line text fields, CSVs, etc.).
    DEL (0x7F) is *not* rejected here -- callers needing stricter
    sanitisation can layer their own check on top.
    """

    for idx, ch in enumerate(value):
        code_point = ord(ch)
        if code_point < 0x20 and code_point not in _ALLOWED_CONTROL_ORDS:
            raise_validation(
                _CODE_CONTROL_CHAR,
                (
                    f"{name} contains forbidden control character "
                    f"\\x{code_point:02x} at position {idx}"
                ),
            )
    return value


# ---------------------------------------------------------------------------
# Byte assertions
# ---------------------------------------------------------------------------
def assert_byte_size(data: bytes, *, max_bytes: int, name: str) -> bytes:
    """Reject byte buffers larger than ``max_bytes``."""

    if max_bytes < 0:
        raise ValueError(f"max_bytes must be >= 0, got {max_bytes}")
    if len(data) > max_bytes:
        raise_validation(
            _CODE_BYTE_SIZE,
            f"{name} size {len(data)} bytes exceeds maximum {max_bytes} bytes",
        )
    return data


def assert_utf8_decodable(data: bytes, *, name: str) -> str:
    """Reject byte buffers that are not valid UTF-8.

    Returns the decoded string on success so callers can chain further
    string-level validators without re-decoding.
    """

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Re-raise as a ValidationError so call sites can use a single
        # ``except`` clause across all io_validator helpers.
        raise_validation(
            _CODE_NOT_UTF8,
            f"{name} is not valid UTF-8: {exc.reason} at byte {exc.start}",
        )
        # raise_validation always raises -- the ``raise`` below is just
        # to satisfy static type checkers (NoReturn is hinted indirectly).
        raise  # pragma: no cover


__all__ = [
    "assert_non_empty",
    "assert_max_length",
    "assert_matches",
    "assert_one_of",
    "assert_no_control_chars",
    "assert_byte_size",
    "assert_utf8_decodable",
]
