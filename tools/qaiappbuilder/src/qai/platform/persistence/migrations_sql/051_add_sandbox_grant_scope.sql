-- ============================================================================
-- Migration 051: security_sandbox_grant scope_kind + scope_key (真 scoping)
--
-- Makes the grant ``session`` / ``process`` scopes REAL instead of the former
-- "just a TTL on a global grant" behaviour:
--
--   * scope_kind — one of once / session / process / permanent. Governs
--     *where* the grant applies (complements expires_at = *when*).
--       - permanent : applies everywhere, forever (seeds native whitelist).
--       - process   : applies only in the current backend process
--                      (scope_key = process boot id; a restart mints a new
--                      boot id → old process grants stop matching).
--       - session   : applies only within one collaboration session
--                      (scope_key = TOP-LEVEL conversation id; shared by the
--                      main agent + all sub-agents / participants of that
--                      session; stops matching in a different conversation).
--       - once      : never persisted (approve path handles single-shot).
--   * scope_key  — the discriminator paired with scope_kind (conversation id
--                  for session; boot id for process; '' for permanent).
--
-- Both columns are tail-appended with defaults (v2.7 §3.1 additive): every
-- grant written before this migration reads back as
-- scope_kind='permanent' / scope_key='' — i.e. the pre-existing
-- process-global, non-expiring semantics, byte-for-byte unchanged. The
-- effective uniqueness of a grant is enforced at the use-case layer
-- (CreateSandboxGrantUseCase) over (subject, path, scope_kind, scope_key),
-- so the same path may now hold e.g. both a permanent grant and a
-- session-scoped grant without clobbering each other.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

ALTER TABLE security_sandbox_grant
    ADD COLUMN scope_kind TEXT NOT NULL DEFAULT 'permanent'
    CHECK (scope_kind IN ('once', 'session', 'process', 'permanent'));

ALTER TABLE security_sandbox_grant
    ADD COLUMN scope_key TEXT NOT NULL DEFAULT '';

-- Hot-path index for the scoped active-grant lookup
-- (subject + scope discriminator).
CREATE INDEX IF NOT EXISTS ix_security_sandbox_grant_scope
    ON security_sandbox_grant(
        subject_kind, subject_identifier, scope_kind, scope_key
    );
