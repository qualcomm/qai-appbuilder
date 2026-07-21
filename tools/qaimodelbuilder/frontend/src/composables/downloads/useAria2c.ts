// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useAria2c` — aria2c availability + V1 5-state banner + 2s polling.
 *
 * V1 source-of-truth references:
 *   composable          useDownloadCenter.js:991-1021 (`fetchAria2cStatus`)
 *   5-state banner      DownloadCenterPanel.js:104-152
 *   cancel              useDownloadCenter.js:931-959 + 627-661 backend
 *
 * Polling rule (V1):
 *   - On mount, fetch once.
 *   - If `install_status === "installing"`, poll every 2s.
 *   - As soon as `install_status` leaves `"installing"`, clear the
 *     interval (idempotent — safe to call multiple times).
 *   - Each terminal SSE frame (`done` / `error`) on a download stream
 *     should also trigger a one-shot refresh (the auto-install may have
 *     completed in parallel). The composable exposes `refresh()` for this.
 *
 * Cancellation rule (V1, see `useDownloadCenter.cancelDownload`):
 *   - The frontend FIRST aborts the SSE fetch via `AbortController.abort()`
 *     — this is the immediate, deterministic stop for both the httpx and
 *     aria2c paths because it tears down the in-flight HTTP body.
 *   - Only if the SSE fetch has already completed (e.g. a background aria2c
 *     daemon kept the file growing) do we call `POST /api/aria2c/cancel/{task_id}`
 *     as a backstop. We expose `cancelBackstop()` so callers can opt in.
 */

import {
  computed,
  getCurrentInstance,
  onMounted,
  onScopeDispose,
  ref,
} from "vue";

import {
  cancelAria2cDownload as apiCancelAria2cDownload,
  fetchAria2cStatus,
} from "@/api/downloads";
import type { Aria2cBannerState, Aria2cStatus } from "@/types/downloads";

const POLL_INTERVAL_MS = 2_000;

/** Initial sentinel — distinguishes "not yet fetched" from a real `idle` state. */
function initialStatus(): Aria2cStatus {
  return {
    available: false,
    can_auto_install: false,
    exe_path: "",
    daemon_running: false,
    daemon_pid: null,
    rpc_port: 6800,
    install_status: "idle",
    install_error: "",
    bin_dir: "",
  };
}

/**
 * Derive the V1 5-state banner key from a status snapshot.
 *
 *   1. `installing` — ongoing auto-install (highest priority for visibility)
 *   2. `failed`     — last auto-install attempt failed
 *   3. `available`  — aria2c is on PATH / discovered
 *   4. `can_auto_install` — Windows + bin_dir configured + not yet installed
 *   5. `missing`    — fallback (httpx single-thread will be used)
 *
 * Mirrors `DownloadCenterPanel.js:104-152` priority chain.
 */
export function aria2cBannerState(s: Aria2cStatus): Aria2cBannerState {
  if (s.install_status === "installing") return "installing";
  if (s.install_status === "failed") return "failed";
  if (s.available) return "available";
  if (s.can_auto_install) return "can_auto_install";
  return "missing";
}

export function useAria2c() {
  const status = ref<Aria2cStatus>(initialStatus());
  const loaded = ref(false);
  const error = ref<string | null>(null);

  let timer: number | null = null;

  /**
   * Fetch once. Updates `status` / `loaded` / `error` and (re)evaluates the
   * polling timer based on the new `install_status`.
   */
  async function refresh(): Promise<void> {
    try {
      const next = await fetchAria2cStatus();
      status.value = next;
      loaded.value = true;
      error.value = null;
      // Polling re-evaluation: only the `installing` state requires a tick.
      if (next.install_status === "installing") {
        ensurePolling();
      } else {
        stopPolling();
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
      // Don't clobber a previously-loaded status on transient failures.
    }
  }

  function ensurePolling(): void {
    if (timer !== null) return;
    timer = window.setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
  }

  function stopPolling(): void {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  /**
   * Server-side cancel backstop — see "Cancellation rule" in the file
   * docstring. Callers SHOULD abort their SSE fetch first; this is purely
   * for the residual-process case.
   */
  async function cancelBackstop(taskId: string): Promise<boolean> {
    try {
      const res = await apiCancelAria2cDownload(taskId);
      return res.cancelled;
    } catch {
      return false;
    }
  }

  const banner = computed<Aria2cBannerState>(() => aria2cBannerState(status.value));

  // The initial probe normally runs on mount. When this composable is created
  // inside a detached `effectScope` (the app-lifetime downloads singleton, see
  // `stores/downloads.ts`) there is no component instance, so `onMounted` would
  // warn and never fire — the singleton triggers `refresh()` explicitly in
  // that case. Only register the hook when a real component owns this scope.
  if (getCurrentInstance()) {
    onMounted(() => {
      void refresh();
    });
  }
  onScopeDispose(() => {
    stopPolling();
  });

  return {
    status,
    loaded,
    error,
    banner,
    refresh,
    cancelBackstop,
  };
}

export type UseAria2cReturn = ReturnType<typeof useAria2c>;
