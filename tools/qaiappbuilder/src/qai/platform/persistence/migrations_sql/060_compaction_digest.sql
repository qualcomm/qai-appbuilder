-- ============================================================================
-- Migration 060: Session digest for compaction checkpoint (P2)
--
-- Adds ``digest_text`` and ``digest_updated_at`` to
-- ``chat_compaction_checkpoint``. Both columns are NULL for rows written
-- before this migration ran; the wire-assembly path treats a NULL digest as
-- absent (no [Session Digest] block injected), so a pre-migration-060 row is
-- byte-for-byte identical to a fresh row whose digest has not been generated
-- yet. Rehydration (``_row_to_checkpoint``) maps NULL back to the dataclass's
-- ``None`` defaults on both fields.
--
-- Additive only (AGENTS.md §8): two new nullable columns on an existing table.
-- No existing column, index, or FK is touched.
--
-- The digest text is written by
-- :class:`qai.chat.application.use_cases.refresh_digest.RefreshDigestUseCase`
-- (CONTEXT-COMPRESSION-NEXT §6). The task is fire-and-forget on the ON_TRUNCATE
-- path, and the use case guards the write against a conversation that has
-- been deleted between the kick and the write. ``digest_updated_at`` is an
-- ISO-8601 UTC timestamp (single source of truth for "when did the digest
-- last evolve"), separate from the checkpoint's ``updated_at`` because the
-- digest refresh runs asynchronously AFTER the checkpoint is written.
--
-- No ``digest_schema_version`` column is added (v3.1 appendix: intentionally
-- deferred to avoid over-design in this phase). The digest is opaque text
-- consumed only by the wire-injection reader; a future schema change would
-- introduce that column in a follow-up migration.
--
-- Idempotence: the migration runner records applied ids in
-- ``_qai_schema_migrations`` and skips already-applied files (see
-- ``qai.platform.persistence.migrations.MigrationRunner``), so a repeated
-- process start never replays this file. SQLite's ``ADD COLUMN`` has no
-- ``IF NOT EXISTS`` clause; the runner's applied-set is the only guard we
-- need.
--
-- Standalone CREATE migration (NOT by editing 044 or 059): existing databases
-- upgrade in-place. The runner manages BEGIN/COMMIT — this file MUST NOT
-- contain transaction statements.
-- ============================================================================

ALTER TABLE chat_compaction_checkpoint
    ADD COLUMN digest_text TEXT;

ALTER TABLE chat_compaction_checkpoint
    ADD COLUMN digest_updated_at TEXT;
