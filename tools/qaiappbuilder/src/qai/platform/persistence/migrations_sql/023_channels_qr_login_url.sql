-- ============================================================================
-- Migration 023: channels_qr_login_challenge qr_url column (V1-parity)
--
-- Adds one nullable TEXT column to channels_qr_login_challenge so the REAL
-- QR-login URL obtained from the wechatbot SDK's on_qr_url callback can be
-- persisted on the challenge row and rendered by GET /api/{kind}/qr/{id}/image.
--
-- V1 parity: the legacy WeChat channel stored the SDK URL in a
-- module-level _qr_url, and the QR route encoded
-- THAT real URL into the QR PNG.  V2 previously discarded the SDK URL (only
-- logged it) and rendered a placeholder qai-channel-qr:// URI that WeChat
-- cannot scan -- this column restores the V1 user-facing behaviour (a
-- scannable WeChat login QR).
--
-- NULL for challenges that have not yet received a URL from the SDK (the
-- route layer returns 404 for image requests until the URL arrives, matching
-- V1 behaviour).
--
-- Done as a standalone ALTER migration (NOT by editing 005) so existing
-- databases that already applied 005 are upgraded in-place; the schema
-- migration runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT -- file MUST NOT contain them.
-- ============================================================================


ALTER TABLE channels_qr_login_challenge ADD COLUMN qr_url TEXT;
