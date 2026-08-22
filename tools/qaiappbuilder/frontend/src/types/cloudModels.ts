// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Cloud-model / provider view-layer types (model_catalog context).
 *
 * The cloud-model list (`GET /api/model-catalog/entries`) and provider
 * registry (`GET /api/model-catalog/providers`) feed several consumers
 * (Settings CloudModelsPanel, channel model selectors, OpenCode panel).
 * These consumer-facing shapes are defined here rather than re-exported
 * from the auto-generated `@/types/api` so a `gen:types` refresh cannot
 * silently break consumers; the field set mirrors the backend
 * `CloudModelDTO` plus the provider metadata the registry emits.
 */

/** A single cloud model entry (mirrors backend `CloudModelDTO`). */
export interface CloudModelEntry {
  model_id: string;
  name: string;
  provider: string;
  context_length?: number | null;
  description?: string | null;
  supports_streaming?: boolean;
  is_local?: boolean;
  params?: Record<string, unknown> | null;
}

/** Wire form of `GET /api/model-catalog/entries`. */
export interface CloudModelsResponse {
  models: CloudModelEntry[];
}

/**
 * How a provider is authenticated. Mirrors the backend `auth_mode` field
 * (`qai/model_catalog/application/use_cases/list_provider_configs.py`):
 *
 * - `"api_key"` — the usual cloud provider: a static key the user supplies.
 *   `has_api_key === false` here genuinely means "unusable until configured".
 * - `"sso"` — keyless: the credential is minted at runtime from the signed-in
 *   identity (the QAI Service token pool trades the Okta `id_token` for a
 *   short-lived per-user JWT). `has_api_key` is ALWAYS false for such a
 *   provider and must NOT be read as "needs configuration" — there is no key
 *   for the user to enter.
 *
 * Unknown/absent (older backend) is treated as `"api_key"` by consumers so
 * behaviour is unchanged against a backend that does not send the field.
 */
export type ProviderAuthMode = "api_key" | "sso";

/** Per-provider metadata (base_url, api key presence, pinned flag, etc.). */
export interface CloudProviderMeta {
  base_url?: string;
  api_key?: string;
  has_api_key?: boolean;
  /** See `ProviderAuthMode`. Absent on an older backend → treat as `api_key`. */
  auth_mode?: ProviderAuthMode | string;
  pinned?: boolean;
  [key: string]: unknown;
}

/** Wire form of `GET /api/model-catalog/providers`. */
export interface CloudProvidersResponse {
  providers: Record<string, CloudProviderMeta>;
}
