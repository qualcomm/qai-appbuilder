# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Clock abstraction for testable time-dependent code.

Provides:
- ``Clock``: a Protocol describing the minimal time surface the rest of the
  platform should depend on (instead of calling ``time.*`` / ``datetime.*``
  directly).
- ``SystemClock``: production implementation backed by the standard library.
- ``FrozenClock``: deterministic, manually-advanced implementation for tests.

Design rules (S1 PR-012):
- Never call ``datetime.utcnow()`` (deprecated) or produce naive datetimes.
- Never expose a module-level singleton; callers construct/inject explicitly.
- ``monotonic_ns()`` must be strictly non-decreasing, even for ``FrozenClock``.
"""

from __future__ import annotations

import asyncio
import time as _stdlib_time
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from .conversions import ensure_aware_utc, utcnow

__all__ = ["Clock", "SystemClock", "FrozenClock"]


@runtime_checkable
class Clock(Protocol):
    """Minimal time surface for the platform.

    All implementations MUST:
    - return timezone-aware UTC datetimes from :meth:`now`;
    - return a strictly non-decreasing integer from :meth:`monotonic_ns`;
    - implement both sync and async sleep.
    """

    def now(self) -> datetime:
        """Return the current wall-clock time as a tz-aware UTC datetime."""
        ...

    def monotonic_ns(self) -> int:
        """Return a strictly non-decreasing monotonic timestamp in nanoseconds."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block the current thread for ``seconds`` seconds."""
        ...

    async def sleep_async(self, seconds: float) -> None:
        """Asynchronously suspend for ``seconds`` seconds."""
        ...


class SystemClock:
    """Production :class:`Clock` implementation backed by ``time`` / ``asyncio``."""

    __slots__ = ()

    def now(self) -> datetime:
        return utcnow()

    def monotonic_ns(self) -> int:
        return _stdlib_time.monotonic_ns()

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"sleep() requires seconds >= 0, got {seconds!r}")
        if seconds == 0:
            return
        _stdlib_time.sleep(seconds)

    async def sleep_async(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(
                f"sleep_async() requires seconds >= 0, got {seconds!r}"
            )
        await asyncio.sleep(seconds)


class FrozenClock:
    """Deterministic :class:`Clock` for tests.

    Wall-clock time and the monotonic counter advance only when the test
    explicitly calls :meth:`advance` / :meth:`set` or invokes :meth:`sleep` /
    :meth:`sleep_async` (which advance the clock instead of really sleeping).

    The monotonic counter is strictly non-decreasing: :meth:`set` may move
    wall-clock time backwards, but :meth:`advance` with a negative value or
    any other operation that would rewind ``monotonic_ns`` raises
    :class:`ValueError`.
    """

    __slots__ = ("_now", "_monotonic_ns")

    def __init__(
        self,
        *,
        now: datetime,
        monotonic_start_ns: int = 0,
    ) -> None:
        if monotonic_start_ns < 0:
            raise ValueError(
                f"monotonic_start_ns must be >= 0, got {monotonic_start_ns!r}"
            )
        self._now: datetime = ensure_aware_utc(now)
        self._monotonic_ns: int = int(monotonic_start_ns)

    # ── Clock protocol ──────────────────────────────────────────────────

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        return self._monotonic_ns

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"sleep() requires seconds >= 0, got {seconds!r}")
        if seconds == 0:
            return
        self.advance(seconds)

    async def sleep_async(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(
                f"sleep_async() requires seconds >= 0, got {seconds!r}"
            )
        if seconds == 0:
            return
        self.advance(seconds)

    # ── Test-only mutators ──────────────────────────────────────────────

    def advance(self, seconds: float) -> None:
        """Advance both wall-clock and monotonic time by ``seconds``.

        ``seconds`` must be >= 0; negative values are rejected to keep the
        monotonic counter non-decreasing.
        """
        if seconds < 0:
            raise ValueError(
                f"advance() requires seconds >= 0, got {seconds!r}"
            )
        # Convert to int ns with banker-safe rounding.
        delta_ns = int(round(seconds * 1_000_000_000))
        self._monotonic_ns += delta_ns
        # datetime arithmetic preserves tz.
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, now: datetime) -> None:
        """Set wall-clock time without touching the monotonic counter.

        Wall-clock may jump backwards (e.g. simulating NTP correction); the
        monotonic counter is intentionally NOT modified, so callers using
        :meth:`monotonic_ns` for timeouts remain correct.
        """
        self._now = ensure_aware_utc(now)
