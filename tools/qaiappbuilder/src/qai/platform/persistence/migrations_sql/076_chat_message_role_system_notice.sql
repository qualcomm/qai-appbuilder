-- ---------------------------------------------------------------------
-- Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
-- SPDX-License-Identifier: BSD-3-Clause
-- ---------------------------------------------------------------------
--
-- Widen chat_message.role CHECK to accept 'system_notice' — a first-class
-- transcript role for background-task completion notifications
-- (sub-agent finished / background exec job exited).  Persisting these
-- notices as their own role instead of masquerading as role='user' keeps
-- history reload / compaction / any user-turn analytics honest.  See
-- MessageRole.SYSTEM_NOTICE in src/qai/chat/domain/content.py and
-- docs/70-multi-agent/complete-solution-plan-2026-08-08.md §12.3.
--
-- Implementation:
-- The chat_message table has FTS5 triggers (migration 016) hanging off
-- it.  A "rebuild + rename" style migration would drop the triggers as
-- a side effect of DROP TABLE, forcing us to re-create four artefacts
-- (three triggers + one virtual-table wiring).  Instead we use SQLite's
-- documented ``PRAGMA writable_schema=ON`` escape hatch to rewrite the
-- CREATE TABLE text in sqlite_master directly — the actual on-disk
-- rows, indexes, and triggers all remain untouched.  See
-- https://www.sqlite.org/lang_altertable.html §"Making Other Kinds Of
-- Table Schema Changes".

PRAGMA writable_schema = ON;

UPDATE sqlite_master
SET sql = replace(
    sql,
    'CHECK (role IN (''system'', ''user'', ''assistant'', ''tool''))',
    'CHECK (role IN (''system'', ''user'', ''assistant'', ''tool'', ''system_notice''))'
)
WHERE type = 'table'
  AND name = 'chat_message';

PRAGMA writable_schema = OFF;
