// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useModelCatalog` — V1 "models" tab state machine.
 *
 * Owns the local-model lifecycle on the frontend:
 *
 *   1. **Catalog**: fetch the hardware-grouped model list
 *      (`GET /api/service-catalog`), partition into NPU / GPU / CPU groups
 *      (V1 `modelsByHardware`).
 *   2. **Local status**: scan the disk
 *      (`GET /api/service-catalog/local-status`) and map back to model_id
 *      via the V1 *three-level lookup* (direct key → install_path/save_path
 *      basename → prefix match), required because the disk scan only knows
 *      directory names while the catalog uses model_id.
 *   3. **Variant selection**: when a model has `variants[]` users pick one;
 *      the chosen `variant_id` becomes the cancellation/lookup task id.
 *   4. **Download**: SSE stream (`POST /api/service-catalog/download`),
 *      shared `downloads` map + speed sampler with the service tab.
 *   5. **Install / delete**: `POST /api/service-catalog/install` /
 *      `DELETE /api/service-catalog/install/{model_id}?delete_zip=true`
 *      (V1 always sets delete_zip=true).
 *
 * V1 source-of-truth references:
 *   useDownloadCenter.js:134-142  `modelsByHardware`
 *   useDownloadCenter.js:529-578  `getInstallModelStatus` / `getModelDownloadStatus` (3-level lookup)
 *   useDownloadCenter.js:656      `getModelVariantTaskId`
 *   useDownloadCenter.js:661-849  fetchModelCatalog / start / install / delete plumbing
 *
 * NB: This composable shares the per-task `downloads` map + speed sampler
 * with `useServiceVersions` via the same `Deps` shape. Service uses
 * `version` / `version-platform_id` task ids; models use `model_id` /
 * `variant_id` — collision impossible by construction.
 */

import { computed, ref, type Ref } from "vue";

import {
  deleteModel as apiDeleteModel,
  fetchCatalogModels,
  fetchModelsLocalStatus,
  installModel as apiInstallModel,
  streamModelDownload,
} from "@/api/downloads";
import { ApiError } from "@/api";
import type {
  CatalogModel,
  DownloadProgress,
  DownloadStateEntry,
  DownloadStatus,
  LocalItemStatus,
  ModelHardware,
  ModelVariant,
} from "@/types/downloads";
import {
  calcSpeed,
  initialSpeedState,
  type SpeedSamplerState,
} from "./format";

const HARDWARE_GROUPS: readonly ModelHardware[] = ["npu", "gpu", "cpu"];

function emptyEntry(taskId: string): DownloadStateEntry {
  return {
    task_id: taskId,
    status: "idle",
    filename: "",
    downloaded_bytes: 0,
    total_bytes: 0,
    percent: 0,
    error: "",
    save_path: "",
    engine: "",
    speed_bps: 0,
    eta_seconds: 0,
  };
}

/** V1 `getDefaultModelVariantId` — first variant or empty. */
export function defaultVariantId(m: CatalogModel): string {
  return m.variants[0]?.variant_id ?? "";
}

/** Resolve the *task id* for a (model, selected variant) tuple. */
export function modelTaskId(
  m: CatalogModel,
  selectedVariantId: string | undefined,
): string {
  if (m.variants.length === 0) return m.model_id;
  if (m.variants.length === 1) return m.variants[0]!.variant_id;
  return selectedVariantId && m.variants.some((v) => v.variant_id === selectedVariantId)
    ? selectedVariantId
    : defaultVariantId(m);
}

/** Find a variant by id (returns first when not found / undefined). */
export function findVariant(
  m: CatalogModel,
  variantId: string | undefined,
): ModelVariant | null {
  if (m.variants.length === 0) return null;
  if (variantId === undefined || variantId === "") {
    return m.variants[0] ?? null;
  }
  return m.variants.find((v) => v.variant_id === variantId) ?? null;
}

/**
 * V1 three-level lookup for local status (`getInstallModelStatus` /
 * `getModelDownloadStatus`).
 *
 * The disk scanner keys by directory basename, which may NOT match the
 * catalog `model_id` after a fresh page load. V1 falls back to:
 *   1. Direct match on the lookup key.
 *   2. Match on `basename(install_path) === key` or `basename(save_path) === key`.
 *   3. Prefix match (the directory name starts with the key — handles the
 *      `<model_id>_<variant>` pattern).
 *
 * Returns the matching `LocalItemStatus` or `null`.
 */
export function lookupLocalStatus(
  table: Record<string, LocalItemStatus>,
  key: string,
): LocalItemStatus | null {
  if (key.length === 0) return null;
  // (1) direct
  const direct = table[key];
  if (direct !== undefined) return direct;
  // (2) basename match on install_path / save_path
  for (const [, item] of Object.entries(table)) {
    if (item.install_path && basename(item.install_path) === key) return item;
    if (item.save_path && basename(item.save_path) === key) return item;
  }
  // (3) prefix match
  for (const [k, item] of Object.entries(table)) {
    if (k.startsWith(key)) return item;
  }
  return null;
}

function basename(p: string): string {
  if (p.length === 0) return "";
  const cleaned = p.replace(/[\\/]+$/, "");
  const slash = Math.max(cleaned.lastIndexOf("/"), cleaned.lastIndexOf("\\"));
  return slash < 0 ? cleaned : cleaned.slice(slash + 1);
}

/** Shared deps with `useServiceVersions`. */
export interface ModelCatalogDeps {
  readonly downloads: Ref<Record<string, DownloadStateEntry>>;
  readonly speedStates: Map<string, SpeedSamplerState>;
  readonly aborts: Map<string, AbortController>;
  /**
   * Optional: invoked once a task hits a terminal state — used to refresh
   * aria2c and (when a terminal `status` is supplied) raise the V1
   * download-complete / download-error toast (V1 useDownloadCenter.js:308-318).
   */
  readonly onTerminal?: (taskId: string, status?: DownloadStatus) => void;
}

export function useModelCatalog(deps: ModelCatalogDeps) {
  const models = ref<CatalogModel[]>([]);
  const localStatus = ref<Record<string, LocalItemStatus>>({});

  const loading = ref(false);
  const loadError = ref<{ kind: "unconfigured" | "transport"; message: string } | null>(null);
  // In-flight catalog fetch promise (in-flight dedup; see `fetchCatalog`).
  let _inFlight: Promise<void> | null = null;

  /** Per-model selected variant_id (drives multi-variant taskId). */
  const selectedVariant = ref<Record<string, string>>({});

  /**
   * Per-task model install lifecycle state (V1 `installModelStatuses`,
   * useDownloadCenter.js: `getInstallModelStatus`). `installing` while the
   * unzip-to-models POST is in flight (the card's done-state row shows a
   * spinner, DownloadCenterPanel.js:616), `error` with `installError` on
   * failure (card keeps a retry-able "Install" entry + red message +
   * "delete bad file" button, DownloadCenterPanel.js:612-613). Cleared on
   * success. Keyed by the resolved task id (model_id / variant_id).
   */
  const installState = ref<Record<string, "installing" | "error">>({});
  const installError = ref<Record<string, string>>({});

  /** V1 `modelsByHardware` — group by hardware key, NPU → GPU → CPU order. */
  const modelsByHardware = computed<Record<ModelHardware, CatalogModel[]>>(() => {
    const groups: Record<ModelHardware, CatalogModel[]> = { npu: [], gpu: [], cpu: [] };
    // Defensive: `models.value` should always be an array, but guard against
    // a malformed backend payload (e.g. 422) leaving it undefined/non-array.
    if (!Array.isArray(models.value)) return groups;
    for (const m of models.value) {
      const hw: ModelHardware = HARDWARE_GROUPS.includes(m.hardware) ? m.hardware : "cpu";
      groups[hw].push(m);
    }
    return groups;
  });

  /** Total number of catalog models — used for the "models" tab badge. */
  const modelCount = computed<number>(() =>
    Array.isArray(models.value) ? models.value.length : 0,
  );

  function setSelectedVariant(modelId: string, variantId: string): void {
    selectedVariant.value = { ...selectedVariant.value, [modelId]: variantId };
  }

  function effectiveVariantId(m: CatalogModel): string {
    return selectedVariant.value[m.model_id] ?? defaultVariantId(m);
  }

  function taskIdFor(m: CatalogModel): string {
    return modelTaskId(m, selectedVariant.value[m.model_id]);
  }

  /**
   * Get the local status for a model.
   *
   * Multi-platform models share **one** on-disk artifact per model (mirroring
   * GenieAPIService where one version's installed bin is shared across
   * platform tabs): the backend lays out `data/downloads/<model_id>/` and
   * `data/models/<model_id>/` regardless of which platform was downloaded /
   * installed (see `scan_models` in `filesystem_installer.py`). Both tabs
   * therefore look up the same key — `model_id` — and present a consistent
   * "Installed / Downloaded + path" row. Which platform's artifact is on
   * disk is recorded in `LocalItemStatus.platform_driver` so the UI can
   * highlight the matching tab and the Installed pill (V1-style aggregation).
   *
   * The legacy `taskId` parameter is retained for call-site compatibility
   * but ignored: status is keyed by `model_id` alone. We still go through the
   * 3-level lookup (direct → basename → prefix) so a renamed-by-hand dir is
   * still recognised after a page reload.
   */
  function localStatusFor(
    m: CatalogModel,
    _taskId: string,
  ): LocalItemStatus | null {
    return lookupLocalStatus(localStatus.value, m.model_id);
  }

  // ── Catalog ────────────────────────────────────────────────────────────

  async function fetchCatalog(): Promise<void> {
    // In-flight dedup: the Downloads panel calls this on every mount (each tab
    // switch back re-runs `init()`), so rapid panel switching could otherwise
    // fire multiple concurrent identical GitHub catalog fetches. While one is
    // running, return the SAME promise so a re-entrant call just awaits the
    // existing request instead of starting another (no duplicate network hits,
    // no last-writer-wins race). Cleared in `finally`.
    if (_inFlight !== null) return _inFlight;
    _inFlight = (async () => {
      loading.value = true;
      loadError.value = null;
      try {
        const res = await fetchCatalogModels();
        models.value = Array.isArray(res.models) ? res.models : [];
        await fetchLocalStatus();
      } catch (e) {
        models.value = [];
        loadError.value = classifyLoadError(e);
      } finally {
        loading.value = false;
        _inFlight = null;
      }
    })();
    return _inFlight;
  }

  async function fetchLocalStatus(): Promise<void> {
    try {
      const res = await fetchModelsLocalStatus();
      localStatus.value = res.models;
    } catch {
      // Swallow — empty local status is acceptable.
    }
  }

  // ── Download ───────────────────────────────────────────────────────────

  async function startDownload(m: CatalogModel): Promise<void> {
    const variant = findVariant(m, selectedVariant.value[m.model_id]);
    const taskId = taskIdFor(m);
    const downloadUrl = variant !== null ? variant.download_url : m.download_url;
    const checksum = variant !== null ? variant.checksum_sha256 : m.checksum_sha256;

    if (deps.aborts.has(taskId)) return;

    const ctrl = new AbortController();
    deps.aborts.set(taskId, ctrl);

    deps.downloads.value = {
      ...deps.downloads.value,
      [taskId]: { ...emptyEntry(taskId), status: "preparing" },
    };
    deps.speedStates.set(taskId, initialSpeedState());

    try {
      await streamModelDownload(
        {
          // Shared per-model save dir (multi-platform models install one
          // shared copy, like GenieAPIService versions). The per-platform
          // cancellation/progress key stays `taskId` (= variant_id) so two
          // platforms can download in parallel without colliding.
          model_id: m.model_id,
          task_id: taskId,
          download_url: downloadUrl,
          checksum_sha256: checksum,
        },
        {
          onChunk: (frame) => mergeFrame(taskId, frame),
        },
        { signal: ctrl.signal },
      );
      void fetchLocalStatus();
    } catch (e) {
      const aborted = ctrl.signal.aborted;
      finaliseTask(taskId, aborted ? "cancelled" : "error", aborted ? "" : asMessage(e));
    } finally {
      deps.aborts.delete(taskId);
      deps.speedStates.delete(taskId);
      deps.onTerminal?.(taskId);
    }
  }

  function mergeFrame(taskId: string, frame: DownloadProgress): void {
    const prev = deps.downloads.value[taskId] ?? emptyEntry(taskId);
    let speed_bps = prev.speed_bps;
    let eta_seconds = prev.eta_seconds;

    if (frame.status === "downloading") {
      const sampler = deps.speedStates.get(taskId) ?? initialSpeedState();
      const out = calcSpeed(sampler, frame.downloaded_bytes, frame.total_bytes);
      speed_bps = out.speed_bps;
      eta_seconds = out.eta_seconds;
      deps.speedStates.set(taskId, sampler);
    } else if (
      frame.status === "done" ||
      frame.status === "error" ||
      frame.status === "cancelled"
    ) {
      speed_bps = 0;
      eta_seconds = 0;
    }

    deps.downloads.value = {
      ...deps.downloads.value,
      [taskId]: {
        task_id: taskId,
        status: frame.status,
        filename: frame.filename,
        downloaded_bytes: frame.downloaded_bytes,
        total_bytes: frame.total_bytes,
        percent: frame.percent,
        error: frame.error,
        save_path: frame.save_path,
        engine: frame.engine,
        speed_bps,
        eta_seconds,
      },
    };

    if (
      frame.status === "done" ||
      frame.status === "error" ||
      frame.status === "cancelled"
    ) {
      void fetchLocalStatus();
      deps.onTerminal?.(taskId, frame.status);
    }
  }

  function finaliseTask(taskId: string, status: DownloadStatus, error: string): void {
    const prev = deps.downloads.value[taskId] ?? emptyEntry(taskId);
    deps.downloads.value = {
      ...deps.downloads.value,
      [taskId]: { ...prev, status, error, speed_bps: 0, eta_seconds: 0 },
    };
  }

  function cancel(taskId: string): void {
    const ctrl = deps.aborts.get(taskId);
    if (ctrl !== undefined) {
      ctrl.abort();
      deps.aborts.delete(taskId);
    }
    finaliseTask(taskId, "cancelled", "");
  }

  function clearStatus(taskId: string): void {
    if (!(taskId in deps.downloads.value)) return;
    const next = { ...deps.downloads.value };
    delete next[taskId];
    deps.downloads.value = next;
  }

  // ── Install / delete ───────────────────────────────────────────────────

  async function install(
    m: CatalogModel,
    savePath: string,
  ): Promise<{ ok: boolean; install_path?: string; error?: string }> {
    // Install state is keyed by `model_id` (one shared install per model,
    // like a GenieAPIService version) so both platform tabs reflect the same
    // "Installing…" / error row.
    const installKey = m.model_id;
    // Re-entry guard: a second install POST after a successful one races
    // against the backend's post-install zip deletion (V1 parity:
    // filesystem_installer deletes the source zip on success), so the retry
    // hits ``invalid save_path`` (422) and surfaces a misleading "install
    // failed" alert even though the model is already installed. Ignore a
    // re-fire while one is already in flight for this task.
    if (installState.value[installKey] === "installing") {
      return { ok: false, error: "install already in progress" };
    }
    // V1 parity (useDownloadCenter.js / getInstallModelStatus): mark
    // `installing` so the done-state row shows an "Installing…" spinner; on
    // success clear it, on failure record `error` so the card keeps a red
    // message + retry-able install entry (DownloadCenterPanel.js:612-616).
    installState.value = { ...installState.value, [installKey]: "installing" };
    {
      const next = { ...installError.value };
      delete next[installKey];
      installError.value = next;
    }
    try {
      // Install dir = `model_id` (a model installs ONE shared copy across its
      // platform variants — mirrors GenieAPIService's per-version bin). This
      // matches the key `localStatusFor` looks up by, so both tabs recognise
      // the install. `savePath` points at the specific platform zip the user
      // downloaded; the backend extracts it into `models/<model_id>/`.
      const res = await apiInstallModel({
        save_path: savePath,
        model_id: m.model_id,
        install_dir: m.model_id,
        // The selected platform variant (taskIdFor → variant_id for a
        // multi-platform model). Persisted as a marker so the Installed pill
        // shows the platform label, even though the install dir is shared.
        variant_id: taskIdFor(m),
      });
      {
        const next = { ...installState.value };
        delete next[installKey];
        installState.value = next;
      }
      await fetchLocalStatus();
      return { ok: res.ok, install_path: res.install_path };
    } catch (e) {
      const msg = asMessage(e);
      installState.value = { ...installState.value, [installKey]: "error" };
      installError.value = { ...installError.value, [installKey]: msg };
      return { ok: false, error: msg };
    }
  }

  async function deleteCatalogModel(
    modelId: string,
  ): Promise<{ ok: boolean; error?: string }> {
    try {
      await apiDeleteModel(modelId, { deleteZip: true });
      clearStatus(modelId);
      await fetchLocalStatus();
      return { ok: true };
    } catch (e) {
      return { ok: false, error: asMessage(e) };
    }
  }

  return {
    models,
    modelsByHardware,
    modelCount,
    localStatus,
    loading,
    loadError,
    selectedVariant,
    installState,
    installError,
    setSelectedVariant,
    effectiveVariantId,
    taskIdFor,
    localStatusFor,
    fetchCatalog,
    fetchLocalStatus,
    startDownload,
    cancel,
    clearStatus,
    install,
    deleteCatalogModel,
  };
}

export type UseModelCatalogReturn = ReturnType<typeof useModelCatalog>;

// ─── helpers ───────────────────────────────────────────────────────────

function classifyLoadError(e: unknown): { kind: "unconfigured" | "transport"; message: string } {
  if (e instanceof ApiError) {
    if (e.code === "service_release.catalog_unavailable") {
      return { kind: "unconfigured", message: e.message };
    }
    return { kind: "transport", message: e.message };
  }
  return { kind: "transport", message: e instanceof Error ? e.message : String(e) };
}

function asMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}
