<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Settings view — V1's 4 top-level tabs + 1 V2-only enhancement tab.
 *
 * 4 tabs aligned to V1 (index.html 2610-2664):
 *   🔧 App Config    → AppConfigPanel    (forge_config.json)
 *   ☁️ Cloud Models  → CloudModelsPanel  (cloud_models.json)
 *   🧠 Coding Modes  → CodePersonasPanel (coding persona prompts)
 *   🤖 AI Coding     → Claude Code / Open Code (segmented sub-tabs)
 *
 * Plus 2 V2-only enhancement tabs (V1 has no counterpart — user-authorized
 * additions; per AGENTS.md 细则 4-bis V2-specific enhancements are protected
 * and must NOT be removed to "align with V1"):
 *   🤝 Agent         → AgentSettingsPanel (agent loop tuning +
 *                      per-profile sub-agent model overrides; backed by
 *                      /api/forge-config + /api/settings/subagent_profile_models)
 *   🪝 Hooks         → ChatHooksSettings (chat action hooks — shell command
 *                      per chat event; backed by /api/settings/chat_hooks)
 *
 * Theme / locale switching lives in the sidebar footer (V1 parity),
 * so the previous V2-only Appearance tab was redundant. V1 has no
 * About tab; build-info is not surfaced here.
 *
 * The page subtitle follows the active tab exactly like V1.
 *
 * Refresh button (V1 parity: index.html:373-381):
 *   - App Config tab     → reload forge_config + app_config + proxy
 *   - Cloud Models tab   → force remount CloudModelsPanel (triggers fetchProviders)
 *   - Coding Modes tab   → reload personas
 *   - AI Coding tab      → no refresh (V1 also null)
 */
import { ref, computed, watch, defineAsyncComponent } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute } from "vue-router";
import UiTabs from "@/components/chat/UiTabs.vue";
// App Config is the default tab (first paint), so it stays a static import and
// is always mounted.
import AppConfigPanel from "@/components/chat/AppConfigPanel.vue";
// The other tab panels are heavy (CloudModelsPanel / Claude Code / Open Code
// config panels each pull in large subtrees). They are code-split via
// defineAsyncComponent AND lazily MOUNTED on first visit (see `visited` below):
// `v-show` alone would mount every panel on SettingsView mount (v-show only
// toggles CSS display, it does not gate mounting), which would eagerly fetch
// every async chunk and defeat the split. Gating the first mount with
// `v-if="visited.has(tab)"` fetches a panel's chunk only when its tab is first
// opened; thereafter `v-show` keeps it alive so in-progress edits survive tab
// switches (the previous keep-alive behaviour is preserved).
const CloudModelsPanel = defineAsyncComponent(
  () => import("@/components/chat/CloudModelsPanel.vue"),
);
const CodePersonasPanel = defineAsyncComponent(
  () => import("@/components/chat/CodePersonasPanel.vue"),
);
const ClaudeCodeConfigPanel = defineAsyncComponent(
  () => import("@/components/ai-coding/ClaudeCodeConfigPanel.vue"),
);
const OpenCodeConfigPanel = defineAsyncComponent(
  () => import("@/components/ai-coding/OpenCodeConfigPanel.vue"),
);
const AgentSettingsPanel = defineAsyncComponent(
  () => import("@/components/chat/AgentSettingsPanel.vue"),
);
const ChatHooksSettings = defineAsyncComponent(
  () => import("@/components/chat/ChatHooksSettings.vue"),
);
const McpServersPanel = defineAsyncComponent(
  () => import("@/components/chat/McpServersPanel.vue"),
);
import { useForgeConfig } from "@/composables/useForgeConfig";
import { useConfig } from "@/composables/useConfig";
import { useProxy } from "@/composables/useProxy";
import { useCodePersonas } from "@/composables/useCodePersonas";
import { useHeaderActions } from "@/composables/useHeaderActions";
import { ICON_REFRESH } from "@/components/icons/topbarIcons";

type SettingsTab = "app" | "cloud-models" | "coding-modes" | "ai-coding" | "agent" | "hooks" | "mcp";
type AiCodingSubTab = "claude-code" | "opencode";

const { t } = useI18n();
const route = useRoute();

// Initial tab from `?tab=` query so deep-links from elsewhere (V1
// `navigateTo('settings','coding-modes')` parity — e.g. the chat code
// persona menu's "Edit prompts..." item) can land on a specific tab.
const _SETTINGS_TABS: ReadonlySet<SettingsTab> = new Set([
  "app",
  "cloud-models",
  "coding-modes",
  "ai-coding",
  "agent",
  "hooks",
  "mcp",
]);
function _initialTab(): SettingsTab {
  const q = route.query.tab;
  if (typeof q === "string" && _SETTINGS_TABS.has(q as SettingsTab)) {
    return q as SettingsTab;
  }
  return "app";
}

const activeTab = ref<SettingsTab>(_initialTab());
const aiCodingSubTab = ref<AiCodingSubTab>("claude-code");

// First-visit tracking for lazy mounting (see the defineAsyncComponent note
// above). A tab's panel is mounted (v-if) once its tab has been visited, then
// kept alive via v-show. The initial tab is pre-seeded so a deep-link (?tab=)
// mounts its panel immediately. `app` is always mounted (static, default tab).
const visited = ref<Set<SettingsTab>>(new Set([_initialTab()]));
watch(
  activeTab,
  (tab) => {
    if (!visited.value.has(tab)) {
      // Re-assign so the Set mutation is reactive (Vue tracks the ref, not the
      // internal Set membership).
      visited.value = new Set(visited.value).add(tab);
    }
  },
  { immediate: true },
);

// React to `?tab=` query changes after mount so in-app deep-links
// (e.g. the Open Code panel's "Go to Cloud Models" jump button —
// router.push({ path: "/settings", query: { tab: "cloud-models" } }))
// switch the active tab even when SettingsView is already mounted (the
// router reuses the component on a query-only change, so `_initialTab()`
// alone would not fire). V1 parity: ctx.navigateTo('settings', tab).
watch(
  () => route.query.tab,
  (q) => {
    if (typeof q === "string" && _SETTINGS_TABS.has(q as SettingsTab)) {
      activeTab.value = q as SettingsTab;
    }
  },
);

const settingsTabs = computed(() => [
  { id: "app", label: "🔧 " + t("settings.tab.appConfig") },
  { id: "cloud-models", label: "☁️ " + t("settings.tab.cloudModels") },
  { id: "coding-modes", label: "🧠 " + t("settings.tab.codingModes") },
  { id: "ai-coding", label: "🤖 " + t("settings.tab.aiCoding") },
  { id: "agent", label: "🤝 " + t("settings.tab.agent") },
  { id: "hooks", label: "🪝 " + t("settings.tab.hooks") },
  { id: "mcp", label: "🔌 " + t("settings.tab.mcp") },
]);

const aiCodingSubTabs = computed(() => [
  { id: "claude-code", label: "Claude Code" },
  { id: "opencode", label: "Open Code" },
]);

// Subtitle follows the active tab, matching V1 index.html:2620-2627.
const subtitle = computed(() => {
  if (activeTab.value === "app") return t("settings.subtitle.app");
  if (activeTab.value === "coding-modes") return t("settings.subtitle.codingModes");
  if (activeTab.value === "agent") return t("settings.subtitle.agent");
  if (activeTab.value === "hooks") return t("settings.subtitle.hooks");
  if (activeTab.value === "mcp") return t("settings.subtitle.mcp");
  if (activeTab.value === "ai-coding") {
    if (aiCodingSubTab.value === "claude-code") return t("settings.subtitle.claudeCode");
    if (aiCodingSubTab.value === "opencode") return t("settings.subtitle.opencode");
    return t("settings.subtitle.aiCoding");
  }
  return t("settings.subtitle.cloudModels");
});

// ─── Refresh (V1 parity: index.html:373-381) ─────────────────────────────────
// V1: app → loadForgeConfig(); coding-modes → codePersonas.loadPersonas();
//     ai-coding → null; cloud-models → loadCloudModels()
// V2: composables are module-level singletons — calling load/fetch here
//     updates the same reactive state the child panels consume.
const { load: loadForgeConfig } = useForgeConfig();
const { fetchConfig } = useConfig();
const { loadProxy } = useProxy();
const { fetchPersonas } = useCodePersonas();

// Force-remount CloudModelsPanel by bumping this key (triggers onMounted fetchProviders).
const cloudModelsKey = ref(0);

const refreshing = ref(false);

async function handleRefresh(): Promise<void> {
  if (refreshing.value) return;
  refreshing.value = true;
  try {
    if (activeTab.value === "app") {
      await Promise.all([fetchConfig(), loadForgeConfig(), loadProxy()]);
    } else if (activeTab.value === "coding-modes") {
      await fetchPersonas();
    } else if (activeTab.value === "cloud-models") {
      cloudModelsKey.value += 1;
    }
    // ai-coding: no refresh (V1 parity — null)
  } finally {
    refreshing.value = false;
  }
}

const showRefresh = computed(
  () =>
    activeTab.value !== "ai-coding" &&
    activeTab.value !== "agent" &&
    activeTab.value !== "hooks" &&
    activeTab.value !== "mcp",
);

// ─── Topbar actions (V1 parity: index.html:373-381 — settings page header
// hosts 🔄 Refresh button; the button is hidden on the AI Coding tab,
// matching V1's null-refresh branch — the factory returns [] so the
// topbar simply renders nothing on that tab.) ──────────────────────────
useHeaderActions(() =>
  showRefresh.value
    ? [
        {
          id: "settings.refresh",
          label: t("common.refresh"),
          iconSvg: ICON_REFRESH,
          title: t("common.refresh"),
          testId: "settings-refresh-btn",
          disabled: refreshing.value,
          onClick: () => {
            void handleRefresh();
          },
        },
      ]
    : [],
);
</script>

<template>
  <section
    class="panel-view"
    :aria-label="t('settings.title')"
  >
    <!-- V1-style Settings header above tabs (title + subtitle only;
         the refresh button is registered as a topbar action via
         useHeaderActions for V1 parity — V1 hosts page-action buttons
         on the global topbar, not inside the body header). -->
    <header class="settings-page-header">
      <div class="settings-page-header__title-row">
        <span class="settings-page-header__icon" aria-hidden="true">
          <svg
            width="24"
            height="24"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68 1.65 1.65 0 0 0 10 3.17V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </span>
        <h2 class="settings-page-header__heading">
          {{ t("settings.title") }}
        </h2>
      </div>
      <p class="settings-page-header__subtitle">
        {{ subtitle }}
      </p>
    </header>

    <!-- Top-level sub-tabs (V1 ui-tabs underline variant) -->
    <UiTabs
      v-model="activeTab"
      :tabs="settingsTabs"
      variant="underline"
      :label="t('settings.title')"
      class="settings-tabs"
    />

    <div class="config-tab-content">
      <!-- App Config tab — default tab, always mounted (static import). -->
      <AppConfigPanel v-show="activeTab === 'app'" />

      <!-- Cloud Models tab — lazily mounted on first visit, then kept alive. -->
      <CloudModelsPanel
        v-if="visited.has('cloud-models')"
        v-show="activeTab === 'cloud-models'"
        :key="cloudModelsKey"
      />

      <!-- Coding Modes tab -->
      <CodePersonasPanel
        v-if="visited.has('coding-modes')"
        v-show="activeTab === 'coding-modes'"
      />

      <!-- AI Coding tab — segmented sub-tabs (Claude Code / Open Code) -->
      <div
        v-if="visited.has('ai-coding')"
        v-show="activeTab === 'ai-coding'"
        class="ai-coding-unified-panel"
      >
        <UiTabs
          v-model="aiCodingSubTab"
          :tabs="aiCodingSubTabs"
          variant="segmented"
          size="sm"
          class="ai-coding-subtabs"
        />
        <ClaudeCodeConfigPanel v-show="aiCodingSubTab === 'claude-code'" />
        <OpenCodeConfigPanel v-show="aiCodingSubTab === 'opencode'" />
      </div>

      <!-- Agent tab — agent loop + sub-agent model overrides -->
      <AgentSettingsPanel
        v-if="visited.has('agent')"
        v-show="activeTab === 'agent'"
      />

      <!-- Hooks tab — chat action hooks (shell commands per chat event) -->
      <ChatHooksSettings
        v-if="visited.has('hooks')"
        v-show="activeTab === 'hooks'"
      />

      <!-- MCP tab — Model Context Protocol servers (external tool providers) -->
      <McpServersPanel
        v-if="visited.has('mcp')"
        v-show="activeTab === 'mcp'"
      />
    </div>
  </section>
</template>

<style>
.settings-page-header {
  margin-bottom: var(--space-5);
}
.settings-page-header__title-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.settings-page-header__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  color: var(--accent);
}
.settings-page-header__heading {
  font-size: var(--text-xl);
  font-weight: 700;
  margin: 0;
  color: var(--text-primary);
}
.settings-page-header__subtitle {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: var(--space-1) 0 0 0;
}
.settings-tabs {
  margin-bottom: var(--space-4);
}
.ai-coding-subtabs {
  margin-bottom: var(--space-4);
  max-width: 300px;
}
</style>
