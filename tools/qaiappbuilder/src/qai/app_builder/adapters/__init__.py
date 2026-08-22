# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""SQLite / non-streaming adapters for the App Builder context (PR-045).

Each adapter implements one or more of the Ports defined in
:mod:`qai.app_builder.application.ports`. The design rules follow
PR-040's security-context template: aiosqlite under
``async with self._db.connection()``, explicit ``await conn.commit()``
on writes, ``BEGIN IMMEDIATE`` for multi-row aggregates, and
``IntegrityError``-style failures translated into domain conflict errors.

S9 close adds two adapters that wire the lane-2 ``feedback`` /
``benchmark`` routes to real persistence (replacing the surface-only
acknowledgements left by PR-304):

* :class:`SqliteFeedbackRepository` — backs
  ``POST /api/app-builder/feedback``;
* :class:`SqliteBenchmarkRepository` — backs
  ``POST /api/app-builder/benchmark`` and the new
  ``GET /api/app-builder/benchmark/{benchmark_id}`` poll route.
"""

from __future__ import annotations

from .app_model_repository import SqliteAppModelRepository
from .benchmark_repository import SqliteBenchmarkRepository
from .feedback_repository import SqliteFeedbackRepository
from .run_repository import SqliteRunRepository
from .share_repository import SqliteShareRepository
from .voice_pref_repository import SqliteVoicePrefRepository
from .worker_status import StickyWorkerStatusAdapter

__all__ = [
    "SqliteAppModelRepository",
    "SqliteBenchmarkRepository",
    "SqliteFeedbackRepository",
    "SqliteRunRepository",
    "SqliteShareRepository",
    "SqliteVoicePrefRepository",
    "StickyWorkerStatusAdapter",
]
