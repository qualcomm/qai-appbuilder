-- ============================================================================
-- Migration 027: app_builder_model_definition.version semver column
--
-- Adds one text column recording the model's semantic version (e.g.
-- "1.0.0"). V1 carried the version inside the Pack ``manifest.json`` and
-- bumped it on re-import under conflict_policy="bump"
-- (the legacy ``_bump_patch`` helper). V2 stores models in the
-- DB, so the version needs a column of its own — the import-commit path writes
-- the manifest version (and a bumped patch when conflict_policy="bump").
--
-- DEFAULT '1.0.0' means every pre-existing row (all built-in seeds) gets a
-- sane semver without any data migration; the seed path leaves it at the
-- default and the import path overwrites it from the Pack manifest.
--
-- Done as a standalone ALTER migration (NOT by editing 003) so existing
-- databases that already applied 003 are upgraded in-place; the schema
-- migration runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE app_builder_model_definition
    ADD COLUMN version TEXT NOT NULL DEFAULT '1.0.0';
