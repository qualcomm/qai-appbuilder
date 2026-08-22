<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ClaudeCodeConfigPanel — full V1-parity Claude Code configuration.
 *
 * Backend contract (interfaces/http/routes/ai_coding.py):
 *   GET  /api/cc/config       → { config: {...} }  (free dict, whitelist-filtered on save)
 *   PUT  /api/cc/config       ← { config: {...} }  (non-sensitive keys only)
 *   GET  /api/cc/credentials  → { credentials: { VAR: {in_store,in_env,configured} } }
 *   POST /api/cc/credentials  ← { credentials: { VAR: value } }  (empty=delete, ****=skip)
 *   DELETE /api/cc/credentials/{var}
 *   GET  /api/cc/health       → { provider, available, available_providers, providers[], models[] }
 *
 * Sensitive credential values flow ONLY through the credentials
 * endpoints (SecretStore); non-sensitive auth env vars are stored in
 * config.auth_env. There is NO temperature/max_tokens — those were
 * never backend fields (removed). There is NO /api/cc/models route in
 * V2: the model catalog is folded into health.models[].
 */
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  fetchCcConfig,
  updateCcConfig,
} from "@/api";
import { useToastStore } from "@/stores/toast";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { useCcAuth } from "@/composables/useCcAuth";
import { useCcModels } from "@/composables/useCcModels";
import { useCcAgents, type CcAgent } from "@/composables/useCcAgents";
import WorkingDirsTextarea from "./config/WorkingDirsTextarea.vue";
import KeyValueTextarea from "./config/KeyValueTextarea.vue";
import ToolPermissionMatrix from "./config/ToolPermissionMatrix.vue";

const { t } = useI18n();
const toast = useToastStore();
// Pill gate lives in forge-config (`ai_coding.cc.enabled`), read by
// ChatComposer via the same `useForgeConfig` singleton. The CC "Enable"
// toggle below edits `ai_coding.config.enabled`; on save we mirror it into
// forge-config so the chat-input pill reflects the choice (V1 parity: the
// CC config "enabled" checkbox and the pill are the same flag).
const { patchAiCoding, load: loadForgeConfig } = useForgeConfig();

function pushToast(kind: "success" | "error" | "info", message: string): void {
  toast.push({ id: crypto.randomUUID(), kind, message, timeoutMs: kind === "error" ? 5000 : 3000 });
}

// ─── Sub-agent shape — exported from useCcAgents composable ──────────────────

// ─── Config model (real backend whitelist fields only) ─────────────────────
interface CcConfig {
  enabled: boolean;
  model: string;
  permission_mode: string;
  allowed_tools: string[];
  disallowed_tools: string[];
  auth_env: Record<string, string>;
  allowed_working_dirs: string[];
  add_dirs: string[];
  effort: string | null;
  thinking: unknown;
  enable_file_checkpointing: boolean;
  agents: Record<string, CcAgent>;
  include_partial_messages: boolean;
  betas: string[];
  max_turns: number;
  session_idle_timeout_minutes: number;
  pending_message_ttl_seconds: number;
  message_timeout_seconds: number;
  permission_approval_timeout_seconds: number;
  system_prompt: string;
  cli_path: string;
  session_env: Record<string, string>;
  tool_catalog: { id: string; desc?: string }[];
}

const cfg = reactive<CcConfig>({
  enabled: false,
  model: "claude-sonnet-4-6",
  permission_mode: "dontAsk",
  allowed_tools: [],
  disallowed_tools: [],
  auth_env: {},
  allowed_working_dirs: [],
  add_dirs: [],
  effort: null,
  thinking: null,
  enable_file_checkpointing: false,
  agents: {},
  include_partial_messages: false,
  betas: [],
  max_turns: 20,
  session_idle_timeout_minutes: 0,
  pending_message_ttl_seconds: 600,
  message_timeout_seconds: 3600,
  permission_approval_timeout_seconds: 120,
  system_prompt: "",
  cli_path: "",
  session_env: {},
  tool_catalog: [],
});

const loading = ref(false);
const saving = ref(false);

// ─── Health + model list (V1 parity) ────────────────────────────────────────
// Owned by `useCcModels` composable: checkHealth → health → modelOptions →
// displayedModels / modelSourceBadge. Destructured BEFORE useCcAuth so the
// latter's onAfterMutation closure can capture `checkHealth`.
const {
  health,
  healthLoading,
  checkHealth,
  modelOptions,
  MODELS_COLLAPSED_LIMIT,
  modelsExpanded,
  displayedModels,
  hiddenModelCount,
  modelSourceBadge,
} = useCcModels({ cfg });

// ─── Model list collapse scroll-back (V1 parity) ────────────────────────────
// V1 parity (ClaudeCodeConfigPanel.js:221-243 toggleModelsExpanded): when the
// user *collapses* a long model list, scroll the viewport back to the top of
// the model area so their gaze does not get lost on the now-shorter list
// (expanding does not scroll). The anchor is the model-source status row above
// the radio list (V1 bound `modelsAnchorEl` to the same status div).
const modelsAnchorEl = ref<HTMLElement | null>(null);
function collapseModels(): void {
  modelsExpanded.value = false;
  // DOM shrink completes next frame, so scroll must wait for nextTick.
  void nextTick(() => {
    modelsAnchorEl.value?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

// ─── Accordion collapse ────────────────────────────────────────────────────
const collapsed = reactive(new Set<string>());
function toggleGroup(g: string): void {
  if (collapsed.has(g)) collapsed.delete(g);
  else collapsed.add(g);
}

// ─── Tool catalog (single source: config.tool_catalog, else fallback) ───────
const FALLBACK_TOOLS = [
  "Read", "Glob", "Grep", "Edit", "MultiEdit", "Write", "LS",
  "Bash", "WebFetch", "WebSearch", "Agent", "Skill", "Task",
];
const tools = computed<{ id: string; desc?: string }[]>(() => {
  if (cfg.tool_catalog.length > 0) {
    return cfg.tool_catalog.filter((x) => typeof x.id === "string").map((x) => ({ id: x.id, desc: x.desc ?? x.id }));
  }
  return FALLBACK_TOOLS.map((id) => ({ id }));
});

// Three-state matrix for CC: default → allowed → disallowed.
const CC_STATES = [
  { id: "default", label: t("aiCoding.config.toolStateDefault", "default"), icon: "○", color: "var(--text-muted)" },
  { id: "allowed", label: t("aiCoding.config.toolStateAllowed", "allowed"), icon: "✓", color: "#4ade80" },
  { id: "disallowed", label: t("aiCoding.config.toolStateBlocked", "blocked"), icon: "✕", color: "#f87171" },
];
function toolStateOf(toolId: string): string {
  if (cfg.allowed_tools.includes(toolId)) return "allowed";
  if (cfg.disallowed_tools.includes(toolId)) return "disallowed";
  return "default";
}
function onToolCycle(toolId: string, next: string): void {
  cfg.allowed_tools = cfg.allowed_tools.filter((x) => x !== toolId);
  cfg.disallowed_tools = cfg.disallowed_tools.filter((x) => x !== toolId);
  if (next === "allowed") cfg.allowed_tools = [...cfg.allowed_tools, toolId];
  else if (next === "disallowed") cfg.disallowed_tools = [...cfg.disallowed_tools, toolId];
}


// ─── Authentication schemes + credential management ──────────────────────────
// Extracted to `useCcAuth` composable (cohesion split). The composable owns
// the AUTH_SCHEMES table, the per-scheme localStorage persistence, the
// collapsed/expanded list view, the unified `varInputs` / `credStatus`
// state, and the load/save/remove credential actions. The host stays
// responsible for `cfg` (reactive) and `checkHealth` (mutation hook).
const {
  AUTH_SCHEMES,
  AUTH_SCHEMES_COLLAPSED_LIMIT,
  authScheme,
  selectScheme,
  currentScheme,
  authSchemesExpanded,
  displayedAuthSchemes,
  authSchemesHiddenCount,
  varInputs,
  credStatus,
  credConfigured,
  credEnvOnly,
  credSaving,
  loadCredentials,
  saveCredentials,
  removeCredential,
  hydrateAuthEnvDefaults,
} = useCcAuth({ cfg, pushToast, onAfterMutation: () => checkHealth() });
void credStatus;
void AUTH_SCHEMES_COLLAPSED_LIMIT;

// ─── Sub-agent editor (extracted to useCcAgents composable, cohesion split) ──
// Pure local-state machine — saves go through the host's `saveConfig` PUT.
const {
  editingAgent,
  editingAgentName,
  agentFormError,
  agentNames,
  openNewAgent,
  openEditAgent,
  saveAgent,
  deleteAgent,
  cancelAgent,
} = useCcAgents({ cfg });

// ─── Beta feature catalog (known CC betas) ──────────────────────────────────
const BETA_OPTIONS = ["context-1m-2025-08-07"];
function toggleBeta(b: string): void {
  if (cfg.betas.includes(b)) cfg.betas = cfg.betas.filter((x) => x !== b);
  else cfg.betas = [...cfg.betas, b];
}

const EFFORT_OPTIONS: { value: string | null; label: string }[] = [
  { value: null, label: t("aiCoding.config.effortDefault", "Default (CLI decides)") },
  { value: "low", label: t("aiCoding.config.effortLow", "Low") },
  { value: "medium", label: t("aiCoding.config.effortMedium", "Medium") },
  { value: "high", label: t("aiCoding.config.effortHigh", "High") },
  { value: "max", label: t("aiCoding.config.effortMax", "Max") },
];


// ─── Load ───────────────────────────────────────────────────────────────────
async function loadConfig(): Promise<void> {
  loading.value = true;
  try {
    const res = await fetchCcConfig();
    const c = res.config as Record<string, unknown>;
    if (typeof c.enabled === "boolean") cfg.enabled = c.enabled;
    if (typeof c.model === "string") cfg.model = c.model;
    if (typeof c.permission_mode === "string") cfg.permission_mode = c.permission_mode;
    if (Array.isArray(c.allowed_tools)) cfg.allowed_tools = c.allowed_tools as string[];
    if (Array.isArray(c.disallowed_tools)) cfg.disallowed_tools = c.disallowed_tools as string[];
    if (c.auth_env && typeof c.auth_env === "object") cfg.auth_env = c.auth_env as Record<string, string>;
    if (Array.isArray(c.allowed_working_dirs)) cfg.allowed_working_dirs = c.allowed_working_dirs as string[];
    if (Array.isArray(c.add_dirs)) cfg.add_dirs = c.add_dirs as string[];
    cfg.effort = typeof c.effort === "string" ? c.effort : null;
    cfg.thinking = c.thinking ?? null;
    cfg.enable_file_checkpointing = Boolean(c.enable_file_checkpointing);
    if (c.agents && typeof c.agents === "object") cfg.agents = c.agents as Record<string, CcAgent>;
    cfg.include_partial_messages = Boolean(c.include_partial_messages);
    if (Array.isArray(c.betas)) cfg.betas = c.betas as string[];
    if (typeof c.max_turns === "number") cfg.max_turns = c.max_turns;
    if (typeof c.session_idle_timeout_minutes === "number") cfg.session_idle_timeout_minutes = c.session_idle_timeout_minutes;
    if (typeof c.pending_message_ttl_seconds === "number") cfg.pending_message_ttl_seconds = c.pending_message_ttl_seconds;
    if (typeof c.message_timeout_seconds === "number") cfg.message_timeout_seconds = c.message_timeout_seconds;
    if (typeof c.permission_approval_timeout_seconds === "number") cfg.permission_approval_timeout_seconds = c.permission_approval_timeout_seconds;
    if (typeof c.system_prompt === "string") cfg.system_prompt = c.system_prompt;
    if (typeof c.cli_path === "string") cfg.cli_path = c.cli_path;
    if (c.session_env && typeof c.session_env === "object") cfg.session_env = c.session_env as Record<string, string>;
    if (Array.isArray(c.tool_catalog)) cfg.tool_catalog = c.tool_catalog as { id: string; desc?: string }[];
    // Hydrate auth_env values into var inputs (V1 parity, owned by useCcAuth).
    hydrateAuthEnvDefaults();
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
    const payload: Record<string, unknown> = {
      enabled: cfg.enabled,
      model: cfg.model,
      permission_mode: cfg.permission_mode,
      allowed_tools: cfg.allowed_tools,
      disallowed_tools: cfg.disallowed_tools,
      allowed_working_dirs: cfg.allowed_working_dirs,
      add_dirs: cfg.add_dirs,
      effort: cfg.effort,
      thinking: cfg.thinking,
      enable_file_checkpointing: cfg.enable_file_checkpointing,
      agents: cfg.agents,
      include_partial_messages: cfg.include_partial_messages,
      betas: cfg.betas,
      max_turns: cfg.max_turns,
      session_idle_timeout_minutes: cfg.session_idle_timeout_minutes,
      pending_message_ttl_seconds: cfg.pending_message_ttl_seconds,
      message_timeout_seconds: cfg.message_timeout_seconds,
      permission_approval_timeout_seconds: cfg.permission_approval_timeout_seconds,
      system_prompt: cfg.system_prompt,
      cli_path: cfg.cli_path,
      session_env: cfg.session_env,
    };
    await updateCcConfig(payload);
    // Mirror the "Enable Claude Code" toggle into forge-config so the
    // chat-input CC pill reflects the choice (V1 parity: the config
    // "enabled" checkbox and the pill share one flag). Fires only on
    // explicit user save — no reactive watcher — so there is no loop.
    // `patchAiCoding` persists via POST /api/forge-config; `loadForgeConfig`
    // then refreshes the shared singleton (V1 "loadForgeConfig after save").
    await patchAiCoding("cc", { enabled: cfg.enabled });
    await loadForgeConfig();
    pushToast("success", t("aiCoding.config.configSaved", "Claude Code configuration saved"));
    await checkHealth();
  } catch (e) {
    pushToast("error", t("aiCoding.config.saveFailed", "Failed to save: ") + (e as Error).message);
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  void loadConfig().then(loadCredentials);
  void checkHealth();
});
</script>

<template>
  <div
    class="config-section"
    data-testid="claude-code-config-panel"
  >
    <h3 class="qai-cc__title">
      {{ t("aiCoding.config.ccTitle", "Claude Code Configuration") }}
    </h3>

    <div
      v-if="loading"
      class="qai-cc__loading"
      data-testid="cc-config-loading"
    >
      {{ t("common.loading", "Loading...") }}
    </div>

    <template v-else>
      <!-- ═══ 🩺 Environment Status ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          style="cursor: default;"
        >
          <span>🩺 {{ t("aiCoding.config.envStatus", "Environment Status") }}</span>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            style="margin-left: auto;"
            data-testid="cc-health-refresh"
            :disabled="healthLoading"
            @click="checkHealth(true)"
          >
            {{ healthLoading ? t("common.loading", "Loading...") : "🔄 " + t("common.refresh", "Refresh") }}
          </button>
        </div>
        <div class="config-group-body">
          <div
            v-if="health"
            class="qai-cc__health"
          >
            <span
              class="qai-cc__badge"
              :class="health.available ? 'qai-cc__badge--ok' : 'qai-cc__badge--err'"
            >
              {{ health.available ? t("aiCoding.config.available", "Available") : t("aiCoding.config.unavailable", "Unavailable") }}
            </span>
            <span>{{ t("aiCoding.config.provider", "Provider") }}: <code>{{ health.provider }}</code></span>
            <span v-if="health.available_providers?.length">
              {{ t("aiCoding.config.providers", "Providers") }}: {{ health.available_providers.join(", ") }}
            </span>
            <span>{{ t("aiCoding.config.modelsCount", "Models") }}: {{ health.models?.length ?? 0 }}</span>
            <span v-if="health.sdk_available !== undefined">
              {{ t("aiCoding.config.sdk", "SDK") }}:
              <span :style="{ color: health.sdk_available ? '#4ade80' : '#f87171' }">{{ health.sdk_available ? "✅ " + (health.sdk_version || "") : "❌" }}</span>
            </span>
            <span v-if="health.auth_configured !== undefined">
              {{ t("aiCoding.config.auth", "Auth") }}:
              <span :style="{ color: health.auth_configured ? '#4ade80' : '#f87171' }">{{ health.auth_configured ? "✅ " + (health.auth_source || "") : "❌" }}</span>
            </span>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:920): the env-status bar
                 shows the enabled flag between Auth and Sessions. V2 health
                 has no `enabled` field (see types/api.ts HealthResponse), so
                 mirror the OC panel and read the reactive `cfg.enabled`
                 (OpenCodeConfigPanel.vue:353-356). -->
            <span>
              {{ t("aiCoding.config.enabledStatus", "Enabled") }}:
              <span :style="{ color: cfg.enabled ? '#4ade80' : 'var(--text-muted)' }">{{ cfg.enabled ? "✅ " + t("common.yes", "Yes") : "⚪ " + t("common.no", "No") }}</span>
            </span>
            <span v-if="health.active_sessions !== undefined">
              {{ t("aiCoding.config.sessions", "Sessions") }}: {{ health.active_sessions }} {{ t("aiCoding.config.sessionsActive", "active") }} · {{ health.total_sessions }} {{ t("aiCoding.config.sessionsTotal", "total") }}
            </span>
          </div>
          <div
            v-else
            class="config-comment"
          >
            {{ t("aiCoding.config.healthUnavailable", "Health information unavailable.") }}
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
          <div class="config-field qai-cc__row">
            <label
              class="config-label"
              style="margin-bottom: 0;"
            >{{ t("aiCoding.config.enable", "Enable Claude Code") }}</label>
            <label class="toggle">
              <input
                v-model="cfg.enabled"
                type="checkbox"
                data-testid="cc-enabled"
              />
              <span class="toggle-slider"></span>
            </label>
            <span
              class="config-comment"
              style="margin: 0;"
            >{{ cfg.enabled ? t("common.enabled", "Enabled") : t("common.disabled", "Disabled") }}</span>
          </div>

          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.model", "Model") }}</label>
            <!-- Model source badge (V1 parity): 4-state source + base_url +
                 independent 🔄 refresh that re-enumerates upstream /v1/models. -->
            <div
              ref="modelsAnchorEl"
              class="cc-models-source"
              data-testid="cc-models-source"
            >
              <span
                v-if="healthLoading"
                class="cc-models-badge"
                :title="t('claudeCode.config.modelsRefreshTitle', 'Force re-fetch upstream /v1/models (bypassing the 5-minute cache)')"
              >⏳ {{ t("claudeCode.config.modelsLoading", "Fetching model list…") }}</span>
              <span
                v-else-if="modelSourceBadge"
                class="cc-models-badge"
                :style="{ color: modelSourceBadge.color, background: modelSourceBadge.bg }"
                :title="modelSourceBadge.title"
                data-testid="cc-models-badge"
              >{{ modelSourceBadge.icon }} {{ modelSourceBadge.label }}</span>
              <span
                v-if="!healthLoading && health?.models_base_url"
                class="cc-models-baseurl"
                :title="health.models_base_url"
              >🌐 <code>{{ health.models_base_url }}</code>
                <span
                  v-if="health.models_base_url_source"
                  class="cc-models-baseurl-src"
                >({{ health.models_base_url_source }})</span>
              </span>
              <button
                type="button"
                class="btn btn-ghost btn-sm cc-models-refresh"
                data-testid="cc-models-refresh"
                :disabled="healthLoading"
                :title="t('claudeCode.config.modelsRefreshTitle', 'Force re-fetch upstream /v1/models (bypassing the 5-minute cache)')"
                @click="checkHealth(true)"
              >
                🔄 {{ t("claudeCode.config.modelsRefresh", "Refresh") }}
              </button>
            </div>
            <!-- Error detail row (V1 parity): only on fallback-error. -->
            <div
              v-if="!healthLoading && modelSourceBadge?.source === 'fallback-error' && health?.models_error"
              class="config-comment cc-models-error"
            >
              {{ health.models_error }}
            </div>
            <!-- Radio list (V1 parity): friendly per-model rows with a
                 "current" badge + collapse, replacing the plain <select>. -->
            <div
              v-if="modelOptions.length"
              class="cc-model-radios"
              data-testid="cc-config-model"
            >
              <label
                v-for="m in displayedModels"
                :key="m.id"
                class="cc-model-radio"
                :class="{ 'cc-model-radio--active': cfg.model === m.id }"
                :title="`${m.label} · ${m.id}`"
              >
                <input
                  v-model="cfg.model"
                  type="radio"
                  :value="m.id"
                  name="cc-model"
                />
                <span class="cc-model-radio__label">{{ m.label }}</span>
                <span
                  v-if="cfg.model === m.id"
                  class="cc-model-radio__badge"
                >{{ t("claudeCode.config.modelSelectedBadge", "current") }}</span>
              </label>
              <button
                v-if="hiddenModelCount > 0 && !modelsExpanded"
                type="button"
                class="cc-model-toggle"
                :title="t('claudeCode.config.modelsExpandTitle', 'Show all models')"
                @click="modelsExpanded = true"
              >
                <span class="cc-model-toggle__arrow">▼</span>
                {{ t("claudeCode.config.modelsExpand", { n: hiddenModelCount }) }}
              </button>
              <button
                v-else-if="modelsExpanded && modelOptions.length > MODELS_COLLAPSED_LIMIT"
                type="button"
                class="cc-model-toggle"
                :title="t('claudeCode.config.modelsCollapseTitle', 'Show only the top few plus current selection')"
                @click="collapseModels"
              >
                <span class="cc-model-toggle__arrow">▲</span>
                {{ t("claudeCode.config.modelsCollapse", "Show less") }}
              </button>
            </div>
            <input
              v-else
              v-model="cfg.model"
              type="text"
              class="config-input"
              data-testid="cc-config-model"
              placeholder="claude-sonnet-4-6"
              style="max-width: 360px;"
            />
            <div
              v-if="cfg.model"
              class="config-comment cc-model-current"
            >
              {{ t("claudeCode.config.modelCurrent", "Current: ") }}<code>{{ cfg.model }}</code>
            </div>
            <div class="config-comment">
              {{ t("aiCoding.config.modelDesc", "Model list comes from the provider health response.") }}
            </div>
          </div>

          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.permissionMode", "Permission Mode") }}</label>
            <select
              v-model="cfg.permission_mode"
              class="config-input"
              data-testid="cc-permission-mode"
              style="max-width: 220px;"
            >
              <option value="dontAsk">
                {{ t("aiCoding.config.permDontAsk", "Don't Ask") }}
              </option>
              <option value="default">
                {{ t("aiCoding.config.permDefault", "Default") }}
              </option>
              <option value="acceptEdits">
                {{ t("aiCoding.config.permAcceptEdits", "Accept Edits") }}
              </option>
            </select>
          </div>

          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.toolPermissions", "Tool Permissions") }}</label>
            <div class="config-comment">
              {{ t("aiCoding.config.toolPermHint", "Click a tool to cycle: default → allowed → blocked.") }}
            </div>
            <!-- Three-state legend (V1 parity: inline horizontal flow,
                 semi-transparent swatches with borders). -->
            <div class="cc-tool-legend">
              <span class="cc-tool-legend__item">
                <span
                  class="cc-tool-legend__swatch"
                  style="background: rgba(74,222,128,0.25); border-color: #4ade80;"
                ></span>
                <strong style="color: #4ade80;">{{ t("claudeCode.config.toolPermAutoLabel", "Auto-approved") }}</strong>
              </span>{{ t("claudeCode.config.toolPermAutoDesc", ": Auto-approved without confirmation;") }}
              <span class="cc-tool-legend__item">
                <span
                  class="cc-tool-legend__swatch"
                  style="background: rgba(248,113,113,0.25); border-color: #f87171;"
                ></span>
                <strong style="color: #f87171;">{{ t("claudeCode.config.toolPermBlockedLabel", "Blocked") }}</strong>
              </span>{{ t("claudeCode.config.toolPermBlockedDesc", ": Always denied (highest priority);") }}
              <span class="cc-tool-legend__item">
                <span
                  class="cc-tool-legend__swatch"
                  style="background: var(--bg-tertiary, #1a2a3a); border-color: var(--border, #2a3a4a);"
                ></span>
                <strong style="color: var(--text-muted);">{{ t("claudeCode.config.toolPermDefaultLabel", "Default") }}</strong>
              </span>{{ t("claudeCode.config.toolPermDefaultDesc", ": Use Permission Mode flow.") }}
            </div>
            <ToolPermissionMatrix
              :tools="tools"
              :states="CC_STATES"
              :state-of="toolStateOf"
              @cycle="onToolCycle"
            />
            <!-- Summary rows (V1 parity). -->
            <div class="cc-tool-summary">
              <div>
                <strong>{{ t("claudeCode.config.toolPermAutoSummary", "Auto-approved:") }}</strong>
                {{ cfg.allowed_tools.length ? cfg.allowed_tools.join(", ") : t("claudeCode.config.toolPermNone", "(none)") }}
              </div>
              <div>
                <strong>{{ t("claudeCode.config.toolPermBlockedSummary", "Blocked:") }}</strong>
                {{ cfg.disallowed_tools.length ? cfg.disallowed_tools.join(", ") : t("claudeCode.config.toolPermNone", "(none)") }}
              </div>
            </div>
            <div class="config-comment cc-tool-clickorder">
              💡 {{ t("claudeCode.config.toolPermClickOrder", "Click order") }}: Default → Auto-approved → Blocked → Default
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ 🔐 Authentication ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('auth')"
        >
          <span>🔐 {{ t("aiCoding.config.authentication", "Authentication") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsed.has('auth') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsed.has('auth')"
          class="config-group-body"
        >
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.authScheme", "Authentication Scheme") }}</label>
            <div class="qai-cc__schemes">
              <label
                v-for="s in displayedAuthSchemes"
                :key="s.id"
                class="qai-cc__scheme"
                :class="{ 'qai-cc__scheme--active': authScheme === s.id }"
                @click="selectScheme(s.id)"
              >
                <input
                  type="radio"
                  :value="s.id"
                  :checked="authScheme === s.id"
                  @change="selectScheme(s.id)"
                />
                <span>
                  <strong>{{ s.label }}</strong>
                  <span
                    class="config-comment"
                    style="display: block; margin: 0;"
                  >{{ s.desc }}</span>
                </span>
              </label>
              <!-- V1 parity: default collapsed; "Show more (N)" / "Show less"
                   toggle (mirrors the model list collapse). -->
              <button
                v-if="!authSchemesExpanded && authSchemesHiddenCount > 0"
                type="button"
                class="cc-model-toggle"
                @click="authSchemesExpanded = true"
              >
                {{ t("claudeCode.config.authSchemesExpand", { n: authSchemesHiddenCount }) }}
              </button>
              <button
                v-else-if="authSchemesExpanded && AUTH_SCHEMES.length > AUTH_SCHEMES_COLLAPSED_LIMIT"
                type="button"
                class="cc-model-toggle"
                @click="authSchemesExpanded = false"
              >
                {{ t("claudeCode.config.authSchemesCollapse", "Show less") }}
              </button>
            </div>
          </div>

          <div
            v-for="v in currentScheme.vars"
            :key="v.name"
            class="config-field"
          >
            <label class="config-label cc-var-label">
              <code>{{ v.name }}</code>
              <!-- required / optional badge (V1 parity) -->
              <span
                v-if="(v.required ?? v.secret)"
                class="cc-var-badge cc-var-badge--required"
              >{{ t("claudeCode.config.varRequired", "required") }}</span>
              <span
                v-else
                class="cc-var-badge cc-var-badge--optional"
              >{{ t("claudeCode.config.varOptional", "optional") }}</span>
              <!-- source badge (V1 parity): secret → Credential Manager; else forge_config -->
              <span
                v-if="v.secret"
                class="cc-var-badge cc-var-badge--cred"
              >{{ t("claudeCode.config.varCredManager", "Credential Manager") }}</span>
              <span
                v-else
                class="cc-var-badge cc-var-badge--forge"
              >{{ t("claudeCode.config.varForgeConfig", "forge_config.json") }}</span>
              <!-- credential status (secret only, right-aligned) -->
              <span
                v-if="v.secret"
                class="cc-var-status"
                :class="credConfigured(v.name) ? (credEnvOnly(v.name) ? 'cc-var-status--env' : 'cc-var-status--store') : 'cc-var-status--unset'"
              >{{ credConfigured(v.name) ? (credEnvOnly(v.name) ? t("claudeCode.config.credInEnv", "Env Var (system)") : t("claudeCode.config.credInStore", "Credential Manager")) : t("claudeCode.config.credNotSet", "Not set") }}</span>
            </label>
            <div
              v-if="v.hint"
              class="config-comment cc-var-hint"
            >
              {{ v.hint }}
            </div>
            <div class="qai-cc__row">
              <input
                v-model="varInputs[v.name]"
                :type="v.secret ? 'password' : 'text'"
                class="config-input mono"
                :placeholder="v.secret && credConfigured(v.name) ? t('claudeCode.config.secretConfiguredPlaceholder', 'Configured (enter a new value to override, leave empty to keep)') : v.placeholder"
                autocomplete="off"
                style="flex: 1;"
              />
              <button
                v-if="v.secret && credConfigured(v.name)"
                type="button"
                class="btn btn-ghost btn-sm"
                @click="removeCredential(v.name)"
              >
                {{ t("aiCoding.config.delete", "Delete") }}
              </button>
            </div>
            <!-- env-only warning (V1 parity): credential lives only in a
                 system env var, not yet persisted to secure storage. -->
            <div
              v-if="v.secret && credConfigured(v.name) && credEnvOnly(v.name)"
              class="config-comment cc-var-envonly"
            >
              {{ t("claudeCode.config.envOnlyHint", "Configured via system environment variable. Enter a value below and save to persist to secure storage.") }}
            </div>
          </div>

          <div class="config-field">
            <button
              type="button"
              class="btn btn-primary btn-sm"
              data-testid="cc-save-auth"
              :disabled="credSaving"
              @click="saveCredentials"
            >
              {{ credSaving ? t("common.saving", "Saving...") : t("aiCoding.config.saveAuth", "Save Authentication") }}
            </button>
            <!-- V1 parity (ClaudeCodeConfigPanel.js storage hint): credentials
                 → OS keyring (Credential Manager); other vars → forge_config.json. -->
            <p class="cc-storage-hint">
              {{ t("claudeCode.config.credStorageHint", "🔒 Sensitive → Credential Manager | 📄 Others → forge_config.json") }}
            </p>
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
            <label class="config-label">{{ t("aiCoding.config.maxTurns", "Max Turns") }}</label>
            <div class="config-comment">
              {{ t("claudeCode.config.maxTurnsDesc", "Max tool-call turns per conversation. Prevents infinite loops.") }}
            </div>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1287): max=100, not 200. -->
            <input
              v-model.number="cfg.max_turns"
              type="number"
              min="1"
              max="100"
              class="config-input"
              style="max-width: 120px;"
            />
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.sessionIdleTimeout", "Session Idle Timeout (min)") }}</label>
            <div class="config-comment">
              {{ t("claudeCode.config.sessionIdleTimeoutDesc", "Sessions idle longer than this are auto-closed. Set to 0 to disable.") }}
            </div>
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
            <label class="config-label">{{ t("aiCoding.config.pendingTtl", "Pending Message TTL (sec)") }}</label>
            <div class="config-comment">
              {{ t("claudeCode.config.pendingMsgTtlDesc", "POST /messages payloads not consumed within this TTL are cleaned up.") }}
            </div>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1299): 60-3600, not 0-86400. -->
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
            <label class="config-label">{{ t("aiCoding.config.messageTimeout", "Message Timeout (sec)") }}</label>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1305): min=60, not 0. -->
            <input
              v-model.number="cfg.message_timeout_seconds"
              type="number"
              min="60"
              max="86400"
              class="config-input"
              style="max-width: 120px;"
            />
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1307-1312): short → red warn,
                 long (≥600s) → green OK hint. -->
            <div
              v-if="cfg.message_timeout_seconds < 120"
              class="config-comment cc-warn"
            >
              {{ t("claudeCode.config.msgTimeoutShortWarn", "Very short timeout — complex tasks may fail frequently") }}
            </div>
            <div
              v-else-if="cfg.message_timeout_seconds >= 600"
              class="config-comment cc-ok"
            >
              {{ t("claudeCode.config.msgTimeoutLongOk", "Recommended for long-running tasks (>= 600s)") }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.approvalTimeout", "Permission Approval Timeout (sec)") }}</label>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1315-1329): the approval
                 timeout only governs the Phase-2 user-approval dialog, which
                 exists for every mode EXCEPT dontAsk. So it is disabled ONLY
                 when permission_mode === 'dontAsk' (not for acceptEdits). -->
            <input
              v-model.number="cfg.permission_approval_timeout_seconds"
              type="number"
              min="10"
              max="600"
              class="config-input"
              style="max-width: 120px;"
              :disabled="cfg.permission_mode === 'dontAsk'"
            />
            <div
              v-if="cfg.permission_mode === 'dontAsk'"
              class="config-comment"
            >
              {{ t("claudeCode.config.approvalTimeoutDisabledHint", "Only active when Permission Mode = default") }}
            </div>
            <div
              v-else-if="cfg.permission_approval_timeout_seconds < 30"
              class="config-comment cc-warn"
            >
              {{ t("claudeCode.config.approvalTimeoutShortWarn", "Very short — users may not have enough time to review") }}
            </div>
            <div
              v-else
              class="config-comment cc-ok"
            >
              {{ t("claudeCode.config.approvalTimeoutOk", { n: cfg.permission_approval_timeout_seconds }) }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.systemPrompt", "Custom System Prompt") }}</label>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1334-1336): desc + placeholder. -->
            <div class="config-comment">
              {{ t("claudeCode.config.systemPromptDesc", "Override the default Claude Code system prompt. Leave empty to use the default.") }}
            </div>
            <textarea
              v-model="cfg.system_prompt"
              class="config-input"
              :placeholder="t('claudeCode.config.systemPromptPlaceholder', 'Leave empty to use the Claude Code default system prompt')"
              style="min-height: 90px; resize: vertical;"
            ></textarea>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.cliPath", "CLI Path") }}</label>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1340-1342): desc + placeholder. -->
            <div class="config-comment">
              {{ t("claudeCode.config.cliPathDesc", "Custom path to the Claude Code CLI executable. Leave empty to use the bundled CLI.") }}
            </div>
            <input
              v-model="cfg.cli_path"
              type="text"
              class="config-input mono"
              :placeholder="t('claudeCode.config.cliPathPlaceholder', 'Leave empty to use the bundled CLI')"
            />
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.sessionEnv", "Session Environment (KEY=VALUE)") }}</label>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1344-1366): rich
                 description (format hint + scope hint + use case + warning) +
                 a count + currently-configured keys list. -->
            <div class="config-comment">
              {{ t("claudeCode.config.sessionEnvFormatHint", "One per line, format: ") }}
              <code>KEY=VALUE</code>。
              {{ t("claudeCode.config.sessionEnvScopeHint", "These variables are injected only into the Claude Code CLI subprocess; they do not affect the current Python process environment (this is the key difference vs auth_env).") }}
              <br />
              {{ t("claudeCode.config.sessionEnvUseCase", "Typical use: ") }}
              <code>DISABLE_TELEMETRY=1</code>、
              <code>CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1</code>{{ t("claudeCode.config.sessionEnvUseCaseSuffix", " (disable telemetry, speed up CLI startup).") }}
              <br />
              <span class="cc-warning">⚠️ {{ t("claudeCode.config.sessionEnvWarning", "Stored as plain text — do NOT put API keys or other sensitive credentials here (use the \"Authentication\" section above for those).") }}</span>
            </div>
            <KeyValueTextarea
              v-model="cfg.session_env"
              placeholder="KEY=VALUE"
            />
            <div class="cc-session-env-count">
              {{ t("claudeCode.config.sessionEnvCount", { n: Object.keys(cfg.session_env || {}).length }) }}
              <span
                v-if="Object.keys(cfg.session_env || {}).length > 0"
                class="cc-session-env-keys"
              >
                ({{ Object.keys(cfg.session_env || {}).join(", ") }})
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ 📂 Additional Dirs ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('addDirs')"
        >
          <span>📂 {{ t("aiCoding.config.addDirs", "Additional Directories") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsed.has('addDirs') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsed.has('addDirs')"
          class="config-group-body"
        >
          <div class="config-field">
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1383-1392): desc + count
                 of currently-configured dirs + the list itself. -->
            <div class="config-comment">
              {{ t("claudeCode.config.addDirsDesc1", "Additional directories Claude can access beyond the working directory (cwd). One absolute path per line.") }}
              <br />
              {{ t("claudeCode.config.addDirsDesc2", "Typical use: allow Claude to read shared library directories (e.g. ") }}
              <code>C:\Libs\shared</code>{{ t("claudeCode.config.addDirsDesc3", ") or doc directories without changing the working directory.") }}
            </div>
            <WorkingDirsTextarea
              v-model="cfg.add_dirs"
              placeholder="C:\Libs\shared"
            />
            <div class="cc-session-env-count">
              {{ t("claudeCode.config.addDirsCount", { n: cfg.add_dirs.length }) }}
              <span
                v-if="cfg.add_dirs.length > 0"
                class="cc-session-env-keys"
              >
                ({{ cfg.add_dirs.join(", ") }})
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ 🧠 Thinking Mode ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('effort')"
        >
          <span>🧠 {{ t("aiCoding.config.thinkingMode", "Thinking Mode") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsed.has('effort') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsed.has('effort')"
          class="config-group-body"
        >
          <div class="config-field">
            <label class="config-label">{{ t("claudeCode.config.thinkingDepthLabel", "Default Thinking Depth (effort)") }}</label>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1406-1409): two-line
                 description above the select. -->
            <div class="config-comment">
              {{ t("claudeCode.config.effortDesc1", "Controls Claude's default thinking depth. null/default: CLI decides; low: fast response; medium: balanced; high: deep thinking; max: deepest thinking.") }}
              <br />
              💡 {{ t("claudeCode.config.effortDesc2", "Can also be switched on the fly from the Claude Code panel header (session-level override).") }}
            </div>
            <select
              v-model="cfg.effort"
              class="config-input"
              style="max-width: 200px;"
            >
              <option
                v-for="o in EFFORT_OPTIONS"
                :key="String(o.value)"
                :value="o.value"
              >
                {{ o.label }}
              </option>
            </select>
          </div>
        </div>
      </div>

      <!-- ═══ ⏪ File Checkpointing ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('checkpoint')"
        >
          <span>⏪ {{ t("aiCoding.config.fileCheckpointing", "File Checkpointing") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsed.has('checkpoint') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsed.has('checkpoint')"
          class="config-group-body"
        >
          <div class="config-field qai-cc__row">
            <label
              class="config-label"
              style="margin-bottom: 0;"
            >{{ t("aiCoding.config.enableCheckpointing", "Enable File Checkpointing") }}</label>
            <label class="toggle">
              <input
                v-model="cfg.enable_file_checkpointing"
                type="checkbox"
              />
              <span class="toggle-slider"></span>
            </label>
          </div>
          <!-- V1 parity (ClaudeCodeConfigPanel.js:1436-1441): describe the
               feature + a yellow disk-I/O warning. -->
          <div class="config-field">
            <div class="config-comment">
              {{ t("claudeCode.config.checkpointingDesc1", "When enabled, the SDK creates a file checkpoint on every user message. From the chat history, click the ") }}
              <b>⏪ {{ t("aiCoding.panel.rewindLabel", "Rewind") }}</b>
              {{ t("claudeCode.config.checkpointingDesc2", " button to roll files back to any historical state.") }}
              <br />
              <span class="cc-warning">⚠️ {{ t("claudeCode.config.checkpointingWarning", "Enabling adds disk I/O — recommend only when you actually need the \"undo Claude file edits\" feature.") }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ 🤖 Custom Sub-Agents ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('agents')"
        >
          <span>🤖 {{ t("aiCoding.config.subAgents", "Custom Sub-Agents") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsed.has('agents') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsed.has('agents')"
          class="config-group-body"
        >
          <div
            v-if="agentNames.length === 0"
            class="config-comment"
          >
            {{ t("aiCoding.config.noAgents", "No custom sub-agents defined.") }}
          </div>
          <div
            v-for="name in agentNames"
            :key="name"
            class="qai-cc__agent"
          >
            <div class="qai-cc__agent-head">
              <strong>{{ name }}</strong>
              <div>
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  @click="openEditAgent(name)"
                >
                  {{ t("common.edit", "Edit") }}
                </button>
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  @click="deleteAgent(name)"
                >
                  {{ t("aiCoding.config.delete", "Delete") }}
                </button>
              </div>
            </div>
            <div
              class="config-comment"
              style="margin: 0;"
            >
              {{ cfg.agents[name]?.description }}
            </div>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1458-1479): show
                 model / tools / maxTurns metadata under the description. -->
            <div
              v-if="cfg.agents[name]?.model || (cfg.agents[name]?.tools && (cfg.agents[name]?.tools?.length ?? 0) > 0) || cfg.agents[name]?.maxTurns"
              class="qai-cc__agent-meta"
            >
              <span v-if="cfg.agents[name]?.model">
                {{ t("claudeCode.config.agentModelLabel", "Model") }}: {{ cfg.agents[name]?.model }}
              </span>
              <span v-if="cfg.agents[name]?.tools && (cfg.agents[name]?.tools?.length ?? 0) > 0">
                {{ t("claudeCode.config.agentToolsLabel", "Tools") }}: {{ cfg.agents[name]?.tools?.join(", ") }}
              </span>
              <span v-if="cfg.agents[name]?.maxTurns">
                {{ t("claudeCode.config.agentMaxTurnsLabel", "Max turns") }}: {{ cfg.agents[name]?.maxTurns }}
              </span>
            </div>
          </div>

          <div
            v-if="editingAgent"
            class="qai-cc__agent-form"
          >
            <div class="config-field">
              <label class="config-label">
                {{ t("aiCoding.config.agentName", "Name") }}
                <!-- V1 parity (ClaudeCodeConfigPanel.js:1469): red required star. -->
                <span class="cc-required-star">*</span>
              </label>
              <input
                v-model="editingAgentName"
                type="text"
                class="config-input"
                placeholder="my-agent"
              />
            </div>
            <div class="config-field">
              <label class="config-label">
                {{ t("aiCoding.config.agentDesc", "Description") }}
                <span class="cc-required-star">*</span>
              </label>
              <input
                v-model="editingAgent.description"
                type="text"
                class="config-input"
              />
            </div>
            <div class="config-field">
              <label class="config-label">
                {{ t("aiCoding.config.agentPrompt", "Prompt") }}
                <span class="cc-required-star">*</span>
              </label>
              <textarea
                v-model="editingAgent.prompt"
                class="config-input"
                style="min-height: 80px; resize: vertical;"
              ></textarea>
            </div>
            <div class="config-field">
              <label class="config-label">{{ t("aiCoding.config.agentModel", "Model (optional)") }}</label>
              <input
                v-model="editingAgent.model"
                type="text"
                class="config-input"
              />
            </div>
            <div class="config-field">
              <label class="config-label">{{ t("aiCoding.config.agentMaxTurns", "Max Turns (optional)") }}</label>
              <input
                v-model.number="editingAgent.maxTurns"
                type="number"
                min="1"
                max="100"
                class="config-input"
                style="max-width: 120px;"
              />
            </div>
            <div
              v-if="agentFormError"
              class="config-comment"
              style="color: #f87171;"
            >
              {{ agentFormError }}
            </div>
            <div class="qai-cc__row">
              <button
                type="button"
                class="btn btn-primary btn-sm"
                @click="saveAgent"
              >
                {{ t("common.save", "Save") }}
              </button>
              <button
                type="button"
                class="btn btn-ghost btn-sm"
                @click="cancelAgent"
              >
                {{ t("common.cancel", "Cancel") }}
              </button>
            </div>
          </div>
          <div
            v-else
            class="config-field"
          >
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="openNewAgent"
            >
              + {{ t("aiCoding.config.newAgent", "New Sub-Agent") }}
            </button>
          </div>
        </div>
      </div>

      <!-- ═══ ⚡ Streaming & Beta ═══ -->
      <div class="config-group">
        <div
          class="config-group-header"
          @click="toggleGroup('beta')"
        >
          <span>⚡ {{ t("aiCoding.config.streamingBeta", "Streaming & Beta") }}</span>
          <span
            class="collapse-arrow"
            :class="{ collapsed: collapsed.has('beta') }"
          >▼</span>
        </div>
        <div
          v-show="!collapsed.has('beta')"
          class="config-group-body"
        >
          <div class="config-field qai-cc__row">
            <label
              class="config-label"
              style="margin-bottom: 0;"
            >{{ t("aiCoding.config.partialMessages", "Include Partial Messages") }}</label>
            <label class="toggle">
              <input
                v-model="cfg.include_partial_messages"
                type="checkbox"
              />
              <span class="toggle-slider"></span>
            </label>
          </div>
          <!-- V1 parity (ClaudeCodeConfigPanel.js:1541-1546): describe the
               streaming behavior + a yellow network/CPU warning. -->
          <div class="config-field">
            <div class="config-comment">
              {{ t("claudeCode.config.partialMsgDesc", "When enabled, partial content starts streaming before the message completes (smoother typewriter effect). The default only renders messages once they are complete.") }}
              <br />
              <span class="cc-warning">⚠️ {{ t("claudeCode.config.partialMsgWarning", "May increase network traffic and CPU usage.") }}</span>
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("aiCoding.config.betas", "Beta Features") }}</label>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1551-1554): describe the
                 betas section above the cards. -->
            <div class="config-comment">
              {{ t("claudeCode.config.betasDesc", "Enable Anthropic Beta features. Currently supported betas:") }}
            </div>
            <!-- V1 parity (ClaudeCodeConfigPanel.js:1556-1576): card-style
                 beta entries with border + label + per-beta description, not
                 a bare checkbox. -->
            <div class="qai-cc__betas">
              <label
                v-for="b in BETA_OPTIONS"
                :key="b"
                class="qai-cc__beta-card"
                :class="{ 'qai-cc__beta-card--active': cfg.betas.includes(b) }"
              >
                <input
                  type="checkbox"
                  :checked="cfg.betas.includes(b)"
                  @change="toggleBeta(b)"
                />
                <div class="qai-cc__beta-body">
                  <div class="qai-cc__beta-name">
                    {{ b === "context-1m-2025-08-07"
                      ? t("claudeCode.config.beta1mContextLabel", "1M Context Window")
                      : b }}
                  </div>
                  <div class="qai-cc__beta-desc">
                    <code>{{ b }}</code>
                    <template v-if="b === 'context-1m-2025-08-07'">
                      — {{ t("claudeCode.config.betaContext1mDesc", "Enable a 1M token context window, suitable for long codebase analysis. Note: increases cost.") }}
                    </template>
                  </div>
                </div>
              </label>
            </div>
            <div class="qai-cc__beta-summary">
              {{ t("claudeCode.config.betasEnabled", "Currently enabled") }}:
              {{ cfg.betas.length > 0 ? cfg.betas.join(", ") : t("claudeCode.config.betasNone", "(none)") }}
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ Save bar ═══ -->
      <div class="config-save-bar">
        <button
          type="button"
          class="btn btn-primary"
          data-testid="cc-config-save-btn"
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
/* ── Model radio list (V1 parity) ── */
.cc-model-radios {
  display: flex;
  flex-direction: column;
  gap: 4px;
  /* V1 parity (ClaudeCodeConfigPanel.js:997): model list fills the container
     width (no max-width); each radio row spans the full row. */
  width: 100%;
}
.cc-model-radio {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
}
.cc-model-radio--active {
  border-color: var(--accent);
  background: var(--accent-light);
}
.cc-model-radio__label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-sm);
}
.cc-model-radio__badge {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--accent-light);
  color: var(--accent);
  font-size: var(--text-xs);
}
.cc-model-toggle {
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
/* V1 parity (ClaudeCodeConfigPanel.js:1027/1031): small dimmed ▼/▲ arrow prefix. */
.cc-model-toggle__arrow {
  font-size: var(--text-xs);
  opacity: 0.7;
}
.cc-model-current code {
  font-family: var(--font-mono, monospace);
}

/* ── Model source badge (V1 /api/cc/models parity) ── */
.cc-models-source {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 6px;
  /* V1 parity (ClaudeCodeConfigPanel.js:951): anchor scroll-margin so the
     collapse scroll-back lands just below the section top, not flush. */
  scroll-margin-top: 12px;
}
.cc-models-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-muted);
}
.cc-models-baseurl {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--text-xs);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 280px;
}
.cc-models-baseurl code {
  font-family: var(--font-mono, monospace);
}
.cc-models-baseurl-src {
  opacity: 0.7;
}
.cc-models-refresh {
  margin-left: auto;
}
.cc-models-error {
  color: #f87171;
  font-style: italic;
}

/* ── Auth var metadata badges (V1 parity) ── */
.cc-var-label {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.cc-var-label code {
  font-family: var(--font-mono, monospace);
}
.cc-var-badge {
  padding: 1px 5px;
  border-radius: 3px;
  font-size: var(--text-xs);
  font-weight: 400;
}
.cc-var-badge--required {
  background: rgba(248, 113, 113, 0.15);
  color: #f87171;
}
.cc-var-badge--optional {
  background: rgba(251, 191, 36, 0.15);
  color: #fbbf24;
}
.cc-var-badge--cred {
  background: rgba(139, 92, 246, 0.15);
  color: #a78bfa;
}
.cc-var-badge--forge {
  background: var(--bg-secondary);
  color: var(--text-muted);
}
.cc-var-status {
  margin-left: auto;
  font-size: var(--text-xs);
}
.cc-var-status--store { color: #4ade80; }
.cc-var-status--env { color: #fbbf24; }
.cc-var-status--unset { color: #f87171; }
.cc-var-hint {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.cc-var-envonly {
  font-size: var(--text-sm);
  color: #fbbf24;
}

/* ── Tool permission legend + summary (V1 parity: inline horizontal flow) ── */
.cc-tool-legend {
  line-height: 1.7;
  margin-bottom: 8px;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.cc-tool-legend__item {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  margin: 0 4px;
}
.cc-tool-legend__swatch {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  border: 1px solid;
  flex-shrink: 0;
}
.cc-tool-summary {
  margin-top: 8px;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.cc-tool-clickorder {
  font-size: var(--text-xs);
  color: var(--text-muted);
}
/* ── Timeout inline warn/ok hints (V1 parity) ── */
.cc-warn {
  margin-top: 4px;
  color: #f87171;
}
.cc-ok {
  margin-top: 4px;
  color: #4ade80;
}
/* ── Yellow inline-warning span (V1 parity: checkpoint / partial-msg /
     session_env warnings — embedded inside <div class="config-comment">). ── */
.cc-warning {
  color: #fbbf24;
}
.qai-cc__title {
  margin: 0 0 12px;
  font-size: var(--text-md, 1rem);
}
.qai-cc__loading {
  color: var(--text-muted);
  padding: 24px;
}
.qai-cc__row {
  /* V1 parity (ClaudeCodeConfigPanel.js:936): the enable row is a horizontal,
     left-aligned flex row (label + toggle + status text). The global
     `.config-field` sets `flex-direction: column`, so we must explicitly
     reset to `row` here, otherwise the row stacks vertically / drifts centre. */
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
/* V1 parity (ClaudeCodeConfigPanel.js:937): inline toggle-row labels reserve a
   fixed min-width so the toggles line up in a column. */
.qai-cc__row .config-label {
  min-width: 120px;
}
.qai-cc__health {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  font-size: var(--text-sm, 0.85rem);
}
.qai-cc__badge {
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 600;
  font-size: var(--text-xs, 0.75rem);
}
.qai-cc__badge--ok {
  background: rgba(74, 222, 128, 0.15);
  color: #4ade80;
}
.qai-cc__badge--err {
  background: rgba(248, 113, 113, 0.15);
  color: #f87171;
}
.qai-cc__schemes {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.qai-cc__scheme {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px;
  border: 1px solid var(--border, #2a3a4a);
  border-radius: 6px;
  cursor: pointer;
}
.qai-cc__scheme--active {
  border-color: var(--accent);
  background: var(--accent-light);
}
.qai-cc__secret-tag {
  font-size: var(--text-xs, 0.7rem);
  background: rgba(248, 113, 113, 0.15);
  color: #f87171;
  padding: 0 6px;
  border-radius: 3px;
  margin-left: 6px;
}
.qai-cc__agent {
  border: 1px solid var(--border, #2a3a4a);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 6px;
}
.qai-cc__agent-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.qai-cc__agent-form {
  border: 1px dashed var(--border, #2a3a4a);
  border-radius: 6px;
  padding: 10px;
  margin-top: 8px;
}
.qai-cc__beta {
  display: flex;
  align-items: center;
  gap: 6px;
}
/* ── Required-field red star (V1 parity: agent form labels) ── */
.cc-required-star {
  margin-left: 4px;
  color: var(--error, #f87171);
}
/* ── Save-bar storage hint (V1 parity) ── */
.cc-storage-hint {
  margin: 6px 0 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
/* ── session_env / add_dirs count + keys list (V1 parity) ── */
.cc-session-env-count {
  margin-top: 4px;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
.cc-session-env-keys {
  margin-left: 6px;
  color: var(--accent, #7eb8f7);
}
/* ── Sub-agent metadata row (V1 parity) ── */
.qai-cc__agent-meta {
  margin-top: 2px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
/* ── Beta cards (V1 parity) ── */
.qai-cc__betas {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}
.qai-cc__beta-card {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border, #2a3a4a);
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.qai-cc__beta-card--active {
  border-color: var(--accent, #7eb8f7);
  background: rgba(126, 184, 247, 0.06);
}
.qai-cc__beta-card input[type="checkbox"] {
  margin-top: 3px;
  flex-shrink: 0;
}
.qai-cc__beta-body {
  flex: 1;
  min-width: 0;
}
.qai-cc__beta-name {
  font-weight: 500;
  font-size: var(--text-base);
  color: var(--text-primary);
}
.qai-cc__beta-desc {
  margin-top: 2px;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
.qai-cc__beta-desc code {
  font-family: var(--font-mono, monospace);
  background: var(--bg-code);
  padding: 0 4px;
  border-radius: 3px;
}
.qai-cc__beta-summary {
  margin-top: 6px;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
</style>
