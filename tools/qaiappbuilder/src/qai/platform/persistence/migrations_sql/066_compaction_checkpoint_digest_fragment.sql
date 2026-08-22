-- ============================================================================
-- Migration 066: Digest fragment table for compaction checkpoint (Task R)
--
-- Splits ``digest_text`` + ``digest_updated_at`` out of
-- ``chat_compaction_checkpoint`` (migration 060) into their own single-row-
-- per-conversation aggregate ``chat_compaction_checkpoint_digest``. The
-- fragment carries an explicit ``core_generation`` snapshot so a writer that
-- read the core row at generation G can be REFUSED at write time if the core
-- row has since advanced past G (see :meth:`SqliteCompactionCheckpointRepository.
-- save_digest`). This eliminates the "P2 digest ↔ P3 turn-prefix" whole-row
-- overwrite race by giving each fragment its own row.
--
-- The legacy columns on ``chat_compaction_checkpoint`` are DEPRECATED but not
-- dropped (AGENTS.md §8 additive-only). The application-layer readers stop
-- consulting them once this migration lands — the fragment table is now the
-- single source of truth for the digest. The backfill below seeds one fragment
-- row for every existing checkpoint whose legacy ``digest_text`` was non-empty,
-- so pre-Task-R conversations light up the fragment immediately without
-- waiting for the next digest refresh.
--
-- ``core_generation = 1`` on backfilled rows matches the value migration 065
-- assigned to legacy checkpoints (the DEFAULT on the new ``generation`` column
-- is 1) — so a fragment written today by a fresh writer that reads the same
-- pre-migration core row observes the SAME generation and CAS succeeds.
--
-- FOREIGN KEY (conversation_id) → chat_compaction_checkpoint(conversation_id)
-- with ON DELETE CASCADE: dropping the core checkpoint automatically wipes
-- the digest fragment. ``foreign_keys=ON`` is enforced globally by
-- ``Database._INIT_PRAGMAS`` (see ``qai.platform.persistence``).
--
-- Idempotence: the migration runner records applied ids in
-- ``_qai_schema_migrations`` and skips already-applied files. The runner
-- manages BEGIN/COMMIT — this file MUST NOT contain transaction statements.
-- The ``INSERT ... WHERE NOT EXISTS`` backfill also guarantees that even if
-- this migration were somehow replayed the digest rows would not duplicate.
-- ============================================================================

CREATE TABLE IF NOT EXISTS chat_compaction_checkpoint_digest (
    conversation_id     TEXT    NOT NULL PRIMARY KEY,
    digest_text         TEXT    NOT NULL,
    digest_updated_at   TEXT    NOT NULL,
    core_generation     INTEGER NOT NULL,
    FOREIGN KEY (conversation_id)
        REFERENCES chat_compaction_checkpoint (conversation_id)
        ON DELETE CASCADE
);

-- Backfill: seed one fragment row per existing checkpoint that carries a
-- non-empty legacy digest. Legacy rows written by migration 060 stored
-- ``digest_text`` verbatim (whitespace-only was collapsed to NULL by the
-- application writer). We also refuse to insert if the row already exists
-- (a repeated migration run inside the same DB — belt-and-suspenders on top
-- of the runner's applied-set).
INSERT INTO chat_compaction_checkpoint_digest (
    conversation_id, digest_text, digest_updated_at, core_generation
)
SELECT
    c.conversation_id,
    c.digest_text,
    COALESCE(c.digest_updated_at, c.updated_at),
    COALESCE(c.generation, 1)
FROM chat_compaction_checkpoint AS c
WHERE c.digest_text IS NOT NULL
  AND TRIM(c.digest_text) <> ''
  AND NOT EXISTS (
      SELECT 1 FROM chat_compaction_checkpoint_digest AS d
      WHERE d.conversation_id = c.conversation_id
  );
