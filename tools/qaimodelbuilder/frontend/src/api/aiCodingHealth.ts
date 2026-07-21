// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * AI Coding health API client.
 *
 * Covers:
 *   GET /api/cc/health — Claude Code provider health + folded providers/models
 *   GET /api/oc/health — OpenCode provider health + folded providers/models
 *
 * PR-105 folds the legacy `/providers` and `/models` enumerations into
 * this response. There is no separate `/api/cc/models` route in V2 —
 * the available model catalog is exposed via `models[]` here.
 */

import { apiJson, type ApiRequestOptions } from "./http";
import type { InterfacesHttpRoutesAiCodingHealthResponse } from "@/types/aiCoding";

/** Friendly alias for the cc/oc health response (`/api/cc|oc/health`). */
export type CodingHealthResponse = InterfacesHttpRoutesAiCodingHealthResponse;

/** Fetch Claude Code provider health.
 *
 * Pass `refresh=true` to force the backend to bypass the model-catalog
 * 5-minute cache and re-enumerate the upstream `/v1/models` (V1
 * `?refresh=1` parity for the model-source badge's 🔄 button).
 */
export async function fetchCcHealth(
  opts?: ApiRequestOptions & { refresh?: boolean },
): Promise<CodingHealthResponse> {
  const path = opts?.refresh ? "/api/cc/health?refresh=1" : "/api/cc/health";
  return apiJson<CodingHealthResponse>("GET", path, undefined, opts);
}

/** Fetch OpenCode provider health. */
export async function fetchOcHealth(
  opts?: ApiRequestOptions,
): Promise<CodingHealthResponse> {
  return apiJson<CodingHealthResponse>("GET", "/api/oc/health", undefined, opts);
}
