# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Sliding-window per-key rate limiter."""
from __future__ import annotations

import time
from collections import defaultdict


class SlidingWindowRateLimiter:
    """Per-key sliding-window rate limiter.

    Usage:
        limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=60)
        if not limiter.allow("user-123"):
            raise TooManyRequestsError()
    """

    def __init__(self, *, max_requests: int = 30, window_seconds: float = 60.0):
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        """Check if a request with the given key is allowed."""
        now = time.monotonic()
        ts = self._timestamps[key]
        # Purge expired entries
        cutoff = now - self._window
        self._timestamps[key] = [t for t in ts if t > cutoff]
        ts = self._timestamps[key]
        if len(ts) >= self._max:
            return False
        ts.append(now)
        return True

    def reset(self, key: str) -> None:
        """Reset the counter for a key."""
        self._timestamps.pop(key, None)
