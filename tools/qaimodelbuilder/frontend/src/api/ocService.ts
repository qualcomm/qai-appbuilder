// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * OpenCode service control API client.
 *
 * Covers the 4 OC-only subprocess control routes (PR-105):
 *   GET  /api/oc/service/status — current process status
 *   POST /api/oc/service/start  — start the managed `opencode serve` process
 *   POST /api/oc/service/stop   — stop the managed process (optional force)
 *   GET  /api/oc/service/logs   — tail the process log buffer
 *
 * NB: this is distinct from the model_runtime service control in
 * `serviceControl.ts` — these routes manage the OpenCode CLI server
 * subprocess, not the local model runtime.
 */

import { apiJson, type ApiRequestOptions } from "./http";
import type {
  OcServiceStatusResponse,
  OcServiceStartResponse,
  OcServiceStopRequest,
  OcServiceStopResponse,
  OcServiceLogsResponse,
} from "@/types/aiCoding";

export async function fetchOcServiceStatus(
  opts?: ApiRequestOptions,
): Promise<OcServiceStatusResponse> {
  return apiJson<OcServiceStatusResponse>(
    "GET",
    "/api/oc/service/status",
    undefined,
    opts,
  );
}

export async function startOcService(
  opts?: ApiRequestOptions,
): Promise<OcServiceStartResponse> {
  return apiJson<OcServiceStartResponse>(
    "POST",
    "/api/oc/service/start",
    undefined,
    opts,
  );
}

export async function stopOcService(
  force = false,
  opts?: ApiRequestOptions,
): Promise<OcServiceStopResponse> {
  const body: OcServiceStopRequest = { force };
  return apiJson<OcServiceStopResponse, OcServiceStopRequest>(
    "POST",
    "/api/oc/service/stop",
    body,
    opts,
  );
}

export async function fetchOcServiceLogs(
  lastN = 100,
  opts?: ApiRequestOptions,
): Promise<OcServiceLogsResponse> {
  return apiJson<OcServiceLogsResponse>(
    "GET",
    "/api/oc/service/logs",
    undefined,
    { ...opts, query: { last_n: lastN, ...(opts?.query ?? {}) } },
  );
}
