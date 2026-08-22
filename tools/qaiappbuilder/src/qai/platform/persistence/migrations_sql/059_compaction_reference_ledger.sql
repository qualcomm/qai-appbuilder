-- ============================================================================
-- Migration 059: Reference ledger for compaction checkpoint (P1.b)
--
-- Adds ``reference_ledger_json`` to ``chat_compaction_checkpoint``. NULL for
-- rows written before this migration ran; the wire-assembly path treats the
-- ledger as empty in that case, so an old row is byte-for-byte identical to a
-- fresh row whose ledger has not been populated yet.
--
-- Additive only (AGENTS.md §8): a new nullable column on an existing table.
-- ``_row_to_checkpoint`` maps NULL back to ``checkpoint.reference_ledger = None``
-- so the dataclass's default is preserved.
--
-- Idempotence: the migration runner records applied ids in
-- ``_qai_schema_migrations`` and skips already-applied files (see
-- ``qai.platform.persistence.migrations.MigrationRunner``), so a repeated
-- process start never replays this file. SQLite's ``ADD COLUMN`` has no
-- ``IF NOT EXISTS`` clause; the runner's applied-set is the only guard we
-- need.
--
-- Standalone CREATE migration (NOT by editing 044): existing databases upgrade
-- in-place. The runner manages BEGIN/COMMIT — this file MUST NOT contain
-- transaction statements.
-- ============================================================================

ALTER TABLE chat_compaction_checkpoint
    ADD COLUMN reference_ledger_json TEXT;
