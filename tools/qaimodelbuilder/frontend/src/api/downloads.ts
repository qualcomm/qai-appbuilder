// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Downloads API client (service_release context — V1 Download Center parity).
 *
 * Covers:
 *   GET    /api/versions                        — list GenieAPIService versions
 *   POST   /api/versions/download               — SSE download stream
 *   POST   /api/versions/service-install        — unzip into bin/
 *   DELETE /api/versions/install/{version}      — delete installed dir
 *   DELETE /api/versions/download/{version}     — delete downloaded zip
 *   GET    /api/versions/local-status           — disk-derived state
 *   GET    /api/versions/download-settings      — forge_config download section
 *   PUT    /api/versions/download-settings
 *   GET    /api/service-catalog                 — hardware-grouped model catalog
 *   POST   /api/service-catalog/download        — SSE download stream
 *   POST   /api/service-catalog/install         — unzip into models/
 *   DELETE /api/service-catalog/install/{model_id}?delete_zip=  — delete
 *   GET    /api/service-catalog/local-status
 *   GET    /api/aria2c/status                   — aria2c availability + 5-state
 *   POST   /api/aria2c/cancel/{task_id}         — cancel an in-flight download
 *
 * SSE wire format (download endpoints):
 *   data: <DownloadProgress JSON>\n\n
 *   data: [DONE]\n\n
 * No `event:` lines (NOT compatible with native EventSource); the OpenAI-compat
 * `apiStream` helper handles it transparently.
 *
 * Error semantics (key cases the UI must handle gracefully — see V1 parity):
 *   - 422 `code: service_release.catalog_unavailable` when `version_list_url`
 *     or `catalog_url` is unset → render the "configure URL in Settings" empty
 *     state, NOT a generic toast.
 *   - 503 `code: service_release.{external,timeout,infrastructure}` for
 *     transport failures — show retry hint.
 *   - 400 `service_release.invalid_download_request` for bad `download_url` /
 *     empty `version`.
 *   - 404 `service_release.download_not_found` for delete-of-nothing.
 */

import { apiJson, type ApiRequestOptions } from "./http";
import { apiStream, type StreamHandler } from "./stream";
import type {
  Aria2cCancelResponse,
  Aria2cStatus,
  CatalogModelsResponse,
  DeleteDownloadedServiceResult,
  DeleteInstalledServiceResult,
  DeleteModelResult,
  DownloadProgress,
  DownloadSettings,
  ModelDownloadRequest,
  ModelInstallRequestBody,
  ModelInstallResult,
  ModelsLocalStatus,
  ServiceDownloadRequest,
  ServiceInstallRequestBody,
  ServiceInstallResult,
  ServiceVersionsResponse,
  VersionsLocalStatus,
} from "@/types/downloads";

// ─── Service versions (GenieAPIService release packages) ───────────────────

/** Fetch all GenieAPIService versions from the configured `version_list_url`. */
export function fetchServiceVersions(
  opts?: ApiRequestOptions,
): Promise<ServiceVersionsResponse> {
  return apiJson<ServiceVersionsResponse>(
    "GET",
    "/api/versions",
    undefined,
    opts,
  );
}

/**
 * Stream a service-package download. Resolves on `[DONE]` or aborts on
 * signal/network error. Frames carry `version === task_id` (V1 wire name).
 */
export function streamServiceDownload(
  body: ServiceDownloadRequest,
  handler: StreamHandler<DownloadProgress>,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  return apiStream<DownloadProgress, ServiceDownloadRequest>(
    "/api/versions/download",
    handler,
    {
      method: "POST",
      body,
      signal: opts?.signal,
    },
  );
}

/**
 * Install a previously-downloaded service zip into `bin/`. The route is
 * `/api/versions/service-install` (NOT `/install`, which is the frozen
 * pip-style stub).
 */
export function installService(
  body: ServiceInstallRequestBody,
  opts?: ApiRequestOptions,
): Promise<ServiceInstallResult> {
  return apiJson<ServiceInstallResult>(
    "POST",
    "/api/versions/service-install",
    body,
    opts,
  );
}

/** Remove an installed service version directory.
 *
 * When ``stopRunning`` is true, the backend gracefully stops a running
 * GenieAPIService for this version first (releases the Genie.dll lock so the
 * delete doesn't fail with WinError 5). Set it after the user confirms the
 * "service is running and will be stopped" dialog.
 */
export function deleteInstalledService(
  version: string,
  stopRunning = false,
  opts?: ApiRequestOptions,
): Promise<DeleteInstalledServiceResult> {
  const query = stopRunning ? "?stop_running=true" : "";
  return apiJson<DeleteInstalledServiceResult>(
    "DELETE",
    `/api/versions/install/${encodeURIComponent(version)}${query}`,
    undefined,
    opts,
  );
}

/** Probe whether the installed service for ``version`` is currently running. */
export function isInstalledServiceRunning(
  version: string,
  opts?: ApiRequestOptions,
): Promise<{ version: string; running: boolean }> {
  return apiJson<{ version: string; running: boolean }>(
    "GET",
    `/api/versions/install/${encodeURIComponent(version)}/running`,
    undefined,
    opts,
  );
}

/** Remove a downloaded-but-not-installed service zip. */
export function deleteDownloadedService(
  version: string,
  opts?: ApiRequestOptions,
): Promise<DeleteDownloadedServiceResult> {
  return apiJson<DeleteDownloadedServiceResult>(
    "DELETE",
    `/api/versions/download/${encodeURIComponent(version)}`,
    undefined,
    opts,
  );
}

/** Disk-derived per-version downloaded/installed state. */
export function fetchVersionsLocalStatus(
  opts?: ApiRequestOptions,
): Promise<VersionsLocalStatus> {
  return apiJson<VersionsLocalStatus>(
    "GET",
    "/api/versions/local-status",
    undefined,
    opts,
  );
}

// ─── Model catalog (hardware-grouped: NPU / GPU / CPU + variants) ──────────

/** Fetch hardware-grouped model catalog from the configured `catalog_url`. */
export function fetchCatalogModels(
  opts?: ApiRequestOptions,
): Promise<CatalogModelsResponse> {
  return apiJson<CatalogModelsResponse>(
    "GET",
    "/api/service-catalog",
    undefined,
    opts,
  );
}

/**
 * Stream a model download. The task id appearing on SSE frames'
 * `version` field is `body.model_id` (which may itself be a `variant_id` if
 * the caller is downloading a specific platform variant).
 */
export function streamModelDownload(
  body: ModelDownloadRequest,
  handler: StreamHandler<DownloadProgress>,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  return apiStream<DownloadProgress, ModelDownloadRequest>(
    "/api/service-catalog/download",
    handler,
    {
      method: "POST",
      body,
      signal: opts?.signal,
    },
  );
}

/** Install a previously-downloaded model zip into `models/`. */
export function installModel(
  body: ModelInstallRequestBody,
  opts?: ApiRequestOptions,
): Promise<ModelInstallResult> {
  return apiJson<ModelInstallResult>(
    "POST",
    "/api/service-catalog/install",
    body,
    opts,
  );
}

/**
 * Remove an installed model directory and (by default) its zip. V1 always
 * passes `delete_zip=true`; we do the same.
 */
export function deleteModel(
  modelId: string,
  opts?: ApiRequestOptions & { deleteZip?: boolean },
): Promise<DeleteModelResult> {
  const deleteZip = opts?.deleteZip ?? true;
  return apiJson<DeleteModelResult>(
    "DELETE",
    `/api/service-catalog/install/${encodeURIComponent(modelId)}`,
    undefined,
    { ...opts, query: { delete_zip: deleteZip } },
  );
}

/** Disk-derived per-model downloaded/installed state. */
export function fetchModelsLocalStatus(
  opts?: ApiRequestOptions,
): Promise<ModelsLocalStatus> {
  return apiJson<ModelsLocalStatus>(
    "GET",
    "/api/service-catalog/local-status",
    undefined,
    opts,
  );
}

// ─── aria2c daemon / cancel ────────────────────────────────────────────────

/**
 * aria2c availability snapshot. Polled on a 2s cadence by
 * `useAria2c` while `install_status === "installing"` (V1 parity).
 */
export function fetchAria2cStatus(
  opts?: ApiRequestOptions,
): Promise<Aria2cStatus> {
  return apiJson<Aria2cStatus>("GET", "/api/aria2c/status", undefined, opts);
}

/**
 * Cancel an in-flight download by task_id. Single-platform service:
 * `version`; multi-platform: `<version>-<platform_id>`; model:
 * `model_id` or `variant_id`. The same id is on SSE frames' `version`.
 *
 * NB: the real cancellation is dispatched first by aborting the SSE
 * fetch via `AbortController.abort()` — this endpoint is the *backstop*
 * for cases where the body has already drained but a server-side aria2c
 * process should still be killed (matches V1 `cancelDownload` ordering).
 */
export function cancelAria2cDownload(
  taskId: string,
  opts?: ApiRequestOptions,
): Promise<Aria2cCancelResponse> {
  return apiJson<Aria2cCancelResponse>(
    "POST",
    `/api/aria2c/cancel/${encodeURIComponent(taskId)}`,
    undefined,
    opts,
  );
}

// ─── Download settings (forge_config download section) ────────────────────

/** Read save_dir / version_list_url / catalog_url / timeouts / ssl_verify. */
export function fetchDownloadSettings(
  opts?: ApiRequestOptions,
): Promise<DownloadSettings> {
  return apiJson<DownloadSettings>(
    "GET",
    "/api/versions/download-settings",
    undefined,
    opts,
  );
}

/**
 * Replace the persisted download settings. The server echoes the persisted
 * values (which may have been normalised) — the caller should sync local
 * state from the response, not the request body.
 */
export function updateDownloadSettings(
  settings: DownloadSettings,
  opts?: ApiRequestOptions,
): Promise<DownloadSettings> {
  return apiJson<DownloadSettings, DownloadSettings>(
    "PUT",
    "/api/versions/download-settings",
    settings,
    opts,
  );
}
