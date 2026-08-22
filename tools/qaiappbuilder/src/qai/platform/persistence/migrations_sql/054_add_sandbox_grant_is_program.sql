-- ============================================================================
-- Migration 054: security_sandbox_grant is_program (permanent per-program allow)
--
-- Adds the ``is_program`` flag that lets a user EXPLICITLY authorize a whole
-- command PROGRAM (binary) instead of a single exact command string — the
-- "permanently allow this program" opt-in surfaced in the permission dialog
-- for exec commands (the "program" grant_range).
--
--   * is_program = 0 (default) : the grant's ``path`` is either an exact
--     command string / file / directory (existing semantics, unchanged).
--   * is_program = 1           : the grant's ``path`` is a NORMALIZED command
--     binary token the user chose (e.g. ``powershell``); ANY exec command
--     whose extracted binary equals it matches — so the user stops being
--     asked for every individual powershell invocation. Only meaningful for
--     kind="exec" resources; mutually exclusive with is_directory.
--
-- Tail-appended with a default (v2.7 §3.1 additive): every grant written
-- before this migration reads back as is_program=0 — i.e. the pre-existing
-- exact-string / directory semantics, byte-for-byte unchanged.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

ALTER TABLE security_sandbox_grant
    ADD COLUMN is_program INTEGER NOT NULL DEFAULT 0
    CHECK (is_program IN (0, 1));
