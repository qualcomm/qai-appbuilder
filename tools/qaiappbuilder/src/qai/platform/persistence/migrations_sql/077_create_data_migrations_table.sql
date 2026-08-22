-- ---------------------------------------------------------------------
-- Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
-- SPDX-License-Identifier: BSD-3-Clause
-- ---------------------------------------------------------------------
--
-- Migration 077: create the _qai_data_migrations sentinel table.
--
-- Distinct from _qai_schema_migrations (which tracks the sequential SQL
-- DDL migrations in this directory): the new table tracks the completion
-- of one-shot RUNTIME DATA migrations that live under scripts/migrate/*
-- and are invoked from apps/api/lifespan.py on every startup. Those
-- migrators typically span multiple stores (SQLite + data/user_config.toml
-- + SecretStore JWT) and therefore cannot be expressed as a single .sql
-- file consumed by MigrationRunner.
--
-- Version numbering is deliberately independent (not sequential, no
-- gap-check): each record uses a semantic id like
-- "qai_service_route1_2026_08" so that data migrations can be added or
-- deleted without touching the strict contiguous numbering rule enforced
-- for schema migrations by _validate_no_gaps in migrations.py.
--
-- Callers write to this table via INSERT OR IGNORE inside their own
-- transactions, AFTER all mutations succeed. A migrator that fails
-- partway must NOT write its sentinel row so the next boot retries.
-- Each migrator MUST be idempotent (see scripts/migrate/__init__.py).

CREATE TABLE IF NOT EXISTS _qai_data_migrations (
    id         TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
