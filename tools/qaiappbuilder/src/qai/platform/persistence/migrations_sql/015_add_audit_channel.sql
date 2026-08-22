-- ============================================================================
-- Migration 015: security_audit_entry.channel column (V1 audit-filter parity)
--
-- Adds the originating channel to audit records so the Security → Audit
-- view can filter decisions by channel (V1 SecurityConfigPanel.js audit
-- filter: web / wechat / feishu / system / child_process). The channel is
-- threaded in from the request context at the apps/api layer and recorded
-- on the AuditEntry; internal/system actions (skill register, ACL apply)
-- record NULL (no originating channel), which the UI renders as "—".
--
-- Tail-appended NULLABLE column (no default): every audit row written
-- before this migration reads back as channel = NULL, exactly as before
-- (zero behaviour change for existing records). Kept as free TEXT (no
-- CHECK) so the trail can also store historical/foreign channel names.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

ALTER TABLE security_audit_entry
    ADD COLUMN channel TEXT;
