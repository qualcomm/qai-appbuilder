# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LRU result cache for ``appbuilder_run`` repeats (PR-094 §17.5 #12).

Restores the legacy ``backend/app_builder/result_cache.py`` (124 LOC) as an
infrastructure adapter. Identical ``(model_id, variant_id, inputs, params)``
quadruples skip re-inference and replay the previously persisted result;
the S9 audit (§3.3 A-14) flagged the loss of this layer as a measurable
latency / cost regression for the ``run > rerun`` UX.

Configuration knobs are read from
:class:`qai.platform.config.settings.AppBuilderSettings`:

* ``result_cache_enabled``    — master toggle; the adapter becomes a
  no-op when False.
* ``result_cache_max_entries`` — LRU size cap (default 64).
* ``result_cache_ttl_seconds`` — per-entry TTL (default 3600 s).

The cache is in-process by design: the sticky-worker host owns its
own model-warm-pool, and a shared distributed cache would re-introduce
a cross-worker dependency that S6 explicitly removed. Cross-process
invalidation is intentionally not part of this caching surface.

Thread-safety
-------------

The legacy module used a ``threading.Lock``; we rewire to
:class:`asyncio.Lock` so the cache stays uncontended on the asyncio hot
path (``app_builder`` runs entirely under asyncio in S3+). Test code that
needs to call ``put`` / ``get`` outside an event loop should use the
:class:`ResultCache.sync_*` helpers — those wrap the same dict with a
``threading.RLock`` for backwards-compat.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

__all__ = ["ResultCache", "ResultCacheStats"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultCacheStats:
    """Snapshot of cache fill state for ``GET /api/app-builder/cache/status``."""

    size: int
    max_entries: int
    ttl_seconds: int
    hits: int
    misses: int


class ResultCache:
    """LRU cache for ``appbuilder_run`` result payloads.

    The cache key is a SHA-256 of the canonical-JSON form of
    ``(model_id, variant_id, inputs, params)``; see :meth:`make_key` for
    the exact serialization. Values are opaque ``dict`` payloads — the use
    case writes ``{ "output": ..., "metrics": ... }`` and reads it back
    verbatim, so the cache stays decoupled from the run record schema.
    """

    __slots__ = (
        "_max",
        "_ttl",
        "_enabled",
        "_cache",
        "_async_lock",
        "_sync_lock",
        "_hits",
        "_misses",
    )

    def __init__(
        self,
        *,
        max_entries: int = 64,
        ttl_seconds: int = 3600,
        enabled: bool = True,
    ) -> None:
        if not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError(
                f"max_entries must be int >= 1, got {max_entries!r}"
            )
        if not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise ValueError(
                f"ttl_seconds must be int >= 1, got {ttl_seconds!r}"
            )
        self._max = max_entries
        self._ttl = ttl_seconds
        self._enabled = bool(enabled)
        self._cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    # ── key construction ─────────────────────────────────────────────

    @staticmethod
    def make_key(
        model_id: str,
        variant_id: str | None,
        inputs: dict[str, Any] | None,
        params: dict[str, Any] | None,
    ) -> str:
        """Stable, key-order-independent SHA-256 of the run signature."""
        norm = {
            "m": str(model_id or ""),
            "v": variant_id or "_default",
            "i": _stable_dict(inputs or {}),
            "p": _stable_dict(params or {}),
        }
        s = json.dumps(norm, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    # ── async API (preferred) ────────────────────────────────────────

    async def get(self, run_id: str) -> Any | None:
        """Return the cached value for ``run_id`` or None on miss / TTL."""
        if not self._enabled:
            return None
        async with self._async_lock:
            return self._get_locked(run_id)

    async def put(self, run_id: str, result: Any) -> None:
        """Insert / replace the cache entry for ``run_id``."""
        if not self._enabled:
            return
        async with self._async_lock:
            self._put_locked(run_id, result)

    async def clear(self) -> int:
        """Drop all cached entries; returns the count removed."""
        async with self._async_lock:
            n = len(self._cache)
            self._cache.clear()
            return n

    async def stats(self) -> ResultCacheStats:
        async with self._async_lock:
            return ResultCacheStats(
                size=len(self._cache),
                max_entries=self._max,
                ttl_seconds=self._ttl,
                hits=self._hits,
                misses=self._misses,
            )

    # ── sync helpers (test fixtures only) ────────────────────────────

    def sync_get(self, run_id: str) -> Any | None:
        if not self._enabled:
            return None
        with self._sync_lock:
            return self._get_locked(run_id)

    def sync_put(self, run_id: str, result: Any) -> None:
        if not self._enabled:
            return
        with self._sync_lock:
            self._put_locked(run_id, result)

    def sync_clear(self) -> int:
        with self._sync_lock:
            n = len(self._cache)
            self._cache.clear()
            return n

    # ── internals ────────────────────────────────────────────────────

    def _get_locked(self, run_id: str) -> Any | None:
        entry = self._cache.get(run_id)
        if entry is None:
            self._misses += 1
            return None
        ts, val = entry
        if (time.time() - ts) > self._ttl:
            try:
                del self._cache[run_id]
            except KeyError:  # pragma: no cover -- defensive
                pass
            self._misses += 1
            return None
        self._cache.move_to_end(run_id)
        self._hits += 1
        return val

    def _put_locked(self, run_id: str, result: Any) -> None:
        self._cache[run_id] = (time.time(), result)
        self._cache.move_to_end(run_id)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)


def _stable_dict(value: Any) -> Any:
    """Recursively sort dict keys for stable JSON serialization."""
    if isinstance(value, dict):
        return {k: _stable_dict(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable_dict(x) for x in value]
    return value
