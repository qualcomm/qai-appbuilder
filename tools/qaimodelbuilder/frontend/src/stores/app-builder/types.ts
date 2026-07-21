// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * App Builder store DTO + view-model types.
 *
 * Extracted verbatim from `stores/appBuilder.ts` (ARCH-1 cohesion split) to
 * keep that store within the §3.6 cohesion budget. These are pure type
 * declarations (no runtime, no reactive state); the store re-exports them so
 * external consumers keep importing them from `@/stores/appBuilder`.
 *
 * Backend contract notes (verified 2026-06-01 against
 * interfaces/http/routes/app_builder.py + use_cases/run_app.py):
 *   - RunCreateRequest = { model_id, inputs }.  variantId / params /
 *     options are NOT top-level fields; the runner reads them OUT of the
 *     `inputs` bag: `inputs.variant_id` (str) and `inputs.params` (dict).
 *     So we fold variant + params + options into `inputs` on create.
 *   - SSE frame contract (app_builder): event names are
 *     `state` | `frame` | `error` | `done` (NOT V1's status/progress/...).
 *     `state.status` is a RunStatus value (pending/running/streaming/
 *     completed/failed/cancelled).  `frame.payload` carries the runner's
 *     output chunk.
 *   - install status: `AppModelResponse.status` (Ready / NotInstalled /
 *     Error — weights present on disk?) + `deps_status` (ready / missing /
 *     installing) + `variant_status[]` are the V1-parity install-state fields
 *     (V1 `GET /api/appbuilder/models` augmented rows). A model that cannot be
 *     run is also reflected as `enabled === false` (domain
 *     `is_runnable === enabled`).
 */
import type { components } from "@/types/api";

export type AppModelResponse = components["schemas"]["AppModelResponse"];
export type AppModelListResponse = components["schemas"]["AppModelListResponse"];
export type RunResponse = components["schemas"]["RunResponse"];
export type ArtifactListResponse = components["schemas"]["ArtifactListResponse"];
export type WorkerStatusResponse = components["schemas"]["WorkerStatusResponse"];
export type PackManifestResponse = components["schemas"]["PackManifestResponse"];

// --- Standalone-app hosting (plan §5.1/§5.3/§5.5, Phase 4) -------------------
// Generated wire shapes for the /api/app-builder/apps/* routes: list a
// generated app project, run/stop its managed process, and read the retained
// stdout/stderr tail.
export type AppEntry = components["schemas"]["AppEntry"];
export type AppListResponse = components["schemas"]["AppListResponse"];
export type AppDetailResponse = components["schemas"]["AppDetailResponse"];
export type RunAppRequest = components["schemas"]["RunAppRequest"];
export type RunAppResponse = components["schemas"]["RunAppResponse"];
export type AppLogsResponse = components["schemas"]["AppLogsResponse"];
export type PackageJobResponse = components["schemas"]["PackageJobResponse"];

/**
 * Local view-model for a hosted app's managed-run state (Phase 4 UI).
 *
 * Projected from the backend `RunAppResponse` (run / stop) + `AppLogsResponse`
 * (status refresh). `error` holds the *localizable error code* (e.g.
 * `app_builder.port_in_use`) surfaced by a failed run/stop so the menu can map
 * it to translated text — never a raw English message (plan §5.7).
 */
export interface AppRunState {
  status: string;
  port: number | null;
  url: string | null;
  pid: number | null;
  processId: string | null;
  manualCommand: string | null;
  /** Localizable error code from the last failed run/stop, or null. */
  error?: string | null;
}

/**
 * Local view-model for a hosted app's packaging job (Phase 5 UI).
 *
 * Backs the "Package → live progress bar → done / error" per-app affordance:
 *   POST /api/app-builder/apps/{id}/package → { job_id }, then
 *   GET  .../apps/{id}/package/{job_id}/progress (SSE `progress`/`done`/`error`).
 *
 * `error` holds the *localizable error code* (e.g. `package_failed`) surfaced by
 * a failed job so the menu can map it to translated text — never a raw English
 * message (plan §5.7). `running` is the in-flight guard for the Package button.
 */
export interface PackageState {
  jobId: string | null;
  phase: string;
  percent: number;
  message: string;
  zipPath: string | null;
  sizeBytes: number | null;
  isComplete: boolean;
  error: string | null;
  running: boolean;
}

// --- Extension response shapes (not in the generated api.ts snapshot) ---
// These mirror the backend DTOs in interfaces/http/routes/app_builder.py.
export interface ModelSchemaResponse {
  model_id: string;
  title: string;
  input_schema?: Record<string, unknown> | null;
  output_schema?: Record<string, unknown> | null;
  variants: Array<Record<string, unknown>>;
}

export interface RunsListResponse {
  runs: RunResponse[];
  limit: number;
  offset: number;
}

export interface TaxonomyNodeResponse {
  path: string[];
  model_count: number;
}

/** One task leaf of the full taxonomy tree (`GET /taxonomy/tree`). */
export interface TaxonomyTreeTask {
  id: string;
  label: string;
  description: string;
  io: string[];
  model_count: number;
}

/** One group node of the full taxonomy tree. */
export interface TaxonomyTreeGroup {
  id: string;
  label: string;
  icon: string;
  tasks: TaxonomyTreeTask[];
}

/** Full taxonomy tree (`GET /taxonomy/tree`), V1 parity. */
export interface TaxonomyTreeResponse {
  version: string;
  groups: TaxonomyTreeGroup[];
}

export interface RunMetricsResponse {
  run_id: string;
  status: string;
  artifact_count: number;
  duration_ms?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  error_message?: string | null;
  // 缺口 #6 — pure-inference latency (V1 `r.metrics.latencyMs`,
  // HistoryPanel.js:237-238). Backend wire field is `latency_ms`
  // (RunMetricsResponse, append-only); the live SSE `metrics` frame
  // projection (frames.ts) sets the camelCase `latencyMs` so a still-
  // streaming run and a reloaded run both expose the value. Either may
  // be present; HistoryPanel.latencyMs() reads `latencyMs ?? latency_ms`
  // and falls back to `duration_ms` / "—" when neither is set.
  latencyMs?: number | null;
  latency_ms?: number | null;
}

/** SSE `state` frame payload (app_builder stream). */
export interface RunStateFrame {
  status: string;
  run_id: string;
  ts?: string;
}

/** SSE `frame` payload wrapper (app_builder stream). */
export interface RunStreamFrame {
  sequence: number;
  payload: Record<string, unknown>;
}

/**
 * One runner log line surfaced from a `payload.event === "log"` frame.
 *
 * V2 backend (`process_runner.py:_decode_stdout_line` + ProcessStderrFrame)
 * shape:
 *   - structured (Pack `emit({type:"log", stream, line})`):
 *       `{event:"log", stream:"stdout"|"stderr", line:"..."}`
 *   - stderr stream chunk (process_runner ProcessStderrFrame):
 *       `{event:"log", stream:"stderr", data:"<utf8 chunk>", size:N}`
 *   - synthesized stdout passthrough (non-NDJSON Pack `print()`):
 *       `{event:"log", stream:"stdout", line:"..."}`
 *
 * Both shapes collapse to `{stream, line}` for the Logs Tab renderer.
 */
export interface LogLine {
  stream: string;
  line: string;
}

/** Local view-model for a run while it is streaming / after completion. */
export interface AppRun {
  id: string | null;
  modelId: string;
  variantId: string | null;
  status: string; // RunStatus value or local 'queued'
  inputs: Record<string, unknown>;
  params: Record<string, unknown>;
  frames: RunStreamFrame[];
  /**
   * Assembled output payload. Populated from `payload.event === "result"`
   * frames (`run.output = { ...run.output, ...payload.output }`) so the
   * DynamicOutput viewer / Send-to-Chat / Download menu see a clean output
   * dict — never polluted by log / metrics / status frame fields.
   */
  output: Record<string, unknown> | null;
  /**
   * Runner log lines accumulated from `payload.event === "log"` frames
   * (V1 NDJSON v3.1 `run.logs` parity). Drives the Logs Tab in DynamicOutput.
   */
  logs: LogLine[];
  metrics: RunMetricsResponse | null;
  artifacts: ArtifactListResponse["items"];
  error: string | null;
  createdAt: number;
  /** ISO timestamps from the server history view (V1 Started / duration). */
  startedAt?: string | null;
  finishedAt?: string | null;
  /** Worker pid from `payload.event === "started"` frames (best-effort). */
  pid?: number | null;
  /** Exit code from `payload.event === "terminated"` frames (best-effort). */
  exitCode?: number | null;
  /** Optional V1 statusHint carried on `payload.event === "status"` frames. */
  statusHint?: string | null;
  /** Queue position from `payload.event === "status"` frames (V1 queuePosition). */
  queuePosition?: number | null;
  /**
   * Structured error detail from `payload.event === "error"` frames.
   * V1 `run.error.detail` shape: { stderr_lines, bootstrap_trace, traceback,
   * exit_code, crash_hint, spawn_context }.
   */
  errorDetail?: Record<string, unknown> | null;
  /**
   * User rating for this run (-1 = 👎, 0 = neutral, 1 = 👍, null = unrated).
   * Populated from history API or after submitRating (V1 run.rating parity).
   */
  rating?: number | null;
}

/** Compare-tray item (front-end only). */
export interface CompareItem {
  id: string;
  modelId: string;
  /** Human-readable model title (V1 CompareItem.modelName). */
  modelName: string;
  /** Run status (success/failed/...) for the status badge / radar Score. */
  status: string;
  /** User rating (-1 / 0 / 1) — feeds the radar Score axis. */
  rating: number;
  /** Variant label (e.g. "FP16"). */
  variant: string | null;
  /** Runtime summary for the Table view's Runtime column. */
  runtime: { backend?: string | null; quantization?: string | null } | null;
  output: Record<string, unknown> | null;
  metrics: CompareMetrics | null;
}

/** Compare metrics (superset of RunMetricsResponse + V1 model-size/mem). */
export interface CompareMetrics {
  latencyMs?: number | null;
  memoryMB?: number | null;
  modelSizeMB?: number | null;
}

/** tool_params projection sent to chat when mode === 'app-builder'. */
export interface AppBuilderToolParams {
  selected_model_id: string | null;
  selected_model_name: string | null;
  category: string | null;
  variant_id: string | null;
  last_run_summary: string | null;
  /**
   * Multi-select of imported model ids chosen in the chat-input strip
   * (additive; backward-compatible). The backend injects these Packs into the
   * system prompt. `selected_model_id` stays populated (first of this list or
   * the single workbench selection) for older consumers.
   */
  selected_model_ids?: string[];
}
