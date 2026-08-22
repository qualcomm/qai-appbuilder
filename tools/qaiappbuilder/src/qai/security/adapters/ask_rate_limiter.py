# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory :class:`AskRateLimiterPort` adapter (PR-501).

Single-process, single-worker default — matching the same single-worker
assumption that governs ``StreamAbortRegistry`` and the WS multi-tab
single-connection design in the chat context. Operators running multiple
uvicorn workers would need a distributed limiter (Redis-backed); that
deployment shape is intentionally outside the supported product surface
and is not part of PR-501's contract.

Implementation — sliding window per ``(channel_name, subject_kind,
subject_identifier)`` triple. Each bucket holds a deque of the
timestamps of recent ASKs. On :meth:`check_and_record` the bucket is
trimmed to the active window before the cap is consulted; an allowed
ASK is appended to the deque before returning.

Concurrency — the limiter exposes an :class:`asyncio.Lock` that
serialises bucket access. The hot path is microseconds (deque trim +
append) so contention is negligible compared to the real network
round-trips that drive ASK requests.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from datetime import datetime, timedelta

from qai.security.domain.value_objects import Channel, Subject

__all__ = ["InMemoryAskRateLimiter"]


_BucketKey = tuple[str, str, str]


class InMemoryAskRateLimiter:
    """Single-process implementation of :class:`AskRateLimiterPort`."""

    __slots__ = ("_buckets", "_lock", "_max_keys")

    # Soft cap on the number of distinct ``(channel, subject)`` keys
    # tracked in memory. Once reached, the least-recently-USED key
    # is evicted on the next insert. Tuned high enough that legitimate
    # workloads never hit it; low enough that an adversarial caller
    # spamming with synthetic subjects can't OOM the process.
    _DEFAULT_MAX_KEYS = 4096

    def __init__(self, *, max_keys: int = _DEFAULT_MAX_KEYS) -> None:
        if not isinstance(max_keys, int) or max_keys <= 0:
            raise ValueError(f"max_keys must be > 0, got {max_keys!r}")
        self._max_keys = max_keys
        # ``OrderedDict`` so we can evict the least-recently-USED key
        # (true LRU) rather than the least-recently-INSERTED key. On every
        # touch we ``move_to_end`` the key so an actively-limited subject
        # is never evicted out from under an adversary's synthetic spam
        # (L-4: insertion-order eviction could drop a hot key).
        self._buckets: OrderedDict[_BucketKey, deque[datetime]] = (
            OrderedDict()
        )
        self._lock = asyncio.Lock()

    async def check_and_record(
        self,
        *,
        channel: Channel,
        subject: Subject,
        window_seconds: int,
        max_asks: int,
        now: datetime,
    ) -> bool:
        key: _BucketKey = (
            channel.name,
            subject.kind,
            subject.identifier,
        )
        cutoff = now - timedelta(seconds=window_seconds)
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # Reuse-or-evict: when at capacity, drop the
                # least-recently-USED key (front of the OrderedDict).
                if len(self._buckets) >= self._max_keys:
                    self._buckets.popitem(last=False)
                bucket = deque()
                self._buckets[key] = bucket
            else:
                # Touch: mark this key as most-recently-used.
                self._buckets.move_to_end(key)

            # Trim entries outside the sliding window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= max_asks:
                return False

            bucket.append(now)
            return True

    # Test affordance: snapshot bucket size for assertions.
    def _bucket_count(
        self, *, channel: Channel, subject: Subject
    ) -> int:
        key = (channel.name, subject.kind, subject.identifier)
        bucket = self._buckets.get(key)
        return 0 if bucket is None else len(bucket)
