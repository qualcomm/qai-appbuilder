-- =====================================================================
-- 007_create_kv_user_prefs.sql
--
-- Shared key-value table for cross-context user preferences (schema doc
-- §6.X.2 + §10.4 decision).  Decoupled from any one bounded context so
-- frequently-mutated UI prefs from forge_config.json have a transactional
-- destination without polluting the static `data/user_config.toml` file.
--
-- Stage-A decision (schema doc §10.4): build the table now; data migration
-- from JSON is S6 PR-060+ work.
-- =====================================================================


CREATE TABLE kv_user_prefs (
    key             TEXT    NOT NULL PRIMARY KEY,
    value_json      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    CONSTRAINT ck_kv_user_prefs_key_length
        CHECK (length(key) BETWEEN 1 AND 128)
);
