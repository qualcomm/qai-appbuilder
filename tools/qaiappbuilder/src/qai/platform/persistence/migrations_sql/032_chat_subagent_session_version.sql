-- ============================================================================
-- Migration 032: chat_subagent_session.version optimistic-lock column
--
-- Adds one integer column used as an OPTIMISTIC-LOCK version (compare-and-swap)
-- on the single-row sub-agent session aggregate, so concurrent writers cannot
-- silently clobber each other's whole-row UPSERT.
--
-- Why this is needed (block 4 — concurrency consistency): under the SHARED
-- ownership model a user can take over a sub-agent (its own SSE turn writing
-- back the session) AT THE SAME TIME the main agent re-wakes (``resume``) the
-- same subagent_id (its sub-agent loop also writing back). Both paths
-- whole-row UPSERT into chat_subagent_session with NO version guard, so a
-- last-writer-wins race could drop the other writer's wire turns. A version
-- column lets ``save`` do a compare-and-swap (update only when the stored
-- version matches the loaded one, then bump it) and surface a conflict instead
-- of a silent overwrite.
--
-- DEFAULT 0 means every pre-existing row gets version 0 with no data
-- migration; the repository bumps it on each successful save.
--
-- Done as a standalone ALTER migration (NOT by editing 030) so existing
-- databases that already applied 030 are upgraded in-place; the migration
-- runner applies each versioned file exactly once.
--
-- runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


ALTER TABLE chat_subagent_session
    ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
