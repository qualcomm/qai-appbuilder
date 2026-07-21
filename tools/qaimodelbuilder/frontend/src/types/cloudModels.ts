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

/** Per-provider metadata (base_url, api key presence, pinned flag, etc.). */
export interface CloudProviderMeta {
  base_url?: string;
  api_key?: string;
  has_api_key?: boolean;
  pinned?: boolean;
  [key: string]: unknown;
}

/** Wire form of `GET /api/model-catalog/providers`. */
export interface CloudProvidersResponse {
  providers: Record<string, CloudProviderMeta>;
}
