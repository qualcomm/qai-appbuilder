// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Cloud Models API client.
 *
 * Covers:
 *   GET  /api/model-catalog/cloud-models — list cloud model entries
 *   GET  /api/model-catalog/providers    — list provider metadata (base_url, pinned, etc.)
 *
 * These endpoints expose the model catalog / provider registry
 * managed by the model_catalog context.
 */

import { apiJson, type ApiRequestOptions } from "./http";
import type {
  CloudModelsResponse,
  CloudProviderMeta,
  CloudProvidersResponse,
} from "@/types/cloudModels";

/**
 * Fetch the full list of configured cloud models.
 *
 * Response shape: `{ models: CloudModelEntry[] }`.
 */
export async function fetchCloudModels(
  opts?: ApiRequestOptions,
): Promise<CloudModelsResponse> {
  return apiJson<CloudModelsResponse>("GET", "/api/model-catalog/cloud-models", undefined, opts);
}

/**
 * Fetch provider-level metadata (base URLs, pinned flags, etc.).
 *
 * Backend returns `{ providers: Array<{ provider_id, config }> }` (list form).
 * Convert to the dict shape `{ providers: Record<string, CloudProviderMeta> }`
 * expected by consumers (useOcModels, useCcModels).
 *
 * The FULL `config` document is preserved verbatim on each entry (spread
 * first) so callers that need to PUT the config back (see
 * `putProviderApiKey`) keep `base_url` / `models` / `pinned` intact — the
 * backend replaces the whole config document on write, so a partial config
 * would drop the models list. `has_api_key` (presence-only boolean, never
 * the secret) is surfaced explicitly on top.
 */
export async function fetchCloudProviders(
  opts?: ApiRequestOptions,
): Promise<CloudProvidersResponse> {
  const raw = await apiJson<{ providers: Array<{ provider_id?: string; name?: string; config?: Record<string, unknown>; pinned?: boolean; [k: string]: unknown }> }>(
    "GET", "/api/model-catalog/providers", undefined, opts,
  );
  const dict: Record<string, CloudProviderMeta> = {};
  for (const row of raw.providers ?? []) {
    const id = row.provider_id ?? row.name ?? "";
    if (!id) continue;
    const config = row.config ?? {};
    dict[id] = {
      // Preserve the whole config document so it can be PUT back intact.
      ...config,
      base_url: (config.base_url as string | undefined) ?? (row as Record<string, unknown>).base_url as string | undefined,
      has_api_key: (config.has_api_key as boolean | undefined) ?? undefined,
      pinned: row.pinned ?? (config.pinned as boolean | undefined) ?? undefined,
    };
  }
  return { providers: dict };
}

/**
 * Save a provider's API key via `PUT /api/model-catalog/providers/{id}`.
 *
 * The backend REPLACES the whole config document, so the caller must pass
 * the provider's existing config (base_url / models / pinned) and this
 * helper merges the real `api_key` on top. The backend strips `api_key`
 * into the SecretStore; sending `api_key: "****"` means "keep existing", so
 * only pass a real key here.
 *
 * The stored `has_api_key` presence flag is dropped from the sent config
 * (it is a read-only derived field, not part of the writable config).
 */
export async function putProviderApiKey(
  providerId: string,
  existingConfig: Record<string, unknown>,
  apiKey: string,
  opts?: ApiRequestOptions,
): Promise<void> {
  const config: Record<string, unknown> = { ...existingConfig, api_key: apiKey };
  // `has_api_key` is a derived, read-only presence flag surfaced by the
  // GET; it is not part of the writable config document.
  delete config.has_api_key;
  await apiJson(
    "PUT",
    `/api/model-catalog/providers/${providerId}`,
    { config },
    opts,
  );
}
