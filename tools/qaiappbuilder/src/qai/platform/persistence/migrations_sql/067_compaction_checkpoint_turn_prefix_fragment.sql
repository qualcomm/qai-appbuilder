-- ============================================================================
-- Migration 067: Turn-prefix fragment table for compaction checkpoint (Task R)
--
-- Splits ``turn_prefix_summaries_json`` out of ``chat_compaction_checkpoint``
-- (migration 062) into ``chat_compaction_checkpoint_turn_prefix``. The
-- fragment carries the ``core_generation`` snapshot the writer observed at
-- ``load`` time; a stale-generation write is refused at CAS time so a P3
-- turn-prefix summary written against an old core cannot overwrite state
-- belonging to a newer core.
--
-- Rationale (see 066 for details): fragment tables let each writer own its
-- own row, so digest (P2) and turn-prefix (P3) never contend over the same
-- SQL row. The legacy ``turn_prefix_summaries_json`` column is DEPRECATED but
-- not dropped (AGENTS.md §8 additive-only); the application-layer readers
-- stop consulting it once this migration lands.
--
-- The backfill copies every pre-Task-R conversation's legacy
-- ``turn_prefix_summaries_json`` into the fragment table when the payload is
-- a non-empty string, seeding fragments so they light up immediately without
-- waiting for the next P3 kick.
--
-- FOREIGN KEY (conversation_id) → chat_compaction_checkpoint(conversation_id)
-- with ON DELETE CASCADE mirrors 066 — a ``/compact clear`` on the core
-- automatically cascades to this fragment.
--
-- Idempotence: same guarantees as 066 (runner applied-set + NOT EXISTS
-- guard). No transaction statements.
-- ============================================================================

CREATE TABLE IF NOT EXISTS chat_compaction_checkpoint_turn_prefix (
    conversation_id     TEXT    NOT NULL PRIMARY KEY,
    summaries_json      TEXT    NOT NULL,
    core_generation     INTEGER NOT NULL,
    FOREIGN KEY (conversation_id)
        REFERENCES chat_compaction_checkpoint (conversation_id)
        ON DELETE CASCADE
);

INSERT INTO chat_compaction_checkpoint_turn_prefix (
    conversation_id, summaries_json, core_generation
)
SELECT
    c.conversation_id,
    c.turn_prefix_summaries_json,
    COALESCE(c.generation, 1)
FROM chat_compaction_checkpoint AS c
WHERE c.turn_prefix_summaries_json IS NOT NULL
  AND TRIM(c.turn_prefix_summaries_json) <> ''
  AND NOT EXISTS (
      SELECT 1 FROM chat_compaction_checkpoint_turn_prefix AS t
      WHERE t.conversation_id = c.conversation_id
  );
