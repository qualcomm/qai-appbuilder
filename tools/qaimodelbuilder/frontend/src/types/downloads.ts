// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Downloads page consumer types — mirrors the V2 backend `service_release`
 * context wire schemas 1:1 (V1 ``backend/version_manager.py`` +
 * ``model_catalog_manager.py`` + ``aria2c_downloader.py`` + ``forge_config_manager.py``
 * parity).
 *
 * Why a hand-written file (not generated `api.ts`):
 *   - The auto-generated `api.ts` types `service-catalog`'s
 *     `CatalogModelsResponse.models` as `Record<string, never>[]` (object[]),
 *     because the route returns `list[dict]` rather than a typed schema. We
 *     need the precise V1-parity field set on the consumer side.
 *   - `DownloadProgress` is the SSE frame payload, not exposed via OpenAPI.
 *   - Mixing a few delete/install responses returned as raw `dict` from
 *     application use cases needs explicit consumer contracts.
 *
 * Source-of-truth references (V2 backend, file:line):
 *   - `src/qai/service_release/domain/value_objects.py`
 *       ServicePackage.to_wire        :122-130
 *       ServiceVersion.to_wire        :149-162
 *       ModelVariant.to_wire          :199-209
 *       CatalogModel.to_wire          :235-255
 *       DownloadProgress.to_wire      :87-99
 *       Aria2cStatus.to_wire          :291-302
 *       LocalItemStatus.to_wire       :319-325
 *       VersionsLocalStatus.to_wire   :336-341
 *       ModelsLocalStatus.to_wire     :350-351
 *       ServiceInstallResult.to_wire  :367-374
 *       ModelInstallResult.to_wire    :384-390
 *       DownloadSettings.to_wire      :404-412
 *   - `interfaces/http/routes/versions.py` (download/install/delete shapes)
 *   - `interfaces/http/routes/service_catalog.py`
 *   - `interfaces/http/routes/aria2c.py`
 *   - `src/qai/service_release/adapters/filesystem_installer.py`
 *       delete_installed_service      :156-170 (NB: no `root_path_cleared` field, V1 had it)
 *       delete_downloaded_service     :172-188
 *       delete_model                  :190-223
 *
 * Wire encoding (SSE):
 *   - `POST /api/versions/download`, `POST /api/service-catalog/download`
 *     stream `data: <DownloadProgress JSON>\n\n` frames followed by
 *     `data: [DONE]\n\n` (NO `event:` lines). Use `apiStream`.
 */

// ─── Enums (string literals, mirror Python enum `.value`) ───────────────────

export type DownloadStatus =
  | "idle"
  | "preparing"
  | "downloading"
  | "done"
  | "error"
  | "cancelled";

export type DownloadEngineKind = "aria2c" | "httpx" | "";

export type ModelHardware = "npu" | "gpu" | "cpu";

export type ModelFormat = "qnn" | "gguf" | "mnn";

export type Aria2cInstallStatus = "idle" | "installing" | "done" | "failed";

/**
 * A user-facing string that may be a plain string OR a per-language map
 * (`{"zh-CN":"...","en":"...","zh-TW":"..."}`). Remote catalogs / release
 * manifests can carry their own translations; the backend passes them through
 * unchanged and the UI resolves the active language at render time via
 * `localize()` (so switching the WebUI language is instant, no re-fetch, and
 * third-party-hosted catalogs can ship their own translations without the app
 * having to ship locale keys for them). A plain string is always valid.
 */
export type LocalizedText = string | Record<string, string>;

// ─── Domain VOs (response shapes) ───────────────────────────────────────────

/** A package within a `ServiceVersion` — one platform variant of a release zip. */
export interface ServicePackage {
  platform: string;
  platform_id: string;
  description: LocalizedText;
  download_url: string;
  min_driver_version: string;
  is_default: boolean;
}

/** GenieAPIService release entry. `GET /api/versions` → `{versions: ServiceVersion[]}`. */
export interface ServiceVersion {
  version: string;
  download_url: string;
  release_date: string;
  checksum_sha256: string;
  size_bytes: number;
  is_recommended: boolean;
  min_driver_version: string;
  changelog: LocalizedText;
  tags: string[];
  description: LocalizedText;
  packages: ServicePackage[];
}

/** A platform/chip variant within a `CatalogModel`. */
export interface ModelVariant {
  variant_id: string;
  platform: string;
  chip: string;
  description: LocalizedText;
  min_driver_version: string;
  download_url: string;
  size_bytes: number;
  checksum_sha256: string;
}

/** Hardware-grouped model entry. `GET /api/service-catalog` → `{models: CatalogModel[]}`. */
export interface CatalogModel {
  model_id: string;
  name: string;
  family: string;
  parameter_size: string;
  /** Wire field is `format` (NOT `model_format`). V1 compat name. */
  format: ModelFormat;
  hardware: ModelHardware;
  context_length: number;
  download_url: string;
  /** Wire field is `type` (NOT `model_type`). Practically `"llm" | "vlm"`. */
  type: string;
  min_driver_version: string;
  quantization: string;
  short_description: LocalizedText;
  description: LocalizedText;
  features: LocalizedText[];
  tags: string[];
  size_bytes: number;
  checksum_sha256: string;
  variants: ModelVariant[];
}

/**
 * One frame from a per-task SSE download stream.
 *
 * The wire field name is `version` for legacy V1 reasons, but it actually
 * carries a *task id*: for service packages it's `<version>` (single-platform)
 * or `<version>-<platform_id>` (multi-platform); for models it's `model_id`
 * or `variant_id`. Match this against the calling site's task id, do NOT
 * interpret as a literal version string.
 *
 * `percent` is computed by the backend (`round(downloaded/total*100, 1)`,
 * `0.0` when total ≤ 0). Speed/ETA are NOT backend fields — derive on the
 * frontend (V1 `_calcSpeed`, see useDownloadCenter).
 */
export interface DownloadProgress {
  /** Task id (see note above), not a literal version string. */
  version: string;
  filename: string;
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  status: DownloadStatus;
  error: string;
  save_path: string;
  engine: DownloadEngineKind;
}

/**
 * aria2c availability snapshot. `GET /api/aria2c/status`.
 *
 * NB: V2's current `Aria2cManager` is a simplified implementation:
 *   - `daemon_running` is hard-coded `false`,
 *   - `daemon_pid` is `null`,
 *   - `install_status` is permanently `"idle"`,
 *   - downloads currently fall back to httpx.
 * The 5-state UI banner (available / installing / failed / can_auto_install /
 * missing) still renders correctly off these fields — the `installing` /
 * `failed` arms simply never trigger until the backend daemon manager is
 * fleshed out (post-milestone-3 work).
 */
export interface Aria2cStatus {
  available: boolean;
  can_auto_install: boolean;
  exe_path: string;
  daemon_running: boolean;
  daemon_pid: number | null;
  rpc_port: number;
  install_status: Aria2cInstallStatus;
  install_error: string;
  bin_dir: string;
}

/** Per-item local-disk derivation row. */
export interface LocalItemStatus {
  downloaded: boolean;
  save_path: string;
  installed: boolean;
  install_path: string;
  /**
   * Platform discriminator parsed from the on-disk artifact name (trailing
   * driver tag, e.g. `v81` = Snapdragon X2 Elite, `v73` = X Elite). Lets the
   * UI attribute installed/downloaded state to the correct platform tab.
   * Empty when the name carries no recognisable tag.
   */
  platform_driver?: string;
}

/**
 * `GET /api/versions/local-status`. The `auto_configured` /
 * `auto_configured_path` fields exist on the wire but the current
 * scanner does NOT populate them (always `false` / `""`). V1 used these
 * to auto-detect a pre-existing GenieAPIService install and write back
 * `forge_config.genie_service.root_path` — that wiring is a known gap.
 */
export interface VersionsLocalStatus {
  versions: Record<string, LocalItemStatus>;
  auto_configured: boolean;
  auto_configured_path: string;
}

/** `GET /api/service-catalog/local-status`. */
export interface ModelsLocalStatus {
  models: Record<string, LocalItemStatus>;
}

// ─── Action results ─────────────────────────────────────────────────────────

/** `POST /api/versions/service-install` (NOT `/install` — that path is the frozen pip stub). */
export interface ServiceInstallResult {
  ok: boolean;
  root_path: string;
  exe_path: string;
  version: string;
  zip_deleted: boolean;
}

/** `POST /api/service-catalog/install`. */
export interface ModelInstallResult {
  ok: boolean;
  install_path: string;
  model_id: string;
  zip_deleted: boolean;
}

/**
 * `DELETE /api/versions/install/{version}`.
 *
 * Note: V1's response shape included `root_path_cleared: boolean` (whether
 * `forge_config.genie_service.root_path` was cleared). V2's adapter
 * (filesystem_installer.py:156-170) does NOT emit that field. UI must not
 * depend on it.
 */
export interface DeleteInstalledServiceResult {
  ok: true;
  deleted_paths: string[];
  version: string;
}

/** `DELETE /api/versions/download/{version}`. */
export interface DeleteDownloadedServiceResult {
  ok: true;
  deleted_files: string[];
  version: string;
}

/** `DELETE /api/service-catalog/install/{model_id}?delete_zip=true`. */
export interface DeleteModelResult {
  ok: true;
  deleted_install_dirs: string[];
  deleted_zip_files: string[];
  model_id: string;
}

// ─── Settings ───────────────────────────────────────────────────────────────

/**
 * `GET / PUT /api/versions/download-settings` body and response are this same
 * shape (PUT echoes back the persisted values). Defaults mirror V1:
 *   `fetch_timeout_seconds=15`, `download_timeout_seconds=300`,
 *   `ssl_verify=false` (enterprise-network leniency).
 */
export interface DownloadSettings {
  save_dir: string;
  version_list_url: string;
  catalog_url: string;
  fetch_timeout_seconds: number;
  download_timeout_seconds: number;
  ssl_verify: boolean;
}

// ─── Top-level response envelopes ───────────────────────────────────────────

export interface ServiceVersionsResponse {
  versions: ServiceVersion[];
}

export interface CatalogModelsResponse {
  models: CatalogModel[];
}

export interface Aria2cCancelResponse {
  cancelled: boolean;
  task_id: string;
}

// ─── Request bodies ─────────────────────────────────────────────────────────

export interface ServiceDownloadRequest {
  version: string;
  download_url: string;
  /** Optional sha256 hex; empty string disables verification. */
  checksum_sha256?: string;
  /**
   * Cancellation/lookup key. Single-platform: `version`. Multi-platform:
   * `<version>-<platform_id>`. The same id will appear on the SSE frames'
   * `version` field.
   */
  task_id?: string;
}

export interface ServiceInstallRequestBody {
  save_path: string;
  version?: string;
}

export interface ModelDownloadRequest {
  model_id: string;
  download_url: string;
  checksum_sha256?: string;
  /**
   * Optional per-platform cancellation key (`variant_id`). Multi-platform
   * models share one `model_id` save dir but cancel each platform's download
   * independently. Omitted ⇒ backend uses `model_id` (single-platform).
   */
  task_id?: string;
}

export interface ModelInstallRequestBody {
  save_path: string;
  model_id?: string;
  install_dir?: string;
  /**
   * Selected platform variant id (e.g. ``qwen3-8b-8480-qnn2.44``). Persisted
   * by the backend as an install marker so the Installed pill can show the
   * platform label ("Installed · Snapdragon X2 Elite"), mirroring
   * ServiceVersionCard. Optional — empty degrades to no label.
   */
  variant_id?: string;
}

// ─── Frontend-only auxiliary types (NOT on the wire) ───────────────────────

/**
 * Per-task download state, kept on the frontend by `useDownloadCenter`.
 *
 * Contains everything required by the V1 progress-area UI (status badge,
 * progress bar, bytes/speed/ETA line, engine pill, save_path footer). The
 * speed/ETA fields are derived from successive `DownloadProgress` frames
 * by the V1 `_calcSpeed` algorithm (sampling window 0.5s) — see the
 * composable for details.
 */
export interface DownloadStateEntry {
  task_id: string;
  status: DownloadStatus;
  filename: string;
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  error: string;
  save_path: string;
  engine: DownloadEngineKind;
  /** Frontend-derived from successive frames (V1 `_calcSpeed`). */
  speed_bps: number;
  /** Frontend-derived; 0 when speed is unknown or task complete. */
  eta_seconds: number;
}

/**
 * `DownloadCenterPanel` tab key.
 *
 * V1 nav key for the Download Center is `updates`, but the two top-level
 * tabs *within* the panel (V1 `dcActiveTab`) are `service` and `models`.
 */
export type DownloadCenterTab = "service" | "models";

/** aria2c banner state derived from `Aria2cStatus` (V1 5-state UI). */
export type Aria2cBannerState =
  | "available"
  | "installing"
  | "failed"
  | "can_auto_install"
  | "missing";
