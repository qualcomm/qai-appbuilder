# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LRU cache for :class:`qai.security.domain.entities.Policy.evaluate` results.

PR-092 §2.1 C-8 / §17.5 #9 — restores the legacy policy decision
cache (``backend/security/policy.py:766-779``). Keyed by
``(operation, normalized_path)`` so two calls with the same logical
request hit the same slot regardless of separator / casing
differences. Entry size is configurable via
``Settings.security.policy_decision_cache_size`` (``0`` disables the
cache; ``2000`` is the legacy default).

The cache is a thin wrapper around :class:`collections.OrderedDict`
under a :class:`threading.Lock` so it is safe to consult from the
PEP 578 audit hook (which runs synchronously on any thread). On a
miss the caller computes the decision and inserts it via
:meth:`PolicyDecisionCache.put`. Cache invalidation is the caller's
responsibility — :meth:`UpdatePolicyUseCase` should call
:meth:`PolicyDecisionCache.clear` after every successful policy save
because previously-cached decisions are no longer authoritative.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Hashable

from qai.security.adapters.path_normalizer import normalize_path

__all__ = ["PolicyDecisionCache"]


class PolicyDecisionCache:
    """Bounded LRU cache for ``(operation, normalized_path)`` decisions."""

    __slots__ = ("_max_size", "_data", "_lock")

    def __init__(self, *, max_size: int = 2000) -> None:
        if not isinstance(max_size, int) or isinstance(max_size, bool):
            raise TypeError(
                f"max_size must be int, got {type(max_size).__name__}"
            )
        if max_size < 0:
            raise ValueError(
                f"max_size must be >= 0, got {max_size}"
            )
        self._max_size = max_size
        self._data: OrderedDict[tuple[str, str], Hashable] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def max_size(self) -> int:
        """Configured upper bound; ``0`` means the cache is disabled."""

        return self._max_size

    @property
    def enabled(self) -> bool:
        """``True`` when the cache will actually retain entries."""

        return self._max_size > 0

    @staticmethod
    def make_key(operation: str, path: "str | None") -> tuple[str, str]:
        """Return the canonical ``(operation, normalized_path)`` key.

        Operations are case-folded and normalised paths are converted
        to lower-case strings so case-only differences hit the same
        slot. The normaliser handles 8.3 short names + symlinks +
        case-folding (see :func:`normalize_path`).
        """

        op = (operation or "").strip().casefold()
        if not path:
            return (op, "")
        normalised = str(normalize_path(path)).casefold()
        return (op, normalised)

    def get(self, key: tuple[str, str]) -> Hashable | None:
        """Return the cached decision or ``None`` on miss."""

        if not self.enabled:
            return None
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key: tuple[str, str], decision: Hashable) -> None:
        """Insert / refresh ``decision`` for ``key``; evicts the oldest entry."""

        if not self.enabled:
            return
        with self._lock:
            self._data[key] = decision
            self._data.move_to_end(key)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        """Drop every cached decision (call after Policy save)."""

        with self._lock:
            self._data.clear()

    def __len__(self) -> int:  # pragma: no cover - trivial
        with self._lock:
            return len(self._data)
