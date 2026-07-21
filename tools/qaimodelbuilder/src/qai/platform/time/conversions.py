# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Datetime / ISO-8601 conversion utilities.

All utilities here enforce timezone-aware UTC. Naive datetimes are treated as
errors per S1 PR-012 / refactor-plan §15.1 (ruff DTZ).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = ["utcnow", "to_iso8601", "from_iso8601", "ensure_aware_utc"]

# Cached zero offset (``timedelta(0)``) for cheap UTC detection.
_ZERO_OFFSET: timedelta = timedelta(0)


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Equivalent to ``datetime.now(timezone.utc)``. Use this instead of the
    deprecated ``datetime.utcnow()`` (which returns a naive datetime).
    """
    return datetime.now(timezone.utc)


def to_iso8601(dt: datetime) -> str:
    """Serialize ``dt`` to a strict ISO-8601 string in UTC.

    The returned format always includes microseconds and a UTC offset, e.g.
    ``"2026-05-29T09:32:01.123456+00:00"``.

    Raises:
        ValueError: if ``dt`` is naive (no tzinfo).
    """
    aware = ensure_aware_utc(dt)
    # ``datetime.isoformat`` produces ``+00:00`` for UTC, which is the
    # canonical ISO-8601 representation we want (not ``Z``).
    return aware.isoformat(timespec="microseconds")


def from_iso8601(s: str) -> datetime:
    """Parse a strict ISO-8601 string into a tz-aware UTC datetime.

    Both ``+00:00`` and ``Z`` UTC suffixes are accepted; non-UTC offsets are
    converted to UTC. Naive inputs (no tz info) are rejected.

    Raises:
        ValueError: if ``s`` is not a valid ISO-8601 datetime, or is naive.
    """
    if not isinstance(s, str):  # pragma: no cover - defensive
        raise ValueError(f"from_iso8601 expected str, got {type(s).__name__}")
    # Python's ``fromisoformat`` (3.11+) accepts the ``Z`` suffix as well as
    # ``+HH:MM``. We don't pre-process the string.
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 datetime: {s!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"naive datetime is not allowed (missing timezone offset): {s!r}"
        )
    return parsed.astimezone(timezone.utc)


def ensure_aware_utc(dt: datetime) -> datetime:
    """Return ``dt`` converted to UTC; reject naive datetimes.

    Args:
        dt: a timezone-aware datetime in any zone.

    Returns:
        ``dt`` converted to UTC. If already UTC, the returned object is
        equivalent (and may be the same instance).

    Raises:
        ValueError: if ``dt`` is naive (``tzinfo is None``).
    """
    if not isinstance(dt, datetime):  # pragma: no cover - defensive
        raise ValueError(
            f"ensure_aware_utc expected datetime, got {type(dt).__name__}"
        )
    if dt.tzinfo is None:
        raise ValueError(
            "naive datetime is not allowed; expected timezone-aware datetime"
        )
    if dt.tzinfo is timezone.utc or dt.utcoffset() == _ZERO_OFFSET:
        # Already UTC; normalize tzinfo to ``timezone.utc`` for consistency.
        return dt if dt.tzinfo is timezone.utc else dt.astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)
