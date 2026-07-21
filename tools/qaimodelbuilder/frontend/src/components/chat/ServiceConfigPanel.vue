<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ServiceConfigPanel — inference service launch configuration form.
 *
 * V1-style grouped-card layout. Reads and writes the ``service_launch``
 * section of the forge-config document via:
 *   GET  /api/forge-config       → { config: { service_launch: {...}, ... } }
 *   POST /api/forge-config       (body { config: { service_launch: {...} } })
 *
 * Additionally reads/writes service_config.json via:
 *   GET  /api/config             → { config: {...}, meta: {...} }
 *   POST /api/config             (body { config: {...} })
 *
 * The per-Tab forms live in dedicated sub-components under ``./service-config``
 * (one component per Tab). This panel owns the shared state (forge-config
 * ``form`` + ``svcCfg`` service_config + available models) and the load/save
 * logic, and delegates the field rendering to the Tab components, keeping each
 * file focused and within the cohesion budget.
 *
 * Tabs:
 *   Local Model   — local_model / models[] (service_config)
 *   Cloud         — cloud_model / enterprise_cloud_model / cloud_shared (service_config)
 *   Routing       — routing.* (service_config)
 *   Security      — sensitivity_detection / desensitization (service_config)
 *   Prompt Opt.   — prompt_optimization (service_config)
 *   Debug         — debug (service_config) + 服务调试 (forge-config service_launch:
 *                   show_prompt_in_ui / service_log_buffer_size) + cloud_shared.log_debug.
 *                   2026-06-16 整合：原「通用」Tab 已删除，其服务调试设置并入 Debug；
 *                   原「云端连接」底部 Debug Log 亦并入 Debug。Debug 保存时同时写
 *                   forge-config 与 service_config 两个后端。
 */
import { ref, reactive, computed, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import { apiJson } from "@/api";
import { useToastStore } from "@/stores/toast";
import type { ForgeConfigResponse, LocalModelsByFormat, ServiceConfig, ServiceConfigResponse } from "./service-config/types";
import ServiceConfigLocalModelTab from "./service-config/ServiceConfigLocalModelTab.vue";
import ServiceConfigCloudTab from "./service-config/ServiceConfigCloudTab.vue";
import ServiceConfigRoutingTab from "./service-config/ServiceConfigRoutingTab.vue";
import ServiceConfigSecurityTab from "./service-config/ServiceConfigSecurityTab.vue";
import ServiceConfigPromptOptTab from "./service-config/ServiceConfigPromptOptTab.vue";
import ServiceConfigDebugTab from "./service-config/ServiceConfigDebugTab.vue";

const { t } = useI18n();
const toast = useToastStore();
const router = useRouter();
const loading = ref(false);
const saving = ref(false);

// Unified save-status pill (V1 modal header `configSaveStatus`): success/error
// message that auto-clears after 3s. Drives the header bar status indicator so
// the user gets feedback at a single, consistent location regardless of which
// Tab they saved from.
type SaveStatus = { type: "success" | "error"; icon: string; message: string } | null;
const saveStatus = ref<SaveStatus>(null);
let saveStatusTimer: ReturnType<typeof setTimeout> | null = null;

function flashSaveStatus(status: SaveStatus): void {
  if (saveStatusTimer !== null) {
    clearTimeout(saveStatusTimer);
    saveStatusTimer = null;
  }
  saveStatus.value = status;
  if (status?.type === "success") {
    saveStatusTimer = setTimeout(() => {
      saveStatus.value = null;
      saveStatusTimer = null;
    }, 3000);
  }
}

// ── Tab state ──────────────────────────────────────────────────────────────
// V1 default tab is "local" (useConfig.js:17 `activeConfigTab = ref('local')`).
// 2026-06-16: 「通用」Tab 已删除（其服务调试设置并入 Debug Tab）。
const activeConfigTab = ref<"local" | "cloud" | "routing" | "security" | "prompt" | "debug">("local");

const configTabs = computed(() => [
  { id: "local" as const, label: "🖥️ " + t("serviceConfig.tabLocalModel") },
  { id: "cloud" as const, label: "☁️ " + t("serviceConfig.tabCloud") },
  { id: "routing" as const, label: "🔀 " + t("serviceConfig.tabRouting") },
  { id: "security" as const, label: "🔒 " + t("serviceConfig.tabSecurity") },
  { id: "prompt" as const, label: "✨ " + t("serviceConfig.tabPrompt") },
  { id: "debug" as const, label: "🐛 " + t("serviceConfig.tabDebug") },
]);

// ── 服务调试设置 (forge-config / service_launch) ───────────────────────────
// 原「通用」Tab 的字段，现并入 Debug Tab 渲染；仍持久化到 forge-config。
const rawServiceLaunch = ref<Record<string, unknown>>({});

// V2 设计简化（用户 2026-06-07 明令）：GenieAPIService 安装路径与模型根路径固定为
// data\ 下默认目录、不再允许用户设置，故服务调试设置仅保留与路径无关的调试字段
// （show_prompt_in_ui / prompt_debug / service_log_buffer_size）。其它 service_launch
// 字段通过 `rawServiceLaunch` 透明 round-trip，避免保存时擦掉服务端已有数据。
const form = reactive({
  show_prompt_in_ui: false,
  prompt_debug: false,
  service_log_buffer_size: 6000,
});

function applyFromConfig(cfg: Record<string, unknown>): void {
  const sl = (cfg.service_launch as Record<string, unknown>) ?? {};
  rawServiceLaunch.value = { ...sl };
  if (sl.show_prompt_in_ui != null) form.show_prompt_in_ui = Boolean(sl.show_prompt_in_ui);
  if (sl.prompt_debug != null) form.prompt_debug = Boolean(sl.prompt_debug);
  if (sl.service_log_buffer_size != null) {
    form.service_log_buffer_size = Number(sl.service_log_buffer_size);
  }
}

// POST the forge-config service_launch debug fields. Throws on failure so
// callers can combine it with other saves; does NOT flash status itself.
async function postForgeConfig(): Promise<void> {
  // 保留 rawServiceLaunch 中其它字段原样 round-trip；只覆盖 2 个调试字段。
  // 安装路径 / 模型根路径已固定为默认、不再由本 Tab 写入。
  const service_launch = {
    ...rawServiceLaunch.value,
    show_prompt_in_ui: form.show_prompt_in_ui,
    prompt_debug: form.prompt_debug,
    service_log_buffer_size: form.service_log_buffer_size,
  };
  const res = await apiJson<ForgeConfigResponse>("POST", "/api/forge-config", {
    config: { service_launch },
  });
  applyFromConfig(res.config ?? {});
}

// ── Service Config tabs (service_config.json via /api/config) ──────────────
const svcCfg = ref<ServiceConfig>({});
const svcCfgMeta = ref<{ using_default_config: boolean; config_file_path: string } | null>(null);
const svcCfgSaving = ref(false);

// Available model names from /api/service/models. The backend already tags
// each entry with its on-disk `format` field (qnn / gguf / mnn — see
// interfaces/http/routes/model_runtime.py:283-301), so we issue a single
// request and bucket by format here, rather than racing three concurrent
// `?format=...` calls (the route does not accept a format query and would
// return the full set three times). The plain `availableModels` (string
// list, all formats) is preserved as a backwards-compatible feed for
// existing tab consumers; `availableModelsByFormat` is the new
// per-format slice for the LocalModelTab realignment to consume once
// that tab is updated by the next agent.
const availableModels = ref<string[]>([]);
const availableModelsByFormat = ref<LocalModelsByFormat>({
  qnn: [],
  gguf: [],
  mnn: [],
});

async function loadServiceConfig(): Promise<void> {
  try {
    const res = await fetch("/api/config");
    if (!res.ok) return;
    const data = (await res.json()) as ServiceConfigResponse;
    svcCfg.value = data.config ?? {};
    svcCfgMeta.value = data.meta ?? null;
  } catch {
    // ignore — backend may not be running
  }
}

async function loadAvailableModels(): Promise<void> {
  try {
    const res = await fetch("/api/service/models");
    if (!res.ok) return;
    const data = (await res.json()) as {
      models?: Array<{ name: string; format?: string }> | string[];
    };
    if (!Array.isArray(data.models)) return;
    const buckets: { qnn: string[]; gguf: string[]; mnn: string[] } = { qnn: [], gguf: [], mnn: [] };
    const flat: string[] = [];
    for (const m of data.models) {
      if (typeof m === "string") {
        flat.push(m);
        continue;
      }
      const name = m.name;
      if (!name) continue;
      flat.push(name);
      const fmt = (m.format ?? "").toLowerCase();
      if (fmt === "qnn") buckets.qnn.push(name);
      else if (fmt === "gguf") buckets.gguf.push(name);
      else if (fmt === "mnn") buckets.mnn.push(name);
    }
    availableModels.value = flat;
    availableModelsByFormat.value = buckets;
  } catch {
    // ignore
  }
}

// POST service_config.json. Throws on failure so callers can combine it with
// other saves; does NOT flash status itself.
async function postServiceConfig(): Promise<void> {
  // V1 parity (useConfig.js:159-165): before POST, force the 3 fixed model
  // slots' backend / device / path so the backend resolves each slot to the
  // correct accelerator (NPU=QNN, GPU=GGUF, CPU=MNN) and on-disk dir
  // (path === name). Without this the Local Model tab leaves backend/device
  // as the empty-string defaults (helpers.ts:33) and the service can't load
  // the selected model.
  const payload = JSON.parse(JSON.stringify(svcCfg.value)) as ServiceConfig;
  const SLOT_DEFAULTS: ReadonlyArray<{ backend: string; device: string }> = [
    { backend: "qnn", device: "npu" },
    { backend: "GGUF", device: "gpu" },
    { backend: "mnn", device: "cpu" },
  ];
  if (Array.isArray(payload.models)) {
    payload.models.forEach((slot, i) => {
      const d = SLOT_DEFAULTS[i];
      if (d !== undefined) {
        slot.backend = d.backend;
        slot.device = d.device;
      }
      slot.path = slot.name;
    });
  }
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: payload }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

async function saveServiceConfig(): Promise<void> {
  svcCfgSaving.value = true;
  try {
    await postServiceConfig();
    flashSaveStatus({ type: "success", icon: "✅", message: t("serviceConfig.savedSuccessfully") });
  } catch (e) {
    flashSaveStatus({ type: "error", icon: "❌", message: (e as Error).message });
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("serviceConfig.saveFailed", { msg: (e as Error).message }),
      timeoutMs: 5000,
    });
  } finally {
    svcCfgSaving.value = false;
  }
}

// Debug Tab persists to BOTH backends: forge-config (service_launch debug
// fields, originally the「通用」Tab) + service_config (debug section +
// cloud_shared.log_debug, originally the「云端连接」Tab). Save both so the
// single "保存更改" action on the Debug tab writes everything it shows.
async function saveDebugTab(): Promise<void> {
  svcCfgSaving.value = true;
  saving.value = true;
  try {
    await Promise.all([postForgeConfig(), postServiceConfig()]);
    flashSaveStatus({ type: "success", icon: "✅", message: t("serviceConfig.savedSuccessfully") });
  } catch (e) {
    flashSaveStatus({ type: "error", icon: "❌", message: (e as Error).message });
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("serviceConfig.saveFailed", { msg: (e as Error).message }),
      timeoutMs: 5000,
    });
  } finally {
    svcCfgSaving.value = false;
    saving.value = false;
  }
}

// ── Unified header-bar actions (V1 modal-header Save/Reset) ────────────────
// Two persistence targets exist: forge-config (service_launch) and
// service_config.json. The 「调试」Tab edits BOTH (服务调试 fields live in
// forge-config; debug/log_debug live in service_config), so its Save/Reset
// dispatch to both backends. All other tabs edit only service_config.json.
const isDebugTab = computed(() => activeConfigTab.value === "debug");
const headerSaving = computed(() => (isDebugTab.value ? saving.value || svcCfgSaving.value : svcCfgSaving.value));

async function loadForgeConfig(): Promise<void> {
  loading.value = true;
  try {
    const res = await apiJson<ForgeConfigResponse>("GET", "/api/forge-config");
    applyFromConfig(res.config ?? {});
  } catch (e) {
    toast.push({ id: crypto.randomUUID(), kind: "error", message: t("serviceConfig.loadFailed", { msg: (e as Error).message }), timeoutMs: 5000 });
  } finally {
    loading.value = false;
  }
}

function onHeaderSave(): void {
  if (isDebugTab.value) void saveDebugTab();
  else void saveServiceConfig();
}

function onHeaderReset(): void {
  flashSaveStatus(null);
  if (isDebugTab.value) {
    void loadForgeConfig();
    void loadServiceConfig();
  } else {
    void loadServiceConfig();
    void loadAvailableModels();
  }
}

// Expose the save/reset actions and live status to the parent (ServiceView)
// so the modal header can render the Save/Reset buttons + status pill on a
// single row alongside the ✕ close button (V1 single-layer modal header,
// index.html:3055-3083). The active-tab dispatch (forge-config vs
// service_config.json) stays owned here so the two backends are preserved.
defineExpose({
  save: onHeaderSave,
  reset: onHeaderReset,
  saving: headerSaving,
  saveStatus,
  loading,
});

// Load service config when component mounts
onMounted(() => {
  void loadForgeConfig();
  void loadServiceConfig();
  void loadAvailableModels();
});

// V1-parity (`ServiceConfigPanel.js:34-45`) default-config banner click
// handler: the warn banner exposes a rich-text link to the Downloads centre
// so the user can install GenieAPIService. (The former second link「前往 通用
// Tab」was removed 2026-06-16 along with the General tab — the install path is
// fixed to data\ and no longer user-settable, so that jump had no target.)
// Implemented as a plain method (no native confirm/alert/prompt — see
// AGENTS.md §3.9) routed through vue-router for the cross-view jump.
function gotoDownloads(): void {
  void router.push({ name: "downloads" });
}
</script>

<template>
  <div class="service-config-panel">
    <!-- Default-config banner (V1 ServiceConfigPanel.js:34-45): rendered once,
         above the tab navigation, driven by service_config meta. Uses the
         yellow "warn" style with a rich-text link to the Downloads centre so
         the user can resolve the missing GenieAPIService install. -->
    <div
      v-if="svcCfgMeta?.using_default_config"
      class="svc-cfg-banner svc-cfg-banner--warn"
    >
      <div class="svc-cfg-banner-title">
        ⚠️ {{ t("serviceConfig.defaultConfigBanner") }}
      </div>
      <div class="svc-cfg-banner-body">
        {{ t("serviceConfig.defaultConfigHint") }}
        <code>service_config.json</code> {{ t("serviceConfig.defaultConfigNeedInstall") }}<br />
        {{ t("serviceConfig.defaultConfigGoTo") }}
        <a
          href="#"
          class="svc-cfg-link"
          @click.prevent="gotoDownloads"
        >📥 {{ t("serviceConfig.goToDownloads") }}</a>
        {{ t("serviceConfig.defaultConfigDownloadHint") }}
      </div>
    </div>

    <!-- Tab bar -->
    <div class="svc-cfg-tabs">
      <button
        v-for="tab in configTabs"
        :key="tab.id"
        :class="['svc-cfg-tab', { active: activeConfigTab === tab.id }]"
        type="button"
        @click="activeConfigTab = tab.id"
      >
        {{ tab.label }}
      </button>
    </div>

    <ServiceConfigLocalModelTab
      v-show="activeConfigTab === 'local'"
      :hide-save="true"
      :cfg="svcCfg"
      :available-models="availableModels"
      :available-models-by-format="availableModelsByFormat"
      :saving="svcCfgSaving"
      @save="saveServiceConfig"
    />

    <ServiceConfigCloudTab
      v-show="activeConfigTab === 'cloud'"
      :hide-save="true"
      :cfg="svcCfg"
      :saving="svcCfgSaving"
      @save="saveServiceConfig"
    />

    <ServiceConfigRoutingTab
      v-show="activeConfigTab === 'routing'"
      :hide-save="true"
      :cfg="svcCfg"
      :saving="svcCfgSaving"
      @save="saveServiceConfig"
    />

    <ServiceConfigSecurityTab
      v-show="activeConfigTab === 'security'"
      :hide-save="true"
      :cfg="svcCfg"
      :saving="svcCfgSaving"
      @save="saveServiceConfig"
    />

    <ServiceConfigPromptOptTab
      v-show="activeConfigTab === 'prompt'"
      :hide-save="true"
      :cfg="svcCfg"
      :saving="svcCfgSaving"
      @save="saveServiceConfig"
    />

    <ServiceConfigDebugTab
      v-show="activeConfigTab === 'debug'"
      :hide-save="true"
      :cfg="svcCfg"
      :form="form"
      :saving="headerSaving"
      @save="saveDebugTab"
    />
  </div>
</template>

<style scoped src="./service-config/service-config.css"></style>
