// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Shared GenieAPIService lifecycle store.
 *
 * Single source of truth for "what state is the local inference daemon in",
 * read by both the Service page and the chat model picker so that a start
 * triggered from one surface (e.g. picking a local model in chat) is visible
 * on the other. The backend `GET /api/service/status` is authoritative; the
 * only client-side overlay is `pending`, which covers the window of an
 * in-flight start/stop HTTP request.
 */
import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { fetchServiceStatus } from "@/api/serviceControl";
import type { ServiceStatusResponse } from "@/types/service";

export type ServiceLifecycle =
  | "stopped"
  | "starting"
  | "loading"
  | "running"
  | "stopping"
  | "error";
export type ServicePending = "starting" | "stopping" | null;

const STEADY_POLL_MS = 5000;
const TRANSITION_POLL_MS = 1000;
const STOP_VERIFY_POLL_MS = 500;
const SETTLE_TIMEOUT_MS = 5 * 60 * 1000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function deriveLifecycle(
  s: ServiceStatusResponse,
  pending: ServicePending,
): ServiceLifecycle {
  if (pending === "stopping") return "stopping";
  if (pending === "starting") return "starting";
  switch (s.state) {
    case "stopping":
      return "stopping";
    case "starting":
      return "starting";
    case "running":
      return s.ready === false ? "loading" : "running";
    case "error":
      return "error";
    case "stopped":
      return "stopped";
    default:
      if (s.running === true) return s.ready === false ? "loading" : "running";
      return "stopped";
  }
}

export const useServiceRuntimeStore = defineStore("serviceRuntime", () => {
  const status = ref<ServiceStatusResponse>({
    running: false,
    pid: null,
    uptime_seconds: null,
    exe_path: "",
    command: "",
    path_warning: "",
  });
  const pending = ref<ServicePending>(null);
  /** Model the in-flight start is loading (the backend reports no model yet). */
  const pendingModel = ref<string | null>(null);

  const lifecycle = computed<ServiceLifecycle>(() =>
    deriveLifecycle(status.value, pending.value),
  );
  /** Model being loaded (during a start) or already loaded, "" when none. */
  const activeModel = computed<string>(() =>
    pending.value === "starting" && pendingModel.value
      ? pendingModel.value
      : (status.value.model ?? ""),
  );
  /** A daemon process exists (running or still loading its model). */
  const isRunning = computed(
    () => lifecycle.value === "running" || lifecycle.value === "loading",
  );
  const isTransitional = computed(
    () =>
      lifecycle.value === "starting" ||
      lifecycle.value === "loading" ||
      lifecycle.value === "stopping",
  );

  let inflight: Promise<void> | null = null;
  async function refresh(): Promise<void> {
    inflight ??= (async () => {
      try {
        status.value = await fetchServiceStatus();
      } catch {
        // keep the last known status
      }
    })().finally(() => {
      inflight = null;
    });
    return inflight;
  }

  let watchers = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;

  function schedule(delay: number): void {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (watchers <= 0) return;
    timer = setTimeout(() => {
      timer = null;
      void refresh().finally(() =>
        schedule(isTransitional.value ? TRANSITION_POLL_MS : STEADY_POLL_MS),
      );
    }, delay);
  }

  /** Start (ref-counted) background status polling. */
  function acquire(): void {
    watchers += 1;
    if (watchers === 1) schedule(0);
  }

  function release(): void {
    watchers = Math.max(0, watchers - 1);
    if (watchers === 0 && timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function begin(kind: Exclude<ServicePending, null>, model?: string): void {
    pending.value = kind;
    pendingModel.value = model ?? null;
    if (watchers > 0) schedule(0);
  }

  /**
   * Clear the in-flight marker only after a fresh backend status arrived, so
   * the lifecycle never falls back to a stale value (e.g. "stopped") between
   * the request finishing and the next poll.
   */
  async function end(): Promise<void> {
    try {
      if (inflight) await inflight;
      await refresh();
    } finally {
      pending.value = null;
      pendingModel.value = null;
      if (watchers > 0) schedule(0);
    }
  }

  function backendStopped(s: ServiceStatusResponse): boolean {
    if (typeof s.state === "string") {
      return s.state === "stopped" || s.state === "error";
    }
    return s.running !== true;
  }

  /** Poll the backend until the process is really gone; false on timeout. */
  async function waitForStopped(timeoutMs = 15000): Promise<boolean> {
    const t0 = Date.now();
    for (;;) {
      await refresh();
      if (backendStopped(status.value)) return true;
      if (Date.now() - t0 >= timeoutMs) return false;
      await sleep(STOP_VERIFY_POLL_MS);
    }
  }

  /**
   * Poll until the lifecycle reaches a stable state (running / stopped /
   * error) — used after a start triggered outside the Service page so the
   * model-loading phase is tracked against the real daemon, not a timer.
   */
  async function settle(timeoutMs = SETTLE_TIMEOUT_MS): Promise<void> {
    acquire();
    try {
      const t0 = Date.now();
      await refresh();
      while (isTransitional.value && Date.now() - t0 < timeoutMs) {
        await sleep(TRANSITION_POLL_MS);
        await refresh();
      }
    } finally {
      release();
    }
  }

  return {
    status,
    pending,
    pendingModel,
    activeModel,
    lifecycle,
    isRunning,
    isTransitional,
    refresh,
    acquire,
    release,
    begin,
    end,
    waitForStopped,
    settle,
  };
});
