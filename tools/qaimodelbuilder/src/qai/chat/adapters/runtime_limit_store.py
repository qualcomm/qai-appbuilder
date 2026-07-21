# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""In-memory runtime-learned API limit store (D2).

Concrete adapter for
:class:`qai.chat.application.ports.RuntimeLimitStorePort`.

D2 lifted the mutable ``_runtime_limits`` dict + ``threading.Lock``
out of the domain layer (``qai.chat.domain.model_profiles``) — domain
code must be pure (no process-global mutable state, no threading
primitives).  The *state* now lives here, behind the port, owned by
the application/adapters layer where holding a lock + a mutable cache
is legitimate.

Behaviour mirrors the legacy ``backend/model_profiles.py`` runtime
cache 1:1 (``_runtime_limits[model_id]["max_tokens_max"] = N``): after
the upstream API rejects an oversized ``max_tokens`` with a
``"expected a value <= N"`` style 400 the learned ceiling is cached and
applied on subsequent requests for the same model id.

The store is wired as a process singleton in ``apps/api/_chat_di`` so
the learned limits survive across requests for the process lifetime,
matching V1's process-global dict.
"""

from __future__ import annotations

import threading

__all__ = ["InMemoryRuntimeLimitStore"]


class InMemoryRuntimeLimitStore:
    """Thread-safe in-memory implementation of ``RuntimeLimitStorePort``.

    Holds a ``{model_id: {key: int}}`` map guarded by a
    :class:`threading.Lock` so concurrent stream turns can record /
    read learned limits without interleaving corruption.
    """

    __slots__ = ("_lock", "_limits")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._limits: dict[str, dict[str, int]] = {}

    def record_limit(self, *, model_id: str, max_tokens_max: int) -> None:
        if not model_id or max_tokens_max <= 0:
            return
        with self._lock:
            self._limits.setdefault(model_id, {})["max_tokens_max"] = max_tokens_max

    def get_limit(self, *, model_id: str, key: str) -> int | None:
        with self._lock:
            return self._limits.get(model_id, {}).get(key)

    def clear(self, *, model_id: str | None = None) -> None:
        with self._lock:
            if model_id is None:
                self._limits.clear()
            else:
                self._limits.pop(model_id, None)
