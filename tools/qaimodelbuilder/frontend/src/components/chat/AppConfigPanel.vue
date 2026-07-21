<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AppConfigPanel — V1-parity accordion/toggle configuration form.
 *
 * Uses `useConfig` composable for fetch/save via GET/POST /api/forge-config.
 * The backend stores config as a nested dict:
 *   { config: { security: { bind_host, allow_exec_tool }, ... } }
 *
 * Groups: Security, Network Proxy, Channels, Workspace, Toolbar Modules,
 * AI Coding, Debug. (Agent Loop tuning moved to the dedicated 🤝 Agent tab —
 * see AgentSettingsPanel.vue.)
 * Sticky save bar at bottom.
 *
 * P4-A T2.7-A: the "Toolbar Modules" group now reads/writes the
 * V1-parity nested `ui.toolbar_modules.<key>.{enabled, order}` shape
 * via `useForgeConfig`, so toggling a module instantly hides its
 * button in `ChatComposer.vue` (shared module-singleton refs).
 */
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useConfig, type AppConfig } from "@/composables/useConfig";
import {
  useForgeConfig,
  type ToolbarModuleEntry,
} from "@/composables/useForgeConfig";
import { useRuntimeConfig } from "@/composables/useRuntimeConfig";
import { useConfirm } from "@/composables/useConfirm";
import { useReboot } from "@/composables/useReboot";
import { useProxy } from "@/composables/useProxy";
import { useToastStore } from "@/stores/toast";
import { useModelCatalogStore } from "@/stores/modelCatalog";
import { useUiStore } from "@/stores/ui";
import { fetchServiceModels } from "@/api/serviceControl";
import ComposerModeIcon from "@/components/chat/composer/ComposerModeIcon.vue";
import {
  clearHidden as clearModeIntroHidden,
  hidePermanently as hideModeIntroPermanently,
  type IntroMode,
} from "@/composables/useModeIntroCardVisibility";
import { useFontSize } from "@/composables/useFontSize";

const { t } = useI18n();
const toast = useToastStore();
const modelCatalogStore = useModelCatalogStore();
const { config, loading, fetchConfig, saveConfig } = useConfig();
const {
  toolbarModules,
  load: loadForgeConfig,
  patchModule: patchToolbarModule,
  ccEnabled,
  ocEnabled,
  patchAiCoding,
  appBuilderShowWorkbench,
  patchAppBuilderShowWorkbench,
} = useForgeConfig();

// Network-proxy credentials live behind the dedicated /api/proxy endpoint
// (username → forge_config.network_proxy.proxy_username, password →
// SecretStore). The proxy URL itself is NOT taken from here anymore — it is
// bound to the live `global_proxy` runtime-config field (see proxyUrlModel /
// saveProxyUrl below) so App Config, the channel "sync global" source and
// every outbound client share one URL truth.
const {
  proxyUsername,
  proxyPassword,
  showPassword: proxyShowPassword,
  saving: proxySaving,
  loadProxy,
  saveProxy,
} = useProxy();

// ─── TLS/SSL verification (security runtime-config surface) ────────────────
// `ssl_verify` lives on the typed /api/security/runtime-config surface (NOT
// the forge-config bulk form), so it saves IMMEDIATELY on toggle rather than
// via the sticky Save bar. Moved here from the Security → Tool Safety panel:
// it is an application/network setting, not a file-protection one. Default is
// edition-derived (internal→off, external→on). It hot-applies to the webfetch
// tool at once; the model-service / MCP transports pick it up on restart, so
// on change we offer the shared reboot-confirm dialog.
const {
  config: runtimeConfig,
  fetchConfig: fetchRuntimeConfig,
  save: saveRuntimeConfig,
} = useRuntimeConfig();
const { confirm } = useConfirm();
const { requestRebootDirect } = useReboot();
const sslVerify = ref(true);

// Proxy URL lives on the live `global_proxy` runtime-config field (the value
// every outbound client actually reads — the old network_proxy.proxy_url was
// write-only/dead). Username + password stay on /api/proxy (useProxy). The URL
// saves immediately (hot-applied to the webfetch/download/catalog clients),
// mirroring the Tool Safety behaviour it replaces.
const proxyUrlModel = ref("");

async function saveProxyUrl(): Promise<void> {
  const next = proxyUrlModel.value.trim() || null;
  await saveRuntimeConfig({ global_proxy: next });
}

async function toggleSslVerify(next: boolean): Promise<void> {
  sslVerify.value = next;
  const { needsReboot } = await saveRuntimeConfig({ ssl_verify: next });
  if (needsReboot) {
    const ok = await confirm({
      icon: "🔄",
      title: t("appConfig.sslVerifyRebootTitle"),
      message: t("appConfig.sslVerifyRebootMessage"),
      confirmText: t("appConfig.sslVerifyRebootConfirm"),
      cancelText: t("appConfig.sslVerifyRebootCancel"),
      confirmStyle: "primary",
    });
    if (ok) {
      await requestRebootDirect();
      return;
    }
    toast.push({
      id: crypto.randomUUID(),
      kind: "info",
      message: t("appConfig.sslVerifyRebootDeferred"),
      timeoutMs: 6000,
    });
    return;
  }
  toast.push({
    id: crypto.randomUUID(),
    kind: "success",
    message: t("appConfig.sslVerifySaved"),
    timeoutMs: 4000,
  });
}

// ─── Local form shape ─────────────────────────────────────────────────────
interface ConfigForm {
  // Security (maps to config.security.*)
  bind_host: string;
  allow_exec_tool: boolean;
  // Channels Config (forge_config.channels.*)
  max_history_rounds: number;
  // Debug → Service Log Buffer (forge_config.service_launch.service_log_buffer_size)
  service_log_buffer_size: number;
  // Debug → Show Prompt in UI (forge_config.service_launch.show_prompt_in_ui)
  show_prompt_in_ui: boolean;
  // Debug → Prompt Debug 控制台 dump (forge_config.service_launch.prompt_debug)
  prompt_debug: boolean;
  // Workspace → model conversion output root (forge_config.workspace.model_root)
  workspace_model_root: string;
}

const form = reactive<ConfigForm>({
  bind_host: "127.0.0.1",
  allow_exec_tool: true,
  max_history_rounds: 20,
  service_log_buffer_size: 6000,
  show_prompt_in_ui: false,
  prompt_debug: false,
  workspace_model_root: "",
});

// Full persisted `channels` / `service_launch` / `workspace` objects,
// round-tripped so the backend's shallow top-level merge does not clobber
// sibling sub-keys.
const rawChannels = ref<Record<string, unknown>>({});
const rawServiceLaunch = ref<Record<string, unknown>>({});
const rawWorkspace = ref<Record<string, unknown>>({});

// ─── Toolbar module display order (T2.7-A) ────────────────────────────────
// V1 layout order: model_builder / app_builder / code / translate / ppt.
// We render whatever keys exist in `toolbarModules`, sorted by `order`. The
// per-row icon is the SHARED `ComposerModeIcon` (keyed by module `key`) so the
// settings list matches the chat-input toolbar exactly — previously this list
// used mismatched / missing emoji prefixes.
interface ToolbarRow extends ToolbarModuleEntry {
  key: string;
}

const toolbarRows = ref<ToolbarRow[]>([]);

function refreshToolbarRows(): void {
  const map = toolbarModules.value;
  const list: ToolbarRow[] = [];
  for (const [key, entry] of Object.entries(map)) {
    list.push({ key, ...entry });
  }
  list.sort((a, b) => a.order - b.order);
  toolbarRows.value = list;
}

async function onToolbarToggle(key: string, enabled: boolean): Promise<void> {
  await patchToolbarModule(key, { enabled });
  refreshToolbarRows();
}

// ─── AI Coding pill toggles (T2.7-B) ──────────────────────────────────────
// `ai_coding.{cc,oc}.enabled` controls whether the Claude Code / Open
// Code pills appear in the chat input toolbar (V1
// `forge_config.ai_coding.<sub>.enabled` parity). Default `true`.

async function onCCEnabledToggle(enabled: boolean): Promise<void> {
  await patchAiCoding("cc", { enabled });
}

async function onOCEnabledToggle(enabled: boolean): Promise<void> {
  await patchAiCoding("oc", { enabled });
}

// ─── App Builder workbench visibility (retained-but-hidden-by-default) ─────
// `ui.app_builder.show_workbench` gates the heavy App Builder model
// workbench. Default `false` — the code/functionality is fully retained but
// hidden until the user opts in here.
async function onShowWorkbenchToggle(show: boolean): Promise<void> {
  await patchAppBuilderShowWorkbench(show);
}

// ─── Chat display: tool-call cards visibility ──────────────────────────────
// Previously exposed as a header toolbar button (ChatView useHeaderActions
// "Tool Calls" pill, 2026-07-20 sunset). It is a low-frequency preference —
// most users leave it on — so it moved into Settings → App Config where it
// belongs, and localStorage persists it across restarts. The store action
// itself writes localStorage (see stores/ui.ts::setShowToolMessages).
const ui = useUiStore();
function onShowToolCallsToggle(show: boolean): void {
  ui.setShowToolMessages(show);
}

// ─── Mode-intro hint visibility (per-mode restore) ─────────────────────────
// The chat-view ModeIntroCard (Plan §7 decision 5 — C+D) can be permanently
// hidden via its "don't show again" checkbox + ×. This settings block lets a
// user restore any of those hidden intros without a page reload: each toggle
// reflects the current localStorage flag, and flipping it clears (or re-sets)
// the flag. State lives entirely in localStorage — no backend / forge_config
// round-trip needed. The composable's module-level bump token drives the
// live ModeIntroCard to re-render immediately when the user restores.
const MODE_INTRO_MODES: IntroMode[] = [
  "app-builder",
  "gomaster",
  "model-build",
  "model-hub",
  "pro",
  "code",
];
const modeIntroVisible = reactive<Record<IntroMode, boolean>>({
  "app-builder": true,
  "gomaster": true,
  "model-build": true,
  "model-hub": true,
  "pro": true,
  "code": true,
});
function refreshModeIntroVisibility(): void {
  for (const m of MODE_INTRO_MODES) {
    // Storage read failures fall back to `not hidden` → toggle shows ON.
    let hidden = false;
    try {
      hidden = window.localStorage.getItem(`modeIntro.hidden.${m}`) === "1";
    } catch {
      hidden = false;
    }
    modeIntroVisible[m] = !hidden;
  }
}
function onToggleModeIntro(mode: IntroMode, show: boolean): void {
  modeIntroVisible[mode] = show;
  if (show) {
    // Turning "show intro" on → clear both local + session hides so the
    // card comes back on the next mode switch (and immediately if a live
    // conversation is already in that mode).
    clearModeIntroHidden(mode);
  } else {
    // Turning "show intro" off → permanently hide (localStorage tier).
    hideModeIntroPermanently(mode);
  }
}

// ─── Accordion collapse state ─────────────────────────────────────────────
const collapsedGroups = reactive(new Set<string>());

function toggleGroup(group: string): void {
  if (collapsedGroups.has(group)) {
    collapsedGroups.delete(group);
  } else {
    collapsedGroups.add(group);
  }
}

// ─── Helper to safely read nested keys ────────────────────────────────────
function getNestedValue(obj: Record<string, unknown>, path: string): unknown {
  const keys = path.split(".");
  let current: unknown = obj;
  for (const key of keys) {
    if (current == null || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

// ─── Sync form from config response ──────────────────────────────────────
function syncForm(): void {
  if (!config.value) return;
  const c = config.value as Record<string, unknown>;
  // Security (nested under security.*)
  const bindHost = getNestedValue(c, "security.bind_host");
  if (typeof bindHost === "string") form.bind_host = bindHost;
  const allowExec = getNestedValue(c, "security.allow_exec_tool");
  if (typeof allowExec === "boolean") form.allow_exec_tool = allowExec;
  // Channels Config (nested under channels.*) — keep the full object so the
  // shallow top-level merge on save preserves sibling sub-keys.
  const channels = c.channels;
  if (channels != null && typeof channels === "object") {
    const ch = channels as Record<string, unknown>;
    rawChannels.value = { ...ch };
    if (typeof ch.max_history_rounds === "number") form.max_history_rounds = ch.max_history_rounds;
  } else {
    rawChannels.value = {};
  }
  // Debug → Service Log Buffer (nested under service_launch.*)
  const serviceLaunch = c.service_launch;
  if (serviceLaunch != null && typeof serviceLaunch === "object") {
    const sl = serviceLaunch as Record<string, unknown>;
    rawServiceLaunch.value = { ...sl };
    if (typeof sl.service_log_buffer_size === "number") {
      form.service_log_buffer_size = sl.service_log_buffer_size;
    }
    if (typeof sl.show_prompt_in_ui === "boolean") {
      form.show_prompt_in_ui = sl.show_prompt_in_ui;
    }
    if (typeof sl.prompt_debug === "boolean") {
      form.prompt_debug = sl.prompt_debug;
    }
  } else {
    rawServiceLaunch.value = {};
  }
  // Workspace (nested under workspace.*) — keep the full object so the
  // shallow top-level merge on save preserves sibling sub-keys.
  const workspace = c.workspace;
  if (workspace != null && typeof workspace === "object") {
    const ws = workspace as Record<string, unknown>;
    rawWorkspace.value = { ...ws };
    if (typeof ws.model_root === "string") {
      form.workspace_model_root = ws.model_root;
    }
  } else {
    rawWorkspace.value = {};
  }
}

// ─── Build nested payload matching backend shape ──────────────────────────
function buildPayload(): Partial<AppConfig> {
  return {
    security: {
      bind_host: form.bind_host,
      allow_exec_tool: form.allow_exec_tool,
    },
    // Round-trip the full channels / service_launch objects so the backend's
    // shallow top-level merge keeps sibling sub-keys intact.
    channels: {
      ...rawChannels.value,
      max_history_rounds: form.max_history_rounds,
    },
    service_launch: {
      ...rawServiceLaunch.value,
      service_log_buffer_size: form.service_log_buffer_size,
      show_prompt_in_ui: form.show_prompt_in_ui,
      prompt_debug: form.prompt_debug,
    },
    workspace: {
      ...rawWorkspace.value,
      model_root: form.workspace_model_root,
    },
  };
}

// ─── Save / Reset ─────────────────────────────────────────────────────────
const saving = ref(false);

async function handleSave(): Promise<void> {
  saving.value = true;
  // Capture bind_host before the save so we can detect a change afterwards.
  // V1 parity (AppConfigPanel.js:28-47): when the WebUI bind address changes,
  // a restart is required for it to take effect, so we surface a warning toast.
  const prevBindHost = form.bind_host;
  // V1 parity (AppConfigPanel.js:29): capture catalog_url before save so we
  // can detect a change and refresh the model catalog afterwards.
  const prevCatalogUrl = (config.value as Record<string, unknown> | null)
    ?.model_catalog != null
    ? ((config.value as Record<string, unknown>).model_catalog as Record<string, unknown>)
        .catalog_url ?? ""
    : "";
  try {
    await saveConfig(buildPayload());
    syncForm();
    // V1 parity (saveForgeConfigAndRefresh → loadServiceModels): refresh
    // the service model list after config save — model_root changes affect
    // which models are discoverable on disk.
    void fetchServiceModels().catch(() => {/* best-effort */});
    // V1 parity (AppConfigPanel.js:34-37): if catalog_url changed, refresh
    // the model catalog entries so the Downloads panel reflects the new source.
    const newCatalogUrl = (config.value as Record<string, unknown> | null)
      ?.model_catalog != null
      ? ((config.value as Record<string, unknown>).model_catalog as Record<string, unknown>)
          .catalog_url ?? ""
      : "";
    if (newCatalogUrl !== prevCatalogUrl) {
      void modelCatalogStore.fetchEntries().catch(() => {/* best-effort */});
    }
    if (form.bind_host !== prevBindHost) {
      toast.push({
        id: crypto.randomUUID(),
        kind: "warning",
        message: t("appConfig.bindAddressChangedToast"),
        timeoutMs: 6000,
      });
    }
  } finally {
    saving.value = false;
  }
}

async function handleReset(): Promise<void> {
  await fetchConfig();
  syncForm();
  await loadForgeConfig();
  refreshToolbarRows();
  await loadProxy();
  await fetchRuntimeConfig();
  sslVerify.value = runtimeConfig.value.ssl_verify;
  proxyUrlModel.value = runtimeConfig.value.global_proxy ?? "";
}

// ─── Init ─────────────────────────────────────────────────────────────────
onMounted(async () => {
  await fetchConfig();
  syncForm();
  await loadForgeConfig();
  refreshToolbarRows();
  await loadProxy();
  await fetchRuntimeConfig();
  sslVerify.value = runtimeConfig.value.ssl_verify;
  proxyUrlModel.value = runtimeConfig.value.global_proxy ?? "";
  refreshModeIntroVisibility();
});

// ─── Global font size (moved here from SettingsView so it lives inside the
// "App Config" tab rather than above all settings tabs) ──────────────────────
const fontSizeCtl = useFontSize();
const globalFontSizePx = computed<number>({
  get: () => fontSizeCtl.fontSizeScale.value,
  set: (value: number) => {
    fontSizeCtl.fontSizeScale.value = Number(value);
  },
});
</script>

<template>
  <div class="config-section">
    <!-- Loading state -->
    <div
      v-if="loading"
      style="padding: 24px; color: var(--text-muted);"
    >
      {{ t("common.loading") }}
    </div>

    <template v-else>
      <!-- ═══ Global font size ═══ -->
      <section
        class="app-config-font-size-card"
        :aria-label="t('fontSize.label')"
      >
        <div class="app-config-font-size-card__main">
          <div class="app-config-font-size-card__title-row">
            <span class="app-config-font-size-card__icon" aria-hidden="true">Aa</span>
            <div>
              <h3 class="app-config-font-size-card__title">
                {{ t("fontSize.label") }}
              </h3>
              <p class="app-config-font-size-card__desc">
                {{ t("fontSize.globalHint") }}
              </p>
            </div>
          </div>
          <output class="app-config-font-size-card__value">
            {{ fontSizeCtl.fontSizeLabel.value }}
          </output>
        </div>

        <div class="app-config-font-size-slider-row">
          <span class="app-config-font-size-slider-row__bound">{{ fontSizeCtl.MIN_FONT_SIZE_PX }}px</span>
          <input
            v-model.number="globalFontSizePx"
            class="app-config-font-size-slider"
            type="range"
            :min="fontSizeCtl.MIN_FONT_SIZE_PX"
            :max="fontSizeCtl.MAX_FONT_SIZE_PX"
            step="1"
            :aria-label="t('fontSize.label')"
          />
          <span class="app-config-font-size-slider-row__bound">{{ fontSizeCtl.MAX_FONT_SIZE_PX }}px</span>
          <button
            class="app-config-font-size-reset"
            type="button"
            :title="t('fontSize.reset')"
            @click="fontSizeCtl.resetFontSize()"
          >
            {{ t("fontSize.reset") }}
          </button>
        </div>
      </section>

      <!-- ═══ Security ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('security')"
        >
          <span>🔒</span>
          <span>{{ t("appConfig.securityTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('security') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('security')"
          class="config-group-body"
        >
          <!-- Bind Host -->
          <div class="config-field">
            <label class="config-label">{{ t("appConfig.bindHostLabel") }}</label>
            <div class="config-comment">
              {{ t("appConfig.bindHostDesc") }}
            </div>
            <select
              v-model="form.bind_host"
              class="config-input"
            >
              <option value="127.0.0.1">
                {{ t("appConfig.bindLocalOnly") }}
              </option>
              <option value="0.0.0.0">
                {{ t("appConfig.bindAll") }}
              </option>
            </select>
            <!-- LAN exposure warning (V1 parity): only when binding to 0.0.0.0 -->
            <!-- eslint-disable vue/no-v-html -- trusted static i18n string with <b> markup -->
            <div
              v-if="form.bind_host === '0.0.0.0'"
              class="config-lan-warning"
              role="alert"
              data-testid="app-bind-lan-warning"
              v-html="t('appConfig.lanWarning')"
            ></div>
            <!-- eslint-enable vue/no-v-html -->
          </div>
          <!-- Allow Exec Tool -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.allowExecLabel") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  v-model="form.allow_exec_tool"
                  type="checkbox"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <!-- eslint-disable vue/no-v-html -->
            <div
              class="config-comment"
              v-html="t('appConfig.allowExecDesc')"
            ></div>
            <!-- eslint-enable vue/no-v-html -->
          </div>
          <!-- Verify TLS/SSL certificates (security runtime-config; saves
               immediately on toggle, offers restart for full effect). -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.sslVerifyLabel") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="sslVerify"
                  data-testid="app-ssl-verify-toggle"
                  @change="toggleSslVerify(($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <!-- eslint-disable vue/no-v-html -->
            <div
              class="config-comment"
              v-html="t('appConfig.sslVerifyDesc')"
            ></div>
            <!-- eslint-enable vue/no-v-html -->
          </div>
        </div>
      </div>

      <!-- ═══ Network Proxy ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('proxy')"
        >
          <span>🌐</span>
          <span>{{ t("appConfig.proxyTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('proxy') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('proxy')"
          class="config-group-body"
        >
          <!-- Proxy authentication (dedicated /api/proxy endpoint;
               password persisted via SecretStore, never in config). -->
          <!-- Proxy auth desc is a trusted i18n constant. -->
          <!-- eslint-disable vue/no-v-html -->
          <div
            class="config-comment"
            v-html="t('appConfig.proxyDesc')"
          ></div>
          <!-- eslint-enable vue/no-v-html -->
          <!-- Proxy URL — bound to the live `global_proxy` runtime-config
               field (the value every outbound client actually reads). Saves
               immediately on blur; username/password below still use /api/proxy. -->
          <div class="config-field">
            <label class="config-label">{{ t("appConfig.proxyUrl") }}</label>
            <input
              v-model="proxyUrlModel"
              type="text"
              class="config-input"
              :placeholder="t('appConfig.proxyUrlPlaceholder')"
              data-testid="app-proxy-url"
              @change="saveProxyUrl"
            />
          </div>
          <!-- Proxy Username -->
          <div class="config-field">
            <label class="config-label">{{ t("appConfig.proxyUsername") }}</label>
            <input
              v-model="proxyUsername"
              type="text"
              class="config-input"
              :placeholder="t('appConfig.proxyUsernamePlaceholder')"
            />
          </div>
          <!-- Proxy Password (SecretStore-backed; masked when set) -->
          <div class="config-field">
            <label class="config-label">{{ t("appConfig.proxyPassword") }}</label>
            <div style="display: flex; gap: 6px; align-items: center;">
              <input
                v-model="proxyPassword"
                :type="proxyShowPassword ? 'text' : 'password'"
                class="config-input"
                style="flex: 1;"
                :placeholder="t('appConfig.proxyPasswordPlaceholder')"
              />
              <button
                type="button"
                class="btn btn-ghost btn-sm config-eye-btn"
                @click="proxyShowPassword = !proxyShowPassword"
              >
                {{ proxyShowPassword ? "🙈" : "👁" }}
              </button>
            </div>
          </div>
          <div class="config-field">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :disabled="proxySaving"
              data-testid="proxy-save-btn"
              @click="saveProxy"
            >
              <!-- V1 parity (AppConfigPanel.js:189-192): 12×12 spinner with
                   2px border + 4px right margin while saving. Inline sizing
                   matches V1 exactly; colors come from the global .spinner
                   tokens (--border + --accent), not hard-coded. -->
              <span
                v-if="proxySaving"
                class="spinner"
                style="width:12px;height:12px;border-width:2px;margin-right:4px"
                aria-hidden="true"
              ></span>
              {{ proxySaving ? t("appConfig.proxySaving") : t("appConfig.proxySaveBtn") }}
            </button>
          </div>
        </div>
      </div>

      <!-- ═══ Channels Config ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('channels')"
        >
          <span>💬</span>
          <span>{{ t("appConfig.channelsTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('channels') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('channels')"
          class="config-group-body"
        >
          <!-- Max History Rounds (shared with ChannelsView; both write
               forge_config.channels.max_history_rounds) -->
          <div class="config-field">
            <label class="config-label">{{ t("appConfig.maxHistoryRoundsLabel") }}</label>
            <!-- eslint-disable vue/no-v-html -->
            <div
              class="config-comment"
              v-html="t('appConfig.maxHistoryRoundsDesc')"
            ></div>
            <!-- eslint-enable vue/no-v-html -->
            <input
              v-model.number="form.max_history_rounds"
              type="number"
              class="config-input config-number"
              min="1"
              max="200"
              placeholder="20"
              data-testid="channels-max-history-rounds"
            />
            <div class="config-comment">
              {{ t("appConfig.maxHistoryRoundsHint") }}
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ Chat Display ═══ -->
      <!-- Preferences that affect how the chat area renders. Currently a
           single toggle (tool-call cards visibility) — kept as its own
           section so future "chat rendering" prefs (e.g. per-message
           timestamps) have a clear home. Persistence: localStorage inside
           the setter (see stores/ui.ts). No forge_config round-trip. -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('chatDisplay')"
        >
          <span>💬</span>
          <span>{{ t("appConfig.chatDisplayTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('chatDisplay') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('chatDisplay')"
          class="config-group-body"
        >
          <div class="config-comment">
            {{ t("appConfig.chatDisplayDesc") }}
          </div>
          <!-- Show tool-call cards -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.showToolCallsLabel") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="ui.showToolMessages"
                  data-testid="chat-display-show-tool-calls-toggle"
                  @change="onShowToolCallsToggle(($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <!-- eslint-disable vue/no-v-html -- trusted static i18n string with <b> markup -->
            <div
              class="config-comment"
              v-html="t('appConfig.showToolCallsDesc')"
            ></div>
            <!-- eslint-enable vue/no-v-html -->
          </div>
        </div>
      </div>

      <!-- ═══ Workspace ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('workspace')"
        >
          <span>📁</span>
          <span>{{ t("appConfig.workspaceTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('workspace') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('workspace')"
          class="config-group-body"
        >
          <!-- Model workspace root
               (forge_config.workspace.model_root, default C:\WoS_AI) -->
          <div class="config-field">
            <label class="config-label">{{ t("appConfig.workspaceModelRootLabel") }}</label>
            <div class="config-comment">
              {{ t("appConfig.workspaceModelRootDesc") }}
            </div>
            <input
              v-model="form.workspace_model_root"
              type="text"
              class="config-input"
              :placeholder="t('appConfig.workspaceModelRootPlaceholder')"
              data-testid="workspace-model-root"
            />
          </div>
        </div>
      </div>

      <!-- ═══ Toolbar Modules ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('toolbar')"
        >
          <span>🧰</span>
          <span>{{ t("appConfig.toolbarModulesTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('toolbar') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('toolbar')"
          class="config-group-body"
        >
          <div class="config-comment">
            {{ t("appConfig.toolbarModulesDesc") }}
          </div>

          <!-- Data-driven row per module: enabled toggle + order input.
               Toggling here patches `ui.toolbar_modules.<key>.enabled`
               via useForgeConfig.patchModule() — Settings → forge-config
               POST is round-tripped immediately, and the chat input
               toolbar re-renders without page reload (shared module
               singleton refs in useForgeConfig). -->
          <div
            v-for="row in toolbarRows"
            :key="row.key"
            class="config-field"
          >
            <label class="config-label">
              <ComposerModeIcon
                :icon-key="row.key"
                :size="16"
                class="toolbar-module-icon"
              />
              {{ t(row.i18n) }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="row.enabled"
                  :data-testid="`toolbar-module-toggle-${row.key}`"
                  @change="onToolbarToggle(row.key, ($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
          </div>
        </div>
      </div>

      <!-- ═══ AI Coding Assistants (T2.7-B) ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('aiCoding')"
        >
          <span>🤖</span>
          <span>{{ t("appConfig.aiCodingTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('aiCoding') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('aiCoding')"
          class="config-group-body"
        >
          <div class="config-comment">
            {{ t("appConfig.aiCodingDesc") }}
          </div>
          <!-- Claude Code toolbar toggle -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.enableCCInToolbar") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="ccEnabled"
                  data-testid="ai-coding-cc-toggle"
                  @change="onCCEnabledToggle(($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <div class="config-comment">
              {{ t("appConfig.enableCCInToolbarDesc") }}
            </div>
          </div>
          <!-- Open Code toolbar toggle -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.enableOCInToolbar") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="ocEnabled"
                  data-testid="ai-coding-oc-toggle"
                  @change="onOCEnabledToggle(($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <div class="config-comment">
              {{ t("appConfig.enableOCInToolbarDesc") }}
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ App Builder ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('appBuilder')"
        >
          <span>🧩</span>
          <span>{{ t("appConfig.appBuilderTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('appBuilder') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('appBuilder')"
          class="config-group-body"
        >
          <div class="config-comment">
            {{ t("appConfig.appBuilderDesc") }}
          </div>
          <!-- Show heavy workbench toggle (retained but hidden by default) -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.showWorkbench") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="appBuilderShowWorkbench"
                  data-testid="app-builder-show-workbench-toggle"
                  @change="onShowWorkbenchToggle(($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <div class="config-comment">
              {{ t("appConfig.showWorkbenchDesc") }}
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ Mode Intro Hints ═══ -->
      <!-- Per-mode "restore the in-conversation intro card" toggles (Plan §7
           decision 5 — C+D). Each toggle mirrors the localStorage flag set
           by the ModeIntroCard's "don't show again" checkbox: ON = the intro
           will show next time the tab enters that mode; OFF = permanently
           hidden. Flipping ON immediately re-shows the card in any tab that
           is currently in that mode (via the composable's shared reset
           token — no page reload needed). -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('modeIntro')"
        >
          <span>💡</span>
          <span>{{ t("appConfig.modeIntroTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('modeIntro') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('modeIntro')"
          class="config-group-body"
        >
          <div class="config-comment">
            {{ t("appConfig.modeIntroDesc") }}
          </div>
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.modeIntroAppBuilder") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="modeIntroVisible['app-builder']"
                  data-testid="mode-intro-toggle-app-builder"
                  @change="onToggleModeIntro('app-builder', ($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
          </div>
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.modeIntroGomaster") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="modeIntroVisible['gomaster']"
                  data-testid="mode-intro-toggle-gomaster"
                  @change="onToggleModeIntro('gomaster', ($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
          </div>
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.modeIntroModelBuilder") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="modeIntroVisible['model-build']"
                  data-testid="mode-intro-toggle-model-build"
                  @change="onToggleModeIntro('model-build', ($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
          </div>
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.modeIntroModelHub") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="modeIntroVisible['model-hub']"
                  data-testid="mode-intro-toggle-model-hub"
                  @change="onToggleModeIntro('model-hub', ($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
          </div>
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.modeIntroPro") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="modeIntroVisible['pro']"
                  data-testid="mode-intro-toggle-pro"
                  @change="onToggleModeIntro('pro', ($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
          </div>
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.modeIntroCode") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  type="checkbox"
                  :checked="modeIntroVisible['code']"
                  data-testid="mode-intro-toggle-code"
                  @change="onToggleModeIntro('code', ($event.target as HTMLInputElement).checked)"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
          </div>
        </div>
      </div>

      <!-- ═══ Debug ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('debug')"
        >
          <span>🐛</span>
          <span>{{ t("appConfig.debugTitle") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsedGroups.has('debug') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsedGroups.has('debug')"
          class="config-group-body"
        >
          <!-- Show Prompt in UI
               (forge_config.service_launch.show_prompt_in_ui) — V1 parity -->
          <div class="config-field config-field--toggle">
            <label class="config-label">{{ t("appConfig.showPromptLabel") }}</label>
            <div class="config-comment">
              {{ t("appConfig.showPromptDesc") }}
            </div>
            <label class="toggle">
              <input
                v-model="form.show_prompt_in_ui"
                type="checkbox"
              />
              <span class="toggle-slider" />
            </label>
          </div>
          <!-- Prompt Debug Log
               (forge_config.service_launch.prompt_debug) — console/log dump -->
          <div class="config-field config-field--toggle">
            <label class="config-label">{{ t("serviceConfig.promptDebug") }}</label>
            <!-- eslint-disable vue/no-v-html -->
            <!-- Trusted static locale string (no user input). -->
            <div
              class="config-comment"
              v-html="t('serviceConfig.promptDebugHint')"
            ></div>
            <!-- eslint-enable vue/no-v-html -->
            <label class="toggle">
              <input
                v-model="form.prompt_debug"
                type="checkbox"
              />
              <span class="toggle-slider" />
            </label>
          </div>
          <!-- Service Log Buffer Size
               (forge_config.service_launch.service_log_buffer_size) -->
          <div class="config-field">
            <label class="config-label">{{ t("appConfig.logBufferLabel") }}</label>
            <!-- eslint-disable vue/no-v-html -->
            <div
              class="config-comment"
              v-html="t('appConfig.logBufferDesc')"
            ></div>
            <!-- eslint-enable vue/no-v-html -->
            <input
              v-model.number="form.service_log_buffer_size"
              type="number"
              class="config-input config-number"
              min="1000"
              max="20000"
              placeholder="6000"
              data-testid="debug-log-buffer-size"
            />
            <div class="config-comment">
              {{ t("appConfig.logBufferHint") }}
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- ═══ Sticky Save Bar ═══ -->
    <!-- V1 parity (AppConfigPanel.js:357-363): saving spinner replaces 💾 emoji,
         button label stays as "Save Settings" (does NOT switch to "Saving…");
         reset button is prefixed with ↺. -->
    <div class="config-save-bar">
      <button
        class="btn btn-primary"
        :disabled="saving"
        @click="handleSave"
      >
        <span
          v-if="saving"
          class="spinner"
          aria-hidden="true"
        ></span>
        <span v-else>💾</span>
        {{ t("appConfig.saveBtn") }}
      </button>
      <button
        class="btn btn-ghost"
        :disabled="saving"
        @click="handleReset"
      >
        ↺ {{ t("appConfig.resetBtn") }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* ── Global font size card (moved from SettingsView) ─────────────────────── */
.app-config-font-size-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  margin-bottom: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-secondary);
  box-shadow: var(--shadow-sm);
}
.app-config-font-size-card__main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}
.app-config-font-size-card__title-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.app-config-font-size-card__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: var(--radius-md);
  color: var(--accent);
  background: var(--accent-light);
  font-weight: 700;
  letter-spacing: -0.04em;
}
.app-config-font-size-card__title {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--text-lg);
  font-weight: 700;
}
.app-config-font-size-card__desc {
  margin: var(--space-1) 0 0 0;
  color: var(--text-secondary);
  font-size: var(--text-sm);
}
.app-config-font-size-card__value {
  min-width: 72px;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  color: var(--accent);
  background: var(--bg-tertiary);
  text-align: center;
  font-family: var(--font-mono);
  font-weight: 700;
}
.app-config-font-size-slider-row {
  display: grid;
  grid-template-columns: auto minmax(160px, 1fr) auto auto;
  align-items: center;
  gap: var(--space-3);
}
.app-config-font-size-slider-row__bound {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}
.app-config-font-size-slider {
  width: 100%;
  accent-color: var(--accent);
}
.app-config-font-size-reset {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-secondary);
  background: var(--bg-tertiary);
  cursor: pointer;
}
.app-config-font-size-reset:hover {
  color: var(--text-primary);
  border-color: var(--accent);
}

.toolbar-module-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-right: var(--space-2, 8px);
  color: var(--text-secondary, #9aa);
  flex: 0 0 auto;
}
.config-lan-warning {
  margin-top: var(--space-2, 8px);
  padding: var(--space-2, 8px) var(--space-3, 12px);
  /* V1 parity (AppConfigPanel.js:136-139): left-border accent only, no full
     border. Reuse global tokens — --warning for the accent stripe and
     --banner-warn-bg for the soft amber background (variables.css:53,73). */
  border: none;
  border-left: 3px solid var(--warning);
  border-radius: var(--radius-md, 6px);
  background: var(--banner-warn-bg);
  color: var(--text-primary);
  font-size: var(--text-sm, 0.85rem);
  line-height: 1.4;
}
</style>
