-- ============================================================================
-- Migration 049: chat_subagent_session — tree-shape rename + tail columns.
--
-- Alpha step of the unified spawn-path refactor: main / sub / grand / …
-- sub-agents are all the same thing modulo depth. The domain aggregate
-- (``SubAgentSession``) now carries:
--
--   * ``root_conversation_id`` — the top-of-tree main-agent conversation
--     (identical for every sub-agent under it, regardless of depth). This is
--     the honest name for what migration 030 called ``parent_conversation_id``
--     (which used to also stand in for "direct parent" — that meaning was
--     always wrong for grand sub-agents; every row historically pointed at
--     the root and mis-labelled itself). We RENAME the column so the semantics
--     match its actual usage. This is a Clean-Cutover rename per v2.7 §2 (zero
--     遗产共存 / zero backward-compat期); the user explicitly authorised
--     destructive DB changes for this step.
--
--   * ``parent_subagent_id`` — the DIRECT parent sub-agent (a soft TEXT
--     reference, no FK: rows delete together via the shared
--     ``root_conversation_id`` cascade — a self-FK would add cycle friction
--     without buying integrity we don't already get via the root cascade).
--     ``NULL`` = my direct parent is the main agent (I am a depth-1 sub-agent).
--
--   * ``depth`` — recursion depth (1 = first-level, 2 = grand, 3 = great-grand,
--     …). Legacy rows default to 1 — which is truthful for every row that was
--     written before this migration (the pre-α ``_spawn_grand_sub_agent`` grand
--     branch was locked to ``allow_spawn=False`` and its transient nested
--     session was never persisted with a distinct depth signal — the "grand"
--     only appeared in the tool result string).
--
-- IMPLEMENTATION: SQLite 3.25+ ``ALTER TABLE ... RENAME COLUMN`` + two
-- ``ADD COLUMN`` statements. The runtime Python 3.13 SQLite库 bundled with
-- our Windows ARM64 venv ships with SQLite well past 3.25 (verified on both
-- fresh and legacy DBs), so the in-place rename is safe.
--
-- Why NOT the classic "CREATE new + INSERT SELECT + DROP + RENAME" swap:
-- with ``PRAGMA foreign_keys = ON`` (the runner sets it) a ``DROP TABLE``
-- on the original table fires ``ON DELETE SET NULL`` for every row of
-- ``chat_participant.subagent_session_id`` — severing the participant →
-- sub-agent link on every existing row. ``PRAGMA foreign_keys = OFF`` is a
-- no-op inside a transaction (which the runner wraps around this file), so
-- the classic swap loses data on upgrade. The in-place ALTER preserves the
-- referencing table's FK values without touching them at all.
--
-- CHECK constraint on ``depth``: SQLite ``ALTER TABLE ADD COLUMN`` supports
-- ``CHECK`` and ``NOT NULL DEFAULT`` in the definition (as long as the
-- default satisfies the constraint), so the invariant ``depth >= 1`` is
-- enforced from day one — matching the domain aggregate's own check.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

-- Rename in place. Any indexes / FK edges declared on the old column name
-- (``ix_chat_subagent_session_parent`` + ``ix_chat_subagent_session_parent_updated``
-- from migration 030, and the FK from ``chat_participant`` — though that FK
-- references THIS table's PK, not the renamed column, so it stays intact
-- regardless) are automatically updated by SQLite to point at the new name.
ALTER TABLE chat_subagent_session
    RENAME COLUMN parent_conversation_id TO root_conversation_id;

-- Direct-parent tree edge. Soft TEXT reference (no FK), documented above.
-- Nullable + default NULL so every existing row defaults to "depth-1 = my
-- direct parent is the main agent", which is truthful for every pre-α row.
ALTER TABLE chat_subagent_session
    ADD COLUMN parent_subagent_id TEXT;

-- Recursion depth. ``NOT NULL DEFAULT 1`` so existing rows land at depth-1
-- (truthful — the pre-α ``_spawn_grand_sub_agent`` branch never persisted a
-- distinct depth signal). CHECK enforces the domain invariant.
ALTER TABLE chat_subagent_session
    ADD COLUMN depth INTEGER NOT NULL DEFAULT 1 CHECK (depth >= 1);

-- The migration 030 indexes still exist under their original names, but
-- SQLite automatically updated their column reference (the RENAME above
-- propagates into every index / trigger / view / foreign key definition
-- that mentions the old column name). We rename them here so the names
-- match the honest column too — otherwise ``ix_chat_subagent_session_parent``
-- would keep suggesting the column was "parent_conversation_id".
DROP INDEX IF EXISTS ix_chat_subagent_session_parent;
DROP INDEX IF EXISTS ix_chat_subagent_session_parent_updated;
CREATE INDEX ix_chat_subagent_session_root
    ON chat_subagent_session(root_conversation_id);
CREATE INDEX ix_chat_subagent_session_root_updated
    ON chat_subagent_session(root_conversation_id, updated_at DESC);

-- New sparse index for walking one sub-agent's direct children (hot path for
-- the unified spawn tree). Sparse (most rows have NULL parent_subagent_id —
-- they are depth-1) so cheap to maintain.
CREATE INDEX ix_chat_subagent_session_parent_subagent
    ON chat_subagent_session(parent_subagent_id)
    WHERE parent_subagent_id IS NOT NULL;
