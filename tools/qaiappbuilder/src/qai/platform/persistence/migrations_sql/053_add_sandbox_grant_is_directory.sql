-- ============================================================================
-- Migration 053: security_sandbox_grant is_directory (P-11B directory grants)
--
-- Adds the ``is_directory`` flag that lets a user EXPLICITLY authorize the
-- whole directory a file lives in, instead of only that single file — the
-- "grant the whole directory" opt-in surfaced in the permission dialog.
--
--   * is_directory = 0 (default) : the grant's ``path`` is a single file and
--     matching stays EXACT string-equal (legacy single-file semantics).
--   * is_directory = 1           : the grant's ``path`` is a directory the
--     user explicitly chose; a resource whose path lies UNDER that directory
--     (path-boundary prefix, e.g. C:\foo matches C:\foo\bar but NOT
--     C:\foobar) matches. This reuses the existing directory-prefix matcher
--     (check_permission._grant_path_ancestor_of) — previously native-only —
--     for in-process / exec subjects too, but ONLY on this explicit opt-in
--     (no implicit privilege widening; the user saw and chose the directory).
--
-- Tail-appended with a default (v2.7 §3.1 additive): every grant written
-- before this migration reads back as is_directory=0 — i.e. the pre-existing
-- single-file exact-match semantics, byte-for-byte unchanged.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

ALTER TABLE security_sandbox_grant
    ADD COLUMN is_directory INTEGER NOT NULL DEFAULT 0
    CHECK (is_directory IN (0, 1));
