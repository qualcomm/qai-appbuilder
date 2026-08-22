-- ============================================================================
-- Migration 056: multi-language (i18n) support for built-in discussion
--                templates + participant template provenance.
--
-- Goal: built-in agent / roster / mode presets ship their business text
-- (name / description / persona / framing / member display_name) in every
-- supported UI language (en / zh-CN / zh-TW) so both the UI display AND the
-- prompt text injected into the LLM follow the user's chosen language.
--
-- Strategy (forward-compatible, AGENTS.md section 8 "Schema
-- Forward-Compatibility"): add nullable ``*_i18n_json`` sidecar columns that
-- carry a per-locale JSON map, e.g.
--     {"en": "...", "zh-CN": "...", "zh-TW": "..."}
-- The original single-language columns (name / description / persona /
-- framing / members_json) STAY UNCHANGED as the canonical fallback. Read
-- code resolves as: "if the *_i18n_json column exists and has the requested
-- locale, use that translation; otherwise fall back to the original column."
-- Custom (is_builtin=0) rows leave the i18n columns NULL and always fall back
-- to the user's own text, so they are never translated.
--
-- Runtime persona override (method A): built-in roles/teams copy their persona
-- into the conversation's chat_participant row at import time, so once
-- imported a participant row could not previously be traced back to its source
-- template. Adding a nullable ``template_id`` column records that provenance,
-- letting the discussion orchestrator re-resolve a built-in participant's
-- persona by (template_id + current locale) at runtime -- so switching the UI
-- language re-localises even already-imported built-in roles. NULL means
-- "not sourced from a template" (user-authored / main / sub-agent) -> no
-- override, existing behaviour byte-for-byte unchanged.
--
-- ZERO legacy-data migration (AGENTS.md section 8): every column below is
-- nullable with no default. Pre-existing rows read back NULL for every new
-- column, so old data + old-shaped rows keep working exactly as before:
--   * NULL *_i18n_json  -> fall back to the original single-language column;
--   * NULL template_id  -> no runtime persona override.
-- Deliberately NO FOREIGN KEY on template_id (mirrors cloned_from_id in 043):
-- a built-in preset may be re-seeded with the same fixed id across installs
-- and a stale reference simply yields "no override", never corruption.
--
-- runner manages BEGIN/COMMIT -- file MUST NOT contain them.
-- ============================================================================

-- Agent (single-role) template: localise name / description / display_name /
-- persona.
ALTER TABLE chat_agent_template ADD COLUMN name_i18n_json TEXT;
ALTER TABLE chat_agent_template ADD COLUMN description_i18n_json TEXT;
ALTER TABLE chat_agent_template ADD COLUMN display_name_i18n_json TEXT;
ALTER TABLE chat_agent_template ADD COLUMN persona_i18n_json TEXT;

-- Roster (team) template: localise name / description and the per-member
-- display_name + persona carried inside members_json (whole localised members
-- array stored as members_i18n_json = {"en": [...], "zh-CN": [...], ...}).
ALTER TABLE chat_roster_template ADD COLUMN name_i18n_json TEXT;
ALTER TABLE chat_roster_template ADD COLUMN description_i18n_json TEXT;
ALTER TABLE chat_roster_template ADD COLUMN members_i18n_json TEXT;

-- Mode (collaboration mode) template: localise name / description / framing.
ALTER TABLE chat_mode_template ADD COLUMN name_i18n_json TEXT;
ALTER TABLE chat_mode_template ADD COLUMN description_i18n_json TEXT;
ALTER TABLE chat_mode_template ADD COLUMN framing_i18n_json TEXT;

-- Participant provenance: which template (if any) a participant row was
-- imported from, so a built-in participant's persona can be re-localised at
-- runtime by (template_id + locale).
ALTER TABLE chat_participant ADD COLUMN template_id TEXT;
