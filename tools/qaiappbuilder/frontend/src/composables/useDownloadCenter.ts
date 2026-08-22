// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useDownloadCenter` — Download Center top-level orchestrator.
 *
 * Composes the four sub-composables that own the V1 download-center state:
 *
 *   - `useServiceVersions` — GenieAPIService release packages tab
 *   - `useModelCatalog`    — hardware-grouped model catalog tab
 *   - `useAria2c`          — aria2c availability + 5-state banner + 2s poll
 *   - `useDownloadSettings` — forge_config download-section editor
 *
 * Owns the *shared* per-task `downloads` map + speed sampler + abort
 * controllers that both download tabs write into. Service tab uses task ids
 * `version` / `version-platform_id`; models tab uses `model_id` /
 * `variant_id` — collisions impossible by construction.
 *
 * Provides V1's "active downloads summary bar" data via a single
 * `activeDownloads` computed.
 *
 * V1 source-of-truth: `useDownloadCenter.js` (1114 lines). The V1 file
 * keeps EVERYTHING in one closure with global refs; this composable splits
 * concerns into testable sub-composables and shares only what must be
 * shared (the per-task download state).
 *
 * Provide / inject:
 *   - This composable is meant to be created ONCE per page (in
 *     `DownloadCenterPanel.vue`'s setup) and the returned object passed to
 *     children via `provide('downloadCtx', ctx)` so cards / banners /
 *     dialogs can read state without prop-drilling.
 *   - The provide key is `Symbol.for('qai/download-ctx')` for type safety
 *     (see `injectDownloadCtx`).
 */

import { computed, type InjectionKey, inject, provide, ref } from "vue";

import { useToastStore } from "@/stores/toast";
import { useI18n } from "vue-i18n";
import type { DownloadCenterTab, DownloadStateEntry, DownloadStatus } from "@/types/downloads";
import type { SpeedSamplerState } from "./downloads/format";
import { isActiveStatus } from "./downloads/format";
import { useAria2c } from "./downloads/useAria2c";
import { useDownloadSettings } from "./downloads/useDownloadSettings";
import { useModelCatalog } from "./downloads/useModelCatalog";
import { useServiceVersions } from "./downloads/useServiceVersions";

export type UseDownloadCenterReturn = ReturnType<typeof useDownloadCenter>;

export const DOWNLOAD_CTX_KEY: InjectionKey<UseDownloadCenterReturn> =
  Symbol.for("qai/download-ctx") as InjectionKey<UseDownloadCenterReturn>;

/**
 * Active tab — persisted to localStorage so the user's choice survives
 * page reloads (V1 parity: `app.js:322-326,2026`, key `qai-downloads-tab`).
 * Defaults to `service` (V1 default) when no valid value is stored.
 */
const TAB_STORAGE_KEY = "qai-downloads-tab";

function isValidTab(v: unknown): v is DownloadCenterTab {
  return v === "service" || v === "models";
}

function initialTab(): DownloadCenterTab {
  if (typeof window === "undefined") return "service";
  try {
    const stored = window.localStorage.getItem(TAB_STORAGE_KEY);
    if (isValidTab(stored)) return stored;
  } catch {
    /* localStorage unavailable (private mode / quota) — fall through */
  }
  return "service";
}

function persistTab(tab: DownloadCenterTab): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(TAB_STORAGE_KEY, tab);
  } catch {
    /* localStorage unavailable — silently no-op (V1 parity) */
  }
}

export function useDownloadCenter() {
  const toast = useToastStore();
  const { t } = useI18n();

  // ── Shared per-task state (both tabs write here) ───────────────────────
  const downloads = ref<Record<string, DownloadStateEntry>>({});
  const speedStates = new Map<string, SpeedSamplerState>();
  const aborts = new Map<string, AbortController>();

  // ── Sub-composables ────────────────────────────────────────────────────
  const aria2c = useAria2c();
  const settings = useDownloadSettings();

  /**
   * Refreshing aria2c on every terminal frame matches V1
   * `useDownloadCenter.js:311,316` — auto-install often completes during
   * the first download, so the 5-state banner needs a tick after `done` /
   * `error` to flip from `installing` to `available`.
   *
   * When a terminal `status` is supplied (from an SSE terminal frame), also
   * raise the V1 download-complete / download-error toast
   * (V1 useDownloadCenter.js:308-318). `cancelled` is silent here because the
   * cancel action already surfaces its own `cancelledToast`.
   */
  function onTerminal(taskId: string, status?: DownloadStatus): void {
    void aria2c.refresh();
    if (status === "done") {
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: t("downloads.completeToast"),
        timeoutMs: 3000,
      });
    } else if (status === "error") {
      const msg = downloads.value[taskId]?.error ?? "";
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: msg
          ? t("downloads.errorToast", { msg })
          : t("downloads.errorToastGeneric", { msg: "" }),
        timeoutMs: 5000,
      });
    }
  }

  /**
   * `onRootPathUpdated` callback — invoked when versions install /
   * delete-installed flips and on `auto_configured=true` from local-status.
   * Currently a no-op hook (the host page is welcome to override via
   * provide / inject); V1 used this to refresh service status.
   */
  function onRootPathUpdated(): void {
    /* hook for the host page (Service panel) */
  }

  const serviceVersions = useServiceVersions({
    downloads,
    speedStates,
    aborts,
    onTerminal,
    onRootPathUpdated,
  });
  const modelCatalog = useModelCatalog({
    downloads,
    speedStates,
    aborts,
    onTerminal,
  });

  // ── Tab state ──────────────────────────────────────────────────────────
  const activeTab = ref<DownloadCenterTab>(initialTab());
  function setActiveTab(tab: DownloadCenterTab): void {
    activeTab.value = tab;
    persistTab(tab);
  }

  // ── Active downloads summary (V1 `activeDownloads` ─ panel:236-246) ───
  const activeDownloads = computed<DownloadStateEntry[]>(() =>
    Object.values(downloads.value).filter((d) => isActiveStatus(d.status)),
  );

  /** Whether ANY service-tab download is active. Drives tab badge. */
  const isAnyServiceDownloading = computed<boolean>(() =>
    Object.values(downloads.value).some((d) => {
      if (!isActiveStatus(d.status)) return false;
      // Service task ids are `version` (no dash) or `version-platformId`.
      // Model task ids are `model_id` (may contain dashes/dots) or
      // `variant_id`. We discriminate by membership in service version set.
      return serviceVersions.versions.value.some((v) => {
        if (d.task_id === v.version) return true;
        if (d.task_id.startsWith(`${v.version}-`)) {
          // Confirm the suffix matches one of the version's platform_ids.
          const suffix = d.task_id.slice(v.version.length + 1);
          return v.packages.some((p) => p.platform_id === suffix);
        }
        return false;
      });
    }),
  );

  /** Whether ANY model-tab download is active. Drives tab badge. */
  const isAnyModelDownloading = computed<boolean>(
    () =>
      activeDownloads.value.length > 0 && !isAnyServiceDownloading.value,
  );

  // ── Convenience cancel (V1 `cancelDownload`) ───────────────────────────

  /**
   * Cancel an in-flight download. Aborts the SSE fetch first, then (after a
   * short delay) calls the aria2c backstop in case the server-side aria2c
   * daemon kept writing.
   */
  async function cancelDownload(taskId: string): Promise<void> {
    // Abort path — instant; sets the per-task entry to `cancelled`.
    serviceVersions.cancel(taskId);
    modelCatalog.cancel(taskId);
    // Backstop — only useful when the SSE fetch had already drained. The
    // result is best-effort.
    void aria2c.cancelBackstop(taskId);
  }

  // ── Save-and-refresh wrapper for download settings (V1 panel:30-36) ──

  async function saveDownloadSettings(): Promise<void> {
    const ok = await settings.save();
    if (ok) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "success",
        message: t("downloads.settingsSavedToast"),
        timeoutMs: 3000,
      });
      // Refresh both catalogs because the user may have just configured the
      // catalog URLs (V1 saveDownloadSettings calls fetchVersions +
      // fetchModelCatalog).
      await Promise.all([
        serviceVersions.fetchVersions(),
        modelCatalog.fetchCatalog(),
      ]);
    } else if (settings.error.value !== null) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "error",
        message: settings.error.value,
        timeoutMs: 5000,
      });
    }
  }

  // ── Top-level init (V1 `init`/`mount` orchestration) ──────────────────

  async function init(): Promise<void> {
    // Load settings first so subsequent catalog fetches use the correct
    // URLs / timeouts (the backend reads forge_config on each request, but
    // surfacing a clear "not configured" hint requires we know the values).
    await settings.load();
    await Promise.all([
      serviceVersions.fetchVersions(),
      modelCatalog.fetchCatalog(),
    ]);
  }

  /** Disposal — abort any in-flight stream + clear timers. */
  function dispose(): void {
    for (const [, ctrl] of aborts) {
      ctrl.abort();
    }
    aborts.clear();
    speedStates.clear();
  }

  return {
    // Sub-composables (full surface).
    serviceVersions,
    modelCatalog,
    aria2c,
    settings,
    // Shared state.
    downloads,
    activeDownloads,
    activeTab,
    setActiveTab,
    isAnyServiceDownloading,
    isAnyModelDownloading,
    // Cross-tab actions.
    cancelDownload,
    saveDownloadSettings,
    init,
    dispose,
  };
}

/**
 * Provide the orchestrator from a host component (typically
 * `DownloadCenterPanel.vue`) so child components can `injectDownloadCtx`.
 */
export function provideDownloadCenter(ctx: UseDownloadCenterReturn): void {
  provide(DOWNLOAD_CTX_KEY, ctx);
}

/**
 * Inject the orchestrator from a child component. Throws if no host
 * provided one — children must live inside a `DownloadCenterPanel`.
 */
export function injectDownloadCtx(): UseDownloadCenterReturn {
  const ctx = inject(DOWNLOAD_CTX_KEY);
  if (ctx === undefined) {
    throw new Error("useDownloadCenter ctx not provided — wrap in <DownloadCenterPanel>.");
  }
  return ctx;
}
