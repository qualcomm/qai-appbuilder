-- ============================================================================
-- Migration 008: security_channel_policy schema (PR-501)
--
-- Promotes the legacy ``PolicyCenter._no_ui_channels`` set + ask-rate
-- quota knobs from an
-- inline JSON field on ``policy.json`` to a first-class table keyed on
-- the channel name.
--
-- One row per channel; ``install_v2`` seeds the canonical six members of
-- ``Channel._ALLOWED_NAMES`` (web / cli / wechat / feishu / wecom /
-- background). Operators may insert / update rows via the
-- ``GET/PUT /api/security/channels`` routes added in PR-504.
--
-- Conventions (schema doc §0.3 / §0.4 / §9):
--   * primary key = lower-case channel name (max 64 chars)
--   * boolean = INTEGER CHECK IN (0, 1)
--   * NULL quota_window_seconds + quota_max_asks ↔ no rate limit
-- ============================================================================


CREATE TABLE IF NOT EXISTS security_channel_policy (
    name                  TEXT    NOT NULL PRIMARY KEY
                                  CHECK (length(name) BETWEEN 1 AND 64),
    requires_ui           INTEGER NOT NULL DEFAULT 1
                                  CHECK (requires_ui IN (0, 1)),
    -- Sliding-window cap on ASKs from this channel; both columns must be
    -- NULL or both NOT NULL (enforced in the application layer via
    -- AskQuotaWindow value object).
    quota_window_seconds  INTEGER          CHECK (quota_window_seconds IS NULL
                                                  OR quota_window_seconds > 0),
    quota_max_asks        INTEGER          CHECK (quota_max_asks IS NULL
                                                  OR quota_max_asks > 0),
    description           TEXT    NOT NULL DEFAULT ''
                                  CHECK (length(description) <= 1024),
    -- Either both quota columns are set or both are NULL.
    CHECK (
        (quota_window_seconds IS NULL AND quota_max_asks IS NULL)
        OR (quota_window_seconds IS NOT NULL AND quota_max_asks IS NOT NULL)
    )
);
