-- ============================================================================
-- Migration 010: channel_pending_message (PR-097 / S9 §6 R-20)
--
-- Persists Layer-3 pending messages from the realtime-delivery service
-- so a server restart does not silently drop CC results the user never
-- saw — restoring parity with the legacy ``_pending_cc_results`` map.
--
-- One row per (instance, user, queued message); pop_all drains every
-- non-expired row in FIFO order (id ASC). Rows past their ``expires_at``
-- are filtered by the application layer and may be garbage-collected
-- by an out-of-band sweep.
--
-- Conventions (schema doc §0.3 / §0.4 / §9):
--   * id = INTEGER autoincrement so FIFO order survives restart
--   * timestamps are ISO-8601 UTC strings (matches channels_session_index)
-- ============================================================================


CREATE TABLE IF NOT EXISTS channel_pending_message (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id     TEXT    NOT NULL
                            CHECK (length(instance_id) BETWEEN 1 AND 256),
    user_id         TEXT    NOT NULL
                            CHECK (length(user_id) BETWEEN 1 AND 256),
    message         TEXT    NOT NULL
                            CHECK (length(message) BETWEEN 1 AND 16384),
    created_at_iso  TEXT    NOT NULL,
    expires_at_iso  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_channel_pending_lookup
    ON channel_pending_message (instance_id, user_id, expires_at_iso);
