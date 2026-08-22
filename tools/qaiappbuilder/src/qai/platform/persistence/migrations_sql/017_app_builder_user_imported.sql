-- ============================================================================
-- Migration 017: app_builder_model_definition.user_imported provenance column
--
-- Adds one boolean-int column recording whether a model was imported by the
-- user (1) or seeded as a built-in from a bundled Pack (0). Built-in models
-- are protected from deletion (V1 parity: only user-imported models may be
-- removed) — the DELETE use case rejects removal of rows where user_imported=0
-- with HTTP 403.
--
-- DEFAULT 0 means every pre-existing row (all built-in seeds, since import is
-- a newer capability) is treated as built-in / protected without any data
-- migration. The seed path explicitly writes 0; the import path writes 1.
--
-- Done as a standalone ALTER migration (NOT by editing 003) so existing
-- databases that already applied 003 are upgraded in-place; the schema
-- migration runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE app_builder_model_definition
    ADD COLUMN user_imported INTEGER NOT NULL DEFAULT 0 CHECK (user_imported IN (0, 1));
