# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports for ``qai.user_prefs`` (PR-601a).

The BC exposes a single :class:`UserPrefsRepositoryPort` because every
endpoint operates on the same shape: load a JSON document under a
namespace key, optionally merge updates, persist.  The
namespace-specific endpoints layered on top of this port — ``GET/PUT
/api/settings/{dep_broker,exec_broker,process_proxy,file_broker,
project_snapshot,file_watcher}``, ``GET/POST /api/proxy``, and
``GET/POST/DELETE /api/code-personas[/select|/{persona_id}]`` — each
add their own application-level invariants in the use cases (PR-601b)
without changing this port surface.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from qai.user_prefs.domain import PrefsDocument, PrefsKey

__all__ = ["UserPrefsRepositoryPort"]


@runtime_checkable
class UserPrefsRepositoryPort(Protocol):
    """Persistence for user-preference JSON documents (KV-style).

    Implementations are expected to back the store with the shared
    ``kv_user_prefs`` table (migration 007); see
    :class:`qai.user_prefs.adapters.KvUserPrefsRepository` for the
    canonical adapter.

    All methods are async because the production adapter is
    aiosqlite-backed; in-memory adapters used by unit tests can be
    sync wrappers exposed via ``async def`` shims.

    Contract:

    * :meth:`load` returns ``{}`` for absent keys (NOT ``None``); this
      keeps callers branch-free and mirrors
      :class:`qai.ai_coding.adapters.KvCodingConfigRepository`.
    * :meth:`save` performs a transactional read-modify-write inside
      the adapter; the use case never sees torn intermediate state.
    * Keys are :class:`PrefsKey` instances (already validated); the
      adapter does not re-validate.
    """

    async def load(self, key: PrefsKey) -> PrefsDocument:
        """Return the persisted document for ``key`` or ``{}``."""
        ...

    async def save(
        self,
        key: PrefsKey,
        *,
        updates: PrefsDocument,
    ) -> PrefsDocument:
        """Shallow-merge ``updates`` into the persisted doc; return result."""
        ...
