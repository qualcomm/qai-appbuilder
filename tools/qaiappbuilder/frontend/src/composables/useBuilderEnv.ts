// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useBuilderEnv` — shared state of the model-conversion environment.
 *
 * One module-level singleton, so the Downloads page card and the chat-page banner see
 * the same data from ONE poll loop instead of each polling on its own.
 *
 * Polling is cheap and self-throttling:
 *   - while an install is running: every 1.5 s (smooth progress);
 *   - otherwise: every 30 s, and only while the tab is visible (an install finished by
 *     hand, or by another window, shows up without a reload);
 *   - the loop runs only while at least one consumer is mounted (`acquire` / `release`).
 *
 * It never blocks anything: a failed poll (server restarting, offline) keeps the last
 * known state and just tries again.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";

import {
  cancelBuilderEnvInstall,
  fetchBuilderEnv,
  startBuilderEnvInstall,
  type BuilderEnvInstallRequest,
  type BuilderEnvStatus,
} from "@/api/builderEnv";
import { ApiError } from "@/api/errors";

export const POLL_RUNNING_MS = 1500;
export const POLL_IDLE_MS = 30_000;

/** How the banner/card should treat the environment. */
export type BuilderEnvPhase =
  | "unknown" //     not loaded yet (or the feature does not exist on this server)
  | "ready"
  | "missing" //     not installed, can be installed
  | "unavailable" // not installed and this build cannot install it
  | "installing"
  | "failed" //      last install failed or was cancelled, environment still not ready
  | "cancelled";

export interface UseBuilderEnv {
  status: Ref<BuilderEnvStatus | null>;
  phase: ComputedRef<BuilderEnvPhase>;
  /** Percent 0–100 of the running (or last) job. */
  percent: ComputedRef<number>;
  busy: Ref<boolean>;
  /** Last error from start/cancel (not from polling). */
  actionError: Ref<string>;
  refresh: () => Promise<void>;
  install: (body?: BuilderEnvInstallRequest) => Promise<boolean>;
  cancel: () => Promise<void>;
  acquire: () => void;
  release: () => void;
}

const status = ref<BuilderEnvStatus | null>(null);
const busy = ref(false);
const actionError = ref("");
/** Set when the endpoint answers 404 (feature absent): stop polling, show nothing. */
const absent = ref(false);

let consumers = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
let inflight: Promise<void> | null = null;

const phase = computed<BuilderEnvPhase>(() => {
  const s = status.value;
  if (absent.value || s === null) return "unknown";
  if (s.job?.state === "running") return "installing";
  if (s.ready) return "ready";
  if (s.job?.state === "failed") return "failed";
  if (s.job?.state === "cancelled") return "cancelled";
  return s.installable ? "missing" : "unavailable";
});

const percent = computed(() => {
  const j = status.value?.job;
  if (!j) return 0;
  return Math.max(0, Math.min(100, Math.round(j.overall)));
});

async function refresh(): Promise<void> {
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      status.value = await fetchBuilderEnv();
      absent.value = false;
    } catch (e) {
      // 404 = this server has no such feature: hide the UI. Anything else (network blip,
      // restart) keeps the last known state.
      if (e instanceof ApiError && e.status === 404) absent.value = true;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

function visible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

function schedule(): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
  if (consumers <= 0 || absent.value) return;
  const running = status.value?.job?.state === "running";
  const delay = running ? POLL_RUNNING_MS : POLL_IDLE_MS;
  timer = setTimeout(async () => {
    timer = null;
    if (running || visible()) await refresh();
    schedule();
  }, delay);
}

function onVisible(): void {
  if (consumers > 0 && visible()) void refresh().then(schedule);
}

function acquire(): void {
  consumers += 1;
  if (consumers === 1 && typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisible);
  }
  void refresh().then(schedule);
}

function release(): void {
  consumers = Math.max(0, consumers - 1);
  if (consumers === 0) {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisible);
    }
  }
}

function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

async function install(body: BuilderEnvInstallRequest = {}): Promise<boolean> {
  if (busy.value) return false;
  busy.value = true;
  actionError.value = "";
  try {
    await startBuilderEnvInstall(body);
    await refresh();
    schedule();
    return true;
  } catch (e) {
    // 409 = already running (another window started it): just show it.
    if (e instanceof ApiError && e.status === 409) {
      await refresh();
      schedule();
      return true;
    }
    actionError.value = messageOf(e);
    return false;
  } finally {
    busy.value = false;
  }
}

async function cancel(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  actionError.value = "";
  try {
    await cancelBuilderEnvInstall();
    await refresh();
    schedule();
  } catch (e) {
    actionError.value = messageOf(e);
  } finally {
    busy.value = false;
  }
}

export function useBuilderEnv(): UseBuilderEnv {
  return { status, phase, percent, busy, actionError, refresh, install, cancel, acquire, release };
}

/** Test hook: reset the singleton between tests. */
export function __resetBuilderEnvForTests(): void {
  status.value = null;
  busy.value = false;
  actionError.value = "";
  absent.value = false;
  consumers = 0;
  if (timer !== null) clearTimeout(timer);
  timer = null;
  inflight = null;
  if (typeof document !== "undefined") {
    document.removeEventListener("visibilitychange", onVisible);
  }
}
