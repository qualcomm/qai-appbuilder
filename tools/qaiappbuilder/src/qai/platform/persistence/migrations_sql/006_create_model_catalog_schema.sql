-- =====================================================================
-- 006_create_model_catalog_schema.sql
--
-- model_catalog bounded context (PR-025): 6 tables.
--
-- Tables (schema doc qai-db-schema.md §6):
--   6.1 model_catalog_entry                    — model aggregate root
--   6.2 model_catalog_version                  — version under an entry
--   6.3 model_catalog_download_job             — 6-state FSM
--   6.4 model_catalog_release_manifest         — CACHE header
--   6.5 model_catalog_release_manifest_entry   — composite PK rows
--   6.6 model_catalog_skill                    — skill manifest catalog
--
-- Enum values mirror the corresponding domain Enum.value strings exactly:
--   ProviderKind         → ('local','ollama','openai_compat','anthropic','generic_cloud')
--   ChecksumAlgorithm    → ('sha256','blake3')
--   ModelVersionStatus   → ('published','downloading','installed','corrupted','uninstalled')
--   DownloadJobState     → ('queued','running','paused','completed','cancelled','failed')
--
-- Cross-context references are SOFT: app_builder_model_definition
-- references model_catalog_entry only via `required_catalog_ids_json`
-- (TEXT JSON, no FOREIGN KEY).  download_job → version is also soft
-- (jobs survive version uninstall for audit history).
-- =====================================================================


-- ---------------------------------------------------------------------
-- 6.1 model_catalog_entry — model aggregate root
-- ---------------------------------------------------------------------
CREATE TABLE model_catalog_entry (
    id                      TEXT    NOT NULL PRIMARY KEY,
    name                    TEXT    NOT NULL,
    provider                TEXT    NOT NULL,
    source_url              TEXT    NOT NULL,
    description             TEXT    NOT NULL DEFAULT '',
    taxonomy_tags_json      TEXT    NOT NULL DEFAULT '[]',
    current_version_id      TEXT,
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL,
    CONSTRAINT ck_catalog_entry_provider
        CHECK (provider IN ('local','ollama','openai_compat','anthropic','generic_cloud')),
    CONSTRAINT ck_catalog_entry_name_length
        CHECK (length(name) BETWEEN 1 AND 255),
    CONSTRAINT ck_catalog_entry_source_url_length
        CHECK (length(source_url) BETWEEN 1 AND 2048),
    CONSTRAINT ck_catalog_entry_source_url_scheme
        CHECK (source_url LIKE 'http://%' OR source_url LIKE 'https://%'),
    CONSTRAINT ck_catalog_entry_description_length
        CHECK (length(description) <= 4096)
);

CREATE INDEX ix_catalog_entry_provider
    ON model_catalog_entry (provider, name);


-- ---------------------------------------------------------------------
-- 6.2 model_catalog_version — version under an entry
--
-- partial UNIQUE: at most one version per parent_model_id may be in
-- 'downloading' state at a time. This mirrors the domain invariant that
-- a model entry has a single in-flight version download (entities.py).
-- ---------------------------------------------------------------------
CREATE TABLE model_catalog_version (
    id                      TEXT    NOT NULL PRIMARY KEY,
    parent_model_id         TEXT    NOT NULL,
    checksum_algorithm      TEXT    NOT NULL DEFAULT 'sha256',
    checksum_value          TEXT    NOT NULL,
    size_bytes              INTEGER NOT NULL,
    manifest_url            TEXT    NOT NULL,
    status                  TEXT    NOT NULL DEFAULT 'published',
    CONSTRAINT fk_catalog_version_parent
        FOREIGN KEY (parent_model_id) REFERENCES model_catalog_entry (id)
        ON DELETE CASCADE,
    CONSTRAINT ck_catalog_version_algorithm
        CHECK (checksum_algorithm IN ('sha256','blake3')),
    CONSTRAINT ck_catalog_version_status
        CHECK (status IN ('published','downloading','installed','corrupted','uninstalled')),
    CONSTRAINT ck_catalog_version_size_bytes
        CHECK (size_bytes >= 0),
    CONSTRAINT ck_catalog_version_manifest_url_scheme
        CHECK (manifest_url LIKE 'http://%' OR manifest_url LIKE 'https://%'),
    CONSTRAINT ck_catalog_version_sha256_format
        CHECK (
            checksum_algorithm <> 'sha256'
            OR length(checksum_value) = 64
        )
);

CREATE INDEX ix_catalog_version_parent_status
    ON model_catalog_version (parent_model_id, status);

-- partial UNIQUE — at most one downloading version per parent.
CREATE UNIQUE INDEX uq_catalog_version_one_downloading
    ON model_catalog_version (parent_model_id)
    WHERE status = 'downloading';


-- ---------------------------------------------------------------------
-- 6.3 model_catalog_download_job — 6-state FSM
--
-- target_model_version_id is a SOFT reference: jobs are kept for audit
-- even if the target version row is later removed. No FOREIGN KEY.
-- ---------------------------------------------------------------------
CREATE TABLE model_catalog_download_job (
    id                          TEXT    NOT NULL PRIMARY KEY,
    target_model_version_id     TEXT    NOT NULL,
    state                       TEXT    NOT NULL DEFAULT 'queued',
    bytes_downloaded            INTEGER NOT NULL DEFAULT 0,
    total_bytes                 INTEGER,
    speed_bps                   REAL    NOT NULL DEFAULT 0.0,
    eta_seconds                 REAL,
    failure_reason              TEXT,
    created_at                  TEXT    NOT NULL,
    updated_at                  TEXT    NOT NULL,
    CONSTRAINT ck_download_job_state
        CHECK (state IN ('queued','running','paused','completed','cancelled','failed')),
    CONSTRAINT ck_download_job_bytes_downloaded
        CHECK (bytes_downloaded >= 0),
    CONSTRAINT ck_download_job_total_bytes
        CHECK (total_bytes IS NULL OR total_bytes >= 0),
    CONSTRAINT ck_download_job_speed_bps
        CHECK (speed_bps >= 0),
    CONSTRAINT ck_download_job_eta_seconds
        CHECK (eta_seconds IS NULL OR eta_seconds >= 0)
);

-- partial: active (non-terminal) jobs sorted by recency
CREATE INDEX ix_download_job_active
    ON model_catalog_download_job (updated_at DESC)
    WHERE state IN ('queued','running','paused');


-- ---------------------------------------------------------------------
-- 6.4 model_catalog_release_manifest — CACHE header (one row per fetch)
-- ---------------------------------------------------------------------
CREATE TABLE model_catalog_release_manifest (
    manifest_version    TEXT    NOT NULL PRIMARY KEY,
    fetched_at          TEXT    NOT NULL,
    CONSTRAINT ck_release_manifest_version_length
        CHECK (length(manifest_version) BETWEEN 1 AND 64)
);


-- ---------------------------------------------------------------------
-- 6.5 model_catalog_release_manifest_entry — composite PK rows
-- ---------------------------------------------------------------------
CREATE TABLE model_catalog_release_manifest_entry (
    manifest_version    TEXT    NOT NULL,
    model_id            TEXT    NOT NULL,
    version_id          TEXT    NOT NULL,
    checksum_algorithm  TEXT    NOT NULL,
    checksum_value      TEXT    NOT NULL,
    size_bytes          INTEGER NOT NULL,
    download_url        TEXT    NOT NULL,
    CONSTRAINT pk_release_manifest_entry
        PRIMARY KEY (manifest_version, model_id, version_id),
    CONSTRAINT fk_release_manifest_entry_manifest
        FOREIGN KEY (manifest_version)
        REFERENCES model_catalog_release_manifest (manifest_version)
        ON DELETE CASCADE,
    CONSTRAINT ck_release_manifest_entry_algorithm
        CHECK (checksum_algorithm IN ('sha256','blake3')),
    CONSTRAINT ck_release_manifest_entry_size_bytes
        CHECK (size_bytes >= 0),
    CONSTRAINT ck_release_manifest_entry_download_url_scheme
        CHECK (download_url LIKE 'http://%' OR download_url LIKE 'https://%'),
    CONSTRAINT ck_release_manifest_entry_sha256_format
        CHECK (
            checksum_algorithm <> 'sha256'
            OR length(checksum_value) = 64
        )
);


-- ---------------------------------------------------------------------
-- 6.6 model_catalog_skill — skill manifest catalog
--
-- This is the CANONICAL (and, since migration 072, the ONLY) skill registry
-- table. The former sibling ai_coding_skill (§4.5) was dropped in 072; the
-- ai_coding context reaches this table through SkillRegistryBridge. See
-- schema doc §10.3 for the decision history.
-- ---------------------------------------------------------------------
CREATE TABLE model_catalog_skill (
    name            TEXT    NOT NULL PRIMARY KEY,
    version         TEXT    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    manifest_json   TEXT    NOT NULL DEFAULT '{}',
    CONSTRAINT ck_catalog_skill_version_length
        CHECK (length(version) BETWEEN 1 AND 64),
    CONSTRAINT ck_catalog_skill_enabled
        CHECK (enabled IN (0, 1))
);
