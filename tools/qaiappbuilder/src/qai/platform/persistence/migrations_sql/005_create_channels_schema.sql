-- =====================================================================
-- 005_create_channels_schema.sql
--
-- channels bounded context (PR-024): 4 tables.
--
-- Tables (schema doc qai-db-schema.md §5):
--   5.1 channels_instance              — aggregate root (one bot deployment)
--   5.2 channels_message               — inbound message + 5-state FSM
--   5.3 channels_session_index         — composite PK (instance, channel_user)
--   5.4 channels_qr_login_challenge    — QR-login challenge lifecycle
--
-- Enum values mirror the corresponding domain Enum.value strings exactly:
--   ChannelKind          → ('feishu','wechat','wecom')                 (kinds.py)
--   ChannelStatus        → ('stopped','starting','running','stopping','error')
--   ChannelMessageStatus → ('received','parsed','dispatched','replied','failed')
--   QrLoginStatus        → ('issued','scanned','confirmed','expired')
--
-- Cross-context references are SOFT: ai_coding_session_id is a TEXT column
-- with no FOREIGN KEY (schema doc §0.4 + §9.14) — channels never imports
-- ai_coding's tables.
-- =====================================================================


-- ---------------------------------------------------------------------
-- 5.1 channels_instance — aggregate root
-- ---------------------------------------------------------------------
CREATE TABLE channels_instance (
    id                      TEXT    NOT NULL PRIMARY KEY,
    kind                    TEXT    NOT NULL,
    name                    TEXT    NOT NULL,
    status                  TEXT    NOT NULL DEFAULT 'stopped',
    credentials_service     TEXT    NOT NULL,
    credentials_key         TEXT    NOT NULL,
    last_error              TEXT    NOT NULL DEFAULT '',
    metadata_json           TEXT    NOT NULL DEFAULT '{}',
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL,
    CONSTRAINT ck_channels_instance_kind
        CHECK (kind IN ('feishu','wechat','wecom')),
    CONSTRAINT ck_channels_instance_status
        CHECK (status IN ('stopped','starting','running','stopping','error')),
    CONSTRAINT ck_channels_instance_name_length
        CHECK (length(name) BETWEEN 1 AND 256),
    CONSTRAINT ck_channels_instance_last_error_length
        CHECK (length(last_error) <= 1024)
);

CREATE INDEX ix_channels_instance_kind_status
    ON channels_instance (kind, status);

-- partial: list "active" instances (running / transitional)
CREATE INDEX ix_channels_instance_active
    ON channels_instance (updated_at DESC)
    WHERE status IN ('starting','running','stopping');


-- ---------------------------------------------------------------------
-- 5.2 channels_message — inbound message + 5-state FSM
--
-- UNIQUE (kind, provider_event_id) is the idempotency key relied on by
-- IngestWebhookUseCase. Without this constraint duplicate webhook
-- deliveries (Feishu retries on timeout) would re-create messages.
-- ---------------------------------------------------------------------
CREATE TABLE channels_message (
    id                          TEXT    NOT NULL PRIMARY KEY,
    instance_id                 TEXT    NOT NULL,
    kind                        TEXT    NOT NULL,
    sender_user_id              TEXT    NOT NULL,
    provider_event_id           TEXT    NOT NULL,
    content_text                TEXT    NOT NULL,
    status                      TEXT    NOT NULL DEFAULT 'received',
    parsed_verb                 TEXT,
    parsed_args_json            TEXT,
    reply_provider_message_id   TEXT,
    failure_reason              TEXT    NOT NULL DEFAULT '',
    arrived_at                  TEXT    NOT NULL,
    updated_at                  TEXT    NOT NULL,
    CONSTRAINT fk_channels_message_instance
        FOREIGN KEY (instance_id) REFERENCES channels_instance (id)
        ON DELETE CASCADE,
    CONSTRAINT uq_channels_message_kind_event
        UNIQUE (kind, provider_event_id),
    CONSTRAINT ck_channels_message_kind
        CHECK (kind IN ('feishu','wechat','wecom')),
    CONSTRAINT ck_channels_message_status
        CHECK (status IN ('received','parsed','dispatched','replied','failed')),
    CONSTRAINT ck_channels_message_sender_length
        CHECK (length(sender_user_id) BETWEEN 1 AND 256),
    CONSTRAINT ck_channels_message_content_length
        CHECK (length(content_text) BETWEEN 1 AND 16384),
    CONSTRAINT ck_channels_message_failure_reason_length
        CHECK (length(failure_reason) <= 1024)
);

-- FK column always indexed (schema doc §9.6 — SQLite does not auto-create).
CREATE INDEX ix_channels_message_instance
    ON channels_message (instance_id);

-- partial: pending / in-flight messages, hot path for retry sweepers
CREATE INDEX ix_channels_message_unfinished
    ON channels_message (instance_id, arrived_at DESC)
    WHERE status NOT IN ('replied','failed');

CREATE INDEX ix_channels_message_sender
    ON channels_message (instance_id, sender_user_id, arrived_at DESC);


-- ---------------------------------------------------------------------
-- 5.3 channels_session_index — composite PK
--
-- Replaces legacy module-level _user_cc_sessions: dict global. Composite
-- PK lets two separate WeChat instances share overlapping wxids without
-- collision.
--
-- coding_session_id is a SOFT cross-context reference to
-- ai_coding_session.id — TEXT only, no FOREIGN KEY.
-- ---------------------------------------------------------------------
CREATE TABLE channels_session_index (
    instance_id         TEXT    NOT NULL,
    channel_user_id     TEXT    NOT NULL,
    internal_user_id    TEXT,
    coding_session_id   TEXT,
    updated_at          TEXT    NOT NULL,
    CONSTRAINT pk_channels_session_index
        PRIMARY KEY (instance_id, channel_user_id),
    CONSTRAINT fk_channels_session_index_instance
        FOREIGN KEY (instance_id) REFERENCES channels_instance (id)
        ON DELETE CASCADE
);

-- partial: only rows that actually point at a coding session
CREATE INDEX ix_channels_session_index_coding
    ON channels_session_index (coding_session_id)
    WHERE coding_session_id IS NOT NULL;


-- ---------------------------------------------------------------------
-- 5.4 channels_qr_login_challenge — QR-login challenge lifecycle
-- ---------------------------------------------------------------------
CREATE TABLE channels_qr_login_challenge (
    id              TEXT    NOT NULL PRIMARY KEY,
    instance_id     TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'issued',
    issued_at       TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL,
    CONSTRAINT fk_channels_qr_instance
        FOREIGN KEY (instance_id) REFERENCES channels_instance (id)
        ON DELETE CASCADE,
    CONSTRAINT ck_channels_qr_status
        CHECK (status IN ('issued','scanned','confirmed','expired'))
);

CREATE INDEX ix_channels_qr_instance
    ON channels_qr_login_challenge (instance_id);

-- partial: active (not yet confirmed / expired) challenges
CREATE INDEX ix_channels_qr_active
    ON channels_qr_login_challenge (instance_id)
    WHERE status IN ('issued','scanned');
