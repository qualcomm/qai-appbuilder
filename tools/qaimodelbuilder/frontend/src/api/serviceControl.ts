// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Service Control API client (model_runtime context).
 *
 * Covers:
 *   GET  /api/service/status — inference service status (running, pid, etc.)
 *   POST /api/service/start  — start the inference daemon
 *   POST /api/service/stop   — stop the inference daemon
 *   GET  /api/service/logs   — retrieve buffered service logs
 *   POST /api/service/logs/clear — clear log buffer and return skip marker
 *
 * The inference service is GenieAPIService (or equivalent) managed by
 * the model_runtime bounded context.
 */

import { apiJson, apiRaw, buildApiUrl, type ApiRequestOptions } from "./http";
import type {
  StartServiceRequest,
  ServiceStatusResponse,
  ServiceLogsClearResponse,
  ServiceModelsResponse,
  ProbeServiceResponse,
  LoadServiceModelRequest,
  LoadServiceModelResponse,
} from "@/types/service";

/**
 * Query the current status of the inference service.
 */
export async function fetchServiceStatus(
  opts?: ApiRequestOptions,
): Promise<ServiceStatusResponse> {
  return apiJson<ServiceStatusResponse>("GET", "/api/service/status", undefined, opts);
}

/**
 * Probe an inference daemon address (V1 Connection-panel "Test" button).
 *
 * `GET /api/service/probe?host=&port=` → `{ reachable, alive, model }`.
 * Backend probes the address (bypassing browser CORS); when host/port are
 * omitted it probes the locally-managed daemon.
 */
export async function probeService(
  params?: { host?: string; port?: number },
  opts?: ApiRequestOptions,
): Promise<ProbeServiceResponse> {
  const query: Record<string, string | number> = {};
  if (params?.host) query.host = params.host;
  if (params?.port != null) query.port = params.port;
  return apiJson<ProbeServiceResponse>("GET", "/api/service/probe", undefined, {
    ...opts,
    query: { ...opts?.query, ...query },
  });
}

/**
 * Start the inference service.
 *
 * @param params Optional start parameters (model name, port, V1 svcParams).
 */
export async function startService(
  params?: StartServiceRequest,
  opts?: ApiRequestOptions,
): Promise<ServiceStatusResponse> {
  return apiJson<ServiceStatusResponse, StartServiceRequest | undefined>(
    "POST",
    "/api/service/start",
    params,
    opts,
  );
}

/**
 * Stop the inference service.
 */
export async function stopService(
  opts?: ApiRequestOptions,
): Promise<ServiceStatusResponse> {
  return apiJson<ServiceStatusResponse, { force: boolean }>(
    "POST",
    "/api/service/stop",
    { force: false },
    opts,
  );
}

/**
 * Build the absolute URL of the SSE log stream (`GET /api/service/logs`).
 *
 * The log endpoint is an `text/event-stream` (V1 parity); callers drive it
 * with `streamServiceLogs` (fetch ReadableStream) so this is mostly used by
 * tests / debugging.
 */
export function serviceLogsUrl(skip?: number): string {
  const query = skip !== undefined && skip > 0 ? { skip } : undefined;
  return buildApiUrl("/api/service/logs", query);
}

/**
 * Stream service logs via SSE (fetch-based ReadableStream).
 *
 * Returns the raw `Response` so the caller can drive the stream
 * reader (same pattern as v1's `streamServiceLogs`).
 *
 * @param skip Optional skip marker to resume from a cleared offset.
 */
export async function streamServiceLogs(
  skip?: number,
  opts?: ApiRequestOptions,
): Promise<Response> {
  const query = skip !== undefined && skip > 0 ? { skip } : undefined;
  return apiRaw("GET", "/api/service/logs", {
    ...opts,
    query: { ...opts?.query, ...query },
  });
}

/**
 * Clear the service log buffer. Returns a `skip_from` marker to pass
 * to subsequent `streamServiceLogs` calls.
 */
export async function clearServiceLogs(
  opts?: ApiRequestOptions,
): Promise<ServiceLogsClearResponse> {
  return apiJson<ServiceLogsClearResponse>("POST", "/api/service/logs/clear", undefined, opts);
}

/**
 * List the inference models available on disk (scanned from the configured
 * models-root directory).
 *
 * `GET /api/service/models` →
 *   `{ models: [{ name, path, size_mb, config_path, format }], models_root_path }`
 */
export async function fetchServiceModels(
  opts?: ApiRequestOptions,
): Promise<ServiceModelsResponse> {
  return apiJson<ServiceModelsResponse>("GET", "/api/service/models", undefined, opts);
}

/**
 * Load / switch the active inference model on a running daemon.
 *
 * `POST /api/service/load-model` body `{ model_name }` → `{ status, model }`
 */
export async function loadServiceModel(
  body: LoadServiceModelRequest,
  opts?: ApiRequestOptions,
): Promise<LoadServiceModelResponse> {
  return apiJson<LoadServiceModelResponse, LoadServiceModelRequest>(
    "POST",
    "/api/service/load-model",
    body,
    opts,
  );
}
