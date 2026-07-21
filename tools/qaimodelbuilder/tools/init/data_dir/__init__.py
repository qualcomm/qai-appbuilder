# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``data_dir`` — fresh empty ``data/`` initialisation (PR-061).

This package implements the first step of the fresh-install
workflow (refactor-plan §9.4.10). It does NOT migrate any legacy
data — the project ships zero user history.

Public entry point: :func:`run` (see :mod:`tools.init.data_dir.runner`).

Operations
----------
On ``apply``, the initialiser performs these steps idempotently:

1. **Create directory tree** under ``--data-root`` (typically the new
   ``data/``):

   - ``db/``                       (parent of ``qai.db`` + WAL)
   - ``db/backups/``               (alembic-style backup folder)
   - ``blobs/chat/``
   - ``blobs/app_builder/``
   - ``blobs/uploads/audio/``
   - ``blobs/uploads/images/``
   - ``audit/app_builder/``        (per-day jsonl rotation target)
   - ``cache/``                    (remote-fetch caches)
   - ``secrets/``                  (FileSecretStore fallback root;
                                    permission-restricted)
   - ``tmp/``                      (transient workspace)

2. **Create empty ``data/db/qai.db``** and apply all 7 SQL migrations
   from ``src/qai/platform/persistence/migrations_sql/`` via the
   project's :class:`qai.platform.persistence.MigrationRunner`.

3. **Copy ``factory/user_config.toml``** to ``data/user_config.toml``
   if a defaults file is present. If absent, write a minimal
   placeholder so PR-062 / PR-063 have something to attach to.

4. **Write a ``data/init_manifest.json``** describing what was
   created (consumed by ``verify`` mode and by PR-064 e2e tests).

Idempotency
-----------
- Existing directories are NOT overwritten.
- An existing ``qai.db`` with at least one applied migration row is
  NOT recreated; the runner instead applies only pending migrations.
- An existing ``user_config.toml`` is NOT overwritten (the user may
  have hand-edited it post-install).

These rules let the initialiser be safely re-run during upgrades when
new SQL migrations are added.

Verify mode
-----------
``verify`` reads the on-disk ``data/`` and checks:

- every expected directory exists
- ``qai.db`` exists and has ALL discovered migrations applied
- the SQL schema contains the 35 expected tables (see schema doc §0.2)
- ``user_config.toml`` is parseable

It returns non-zero on any miss.
"""

from __future__ import annotations

from .runner import EXPECTED_DIRS, EXPECTED_TABLES, run

__all__ = ["EXPECTED_DIRS", "EXPECTED_TABLES", "run"]
