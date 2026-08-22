-- ============================================================================
-- Migration 011: app_builder feedback + benchmark schema (S9 close)
--
-- Wires two surface-only routes from PR-304 to real persistence:
--   * POST /api/app-builder/feedback   → app_builder_feedback
--   * POST /api/app-builder/benchmark  → app_builder_benchmark
--
-- Both tables soft-reference app_builder_run.id with ON DELETE CASCADE so
-- DELETE /api/app-builder/history/runs/{run_id} (also wired in S9 close)
-- removes downstream rows without manual cleanup.
--
-- Migration runner manages BEGIN/COMMIT — file MUST NOT contain them.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- app_builder_feedback: user-submitted Likert rating + comment per Run.
-- Multiple rows per run are allowed (chronological history); the
-- InjectQualityScoreUseCase reads the latest row when biasing the LLM Pack
-- catalog. rating is enforced to [1, 5] at the domain layer; we add the
-- CHECK here too so accidental direct-insert paths still fail loudly.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_builder_feedback (
    id           TEXT    NOT NULL PRIMARY KEY,
    run_id       TEXT    NOT NULL,
    rating       INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    text         TEXT    NOT NULL DEFAULT '' CHECK (length(text) <= 4000),
    extra_json   TEXT    NOT NULL DEFAULT '{}',
    created_at   TEXT    NOT NULL,
    FOREIGN KEY (run_id) REFERENCES app_builder_run(id) ON DELETE CASCADE
);

-- list_for_run / latest_for_run hot path
CREATE INDEX IF NOT EXISTS ix_app_builder_feedback_run_created
    ON app_builder_feedback(run_id, created_at DESC);


-- ----------------------------------------------------------------------------
-- app_builder_benchmark: per-invocation latency stats for POST /benchmark.
-- raw_latencies_json is a JSON array of per-iteration latency_ms values
-- (post-warmup); stats_json holds the derived p50/p90/p99/mean/std/min/max
-- aggregate. status literals match the domain enum
-- ('scheduled' | 'running' | 'completed' | 'failed').
-- model_id soft-references app_builder_model_definition (no CASCADE — when
-- the model is deleted via DELETE /models/{id} we surface that as a
-- RESTRICT violation; benchmark history must outlive model deletion).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_builder_benchmark (
    id                  TEXT    NOT NULL PRIMARY KEY,
    model_id            TEXT    NOT NULL,
    iterations          INTEGER NOT NULL CHECK (iterations >= 1),
    warmup              INTEGER NOT NULL DEFAULT 0 CHECK (warmup >= 0),
    inputs_json         TEXT    NOT NULL DEFAULT '{}',
    status              TEXT    NOT NULL DEFAULT 'scheduled'
                                CHECK (status IN ('scheduled', 'running',
                                                  'completed', 'failed')),
    stats_json          TEXT    NOT NULL DEFAULT '{}',
    raw_latencies_json  TEXT    NOT NULL DEFAULT '[]',
    error_message       TEXT,
    created_at          TEXT    NOT NULL,
    finished_at         TEXT
);

-- list_for_model hot path
CREATE INDEX IF NOT EXISTS ix_app_builder_benchmark_model_created
    ON app_builder_benchmark(model_id, created_at DESC);

-- Active benchmarks (worker loop / status polling)
CREATE INDEX IF NOT EXISTS ix_app_builder_benchmark_active
    ON app_builder_benchmark(created_at DESC)
    WHERE status IN ('scheduled', 'running');
