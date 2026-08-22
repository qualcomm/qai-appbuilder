# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Adapters for ``qai.user_prefs`` (PR-601a).

PR-601a ships one adapter:

* :class:`KvUserPrefsRepository` — ``UserPrefsRepositoryPort`` impl
  backed by the shared ``kv_user_prefs`` SQLite table (migration 007).

The pattern (transactional read-modify-write under
``BEGIN IMMEDIATE``) mirrors
:class:`qai.ai_coding.adapters.coding_config_repository.KvCodingConfigRepository`
deliberately — we do not factor a shared base class into
``qai.platform`` yet because the two adapters live in different BCs
and a premature shared kernel would create a context-isolation
violation (each BC reading the other's adapter via the shared base).
"""
from qai.user_prefs.adapters.kv_repository import KvUserPrefsRepository

__all__ = ["KvUserPrefsRepository"]
