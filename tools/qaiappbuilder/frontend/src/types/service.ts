// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Service-control (model_runtime / GenieAPIService) view-layer types.
 *
 * The backend `GET /api/service/status` and `/models` routes return an
 * untyped `dict[str, Any]` (no OpenAPI response_model), so these wire
 * shapes are defined here in the frontend rather than re-exported from
 * the auto-generated `@/types/api`. They mirror the fields the
 * model_runtime context actually emits (see
 * `interfaces/http/routes/model_runtime.py` + `infrastructure/process_service.py`).
 */

/** Wire form of `GET /api/service/status`. */
export interface ServiceStatusResponse {
  /** V1-compat boolean (derived from `state === "running"`). */
  running?: boolean;
  /** V2 canonical lifecycle state: "running" | "stopped" | ... */
  state?: string;
  pid?: number | null;
  uptime_seconds?: number | null;
  model?: string | null;
  port?: number | null;
  /** Resolved GenieAPIService executable path (empty when not installed). */
  exe_path?: string;
  /** Full launch command preview emitted by the backend. */
  command?: string;
  /** Non-empty when the exe / models-root path has unsafe (non-ASCII/space) chars. */
  path_warning?: string;
  memory_mb?: number;
}

/** Wire form of `GET /api/service/probe?host=&port=`. */
export interface ProbeServiceResponse {
  /** V1 Connection-panel reachability flag. */
  reachable: boolean;
  alive?: boolean;
  model?: string | null;
}

/** A single on-disk model entry from `GET /api/service/models`. */
export interface ServiceModelEntry {
  name: string;
  path: string;
  size_mb: number;
  /** Path to the model's `config.json` (used by the command preview). */
  config_path?: string;
  /** Accelerator family inferred from the model dir: "qnn" | "gguf" | "mnn". */
  format?: string;
  /**
   * Context-window size (tokens) read from the model config.json
   * (`dialog.context.size`); 0 / absent when unknown. V1 parity — drives the
   * chat dropdown ctx badge. Tail-appended by the backend (§3.1).
   */
  context_length?: number;
  /**
   * Whether this local model is the one currently loaded in the running
   * daemon. V1 parity (`/api/models` per-entry `is_running` → ● Running dot).
   * Backend-authoritative; tail-appended (§3.1).
   */
  is_running?: boolean;
}

/** Wire form of `GET /api/service/models`. */
export interface ServiceModelsResponse {
  models: ServiceModelEntry[];
  /** Configured models-root directory the scan ran against. */
  models_root_path?: string;
}

/** Wire form of `POST /api/service/logs/clear`. */
export interface ServiceLogsClearResponse {
  status?: string;
  /** Offset marker to resume the SSE log stream from after a clear. */
  skip_from?: number;
}

/** Body of `POST /api/service/load-model`. */
export interface LoadServiceModelRequest {
  model_name: string;
}

/** Wire form of `POST /api/service/load-model`. */
export interface LoadServiceModelResponse {
  status: string;
  model: string;
}

/**
 * Body of `POST /api/service/start`.
 *
 * The backend accepts a V1 `svcParams` superset (extra fields ignored),
 * so beyond the canonical `model_name` / `port` the optional V1 launch
 * params are permitted.
 */
export interface StartServiceRequest {
  model_name?: string;
  port?: number;
  loglevel?: number;
  host_mode?: "local" | "remote";
  load_model?: boolean;
  [key: string]: unknown;
}

