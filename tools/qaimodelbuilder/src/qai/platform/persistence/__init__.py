# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.platform.persistence — Persistence primitives.

Submodules:
- ``database`` — async SQLite engine wrapper with health check + safe defaults.
- ``migrations`` — schema migration runner (sequential SQL files).
- ``secrets`` — SecretStore port + keyring / file backends (PR-013).

Usage in apps/api/lifespan.py (see PR-014):
    db = Database(path=settings.data_paths().db_path())
    await db.start()
    await migrate(db, migrations_dir=...)
"""

from __future__ import annotations

from .database import Database, DatabaseHealth
from .migrations import Migration, MigrationRunner, migrate

__all__ = [
    "Database",
    "DatabaseHealth",
    "Migration",
    "MigrationRunner",
    "migrate",
]
