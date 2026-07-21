// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatModelList — unified local + cloud + running-status model loader.
 *
 * V1 parity (`useModels.js` / `useChat.js:1638-1722`): V1 served a single
 * `/api/models` payload containing local + cloud + per-entry `is_running`,
 * which `/models` and `/model` consumed via a continuously-numbered list.
 *
 * V2 split this across three Clean-Cutover endpoints; this composable
 * aggregates them so callers get back a single list with the V1 fields
 * (`model_id`, `name`, `provider`, `is_local`, `is_running`) — the same
 * shape the slash commands and ModelDropdown both need. Centralising the
 * fan-out here removes the duplicate fetch logic that previously lived in
 * `ModelDropdown.vue` (and re-implementations elsewhere).
 *
 * V2 backend shape (TestClient-verified):
 *   GET /api/service/models             → { models: [{ name, path, size_mb, ... }] }
 *   GET /api/model-catalog/cloud-models → { models: [{ model_id, name, provider, ... }] }
 *   GET /api/service/status             → { running, model, ... }
 *
 * Running-state derivation matches V1 `m.is_running` semantics: a local
 * model is "running" iff the daemon is up AND its currently-loaded `model`
 * equals the model's `name` / `model_id`. Cloud entries are never reported
 * as running (V1 only ever set `is_running` on local entries).
 */
import { apiJson } from "@/api";
import { fetchServiceStatus } from "@/api/serviceControl";
import type {
  ServiceModelEntry,
  ServiceModelsResponse,
} from "@/types/service";

/**
 * Cloud catalog entry shape (V1 `useModels.js` parity). The model_runtime
 * cloud route surfaces `model_id` + optional `name` / `provider`.
 */
interface CloudModelEntry {
  model_id: string;
  name?: string;
  provider?: string;
  description?: string;
  context_length?: number;
  is_placeholder?: boolean;
  [key: string]: unknown;
}

interface CloudModelsResponse {
  models: CloudModelEntry[];
}

/**
 * Unified chat-model entry returned by `loadAll`. Mirrors the V1 fields
 * that `/models` / `/model` and ModelDropdown both rely on.
 */
export interface ChatModelItem {
  /** Stable id used for backend operations (V1 `model_id`). */
  model_id: string;
  /** Display label (V1 `name`). */
  name: string;
  /** Cloud provider slug (e.g. "cloud_llm"); empty for local. */
  provider: string;
  is_local: boolean;
  is_running: boolean;
  is_placeholder: boolean;
  /** Original local-only fields, preserved for advanced callers. */
  path?: string;
  size_mb?: number;
  config_path?: string;
  format?: string;
  /** Optional cloud-only ctx_length hint (used by ModelDropdown badge). */
  context_length?: number;
}

/**
 * Display id for a local on-disk entry.
 *
 * V1 convention: local model ids carry a ``local::`` prefix so the chat
 * backend's model resolver routes the turn to the running local Genie
 * service endpoint (``ProviderAwareModelResolver``) rather than the cloud
 * provider registry / offline fallback. The prefix is stripped again
 * backend-side to obtain the wire model name the daemon expects, and
 * frontend display strips it via ``useAssistantLabel``.
 */
function localId(m: ServiceModelEntry): string {
  return `local::${m.name}`;
}

/**
 * Project a local on-disk entry to the unified shape.
 */
function fromLocal(m: ServiceModelEntry, runningName: string): ChatModelItem {
  const id = localId(m);
  // Prefer the backend-authoritative ``is_running`` (V1 ``/api/models`` per
  // entry) when present; fall back to the status-derived comparison for
  // older payloads that don't carry it. ``status.model`` is the daemon's
  // loaded model name (bare, not the ``local::``-prefixed selector id).
  const isRunning =
    typeof m.is_running === "boolean"
      ? m.is_running
      : runningName !== "" && runningName === m.name;
  return {
    model_id: id,
    name: m.name,
    provider: "",
    is_local: true,
    is_running: isRunning,
    is_placeholder: false,
    path: m.path,
    size_mb: m.size_mb,
    config_path: m.config_path,
    format: m.format,
    // V1 ctx badge (index.html:1025): surface the backend-provided
    // context_length so the dropdown shows "8K"/"32K" for local models too.
    context_length: m.context_length,
  };
}

/**
 * Project a cloud catalog entry to the unified shape.
 */
function fromCloud(m: CloudModelEntry): ChatModelItem {
  return {
    model_id: m.model_id,
    name: m.name ?? m.model_id,
    provider: m.provider ?? "",
    is_local: false,
    is_running: false,
    is_placeholder: m.is_placeholder === true,
    context_length: m.context_length,
  };
}

export function useChatModelList() {
  /**
   * Fetch and merge the three V2 endpoints into a single V1-shaped list.
   * Local entries come first (V1 ordering: locals before cloud). Failures
   * on any one endpoint degrade gracefully — the affected sub-list is
   * treated as empty rather than failing the whole call.
   */
  async function loadAll(): Promise<ChatModelItem[]> {
    const localPromise = apiJson<ServiceModelsResponse>(
      "GET",
      "/api/service/models",
    ).then(
      (r) => r.models ?? [],
      () => [] as ServiceModelEntry[],
    );
    const cloudPromise = apiJson<CloudModelsResponse>(
      "GET",
      "/api/model-catalog/cloud-models",
    ).then(
      (r) => r.models ?? [],
      () => [] as CloudModelEntry[],
    );
    const statusPromise = fetchServiceStatus().catch(() => ({
      running: false,
      model: "",
    }));

    const [local, cloud, status] = await Promise.all([
      localPromise,
      cloudPromise,
      statusPromise,
    ]);

    const runningName =
      status.running === true ? (status.model ?? "") || "" : "";

    return [
      ...local.map((m) => fromLocal(m, runningName)),
      ...cloud.map(fromCloud),
    ];
  }

  return { loadAll };
}
