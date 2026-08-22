// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useServiceControl` — GenieAPIService (model_runtime) control state machine.
 *
 * V1-parity composable ported from the legacy
 * `frontend/js/composables/useServiceControl.js` (the validated source of
 * truth). It owns the Service page's reactive state and behaviours:
 *   - launch params (`svcParams`) synced to forge-config `service_launch`;
 *   - model list grouped by accelerator (NPU/GPU/CPU) from
 *     `GET /api/service/models` + selected-model persistence via
 *     `/api/preferences.selected_service_model`;
 *   - connection panel (local/remote, "Test" probe);
 *   - start / stop with status polling;
 *   - SSE log streaming with RAF-batched flush + smart auto-scroll;
 *   - command preview, path-safety warning, collapse states.
 *
 * Design (vs V1): V1 was one 521-line module of loose refs. Here the same
 * behaviour is a single cohesive composable returning a typed surface; no
 * global refs, no monolith. The view (`ServiceView.vue`) stays a thin
 * template that binds to this surface.
 */
import { computed, ref, watch, type Ref } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson } from "@/api";
import {
  clearServiceLogs as apiClearLogs,
  fetchServiceModels,
  fetchServiceStatus,
  probeService,
  startService,
  stopService,
  streamServiceLogs,
} from "@/api/serviceControl";
import { useToastStore } from "@/stores/toast";
import type {
  ServiceModelEntry,
  ServiceStatusResponse,
} from "@/types/service";

/** Launch parameters mirrored to forge-config `service_launch`. */
export interface SvcParams {
  models_root_path: string;
  load_model: boolean;
  host_mode: "local" | "remote";
  remote_host: string;
  local_port: number;
  remote_port: number;
  loglevel: number;
  all_text: boolean;
  enable_thinking: boolean;
  prompt_debug: boolean;
  adapter: string;
  lora_alpha: number | null;
  service_log_buffer_size?: number;
}

const DEFAULT_PORT = 8910;
const LOG_SCROLL_BOTTOM_THRESHOLD = 80;
const STATUS_POLL_MS = 5000;

function defaultSvcParams(): SvcParams {
  return {
    models_root_path: "",
    load_model: true,
    host_mode: "local",
    remote_host: "",
    local_port: DEFAULT_PORT,
    remote_port: DEFAULT_PORT,
    loglevel: 3,
    all_text: false,
    enable_thinking: false,
    prompt_debug: false,
    adapter: "",
    lora_alpha: null,
  };
}

export function useServiceControl(logPanel: Ref<HTMLElement | null>) {
  const { t } = useI18n();
  const toast = useToastStore();

  function notify(kind: "success" | "error" | "info", message: string): void {
    toast.push({ id: crypto.randomUUID(), kind, message, timeoutMs: kind === "error" ? 5000 : 3000 });
  }

  // ── Core reactive state ────────────────────────────────────────────────
  const serviceStatus = ref<ServiceStatusResponse>({
    running: false,
    pid: null,
    uptime_seconds: null,
    exe_path: "",
    command: "",
    path_warning: "",
  });
  const serviceStarting = ref(false);
  const serviceStopping = ref(false);

  const serviceLogs = ref<string[]>([]);
  const serviceLogsStreaming = ref(false);

  const serviceModels = ref<ServiceModelEntry[]>([]);
  // V1 first-screen parity: the model list starts in a LOADING state, not an
  // empty/"not found" state. In V1 the Service DOM is always mounted and
  // `loadServiceModels()` (which flips this flag true synchronously at its
  // first line) is kicked off at boot, so by the first perceivable render the
  // flag is already true. V2 mounts ServiceView on-route and `init()` awaits
  // params+status BEFORE models, leaving a window where the flag is false and
  // every guidance card guards on `!serviceModelsLoading` — that window is the
  // first-screen false "GenieAPIService not found / no models" flash that a
  // single "Refresh" click clears. Seeding it `true` makes the first render a
  // loading placeholder until the model list actually arrives (V1 behaviour).
  const serviceModelsLoading = ref(true);
  const selectedServiceModelName = ref("");

  const svcParams = ref<SvcParams>(defaultSvcParams());
  const svcParamsSaving = ref(false);

  const paramsCollapsed = ref(false);
  const connectionCollapsed = ref(true);
  const logsExpanded = ref(false);

  const connectionTesting = ref(false);
  const connectionTestResult = ref<null | "ok" | "fail">(null);

  // Streaming / scrolling internals
  let logAbort: AbortController | null = null;
  let statusTimer: ReturnType<typeof setInterval> | null = null;
  let logSkipFrom = 0;
  let paramsAutoCollapsed = false;
  // Set once `dispose()` runs (component unmounted). Guards the deferred
  // `streamLogs()` that `init()` fires AFTER its `await Promise.all(...)`: if
  // the user navigated away during that window, `dispose()` already ran while
  // `logAbort` was still null (no-op), so without this flag the post-await
  // `streamLogs()` would open an indefinite `/api/service/logs` tail with no
  // live component and no future `dispose()` to abort it — permanently leaking
  // one of the browser's 6 HTTP/1.1 sockets per Service-view visit (root cause
  // of the "页面加载不出来 / 连接池耗尽" symptom).
  let disposed = false;
  // Monotonic counter incremented at the top of every `init()` call. Each
  // call captures its own epoch and re-checks it after `await Promise.all`
  // so the FIRST init's deferred `streamLogs()`/`startStatusPolling()` are
  // suppressed if the user has since deactivated AND re-activated (which
  // would have set `disposed = false` on the second `init()` and otherwise
  // defeat the simple `if (disposed) return` guard — KA-SVC-EPOCH-1).
  let initEpoch = 0;
  const logUserScrolledUp = ref(false);

  // ── Derived state ──────────────────────────────────────────────────────
  const isRunning = computed(() => {
    const s = serviceStatus.value;
    if (typeof s.state === "string") return s.state === "running";
    return s.running === true;
  });

  const isRemoteMode = computed(() => svcParams.value.host_mode === "remote");

  const serviceModelsByAccel = computed(() => ({
    npu: serviceModels.value.filter((m) => m.format === "qnn"),
    gpu: serviceModels.value.filter((m) => m.format === "gguf"),
    cpu: serviceModels.value.filter((m) => m.format === "mnn"),
  }));

  const localUrl = computed(() => {
    const h = isRemoteMode.value
      ? (svcParams.value.remote_host || "").trim() || "?"
      : "127.0.0.1";
    const p = isRemoteMode.value
      ? svcParams.value.remote_port || DEFAULT_PORT
      : svcParams.value.local_port || DEFAULT_PORT;
    return `http://${h}:${p}`;
  });

  const serviceCommandPreview = computed(() => {
    const p = svcParams.value;
    const parts: string[] = [];
    const model = selectedServiceModelName.value;
    if (model && p.models_root_path) {
      const sep = p.models_root_path.includes("/") ? "/" : "\\";
      const root = p.models_root_path.replace(/[/\\]+$/, "");
      parts.push("-c", `${root}${sep}${model}${sep}config.json`);
    } else if (model) {
      parts.push("-c", `<models_root>\\${model}\\config.json`);
    }
    parts.push("-l");
    parts.push("-n", "-1");
    parts.push("-p", String(p.local_port));
    parts.push("-d", String(p.loglevel));
    return parts.join(" ");
  });

  const canStartService = computed(
    () =>
      !isRemoteMode.value &&
      serviceModels.value.length > 0 &&
      !!selectedServiceModelName.value,
  );

  function formatUptime(secs: number | null | undefined): string {
    if (secs == null) return "";
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = Math.floor(secs % 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  // ── Forge-config (service_launch) load/save ────────────────────────────
  interface ForgeConfigResponse {
    config?: Record<string, unknown>;
  }

  async function loadServiceParams(): Promise<void> {
    try {
      const res = await apiJson<ForgeConfigResponse>("GET", "/api/forge-config");
      const sl = (res.config?.service_launch as Record<string, unknown>) ?? {};
      const patch: Partial<SvcParams> = {};
      for (const key of Object.keys(svcParams.value) as (keyof SvcParams)[]) {
        if (key in sl) {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (patch as any)[key] = sl[key as string];
        }
      }
      svcParams.value = { ...svcParams.value, ...patch };
    } catch {
      // graceful — keep defaults
    }
  }

  async function saveServiceParams(): Promise<void> {
    svcParamsSaving.value = true;
    try {
      await apiJson("POST", "/api/forge-config", {
        config: { service_launch: { ...svcParams.value } },
      });
      notify("success", t("service.paramsSaved"));
      // host_mode may have toggled — refresh the model list.
      await loadServiceModels();
    } catch (e) {
      notify("error", `${t("service.saveFailed")}: ${(e as Error).message}`);
    } finally {
      svcParamsSaving.value = false;
    }
  }

  // ── Status ─────────────────────────────────────────────────────────────
  async function loadServiceStatus(): Promise<void> {
    try {
      serviceStatus.value = await fetchServiceStatus();
      if (isRunning.value && !paramsAutoCollapsed) {
        paramsCollapsed.value = true;
        paramsAutoCollapsed = true;
      }
    } catch {
      // graceful
    }
  }

  // ── Models + selection preference ──────────────────────────────────────
  interface PreferencesResponse {
    selected_service_model?: string;
  }

  async function loadServiceModels(): Promise<void> {
    serviceModelsLoading.value = true;
    try {
      const [modelsData, prefs] = await Promise.all([
        fetchServiceModels(),
        apiJson<PreferencesResponse>("GET", "/api/preferences").catch(
          () => ({}) as PreferencesResponse,
        ),
      ]);
      serviceModels.value = modelsData.models ?? [];
      if (modelsData.models_root_path) {
        svcParams.value.models_root_path = modelsData.models_root_path;
      }

      if (serviceModels.value.length > 0) {
        const saved = prefs.selected_service_model || "";
        const savedFound =
          saved && serviceModels.value.some((m) => m.name === saved);
        if (savedFound) {
          selectedServiceModelName.value = saved;
        } else if (
          !serviceModels.value.some(
            (m) => m.name === selectedServiceModelName.value,
          )
        ) {
          selectedServiceModelName.value = serviceModels.value[0]?.name ?? "";
        }
      } else {
        selectedServiceModelName.value = "";
      }
    } catch {
      // graceful
    } finally {
      serviceModelsLoading.value = false;
    }
  }

  function saveServiceModelPreference(modelName: string): void {
    void apiJson("POST", "/api/preferences", {
      selected_service_model: modelName,
    }).catch(() => {
      /* best-effort */
    });
  }

  // ── Connection test ────────────────────────────────────────────────────
  async function testConnection(): Promise<void> {
    connectionTesting.value = true;
    connectionTestResult.value = null;
    try {
      const host = isRemoteMode.value
        ? (svcParams.value.remote_host || "").trim()
        : "127.0.0.1";
      const port = isRemoteMode.value
        ? svcParams.value.remote_port || DEFAULT_PORT
        : svcParams.value.local_port || DEFAULT_PORT;
      const data = await probeService({ host, port });
      connectionTestResult.value = data.reachable ? "ok" : "fail";
    } catch {
      connectionTestResult.value = "fail";
    } finally {
      connectionTesting.value = false;
    }
  }

  // ── Start / Stop ───────────────────────────────────────────────────────
  async function startServiceAction(): Promise<void> {
    if (!selectedServiceModelName.value) {
      notify("error", t("service.selectModelFirst"));
      return;
    }
    if (serviceModels.value.length === 0) {
      notify("error", t("service.noModelsAvailable"));
      return;
    }
    serviceStarting.value = true;
    try {
      const result = await startService({
        model_name: selectedServiceModelName.value,
        port: svcParams.value.local_port,
        loglevel: svcParams.value.loglevel,
        host_mode: svcParams.value.host_mode,
        load_model: svcParams.value.load_model,
      });
      serviceStatus.value = {
        ...serviceStatus.value,
        ...result,
        running: true,
      };
      notify(
        "success",
        result.pid != null
          ? `${t("service.startSuccess")} (PID: ${result.pid})`
          : t("service.startSuccess"),
      );
      paramsCollapsed.value = true;
      paramsAutoCollapsed = true;
      void streamLogs();
      startStatusPolling();
      // V1 parity (useServiceControl.js:311): refresh service model list after start
      void loadServiceModels();
    } catch (e) {
      // Single-instance guard: the backend returns 409
      // code="model_runtime.service_port_in_use" when the target port is
      // already occupied (a daemon is already running, or a leftover process
      // holds the port). Show a friendly, actionable message instead of the
      // generic "start failed". `ApiError` carries `.code` + `.details.port`.
      const err = e as { code?: string; details?: { port?: number }; message?: string };
      if (err?.code === "model_runtime.service_port_in_use") {
        const port = err.details?.port ?? svcParams.value.local_port;
        notify("error", t("service.portInUse", { port }));
      } else {
        notify("error", `${t("service.startFailed")}: ${(e as Error).message}`);
      }
    } finally {
      serviceStarting.value = false;
    }
  }

  async function stopServiceAction(): Promise<void> {
    serviceStopping.value = true;
    try {
      await stopService();
      serviceStatus.value = {
        ...serviceStatus.value,
        running: false,
        state: "stopped",
        pid: null,
        uptime_seconds: null,
      };
      notify("info", t("service.stopSuccess"));
      paramsAutoCollapsed = false;
      stopStatusPolling();
      // V1 parity (useServiceControl.js:332): refresh service model list after stop
      void loadServiceModels();
    } catch (e) {
      notify("error", `${t("service.stopFailed")}: ${(e as Error).message}`);
    } finally {
      serviceStopping.value = false;
    }
  }

  // ── Status polling ─────────────────────────────────────────────────────
  function startStatusPolling(): void {
    stopStatusPolling();
    statusTimer = setInterval(() => {
      void (async () => {
        await loadServiceStatus();
        if (!isRunning.value) stopStatusPolling();
      })();
    }, STATUS_POLL_MS);
  }

  function stopStatusPolling(): void {
    if (statusTimer) {
      clearInterval(statusTimer);
      statusTimer = null;
    }
  }

  // ── Log streaming (SSE) with RAF-batched flush ─────────────────────────
  async function streamLogs(): Promise<void> {
    // Disposed-guard: never open a new stream on an unmounted instance (e.g.
    // the deferred call from `init()` after `await Promise.all`, or a stray
    // re-entry). Opening here would leak an un-abortable socket.
    if (disposed) return;
    if (logAbort) logAbort.abort();
    logAbort = new AbortController();
    serviceLogsStreaming.value = true;

    let pending: string[] = [];
    let rafId: number | null = null;
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    const flush = (): void => {
      rafId = null;
      if (pending.length === 0) return;
      for (const line of pending) serviceLogs.value.push(line);
      pending = [];
      const limit = svcParams.value.service_log_buffer_size || 6000;
      if (serviceLogs.value.length > limit) {
        serviceLogs.value.splice(0, serviceLogs.value.length - limit);
      }
      scrollLogToBottom();
    };

    try {
      const response = await streamServiceLogs(logSkipFrom, {
        signal: logAbort.signal,
      });
      if (!response.ok || !response.body) {
        serviceLogsStreaming.value = false;
        return;
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const dataStr = line.slice(6);
          if (dataStr === "[DONE]") {
            serviceLogsStreaming.value = false;
            return;
          }
          try {
            const obj = JSON.parse(dataStr) as { line?: string };
            if (obj.line !== undefined) {
              pending.push(obj.line);
              if (rafId === null) rafId = requestAnimationFrame(flush);
            }
          } catch {
            /* ignore malformed frame */
          }
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        // swallow — stream ended / network blip
      }
    } finally {
      serviceLogsStreaming.value = false;
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      // Release the reader on EVERY exit path (EOF / [DONE] / error / abort),
      // matching the guaranteed `reader.cancel()` in `apiSSE`/`apiStream`. The
      // abort path alone is not enough — a normal EOF/[DONE]/error return must
      // also release the lock so the underlying socket is freed promptly.
      if (reader !== null) {
        try {
          await reader.cancel();
        } catch {
          // ignore — already released / aborted.
        }
      }
      flush();
    }
  }

  async function clearLogs(): Promise<void> {
    try {
      const result = await apiClearLogs();
      logSkipFrom = result.skip_from ?? 0;
    } catch {
      logSkipFrom = 0;
    }
    serviceLogs.value = [];
    logUserScrolledUp.value = false;
    void streamLogs();
  }

  async function copyLogs(): Promise<void> {
    if (serviceLogs.value.length === 0) return;
    try {
      await navigator.clipboard.writeText(serviceLogs.value.join("\n"));
      notify("success", t("service.logsCopied", { n: serviceLogs.value.length }));
    } catch (e) {
      notify("error", `${t("service.copyFailed")}: ${(e as Error).message}`);
    }
  }

  // ── Smart scroll (mirrors Chat view behaviour) ─────────────────────────
  function logIsAtBottom(): boolean {
    const el = logPanel.value;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= LOG_SCROLL_BOTTOM_THRESHOLD;
  }

  function onLogScroll(): void {
    logUserScrolledUp.value = !logIsAtBottom();
  }

  function scrollLogToBottom(force = false, smooth = false): void {
    const el = logPanel.value;
    if (!el) return;
    if (logUserScrolledUp.value && !force) return;
    if (smooth) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
      return;
    }
    el.scrollTop = el.scrollHeight;
  }

  function scrollLogToTop(): void {
    const el = logPanel.value;
    if (!el) return;
    logUserScrolledUp.value = true;
    el.scrollTo({ top: 0, behavior: "smooth" });
  }

  // Keep model-format derived selection valid when models change.
  watch(serviceModels, (list) => {
    if (
      selectedServiceModelName.value &&
      !list.some((m) => m.name === selectedServiceModelName.value)
    ) {
      selectedServiceModelName.value = list[0]?.name ?? "";
    }
  });

  // ── Lifecycle helpers ──────────────────────────────────────────────────
  async function init(): Promise<void> {
    // Re-entrant: under <KeepAlive> the ServiceView component is not unmounted
    // on navigate-away — instead `dispose()` runs in onDeactivated and `init()`
    // re-runs in onActivated. Clear the `disposed` latch up front so a return
    // visit re-opens the log tail / status polling that the previous
    // `dispose()` aborted (otherwise the `if (disposed) return;` guard below
    // would permanently suppress the log stream after the first hide).
    disposed = false;
    // Capture this call's epoch BEFORE the await so we can detect a rapid
    // dispose → init pair while the concurrent fetches are in flight: the
    // OLD init's epoch (= myEpoch) will no longer match `initEpoch` after
    // the NEW init bumps it, so its post-await `streamLogs()` is skipped
    // (the NEW init will fire its own `streamLogs()` instead). Without
    // this, the OLD init's `if (disposed) return;` is defeated because the
    // NEW init reset `disposed = false`, and we would open TWO log
    // streams + TWO status pollers — the new ones immediately abort/stop
    // the duplicates internally (both APIs are idempotent), but the
    // resulting "stream → abort → reconnect" flicker is observable.
    initEpoch += 1;
    const myEpoch = initEpoch;
    // Keep the model list in its loading state across the whole first fetch so
    // the view never paints the "not found / no models" guidance before the
    // real data lands (V1 first-screen parity — see `serviceModelsLoading`
    // seeding above). Defensive re-set in case `init` is invoked again.
    serviceModelsLoading.value = true;
    // V1 boots params/status/models concurrently (app.js:2074-2085 Promise.all
    // + the immediate `currentView==='service'` watcher firing loadServiceModels)
    // rather than serially. Fire them together so no single round-trip stalls
    // the model list behind status/params.
    await Promise.all([
      loadServiceParams(),
      loadServiceStatus(),
      loadServiceModels(),
    ]);
    // If the component unmounted while the concurrent fetches were in flight,
    // `dispose()` already ran (and `logAbort` was still null then). Do NOT open
    // the indefinite log tail on a dead instance — that would leak a socket.
    // The epoch check also handles "dispose() then init() again while
    // awaiting" — in that case `disposed` was reset to false by the new init,
    // but `initEpoch` has moved past `myEpoch`, so we still bail.
    if (disposed || myEpoch !== initEpoch) return;
    void streamLogs();
    if (isRunning.value) startStatusPolling();
  }

  function dispose(): void {
    disposed = true;
    stopStatusPolling();
    if (logAbort) logAbort.abort();
  }

  return {
    // state
    serviceStatus,
    serviceStarting,
    serviceStopping,
    serviceLogs,
    serviceLogsStreaming,
    serviceModels,
    serviceModelsLoading,
    selectedServiceModelName,
    svcParams,
    svcParamsSaving,
    paramsCollapsed,
    connectionCollapsed,
    logsExpanded,
    connectionTesting,
    connectionTestResult,
    logUserScrolledUp,
    // derived
    isRunning,
    isRemoteMode,
    serviceModelsByAccel,
    localUrl,
    serviceCommandPreview,
    canStartService,
    // helpers
    formatUptime,
    // actions
    init,
    dispose,
    loadServiceStatus,
    loadServiceModels,
    saveServiceParams,
    saveServiceModelPreference,
    testConnection,
    startServiceAction,
    stopServiceAction,
    clearLogs,
    copyLogs,
    onLogScroll,
    scrollLogToBottom,
    scrollLogToTop,
  };
}

export type ServiceControl = ReturnType<typeof useServiceControl>;
