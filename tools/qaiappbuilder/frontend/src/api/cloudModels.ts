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
 * the secret) and `auth_mode` (how the provider authenticates) are surfaced
 * explicitly on top; both are read-only derived fields.
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
      // Backend emits this at the same level as `has_api_key`; fall back to the
      // row for the flat wire shape. Absent → consumers default to "api_key".
      auth_mode:
        (config.auth_mode as string | undefined) ??
        ((row as Record<string, unknown>).auth_mode as string | undefined),
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
 * The stored `has_api_key` / `auth_mode` presence flags are dropped from the
 * sent config (they are read-only derived fields, not part of the writable
 * config document).
 */
export async function putProviderApiKey(
  providerId: string,
  existingConfig: Record<string, unknown>,
  apiKey: string,
  opts?: ApiRequestOptions,
): Promise<void> {
  const config: Record<string, unknown> = { ...existingConfig, api_key: apiKey };
  // `has_api_key` / `auth_mode` are derived, read-only fields surfaced by the
  // GET; they are not part of the writable config document.
  delete config.has_api_key;
  delete config.auth_mode;
  await apiJson(
    "PUT",
    `/api/model-catalog/providers/${providerId}`,
    { config },
    opts,
  );
}

/**
 * Persist a QGenie model's quota bucket via `PUT /providers/{id}`.
 *
 * QGenie bills each request to one of two independent daily allowances and
 * chooses which from the outbound `User-Agent`. The backend sets that header
 * from the resolved model's `params.traffic_class`, so writing the choice into
 * the model's catalog entry is what actually moves subsequent requests — a
 * purely in-memory preference would leave the user's confirmed switch with no
 * effect on the wire.
 *
 * Stored per MODEL because the allowances are per-model: one model running dry
 * must not move another onto its fallback.
 *
 * Like `putProviderApiKey`, the backend REPLACES the whole config document, so
 * the full existing config must be passed and is merged into rather than
 * replaced — otherwise the models list and base_url would be dropped. The
 * `api_key` is deliberately NOT sent: omitting it leaves the SecretStore entry
 * untouched, whereas sending a masked placeholder risks overwriting it.
 */
export async function putModelTrafficClass(
  providerId: string,
  existingConfig: Record<string, unknown>,
  modelId: string,
  trafficClass: "Api" | "UI",
  opts?: ApiRequestOptions,
): Promise<void> {
  const config: Record<string, unknown> = { ...existingConfig };
  delete config.has_api_key;
  delete config.auth_mode;
  delete config.api_key;

  const models = Array.isArray(config.models) ? [...config.models] : [];
  const index = models.findIndex(
    (m) =>
      m !== null &&
      typeof m === "object" &&
      (m as Record<string, unknown>).model_id === modelId,
  );
  if (index < 0) return;
  const entry = { ...(models[index] as Record<string, unknown>) };
  const params =
    entry.params !== null && typeof entry.params === "object"
      ? { ...(entry.params as Record<string, unknown>) }
      : {};
  params.traffic_class = trafficClass;
  entry.params = params;
  models[index] = entry;
  config.models = models;

  await apiJson("PUT", `/api/model-catalog/providers/${providerId}`, { config }, opts);
}
