// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useServiceVersions` — V1 "service" tab state machine.
 *
 * Owns the GenieAPIService release-package lifecycle on the frontend:
 *
 *   1. **Catalog**: fetch the remote release manifest (`GET /api/versions`),
 *      sorted descending by semantic version (V1 `compareVersionsDesc`).
 *   2. **Local status**: derive `installed` / `downloaded` per version from
 *      a single disk scan (`GET /api/versions/local-status`).
 *   3. **Per-package selection**: when a version has multiple platform
 *      packages (e.g. v73 / v81), users pick one; the chosen one becomes
 *      the cancellation/lookup task id `<version>-<platform_id>`.
 *   4. **Download**: open an SSE stream (`POST /api/versions/download`),
 *      merge each `DownloadProgress` frame into the per-task `downloads`
 *      map, derive `speed_bps` / `eta_seconds` via the V1 `_calcSpeed`
 *      sampler.
 *   5. **Install**: unzip a completed download (`POST
 *      /api/versions/service-install`).
 *   6. **Delete**: remove the installed dir (`DELETE
 *      /api/versions/install/{version}`) or the downloaded zip
 *      (`DELETE /api/versions/download/{version}`).
 *   7. **Cancel**: abort the in-flight SSE fetch + (backstop) call
 *      `POST /api/aria2c/cancel/{task_id}`.
 *
 * V1 source-of-truth references:
 *   useDownloadCenter.js:11-22   `STATUS` / `getStatusLabel`
 *   useDownloadCenter.js:218-235 `_calcSpeed`
 *   useDownloadCenter.js:276-282 SSE frame parsing (handled by `apiStream`)
 *   useDownloadCenter.js:376     `getPkgTaskId` (single-platform → version, else `version-platformId`)
 *   useDownloadCenter.js:380-397 `getPkgStatus` aggregation across platforms
 *   useDownloadCenter.js:580-598 `fetchVersions` + auto local-status
 *   useDownloadCenter.js:599-748 download/install/delete plumbing
 *
 * NB: This composable is *creator-singleton* — instantiate it ONCE in
 * `useDownloadCenter` and provide/inject the result to children. Calling
 * it twice creates two SSE streams per task.
 */

import { computed, ref, type Ref } from "vue";

import {
  deleteDownloadedService,
  deleteInstalledService,
  fetchServiceVersions,
  fetchVersionsLocalStatus,
  installService as apiInstallService,
  streamServiceDownload,
} from "@/api/downloads";
import { ApiError } from "@/api";
import type {
  DownloadProgress,
  DownloadStateEntry,
  DownloadStatus,
  LocalItemStatus,
  ServicePackage,
  ServiceVersion,
} from "@/types/downloads";
import {
  calcSpeed,
  compareVersionsDesc,
  initialSpeedState,
  type SpeedSamplerState,
} from "./format";

/**
 * Empty progress entry used to seed `downloads[taskId]` lazily so the V1
 * progress-row template can read fields uniformly.
 */
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

/**
 * Compute the cancel/lookup task id for a service version (V1 `getPkgTaskId`).
 *
 * Single-platform package → `version`.
 * Multi-platform → `<version>-<platform_id>` for the user-selected (or
 * default) package.
 */
export function serviceTaskId(
  v: ServiceVersion,
  selectedPlatformId: string | undefined,
): string {
  if (v.packages.length <= 1) return v.version;
  return `${v.version}-${selectedPlatformId ?? defaultPlatformId(v)}`;
}

/** V1 `getDefaultPlatformId` — the package with `is_default: true`, else first. */
export function defaultPlatformId(v: ServiceVersion): string {
  if (v.packages.length === 0) return "";
  const pinned = v.packages.find((p) => p.is_default);
  return (pinned ?? v.packages[0])?.platform_id ?? "";
}

/** Find a package by platform_id (returns the first when not found). */
export function findPackage(
  v: ServiceVersion,
  platformId: string | undefined,
): ServicePackage | null {
  if (v.packages.length === 0) return null;
  if (platformId === undefined || platformId === "") {
    return v.packages.find((p) => p.is_default) ?? v.packages[0] ?? null;
  }
  return v.packages.find((p) => p.platform_id === platformId) ?? null;
}

/**
 * Shared dependency: the per-task download state map. Lives on
 * `useDownloadCenter` and is passed in so service + model tabs share it
 * (a task id collision across tabs is impossible by construction —
 * versions use semver, models use model_id / variant_id).
 */
export interface ServiceVersionsDeps {
  /** `downloads[taskId]` is the live state for that download. */
  readonly downloads: Ref<Record<string, DownloadStateEntry>>;
  /** Per-task speed sampler state. Mutated in place. */
  readonly speedStates: Map<string, SpeedSamplerState>;
  /** Per-task abort handle for cancellation. */
  readonly aborts: Map<string, AbortController>;
  /**
   * Optional: invoked once a task hits a terminal state — used to refresh
   * aria2c and (when a terminal `status` is supplied) raise the V1
   * download-complete / download-error toast (V1 useDownloadCenter.js:308-318).
   * Called with the terminal `status` from the SSE frame; called without a
   * status on stream EOF / abort (refresh-only).
   */
  readonly onTerminal?: (taskId: string, status?: DownloadStatus) => void;
  /** Optional: invoked when forge_config / install state may have changed. */
  readonly onRootPathUpdated?: () => void;
}

export function useServiceVersions(deps: ServiceVersionsDeps) {
  const versions = ref<ServiceVersion[]>([]);
  const localStatus = ref<Record<string, LocalItemStatus>>({});
  const autoConfigured = ref(false);
  const autoConfiguredPath = ref("");

  const loading = ref(false);
  /**
   * Reason the catalog could not be loaded — kept distinct from a generic
   * error toast so the UI can render the V1 empty-state hint
   * ("configure version_list_url in Settings") for the 422
   * `service_release.catalog_unavailable` case.
   */
  const loadError = ref<{ kind: "unconfigured" | "transport"; message: string } | null>(null);
  // In-flight versions fetch promise (in-flight dedup; see `fetchVersions`).
  let _inFlight: Promise<void> | null = null;

  /** Per-version selected platform_id (drives multi-package taskId). */
  const selectedPlatform = ref<Record<string, string>>({});

  /**
   * Per-version install lifecycle state (V1 `installStatuses`,
   * useDownloadCenter.js:888-925): `installing` while the unzip-to-bin POST
   * is in flight (card shows a spinner), `error` with `installError` on
   * failure (card keeps the retry-able "Install to bin" entry + red message).
   * Cleared back to `idle`/absent on success.
   */
  const installState = ref<Record<string, "installing" | "error">>({});
  const installError = ref<Record<string, string>>({});

  // Sorted view (V1 always renders newest-first).
  const sortedVersions = computed<ServiceVersion[]>(() =>
    // Defensive: guard against a malformed payload leaving `versions.value`
    // non-array (e.g. backend 422), which would otherwise throw on spread.
    (Array.isArray(versions.value) ? [...versions.value] : []).sort((a, b) =>
      compareVersionsDesc(a.version, b.version),
    ),
  );

  /** Convenience: choose a platform (V1 `setSelectedPlatform`). */
  function setSelectedPlatform(version: string, platformId: string): void {
    selectedPlatform.value = { ...selectedPlatform.value, [version]: platformId };
  }

  /** Resolve the *effective* platform_id for a version (selected, else default). */
  function effectivePlatformId(v: ServiceVersion): string {
    return selectedPlatform.value[v.version] ?? defaultPlatformId(v);
  }

  /** Resolve the *task id* for a version (single → version, multi → version-platformId). */
  function taskIdFor(v: ServiceVersion): string {
    return serviceTaskId(v, selectedPlatform.value[v.version]);
  }

  // ── Catalog ────────────────────────────────────────────────────────────

  /**
   * Fetch versions + local status. On a fresh load (`forceLocal=true`),
   * always re-scan the disk; on background reloads we still scan because
   * `installed` / `downloaded` flags drive button choice.
   */
  async function fetchVersions(): Promise<void> {
    // In-flight dedup (see `useModelCatalog.fetchCatalog`): the Downloads panel
    // re-runs `init()` on every mount, so rapid tab switching could fire
    // multiple concurrent identical GitHub release-manifest fetches. Reuse the
    // running promise instead of starting another. Cleared in `finally`.
    if (_inFlight !== null) return _inFlight;
    _inFlight = (async () => {
      loading.value = true;
      loadError.value = null;
      try {
        const res = await fetchServiceVersions();
        versions.value = Array.isArray(res.versions) ? res.versions : [];
        // Re-derive local status now that we know which versions exist.
        await fetchLocalStatus();
      } catch (e) {
        versions.value = [];
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
      const res = await fetchVersionsLocalStatus();
      localStatus.value = res.versions;
      autoConfigured.value = res.auto_configured;
      autoConfiguredPath.value = res.auto_configured_path;
      if (res.auto_configured) deps.onRootPathUpdated?.();
    } catch {
      // Silent — empty local status is the same as "nothing known yet".
    }
  }

  // ── Download ───────────────────────────────────────────────────────────

  /**
   * Start an SSE-streamed download for a (version, package) tuple.
   *
   * Status machine on the per-task entry:
   *   `idle / preparing` → `downloading` (each frame) → terminal (`done` /
   *   `error` / `cancelled`).
   *
   * Re-entrancy: if a task with the same id is already downloading, this
   * is a no-op — the V1 toast `alreadyDownloading` should be raised by
   * the caller (`useDownloadCenter`) so it can include i18n + toast store.
   */
  async function startDownload(v: ServiceVersion): Promise<void> {
    const pkg = findPackage(v, selectedPlatform.value[v.version]);
    if (pkg === null && v.packages.length > 0) return;

    const taskId = taskIdFor(v);
    const downloadUrl = pkg !== null ? pkg.download_url : v.download_url;

    if (deps.aborts.has(taskId)) return; // already streaming

    const ctrl = new AbortController();
    deps.aborts.set(taskId, ctrl);

    // Seed entry + speed sampler.
    deps.downloads.value = {
      ...deps.downloads.value,
      [taskId]: {
        ...emptyEntry(taskId),
        status: "preparing",
      },
    };
    deps.speedStates.set(taskId, initialSpeedState());

    try {
      await streamServiceDownload(
        {
          version: v.version,
          download_url: downloadUrl,
          checksum_sha256: v.checksum_sha256,
          task_id: taskId,
        },
        {
          onChunk: (frame) => mergeFrame(taskId, frame),
        },
        { signal: ctrl.signal },
      );
      // Stream EOF without terminal frame → consider complete; let local
      // status reconcile authoritatively.
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

  /** Merge an SSE frame onto the per-task entry + run the speed sampler. */
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
      // Terminal frames force speed/ETA to zero so the UI doesn't keep
      // showing a stale rate (V1 useDownloadCenter.js:298-300).
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

    // Refresh local-status on terminal frames so the button row flips
    // promptly to "Install / Delete". Also notify the orchestrator with the
    // terminal status so it can raise the V1 complete/error toast
    // (V1 useDownloadCenter.js:308-318).
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
      [taskId]: {
        ...prev,
        status,
        error,
        speed_bps: 0,
        eta_seconds: 0,
      },
    };
  }

  /** V1 `cancelDownload` — abort SSE first, fall back to server cancel. */
  function cancel(taskId: string): void {
    const ctrl = deps.aborts.get(taskId);
    if (ctrl !== undefined) {
      ctrl.abort();
      deps.aborts.delete(taskId);
    }
    finaliseTask(taskId, "cancelled", "");
  }

  /** V1 `clearDownloadStatus` — drop the per-task entry (used by Close button). */
  function clearStatus(taskId: string): void {
    if (!(taskId in deps.downloads.value)) return;
    const next = { ...deps.downloads.value };
    delete next[taskId];
    deps.downloads.value = next;
  }

  // ── Install / delete ───────────────────────────────────────────────────

  async function install(v: ServiceVersion, savePath: string): Promise<{ ok: boolean; error?: string }> {
    // V1 parity (useDownloadCenter.js:888-925): mark `installing` so the card
    // shows a spinner; on success clear it, on failure record `error`.
    installState.value = { ...installState.value, [v.version]: "installing" };
    {
      const next = { ...installError.value };
      delete next[v.version];
      installError.value = next;
    }
    try {
      const res = await apiInstallService({ save_path: savePath, version: v.version });
      {
        const next = { ...installState.value };
        delete next[v.version];
        installState.value = next;
      }
      // Drop the completed download task entry so the card stops showing the
      // "done" row (Install / Copy / Delete / Close) and flips to the
      // "installed" row. Without this, ``isDone`` (status === 'done') stays
      // true and shadows the installed row — so "Install to bin directory"
      // lingered even after a successful install (V1 cleared it here too).
      clearStatus(taskIdFor(v));
      await fetchLocalStatus();
      deps.onRootPathUpdated?.();
      return { ok: res.ok };
    } catch (e) {
      const msg = asMessage(e);
      installState.value = { ...installState.value, [v.version]: "error" };
      installError.value = { ...installError.value, [v.version]: msg };
      return { ok: false, error: msg };
    }
  }

  async function deleteInstalled(
    version: string,
    stopRunning = false,
  ): Promise<{ ok: boolean; error?: string }> {
    try {
      await deleteInstalledService(version, stopRunning);
      await fetchLocalStatus();
      deps.onRootPathUpdated?.();
      return { ok: true };
    } catch (e) {
      return { ok: false, error: asMessage(e) };
    }
  }

  async function deleteDownloaded(version: string): Promise<{ ok: boolean; error?: string }> {
    try {
      await deleteDownloadedService(version);
      // Drop any local status flag immediately so the button row flips.
      clearStatus(version);
      await fetchLocalStatus();
      return { ok: true };
    } catch (e) {
      return { ok: false, error: asMessage(e) };
    }
  }

  return {
    versions,
    sortedVersions,
    localStatus,
    autoConfigured,
    autoConfiguredPath,
    loading,
    loadError,
    selectedPlatform,
    installState,
    installError,
    setSelectedPlatform,
    effectivePlatformId,
    taskIdFor,
    fetchVersions,
    fetchLocalStatus,
    startDownload,
    cancel,
    clearStatus,
    install,
    deleteInstalled,
    deleteDownloaded,
  };
}

export type UseServiceVersionsReturn = ReturnType<typeof useServiceVersions>;

// ─── helpers ────────────────────────────────────────────────────────────

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
