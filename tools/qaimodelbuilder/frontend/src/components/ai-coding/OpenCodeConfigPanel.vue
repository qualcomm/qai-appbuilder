<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * OpenCodeConfigPanel — full V1-parity OpenCode configuration.
 *
 * Backend contract (interfaces/http/routes/ai_coding.py):
 *   GET  /api/oc/config        → { config: {...} }
 *   PUT  /api/oc/config        ← { config: {...} }  (whitelist-filtered)
 *   GET  /api/oc/health        → { provider, available, available_providers, providers[], models[] }
 *   GET  /api/oc/service/status → { running, pid, uptime_seconds, port, cli_path, external }
 *   POST /api/oc/service/start | stop
 *   GET/POST/DELETE /api/oc/credentials  (password → SecretStore, never config)
 *
 * Model selection reuses the cloud model catalog
 * (/api/model-catalog/{entries,providers}) when use_cloud_models is on.
 * `password` is NOT a config field — it is credential material stored
 * via OPENCODE_PASSWORD in the SecretStore.
 */
import { onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  fetchOcConfig,
  updateOcConfig,
} from "@/api";
import { useToastStore } from "@/stores/toast";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { useOcModels, type OcModelListEntry } from "@/composables/useOcModels";
import { useOcPermission, type OcPermissionMap } from "@/composables/useOcPermission";
import { useOcService } from "@/composables/useOcService";
import WorkingDirsTextarea from "./config/WorkingDirsTextarea.vue";
import KeyValueTextarea from "./config/KeyValueTextarea.vue";
import ToolPermissionMatrix from "./config/ToolPermissionMatrix.vue";

const { t } = useI18n();
const toast = useToastStore();
function pushToast(kind: "success" | "error" | "info", message: string): void {
  toast.push({ id: crypto.randomUUID(), kind, message, timeoutMs: kind === "error" ? 5000 : 3000 });
}
// Pill gate lives in forge-config (`ai_coding.oc.enabled`), read by
// ChatComposer via the same `useForgeConfig` singleton. On save we mirror
// the OC "enabled" flag into forge-config so the chat-input OC pill reflects
// the choice (V1 parity: the OC config "enabled" and the pill are one flag).
const { patchAiCoding, load: loadForgeConfig } = useForgeConfig();

interface OcConfig {
  enabled: boolean;
  base_url: string;
  hostname: string;
  provider_id: string;
  model_id: string;
  message_timeout_seconds: number;
  pending_message_ttl_seconds: number;
  session_idle_timeout_minutes: number;
  allowed_working_dirs: string[];
  cli_path: string;
  auto_start: boolean;
  use_cloud_models: boolean;
  username: string;
  provider_mapping: Record<string, string>;
  permission: OcPermissionMap;
  model_list: OcModelListEntry[];
}

const cfg = reactive<OcConfig>({
  enabled: false,
  base_url: "",
  hostname: "127.0.0.1",
  provider_id: "opencode",
  model_id: "big-pickle",
  message_timeout_seconds: 300,
  pending_message_ttl_seconds: 600,
  session_idle_timeout_minutes: 0,
  allowed_working_dirs: [],
  cli_path: "",
  auto_start: false,
  use_cloud_models: true,
  username: "",
  provider_mapping: {},
  permission: {},
  model_list: [],
});

const loading = ref(false);
const saving = ref(false);

const collapsed = reactive(new Set<string>());
function toggleGroup(g: string): void {
  if (collapsed.has(g)) collapsed.delete(g);
  else collapsed.add(g);
}

// ─── Model selection (extracted to useOcModels composable, cohesion split) ───
const {
  cloudProviders,
  availableModels,
  selectedModelKey,
  modelsExpanded,
  visibleModels,
  loadCloudModels,
  navigateToCloudModels,
} = useOcModels({ cfg });
void cloudProviders;
void availableModels;

// ─── Tool permission (extracted to useOcPermission composable, cohesion split) ─
const {
  OC_TOOLS,
  OC_STATES,
  ocToolStateOf,
  onOcToolCycle,
  ocAllowList,
  ocAskList,
  ocDenyList,
  permissionText,
  permissionParseError,
  permissionToText,
  permissionFromText,
  syncPermissionText,
} = useOcPermission({ cfg });

// ─── Service control + health + credential (extracted to useOcService) ───────
const {
  health,
  healthLoading,
  procStatus,
  procLoading,
  uptimeText,
  passwordInput,
  passwordConfigured,
  showPassword,
  checkHealth,
  loadProcStatus,
  loadCredentials,
  onStart,
  onStop,
  onRefresh,
  savePassword,
  clearPassword,
} = useOcService({ enabled: () => cfg.enabled, pushToast });
void healthLoading;
void showPassword;

// ─── Load ───────────────────────────────────────────────────────────────────
async function loadConfig(): Promise<void> {
  loading.value = true;
  try {
    const res = await fetchOcConfig();
    const c = res.config as Record<string, unknown>;
    if (typeof c.enabled === "boolean") cfg.enabled = c.enabled;
    if (typeof c.base_url === "string") cfg.base_url = c.base_url;
    if (typeof c.hostname === "string") cfg.hostname = c.hostname;
    if (typeof c.provider_id === "string") cfg.provider_id = c.provider_id;
    if (typeof c.model_id === "string") cfg.model_id = c.model_id;
    if (typeof c.message_timeout_seconds === "number") cfg.message_timeout_seconds = c.message_timeout_seconds;
    if (typeof c.pending_message_ttl_seconds === "number") cfg.pending_message_ttl_seconds = c.pending_message_ttl_seconds;
    if (typeof c.session_idle_timeout_minutes === "number") cfg.session_idle_timeout_minutes = c.session_idle_timeout_minutes;
    if (Array.isArray(c.allowed_working_dirs)) cfg.allowed_working_dirs = c.allowed_working_dirs as string[];
    if (typeof c.cli_path === "string") cfg.cli_path = c.cli_path;
    if (typeof c.auto_start === "boolean") cfg.auto_start = c.auto_start;
    if (typeof c.use_cloud_models === "boolean") cfg.use_cloud_models = c.use_cloud_models;
    if (typeof c.username === "string") cfg.username = c.username;
    if (c.provider_mapping && typeof c.provider_mapping === "object") cfg.provider_mapping = c.provider_mapping as Record<string, string>;
    if (Array.isArray(c.model_list)) cfg.model_list = c.model_list as OcModelListEntry[];
    if (c.permission && typeof c.permission === "object") {
      cfg.permission = c.permission as OcPermissionMap;
      syncPermissionText();
    }
  } catch (e) {
    pushToast("error", t("aiCoding.config.loadFailed", "Failed to load config: ") + (e as Error).message);
  } finally {
    loading.value = false;
  }
}

// ─── Save ─────────────────────────────────────────────────────────────────
async function saveConfig(): Promise<void> {
  saving.value = true;
  try {
    // Validate the advanced permission JSON (V1 parity): an invalid,
    // non-empty textarea blocks the save with a red inline error rather
    // than silently dropping the operator's rules.
    permissionParseError.value = "";
    const parsedPermission = permissionFromText(permissionText.value);
    if (permissionText.value.trim() && parsedPermission === null) {
      permissionParseError.value = t(
        "openCode.config.permissionJsonError",
        "Invalid JSON — please fix the permission rules before saving.",
      );
      saving.value = false;
      return;
    }
    cfg.permission = parsedPermission ?? {};
    const payload: Record<string, unknown> = {
      enabled: cfg.enabled,
      base_url: cfg.base_url,
      hostname: cfg.hostname,
      provider_id: cfg.provider_id,
      model_id: cfg.model_id,
      message_timeout_seconds: cfg.message_timeout_seconds,
      pending_message_ttl_seconds: cfg.pending_message_ttl_seconds,
      session_idle_timeout_minutes: cfg.session_idle_timeout_minutes,
      allowed_working_dirs: cfg.allowed_working_dirs,
      cli_path: cfg.cli_path,
      auto_start: cfg.auto_start,
      use_cloud_models: cfg.use_cloud_models,
      username: cfg.username,
      provider_mapping: cfg.provider_mapping,
      permission: cfg.permission,
      model_list: cfg.model_list,
    };
    await updateOcConfig(payload);
    // Password → credential store (only if changed; "****" is masked-skip).
    await savePassword();
    pushToast("success", t("aiCoding.config.ocSaved", "Open Code configuration saved"));
    // Mirror the OC "enabled" flag into forge-config so the chat-input OC
    // pill reflects the choice (V1 parity: config "enabled" and the pill
    // share one flag). Fires only on explicit user save — no reactive
    // watcher — so there is no loop. `patchAiCoding` persists via
    // POST /api/forge-config; `loadForgeConfig` refreshes the shared
    // singleton (V1 "loadForgeConfig after save").
    await patchAiCoding("oc", { enabled: cfg.enabled });
    await loadForgeConfig();
    await checkHealth();
  } catch (e) {
    pushToast("error", t("aiCoding.config.saveFailed", "Failed to save: ") + (e as Error).message);
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  void loadConfig();
  void loadCloudModels();
  void loadCredentials();
  void checkHealth();
  void loadProcStatus();
});
</script>

<template>
  <div
    class="config-section"
    data-testid="open-code-config-panel"
  >
    <h3 class="qai-oc__title">
      {{ t("aiCoding.config.ocTitle", "Open Code Configuration") }}
    </h3>

    <div
      v-if="loading"
      class="qai-oc__loading"
      data-testid="oc-config-loading"
    >
      {{ t("common.loading", "Loading...") }}
    </div>

    <template v-else>
      <!-- ═══ 🩺 Environment Status + Service Control ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          style="cursor: default;"
        >
          <span>🩺 {{ t("aiCoding.config.envStatus", "Environment Status") }}</span>
          <div style="margin-left: auto; display: flex; gap: 6px; align-items: center;">
            <button
              v-if="!procStatus || !procStatus.running"
              type="button"
              class="btn btn-primary btn-sm qai-oc__svc-btn"
              data-testid="oc-service-start"
              :disabled="procLoading || !cfg.enabled"
              @click="onStart"
            >
              ▶ {{ t("aiCoding.config.start", "Start") }}
            </button>
            <button
              v-else-if="procStatus.external"
              type="button"
              class="btn btn-ghost btn-sm qai-oc__svc-btn"
              disabled
              :title="t('aiCoding.config.externalProcess', 'Externally managed process')"
            >
              ⏹ {{ t("aiCoding.config.stop", "Stop") }}
            </button>
            <button
              v-else
              type="button"
              class="btn btn-ghost btn-sm qai-oc__svc-btn"
              data-testid="oc-service-stop"
              :disabled="procLoading"
              @click="onStop"
            >
              ⏹ {{ t("aiCoding.config.stop", "Stop") }}
            </button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              data-testid="oc-health-refresh"
              :disabled="healthLoading || procLoading"
              @click="onRefresh"
            >
              🔄 {{ t("common.refresh", "Refresh") }}
            </button>
          </div>
        </div>
        <div class="config-group-body">
          <div class="qai-oc__status">
            <span>
              {{ t("aiCoding.config.process", "Process") }}:
              <strong :style="{ color: procStatus?.running ? '#4ade80' : 'var(--text-muted)' }">
                {{ procStatus ? (procStatus.running ? "● " + t("aiCoding.config.statusRunning", "Running") : "○ " + t("aiCoding.config.statusStopped", "Stopped")) : "○ " + t("aiCoding.config.statusUnknown", "Unknown") }}
              </strong>
              <span
                v-if="procStatus?.external"
                class="qai-oc__tag"
              >{{ t("aiCoding.config.externalTag", "external") }}</span>
              <span
                v-if="procStatus?.running && procStatus.pid"
                class="config-comment"
                style="margin: 0;"
              >PID {{ procStatus.pid }}</span>
              <span
                v-if="uptimeText"
                class="config-comment"
                style="margin: 0;"
              >· {{ uptimeText }}</span>
            </span>
            <span>
              HTTP:
              <strong :style="{ color: health?.available ? '#4ade80' : 'var(--text-muted)' }">
                {{ health ? (health.available ? "✅ " + t("aiCoding.config.statusConnected", "Connected") : "❌ " + t("aiCoding.config.statusDisconnected", "Disconnected")) : "○ " + t("aiCoding.config.statusUnknown", "Unknown") }}
              </strong>
              <!-- V1 parity (OpenCodeConfigPanel.js:526-528): when HTTP is
                   connected, show the configured base_url next to the status.
                   V1 read this from health.base_url; V2 health does not yet
                   expose it (TODO: extend backend), so display the user-
                   configured base_url which matches the user-perceived V1
                   behavior. -->
              <span
                v-if="health?.available && cfg.base_url"
                class="oc-env-baseurl"
              >{{ cfg.base_url }}</span>
            </span>
            <span v-if="health && health.auth_configured !== undefined">
              {{ t("aiCoding.config.auth", "Auth") }}:
              <strong :style="{ color: health.auth_configured ? '#4ade80' : 'var(--text-muted)' }">{{ health.auth_configured ? "✅ " + (health.auth_source || "") : "❌" }}</strong>
            </span>
            <span v-if="health && health.active_sessions !== undefined">
              {{ t("aiCoding.config.sessions", "Sessions") }}: {{ health.active_sessions }} {{ t("aiCoding.config.sessionsActive", "active") }} · {{ health.total_sessions }} {{ t("aiCoding.config.sessionsTotal", "total") }}
            </span>
            <span v-if="health && health.models?.length">
              {{ t("aiCoding.config.modelsCount", "Models") }}: {{ health.models?.length ?? 0 }}
            </span>
            <span>
              {{ t("aiCoding.config.enableOc", "Enable Open Code") }}:
              <strong :style="{ color: cfg.enabled ? '#4ade80' : 'var(--text-muted)' }">{{ cfg.enabled ? "✅" : "⚪" }}</strong>
            </span>
          </div>
          <div
            v-if="health && !health.available"
            class="qai-oc__warn"
          >
            ⚠️ {{ t("aiCoding.config.notConnectedHint", "Service not connected. Start it here or run") }}
            <code>opencode serve</code>
          </div>
        </div>
      </div>

      <!-- ═══ ⚙️ Basic Settings ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('basic')"
        >
          <span>⚙️ {{ t("aiCoding.config.basicSettings", "Basic Settings") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsed.has('basic') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsed.has('basic')"
          class="config-group-body"
        >
          <div class="config-field qai-oc__row">
            <label
              class="config-label"
              style="margin-bottom: 0;"
            >{{ t("aiCoding.config.enableOc", "Enable Open Code") }}</label>
            <label class="toggle">
              <input
                v-model="cfg.enabled"
                type="checkbox"
                data-testid="oc-enabled"
              />
              <span class="toggle-slider"></span>
            </label>
            <span
              class="config-comment"
              style="margin: 0;"
            >{{ cfg.enabled ? t("common.enabled", "Enabled") : t("common.disabled", "Disabled") }}</span>
          </div>

          <div class="config-field qai-oc__row">
            <label
              class="config-label"
              style="margin-bottom: 0;"
            >{{ t("aiCoding.config.useCloudModels", "Use Cloud Models") }}</label>
            <label class="toggle">
              <input
                v-model="cfg.use_cloud_models"
                type="checkbox"
              />
              <span class="toggle-slider"></span>
            </label>
          </div>

          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.model", "Model") }}</label>
            <div class="config-comment">
              {{ t("openCode.config.modelDesc", "Model used for all Open Code sessions. Takes effect on the next message.") }}
            </div>

            <!-- Built-in / cloud list info bar + jump button (V1 parity
                 OpenCodeConfigPanel.js:598-620). -->
            <div class="qai-oc__listbar">
              <span class="qai-oc__listbar-badge">{{ t("openCode.config.builtinListBadge", "Built-in list") }}</span>
              <span class="qai-oc__listbar-hint">{{ t("openCode.config.builtinListHint", "These models come from \"Cloud Models\". To add or remove models, edit the cloud models configuration.") }}</span>
              <button
                type="button"
                class="btn btn-ghost btn-sm qai-oc__listbar-go"
                data-testid="oc-goto-cloud-models"
                @click="navigateToCloudModels"
              >
                {{ t("openCode.config.builtinListGoBtn", "Go to Cloud Models") }} →
              </button>
            </div>

            <div
              v-if="visibleModels.length"
              class="qai-oc__models"
            >
              <label
                v-for="m in visibleModels"
                :key="m.uniqueKey"
                class="qai-oc__model"
                :class="{ 'qai-oc__model--active': selectedModelKey === m.uniqueKey }"
                :title="m.label"
              >
                <input
                  type="radio"
                  :value="m.uniqueKey"
                  :checked="selectedModelKey === m.uniqueKey"
                  @change="selectedModelKey = m.uniqueKey"
                />
                <span>{{ m.label }}</span>
              </label>
            </div>
            <div
              v-else
              class="config-comment"
            >
              {{ t("aiCoding.config.noCloudModels", "No cloud models configured (see Cloud Models tab).") }}
            </div>
            <div
              v-if="availableModels.length > 3"
              style="margin-top: 4px;"
            >
              <button
                type="button"
                class="qai-oc__model-toggle"
                @click="modelsExpanded = !modelsExpanded"
              >
                <!-- V1 parity (OpenCodeConfigPanel.js: showMoreModels): small
                     dimmed ▼/▲ arrow prefix + the *hidden* count (total -
                     visible), not the total. -->
                <span class="qai-oc__model-toggle-arrow">{{ modelsExpanded ? "▲" : "▼" }}</span>
                {{ modelsExpanded
                  ? t("aiCoding.config.showLess", "Show less")
                  : t("openCode.config.showMoreModels", { n: availableModels.length - visibleModels.length }) }}
              </button>
            </div>
            <div class="config-comment">
              {{ t("aiCoding.config.modelCurrent", "Current") }}: <code>{{ cfg.provider_id }}::{{ cfg.model_id || "(not set)" }}</code>
              <span
                v-if="cfg.use_cloud_models"
                style="margin-left: 8px;"
              >
                · {{ availableModels.length }} {{ t("openCode.config.modelsFromPanel", "models from Cloud Models panel") }}
              </span>
            </div>

            <!-- Provider ID input only when NOT using the cloud catalog
                 (V1 parity OpenCodeConfigPanel.js:660-665). -->
            <div
              v-if="!cfg.use_cloud_models"
              style="margin-top: 8px;"
            >
              <label class="config-label">{{ t("aiCoding.config.providerId", "Provider ID") }}</label>
              <input
                v-model="cfg.provider_id"
                type="text"
                class="config-input mono"
                placeholder="openai"
                style="max-width: 200px;"
              />
            </div>
          </div>

          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.toolPermissions", "Tool Permissions") }}</label>
            <!-- 4-state legend (V1 parity OpenCodeConfigPanel.js:671-689). -->
            <div class="config-comment qai-oc__legend">
              {{ t("openCode.config.toolPermClickHint", "Click a tool name to toggle its permission state.") }}
              <span class="qai-oc__legend-item">
                <span
                  class="qai-oc__legend-swatch"
                  style="background: rgba(74,222,128,0.25); border-color: #4ade80;"
                ></span>
                <b style="color: #4ade80;">{{ t("openCode.config.toolPermAllowLabel", "Allow") }}</b>
              </span>{{ t("openCode.config.toolPermAllowDesc", ": Run directly, no confirmation;") }}
              <span class="qai-oc__legend-item">
                <span
                  class="qai-oc__legend-swatch"
                  style="background: rgba(251,191,36,0.25); border-color: #fbbf24;"
                ></span>
                <b style="color: #fbbf24;">{{ t("openCode.config.toolPermAskLabel", "Ask") }}</b>
              </span>{{ t("openCode.config.toolPermAskDesc", ": Ask before execution;") }}
              <span class="qai-oc__legend-item">
                <span
                  class="qai-oc__legend-swatch"
                  style="background: rgba(248,113,113,0.25); border-color: #f87171;"
                ></span>
                <b style="color: #f87171;">{{ t("openCode.config.toolPermDenyLabel", "Deny") }}</b>
              </span>{{ t("openCode.config.toolPermDenyDesc", ": Always denied (highest priority);") }}
              <span class="qai-oc__legend-item">
                <span
                  class="qai-oc__legend-swatch"
                  style="background: var(--bg-tertiary, #1a2a3a); border-color: var(--border, #2a3a4a);"
                ></span>
                <b style="color: var(--text-muted);">{{ t("openCode.config.toolPermDefaultLabel", "Default") }}</b>
              </span>{{ t("openCode.config.toolPermDefaultDesc", ": Use Open Code default policy (typically ask).") }}
            </div>
            <ToolPermissionMatrix
              :tools="OC_TOOLS"
              :states="OC_STATES"
              :state-of="ocToolStateOf"
              @cycle="onOcToolCycle"
            />
            <!-- Current configuration summary (V1 parity:731-745). -->
            <div class="qai-oc__perm-summary">
              <span>
                <b style="color: #4ade80;">{{ t("openCode.config.toolPermAllowSummary", "Allow:") }}</b>
                {{ ocAllowList.length ? ocAllowList.join(", ") : t("openCode.config.toolPermNone", "(none)") }}
              </span>
              <span>
                <b style="color: #fbbf24;">{{ t("openCode.config.toolPermAskSummary", "Ask:") }}</b>
                {{ ocAskList.length ? ocAskList.join(", ") : t("openCode.config.toolPermNone", "(none)") }}
              </span>
              <span>
                <b style="color: #f87171;">{{ t("openCode.config.toolPermDenySummary", "Deny:") }}</b>
                {{ ocDenyList.length ? ocDenyList.join(", ") : t("openCode.config.toolPermNone", "(none)") }}
              </span>
            </div>
            <div class="config-comment qai-oc__perm-order">
              💡 {{ t("openCode.config.toolPermClickOrder", "Click order") }}:
              <b style="color: var(--text-secondary);">Default</b> →
              <b style="color: #4ade80;">Allow</b> →
              <b style="color: #fbbf24;">Ask</b> →
              <b style="color: #f87171;">Deny</b> →
              <b style="color: var(--text-secondary);">Default</b>
            </div>
          </div>

          <!-- Advanced: fine-grained Permission JSON (V1 parity). Overrides
               / complements the 4-state matrix above; source of truth on
               save. Invalid JSON blocks the save with an inline error. -->
          <div class="config-field">
            <label class="config-label">{{ t("openCode.config.advancedPermJson", "Advanced: Permission JSON (fine-grained config)") }}</label>
            <div class="config-comment">
              {{ t("openCode.config.advancedPermJsonDesc", "Supports fine-grained pattern matching, e.g.:") }}
              <code>{"bash":{"*":"ask","git *":"allow","rm *":"deny"}}</code>
              <br />
              {{ t("openCode.config.advancedPermJsonHint", "Leave empty to use the Tag selector above. Edits here override the Tag selector state.") }}
            </div>
            <textarea
              v-model="permissionText"
              class="config-input oc-perm-json"
              placeholder="{&quot;bash&quot;:&quot;ask&quot;,&quot;edit&quot;:&quot;deny&quot;,&quot;webfetch&quot;:&quot;allow&quot;}"
              spellcheck="false"
              @input="permissionParseError = ''"
            ></textarea>
            <div
              v-if="permissionParseError"
              class="oc-perm-json__error"
              role="alert"
            >
              {{ permissionParseError }}
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ 📁 Working Directory Whitelist ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('dirs')"
        >
          <span>📁 {{ t("aiCoding.config.workingDirs", "Working Directory Whitelist") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsed.has('dirs') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsed.has('dirs')"
          class="config-group-body"
        >
          <div class="config-field">
            <div class="config-comment">
              {{ t("aiCoding.config.workingDirsDesc", "One absolute path per line.") }}
            </div>
            <WorkingDirsTextarea
              v-model="cfg.allowed_working_dirs"
              placeholder="C:\Projects\MyApp&#10;D:\Work\Backend"
              warn-empty
              :empty-warning="t('aiCoding.config.noWhitelistWarn', 'No whitelist set — all directories may be denied.')"
            />
          </div>
        </div>
      </div>

      <!-- ═══ 🔧 Advanced Settings ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('advanced')"
        >
          <span>🔧 {{ t("aiCoding.config.advanced", "Advanced Settings") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsed.has('advanced') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsed.has('advanced')"
          class="config-group-body"
        >
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.cliPath", "CLI Path") }}</label>
            <!-- V1 parity (OpenCodeConfigPanel.js:809): describe the cli_path. -->
            <div class="config-comment">
              {{ t("openCode.config.cliPathDesc1", "Full path to the Open Code executable. Leave empty to use the ") }}
              <code>opencode</code>{{ t("openCode.config.cliPathDesc2", " command on PATH.") }}
            </div>
            <input
              v-model="cfg.cli_path"
              type="text"
              class="config-input mono"
              placeholder="opencode"
            />
          </div>
          <div class="config-field qai-oc__row">
            <label
              class="config-label"
              style="margin-bottom: 0;"
            >{{ t("aiCoding.config.autoStart", "Auto Start") }}</label>
            <label class="toggle">
              <input
                v-model="cfg.auto_start"
                type="checkbox"
                :disabled="!cfg.enabled"
              />
              <span class="toggle-slider"></span>
            </label>
          </div>
          <!-- V1 parity (OpenCodeConfigPanel.js:822-826): describe what
               auto_start does. -->
          <div class="config-field">
            <div class="config-comment">
              {{ t("openCode.config.autoStartDesc", "When enabled, the QAIModelBuilder service auto-starts the Open Code process on launch (requires enabled=true and cli_path set).") }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.hostname", "Hostname") }}</label>
            <div class="qai-oc__row">
              <select
                v-model="cfg.hostname"
                class="config-input"
                style="width: 180px;"
              >
                <option value="127.0.0.1">
                  127.0.0.1 ({{ t("aiCoding.config.hostnameLocal", "local only") }})
                </option>
                <option value="0.0.0.0">
                  0.0.0.0 ({{ t("aiCoding.config.hostnameAll", "all interfaces") }})
                </option>
              </select>
              <input
                v-model="cfg.hostname"
                type="text"
                class="config-input mono"
                placeholder="127.0.0.1"
                style="flex: 1;"
              />
            </div>
            <!-- V1 parity (OpenCodeConfigPanel.js:843-847): describe hostname,
                 referencing the underlying CLI flag and recommendations. -->
            <div class="config-comment">
              {{ t("openCode.config.hostnameDesc1", "Open Code service listen address. Maps to ") }}
              <code>opencode serve --hostname</code>{{ t("openCode.config.hostnameDesc2", ".") }}
              {{ t("openCode.config.hostnameDesc3", "Use ") }}
              <code>127.0.0.1</code>{{ t("openCode.config.hostnameDesc4", " for local access (recommended); use ") }}
              <code>0.0.0.0</code>。
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.baseUrl", "Base URL") }}</label>
            <input
              v-model="cfg.base_url"
              type="text"
              class="config-input mono"
              placeholder="http://127.0.0.1:54321"
            />
            <!-- V1 parity (OpenCodeConfigPanel.js:854-857): describe base_url. -->
            <div class="config-comment">
              {{ t("openCode.config.baseUrlDesc1", "Full URL of the Open Code HTTP service (with port), default http://127.0.0.1:54321.") }}
              <br />
              {{ t("openCode.config.baseUrlDesc2", "The Python SDK connects to Open Code via this URL.") }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.username", "Username") }}</label>
            <input
              v-model="cfg.username"
              type="text"
              class="config-input mono"
              autocomplete="off"
            />
            <!-- V1 parity (OpenCodeConfigPanel.js:866-869): describe the
                 Basic Auth username + the underlying env var. -->
            <div class="config-comment">
              {{ t("openCode.config.usernameDesc1", "Maps to the ") }}
              <code>OPENCODE_SERVER_USERNAME</code>{{ t("openCode.config.usernameDesc2", " environment variable.") }}
              {{ t("openCode.config.usernameDesc3", "Defaults to ") }}
              <code>opencode</code>。{{ t("openCode.config.usernameDesc4", "Local-dev: prefer to leave Basic Auth disabled.") }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">
              {{ t("aiCoding.config.password", "Password") }}
              <span class="qai-oc__secret-tag">{{ t("aiCoding.config.secret", "secret") }}</span>
            </label>
            <div class="qai-oc__row">
              <input
                v-model="passwordInput"
                :type="showPassword ? 'text' : 'password'"
                class="config-input mono"
                autocomplete="new-password"
                style="flex: 1;"
              />
              <button
                type="button"
                class="btn btn-ghost btn-sm"
                @click="showPassword = !showPassword"
              >
                {{ showPassword ? t("aiCoding.config.hide", "Hide") : t("aiCoding.config.show", "Show") }}
              </button>
              <button
                v-if="passwordConfigured"
                type="button"
                class="btn btn-ghost btn-sm"
                @click="clearPassword"
              >
                {{ t("aiCoding.config.delete", "Delete") }}
              </button>
            </div>
            <div class="config-comment">
              {{ t("aiCoding.config.passwordDesc", "Stored in the secure store (never in config). Leave blank to keep unchanged.") }}
            </div>
            <!-- V1 parity (OpenCodeConfigPanel.js:878-882): describe the
                 underlying env var + a yellow security warning. -->
            <div class="config-comment">
              {{ t("openCode.config.passwordDesc1", "Maps to the ") }}
              <code>OPENCODE_SERVER_PASSWORD</code>{{ t("openCode.config.passwordDesc2", " environment variable.") }}
              {{ t("openCode.config.passwordDesc3", "Leave empty to disable Basic Auth (recommended for local dev).") }}
              <br />
              <span class="qai-oc__warning-text">⚠️ {{ t("openCode.config.passwordWarning", "Password is not echoed back; you must re-enter it after a change.") }}</span>
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.messageTimeout", "Message Timeout (sec)") }}</label>
            <input
              v-model.number="cfg.message_timeout_seconds"
              type="number"
              min="30"
              max="3600"
              class="config-input"
              style="max-width: 120px;"
            />
            <!-- V1 parity (OpenCodeConfigPanel.js msgTimeout warn): short → red,
                 long (≥600s) → green. -->
            <div
              v-if="cfg.message_timeout_seconds < 120"
              class="config-comment qai-oc__warn-text"
            >
              {{ t("openCode.config.msgTimeoutShortWarn", "Very short timeout — complex tasks may fail frequently") }}
            </div>
            <div
              v-else-if="cfg.message_timeout_seconds >= 600"
              class="config-comment qai-oc__ok-text"
            >
              {{ t("openCode.config.msgTimeoutLongOk", "Recommended for long-running tasks (>= 600s)") }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.pendingTtl", "Pending Message TTL (sec)") }}</label>
            <input
              v-model.number="cfg.pending_message_ttl_seconds"
              type="number"
              min="60"
              max="3600"
              class="config-input"
              style="max-width: 120px;"
            />
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.sessionIdleTimeout", "Session Idle Timeout (min)") }}</label>
            <input
              v-model.number="cfg.session_idle_timeout_minutes"
              type="number"
              min="0"
              max="1440"
              class="config-input"
              style="max-width: 120px;"
            />
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.providerMapping", "Provider Mapping (KEY=VALUE)") }}</label>
            <!-- V1 parity (OpenCodeConfigPanel.js:916-932): full multi-paragraph
                 description explaining the format + extraction rule + examples. -->
            <div class="config-comment">
              {{ t("openCode.config.providerMappingDesc1", "Cloud Models provider name → Open Code provider ID mapping. One per line, format: ") }}
              <code>CloudProviderName=ocProviderId</code>。
              <br />
              {{ t("openCode.config.providerMappingDesc2", "When a model ID contains ") }}
              <code>::</code>{{ t("openCode.config.providerMappingDesc3", " (Cloud Models format, e.g. ") }}
              <code>CustomProvider::claude-4-6-sonnet</code>{{ t("openCode.config.providerMappingDesc4", "), the model name is auto-extracted and routed to the corresponding Open Code provider via this mapping.") }}
              <br />
              {{ t("openCode.config.providerMappingExamples", "Examples: ") }}
              <code>CustomProvider=customprovider</code>、<code>OpenAI=openai</code>、<code>Anthropic=anthropic</code>
            </div>
            <KeyValueTextarea
              v-model="cfg.provider_mapping"
              placeholder="OpenAI=openai&#10;Anthropic=anthropic"
            />
            <div class="qai-oc__count">
              {{ t("openCode.config.providerMappingCount", { n: Object.keys(cfg.provider_mapping || {}).length }) }}
              <span
                v-if="Object.keys(cfg.provider_mapping || {}).length > 0"
                class="qai-oc__count-keys"
              >
                ({{ Object.entries(cfg.provider_mapping || {}).map(([k, v]) => k + "→" + v).join(", ") }})
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ Save bar ═══ -->
      <div class="config-save-bar">
        <button
          type="button"
          class="btn btn-primary"
          data-testid="oc-config-save-btn"
          :disabled="saving"
          @click="saveConfig"
        >
          {{ saving ? t("common.saving", "Saving...") : "💾 " + t("aiCoding.config.saveSettings", "Save Settings") }}
        </button>
        <button
          type="button"
          class="btn btn-ghost"
          :disabled="loading"
          @click="loadConfig"
        >
          ↺ {{ t("common.reset", "Reset") }}
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.oc-perm-json {
  width: 100%;
  min-height: 100px;
  font-family: var(--font-mono, monospace);
  font-size: var(--text-sm);
  resize: vertical;
}
.oc-perm-json__error {
  margin-top: 4px;
  font-size: var(--text-sm);
  color: var(--danger, #f87171);
}
.qai-oc__title {
  margin: 0 0 12px;
  font-size: var(--text-md, 1rem);
}
.qai-oc__loading {
  color: var(--text-muted);
  padding: 24px;
}
.qai-oc__row {
  /* V1 parity (OpenCodeConfigPanel.js): enable/inline rows are horizontal,
     left-aligned flex rows. The global `.config-field` sets
     `flex-direction: column`, so we must explicitly reset to `row` here,
     otherwise the row stacks vertically / drifts centre. */
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
/* V1 parity (OpenCodeConfigPanel.js): inline toggle-row labels reserve a fixed
   min-width so the toggles line up in a column. */
.qai-oc__row .config-label {
  min-width: 120px;
}
/* V1 parity: service start/stop buttons keep a constant width so the control
   does not jump when toggling between ▶ Start and ⏹ Stop. */
.qai-oc__svc-btn {
  min-width: 72px;
}
.qai-oc__status {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: var(--text-sm, 0.85rem);
}
.qai-oc__tag {
  font-size: var(--text-xs, 0.7rem);
  background: rgba(251, 191, 36, 0.15);
  color: #fbbf24;
  padding: 1px 5px;
  border-radius: 3px;
  margin: 0 4px;
}
.qai-oc__warn {
  margin-top: 10px;
  padding: 10px 12px;
  background: rgba(251, 191, 36, 0.08);
  border: 1px solid rgba(251, 191, 36, 0.3);
  border-radius: 8px;
  font-size: var(--text-sm, 0.85rem);
  color: #fbbf24;
}
.qai-oc__models {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.qai-oc__model {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border, #2a3a4a);
  border-radius: 6px;
  cursor: pointer;
}
.qai-oc__model--active {
  border-color: var(--accent);
  background: var(--accent-light);
}
.qai-oc__model-toggle {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 5px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
}
/* V1 parity (OpenCodeConfigPanel.js:643/647): small dimmed ▼/▲ arrow prefix. */
.qai-oc__model-toggle-arrow {
  font-size: var(--text-xs);
  opacity: 0.7;
}
.qai-oc__secret-tag {
  font-size: var(--text-xs, 0.7rem);
  background: rgba(248, 113, 113, 0.15);
  color: #f87171;
  padding: 0 6px;
  border-radius: 3px;
  margin-left: 6px;
}
/* ── Built-in / cloud list info bar (V1 parity) ── */
.qai-oc__listbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 8px 0 10px;
  padding: 7px 10px;
  border-radius: 6px;
  background: rgba(126, 184, 247, 0.06);
  border: 1px solid rgba(126, 184, 247, 0.18);
  font-size: var(--text-xs, 0.7rem);
}
.qai-oc__listbar-badge {
  color: #7eb8f7;
  background: rgba(126, 184, 247, 0.15);
  padding: 1px 7px;
  border-radius: 3px;
  font-weight: 600;
  flex-shrink: 0;
}
.qai-oc__listbar-hint {
  color: var(--text-muted);
  flex: 1;
  min-width: 0;
}
.qai-oc__listbar-go {
  flex-shrink: 0;
  white-space: nowrap;
  color: #7eb8f7;
  border: 1px solid rgba(126, 184, 247, 0.35);
}
/* ── Tool permission legend / summary / order (V1 parity) ── */
.qai-oc__legend {
  line-height: 1.7;
}
.qai-oc__legend-item {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  margin: 0 4px;
}
.qai-oc__legend-swatch {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  border: 1px solid;
}
.qai-oc__perm-summary {
  margin-top: 10px;
  font-size: var(--text-sm, 0.85rem);
  color: var(--text-muted);
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.qai-oc__perm-order {
  margin-top: 4px;
  font-size: var(--text-xs, 0.7rem);
  color: var(--text-muted);
}
.qai-oc__warn-text {
  margin-top: 4px;
  color: #f87171;
}
.qai-oc__ok-text {
  margin-top: 4px;
  color: #4ade80;
}
/* ── Inline yellow warning (V1 parity: password warning span) ── */
.qai-oc__warning-text {
  color: #fbbf24;
}
/* ── HTTP env badge: configured base_url next to "Connected" ── */
.oc-env-baseurl {
  margin-left: 6px;
  font-size: var(--text-xs, 0.7rem);
  color: var(--text-muted);
  font-family: var(--font-mono, monospace);
}
/* ── provider_mapping count + currently-configured pairs (V1 parity) ── */
.qai-oc__count {
  margin-top: 4px;
  font-size: var(--text-xs, 0.7rem);
  color: var(--text-muted);
}
.qai-oc__count-keys {
  margin-left: 6px;
  color: var(--accent, #7eb8f7);
}
</style>
