// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * AI Coding Config API client.
 *
 * Covers:
 *   GET  /api/cc/config — read Claude Code configuration
 *   PUT  /api/cc/config — update Claude Code configuration
 *   GET  /api/oc/config — read OpenCode configuration
 *   PUT  /api/oc/config — update OpenCode configuration
 *
 * The config document is a free-form dict wrapped in `{ config: {...} }`.
 * Sensitive credentials are NOT in this endpoint (use /api/cc/credentials).
 */

import { apiJson, type ApiRequestOptions } from "./http";
import type {
  CodingConfigResponse,
  SaveCodingConfigRequest,
  SaveCodingConfigResponse,
} from "@/types/aiCoding";

// ---------------------------------------------------------------------------
// Claude Code (CC)
// ---------------------------------------------------------------------------

/**
 * Fetch the persisted Claude Code configuration.
 */
export async function fetchCcConfig(
  opts?: ApiRequestOptions,
): Promise<CodingConfigResponse> {
  return apiJson<CodingConfigResponse>("GET", "/api/cc/config", undefined, opts);
}

/**
 * Update the Claude Code configuration.
 *
 * Only keys present in `config` are persisted; omitted keys are unchanged.
 */
export async function updateCcConfig(
  config: Record<string, unknown>,
  opts?: ApiRequestOptions,
): Promise<SaveCodingConfigResponse> {
  const body: SaveCodingConfigRequest = { config };
  return apiJson<SaveCodingConfigResponse, SaveCodingConfigRequest>(
    "PUT",
    "/api/cc/config",
    body,
    opts,
  );
}

// ---------------------------------------------------------------------------
// OpenCode (OC)
// ---------------------------------------------------------------------------

/**
 * Fetch the persisted OpenCode configuration.
 */
export async function fetchOcConfig(
  opts?: ApiRequestOptions,
): Promise<CodingConfigResponse> {
  return apiJson<CodingConfigResponse>("GET", "/api/oc/config", undefined, opts);
}

/**
 * Update the OpenCode configuration.
 *
 * Only keys present in `config` are persisted; omitted keys are unchanged.
 */
export async function updateOcConfig(
  config: Record<string, unknown>,
  opts?: ApiRequestOptions,
): Promise<SaveCodingConfigResponse> {
  const body: SaveCodingConfigRequest = { config };
  return apiJson<SaveCodingConfigResponse, SaveCodingConfigRequest>(
    "PUT",
    "/api/oc/config",
    body,
    opts,
  );
}
