-- ============================================================================
-- Migration 050: security_audit_entry op + native-actor columns
--
-- Two coupled audit-trail enrichments (both tail-appended, additive):
--
--   * ``op``            — the operation kind that triggered the decision
--                         (read / write / delete / exec). Lets the audit view
--                         distinguish a delete from a plain write even though
--                         both are evaluated against the write permission bit
--                         (SEC-ENHANCE-TOOLCOVER 1-B).
--   * ``process_path``  — real image path of the process that triggered a
--                         NATIVE sub-process file event (guard64.dll V2 events
--                         carry it; in-process tool events store '').
--   * ``command_line``  — command line of that triggering sub-process.
--   * ``actor_pid``     — PID of the triggering sub-process (NULL for
--                         in-process tool events).
--   * ``actor_parent_pid`` — parent PID of the triggering sub-process.
--
-- Together the four actor columns let the Security → Audit view attribute a
-- native sub-process file write to the concrete executable + command line
-- that caused it (SEC-ENHANCE-AUDITUX 3-B); previously the native ASK bridge
-- dropped this metadata before it reached the audit sink.
--
-- All columns are tail-appended and defaulted (v2.7 §3.1 additive): the three
-- TEXT columns are NOT NULL DEFAULT '' and the two pid columns are NULLABLE
-- INTEGER. Every audit row written before this migration reads back with
-- op = '' / process_path = '' / command_line = '' / actor_pid = NULL /
-- actor_parent_pid = NULL — zero behaviour change for existing records.
-- Kept as free TEXT/INTEGER (no CHECK) to avoid coupling the audit trail to
-- a fixed op vocabulary.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================

ALTER TABLE security_audit_entry
    ADD COLUMN op TEXT NOT NULL DEFAULT '';

ALTER TABLE security_audit_entry
    ADD COLUMN process_path TEXT NOT NULL DEFAULT '';

ALTER TABLE security_audit_entry
    ADD COLUMN command_line TEXT NOT NULL DEFAULT '';

ALTER TABLE security_audit_entry
    ADD COLUMN actor_pid INTEGER;

ALTER TABLE security_audit_entry
    ADD COLUMN actor_parent_pid INTEGER;
