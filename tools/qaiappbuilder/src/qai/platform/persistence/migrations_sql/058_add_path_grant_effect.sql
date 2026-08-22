-- ============================================================================
-- Migration 058: security_path_grant effect (allow | deny) — constraint G
--
-- Adds the ``effect`` column that distinguishes a POSITIVE grant (a remembered
-- APPROVAL) from a NEGATIVE grant (a remembered REJECTION):
--
--   * effect = 'allow' (default) : the historical grant — the (subject, path,
--     mask, scope) is AUTHORISED, so a matching request is allowed without a
--     prompt. Every grant written before this migration reads back as
--     'allow' (byte-for-byte unchanged).
--   * effect = 'deny'            : a persisted rejection (constraint G, reject
--     side) — a matching request is DENIED without re-prompting. The decision
--     cascade consults deny-effect grants BEFORE allow-effect grants, so a
--     remembered rejection wins over a remembered approval for the same scope.
--
-- Because ``effect`` is part of the (subject, path, scope, effect) identity, an
-- allow-grant and a deny-grant may coexist for the same (path, scope); the
-- deny-first cascade ordering resolves which applies. Only ``permanent`` deny
-- grants seed the native layer (as a deny_mask op-mask rule).
--
-- Tail-appended with a default (v2.7 §3.1 additive).
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

ALTER TABLE security_path_grant
    ADD COLUMN effect TEXT NOT NULL DEFAULT 'allow'
    CHECK (effect IN ('allow', 'deny'));
