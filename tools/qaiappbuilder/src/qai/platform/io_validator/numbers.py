# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Numeric range assertion.

A single helper, deliberately generic over ``int`` and ``float``,
suitable for rejecting out-of-range payload fields before they reach
business logic.
"""

from __future__ import annotations

from typing import Final, TypeVar

from ._errors import raise_validation

_CODE_RANGE: Final[str] = "validation.number.range"

# We restrict to the two numeric primitives we actually care about at
# the platform layer.  ``Decimal`` / ``Fraction`` callers can pre-cast.
T = TypeVar("T", int, float)


def assert_in_range(
    value: T,
    *,
    min: T | None,
    max: T | None,
    name: str,
) -> T:
    """Reject ``value`` if it falls outside ``[min, max]``.

    Either bound may be ``None`` to indicate "no lower / upper bound".
    Both bounds are *inclusive* -- the spec consistently uses closed
    intervals (e.g. timeout >= 0, percentage <= 100).

    Returns the value unchanged on success.

    The parameter names ``min`` and ``max`` shadow the builtins; that
    is intentional because they must match the public spec.  We don't
    use the builtins inside this function, so the shadowing is local
    and harmless.
    """

    if min is not None and max is not None and min > max:
        # Programmer error -- raise plain ValueError, not ValidationError.
        raise ValueError(
            f"min ({min!r}) must not exceed max ({max!r}) for {name!r}",
        )

    if min is not None and value < min:
        raise_validation(
            _CODE_RANGE,
            f"{name} must be >= {min}, got {value}",
        )
    if max is not None and value > max:
        raise_validation(
            _CODE_RANGE,
            f"{name} must be <= {max}, got {value}",
        )
    return value


__all__ = ["assert_in_range"]
