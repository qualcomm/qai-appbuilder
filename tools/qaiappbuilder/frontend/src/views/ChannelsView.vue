<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Channels view — card grid layout (V1 style).
 *
 * Shows all channel cards side-by-side in a CSS grid.
 * Each card mounts its respective config panel component.
 */
import {
  ref,
  reactive,
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  onMounted,
  computed,
} from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { useToastStore } from "@/stores/toast";
import { useHeaderActions } from "@/composables/useHeaderActions";
import { ICON_SETTINGS, ICON_HELP } from "@/components/icons/topbarIcons";
import { provideWechat } from "@/composables/useWechat";
import { provideFeishu } from "@/composables/useFeishu";
import FeishuConfigPanel from "@/components/channels/FeishuConfigPanel.vue";
import WechatConfigPanel from "@/components/channels/WechatConfigPanel.vue";
import WechatInfoDialog from "@/components/channels/WechatInfoDialog.vue";
import FeishuInfoDialog from "@/components/channels/FeishuInfoDialog.vue";
import ChannelsGuideDialog from "@/components/channels/ChannelsGuideDialog.vue";
// Shared help-manual affordance (per-channel task-oriented guide). Rendered
// in the channel-card HEADER right before the existing ℹ️ info button so
// the two discoverable-help affordances sit side-by-side (Help + Info).
// The header placement was chosen over the earlier "top-of-panel help-row"
// design because the row visually competed with the model selector and
// looked stray — users' eyes land on the header first, and pairing with
// the info button lets them treat both as one "help cluster".
import HelpButton from "@/components/common/HelpButton.vue";

const { t } = useI18n();
const toast = useToastStore();

// Own the single WeChat + Feishu composable instances here and provide them
// so the card-header status badges and the config panels share ONE state
// (V2 component-isolation alternative to V1's global refs).
const wechat = provideWechat();
const feishu = provideFeishu();

// Card-header status badge classes (V1 badge-local / badge-warning / badge-error).
const wechatBadgeClass = computed(() => ({
  "badge-local": wechat.wechatStatus.value === "connected",
  "badge-warning":
    wechat.wechatStatus.value === "logging_in" || wechat.wechatStatus.value === "scanned",
  "badge-error":
    wechat.wechatStatus.value === "expired" || wechat.wechatStatus.value === "error",
}));
const feishuBadgeClass = computed(() => ({
  "badge-local": feishu.feishuStatus.value === "running",
  "badge-warning": feishu.feishuStatus.value === "starting",
  "badge-error": feishu.feishuStatus.value === "error",
}));

const showUsageGuide = ref(false);
const wechatInfoOpen = ref(false);
const feishuInfoOpen = ref(false);

// --- Channel public settings (forge_config.channels) ---
interface ForgeConfigResponse {
  config: Record<string, unknown>;
}

const settingsOpen = ref(false);
const settingsLoading = ref(false);
const settingsSaving = ref(false);
// Full persisted ``channels`` object — round-tripped so the shallow
// top-level merge in the backend does not drop sub-keys we do not edit.
const rawChannels = ref<Record<string, unknown>>({});
const settings = reactive({
  max_history_rounds: 20,
});

function applyFromConfig(cfg: Record<string, unknown>): void {
  const ch = (cfg.channels as Record<string, unknown>) ?? {};
  rawChannels.value = { ...ch };
  if (ch.max_history_rounds != null) {
    settings.max_history_rounds = Number(ch.max_history_rounds);
  }
}

async function loadSettings(): Promise<void> {
  settingsLoading.value = true;
  try {
    const res = await apiJson<ForgeConfigResponse>("GET", "/api/forge-config");
    applyFromConfig(res.config ?? {});
  } catch (e) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: `${t("channels.settingsLoadFailed")}: ${(e as Error).message}`,
      timeoutMs: 5000,
    });
  } finally {
    settingsLoading.value = false;
  }
}

async function openSettings(): Promise<void> {
  settingsOpen.value = true;
  await loadSettings();
}

async function saveSettings(): Promise<void> {
  settingsSaving.value = true;
  try {
    const rounds = Math.max(1, Math.min(100, Number(settings.max_history_rounds) || 20));
    settings.max_history_rounds = rounds;
    const channels = {
      ...rawChannels.value,
      max_history_rounds: rounds,
    };
    const res = await apiJson<ForgeConfigResponse>("POST", "/api/forge-config", {
      config: { channels },
    });
    applyFromConfig(res.config ?? {});
    toast.push({
      id: crypto.randomUUID(),
      kind: "success",
      message: t("channels.settingsSaved"),
      timeoutMs: 3000,
    });
    settingsOpen.value = false;
  } catch (e) {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: `${t("channels.settingsSaveFailed")}: ${(e as Error).message}`,
      timeoutMs: 5000,
    });
  } finally {
    settingsSaving.value = false;
  }
}

// Lifecycle (KeepAlive-aware). AppMain.vue wraps <RouterView> in
// <KeepAlive>, so navigating away from /channels does NOT trigger
// onBeforeUnmount — it triggers onDeactivated. If we cleaned up timers in
// onBeforeUnmount, the wechat/feishu pollers (QR-image probe every 2s, status
// every 2s, login countdown every 1s) would leak forever once the user
// connected and then switched to another tab. Mirror activate/deactivate.
//
// onMounted/onBeforeUnmount kept as a safety net for non-KeepAlive contexts
// (e.g. component-level unit tests that mount without the AppMain KeepAlive
// wrapper). The dispose functions are idempotent so the duplicated invocation
// on first mount (onMounted + onActivated both fire) is harmless.
//
// We skip the FIRST onActivated to avoid double-fetching loadSettings /
// loadWechatStatus / loadFeishuStatus when both onMounted and the
// immediately-following onActivated would otherwise both call them. On every
// subsequent activation (user returns from another view) we DO want a fresh
// reload so the badge mirrors any back-end state that changed while hidden.
function reloadStatuses(): void {
  void loadSettings();
  void wechat.loadWechatStatus();
  void feishu.loadFeishuStatus();
}

function disposePollers(): void {
  wechat.dispose();
  feishu.dispose();
}

let activatedOnce = false;
onMounted(reloadStatuses);
onActivated(() => {
  if (!activatedOnce) {
    activatedOnce = true;
    return;
  }
  reloadStatuses();
});
onDeactivated(disposePollers);
onBeforeUnmount(disposePollers);

// ─── Topbar actions (V1 parity: index.html:359-369 — channels header
// hosts ⚙️ Settings + 📖 Usage Guide) ──────────────────────────────────
useHeaderActions(() => [
  {
    id: "channels.settings",
    label: t("channels.settingsBtn"),
    iconSvg: ICON_SETTINGS,
    title: t("channels.settingsBtn"),
    testId: "channels-settings-btn",
    onClick: () => {
      void openSettings();
    },
  },
  {
    id: "channels.guide",
    label: t("channels.guideBtn"),
    iconSvg: ICON_HELP,
    title: t("channels.guideBtn"),
    testId: "channels-guide-btn",
    onClick: () => {
      showUsageGuide.value = true;
    },
  },
]);
</script>

<template>
  <section
    class="panel-view"
    :aria-label="t('channels.title')"
  >
    <!-- Panel header (V1 index.html:2257-2268 parity: 3-line icon + title +
         subtitle). -->
    <div class="panel-header">
      <div>
        <div class="panel-title">
          <svg
            class="panel-title-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <line
              x1="4"
              y1="6"
              x2="20"
              y2="6"
            />
            <line
              x1="4"
              y1="12"
              x2="20"
              y2="12"
            />
            <line
              x1="4"
              y1="18"
              x2="14"
              y2="18"
            />
          </svg>
          {{ t("channels.title") }}
        </div>
        <div class="panel-subtitle">
          {{ t("channels.subtitle") }}
        </div>
      </div>
    </div>

    <div class="channels-grid">
      <!-- WeChat channel card -->
      <div
        class="channel-card"
        :class="{ connected: wechat.wechatStatus.value === 'connected' }"
      >
        <div class="channel-card-header">
          <div class="channel-icon">
            <!-- WeChat brand icon (V1 parity, index.html:2539-2542):
                 two overlapping #07C160 ellipses (WeChat logo style). -->
            <svg
              width="22"
              height="22"
              viewBox="0 0 24 24"
              fill="none"
              style="vertical-align: middle"
            >
              <ellipse
                cx="9"
                cy="9"
                rx="7"
                ry="5.5"
                fill="#07C160"
              />
              <ellipse
                cx="16"
                cy="15"
                rx="6"
                ry="4.5"
                fill="#07C160"
                opacity="0.65"
              />
            </svg>
          </div>
          <div style="flex: 1">
            <div class="channel-name">
              {{ t("channels.wechat.name") }}
            </div>
            <div class="channel-desc">
              {{ t("channels.wechat.cardDesc") }}
            </div>
          </div>
          <!-- Task-oriented help (how to set up + common issues). Sits
               directly next to the ℹ️ info button so both discoverable-
               help entry points cluster in the header. -->
          <HelpButton
            doc-key="wechat-ilink"
            external-url="https://ilink.dev/"
            size="sm"
          />
          <button
            class="btn btn-ghost btn-sm channel-info-btn"
            type="button"
            data-testid="wechat-info-btn"
            :title="t('channels.wechat.info.btnTitle')"
            @click="wechatInfoOpen = true"
          >
            ℹ️
          </button>
          <span
            class="badge channel-status-badge"
            :class="wechatBadgeClass"
            data-testid="wechat-status-badge"
          >
            {{ t("channels.wechat.status." + (wechat.wechatStatus.value || "idle")) }}
          </span>
        </div>
        <WechatConfigPanel />
      </div>

      <!-- Feishu channel card -->
      <div
        class="channel-card"
        :class="{ connected: feishu.feishuStatus.value === 'running' }"
      >
        <div class="channel-card-header">
          <div class="channel-icon">
            🪶
          </div>
          <div style="flex: 1">
            <div class="channel-name">
              {{ t("channels.feishu.name") }}
            </div>
            <div class="channel-desc">
              {{ t("channels.feishu.cardDesc") }}
            </div>
          </div>
          <!-- Task-oriented help (how to set up + common issues). Sits
               directly next to the ℹ️ info button so both discoverable-
               help entry points cluster in the header. Symmetric with
               the WeChat card above. -->
          <HelpButton
            doc-key="feishu-setup"
            external-url="https://open.feishu.cn/app"
            size="sm"
          />
          <button
            class="btn btn-ghost btn-sm channel-info-btn"
            type="button"
            data-testid="feishu-info-btn"
            :title="t('channels.feishu.info.btnTitle')"
            @click="feishuInfoOpen = true"
          >
            ℹ️
          </button>
          <span
            class="badge channel-status-badge"
            :class="feishuBadgeClass"
            data-testid="feishu-status-badge"
          >
            {{ t("channels.feishu.status." + (feishu.feishuStatus.value || "stopped")) }}
          </span>
        </div>
        <FeishuConfigPanel />
      </div>
    </div>

    <!-- WeChat info dialog (ℹ️ on the WeChat card) -->
    <WechatInfoDialog
      :open="wechatInfoOpen"
      @close="wechatInfoOpen = false"
    />

    <!-- Feishu info dialog (ℹ️ on the Feishu card) -->
    <FeishuInfoDialog
      :open="feishuInfoOpen"
      @close="feishuInfoOpen = false"
    />

    <!-- Generic channels usage guide (topbar button) -->
    <ChannelsGuideDialog
      :open="showUsageGuide"
      @close="showUsageGuide = false"
    />

    <!-- Channel Public Settings Modal -->
    <Teleport to="body">
      <div
        v-if="settingsOpen"
        class="channels-settings-overlay"
        @click.self="settingsOpen = false"
      >
        <div class="channels-settings-modal">
          <div class="channels-settings-modal__header">
            <div class="channels-settings-modal__header-text">
              <h3>⚙ {{ t("channels.settings.title") }}</h3>
              <p class="channels-settings-modal__subtitle">
                {{ t("channels.settings.subtitle") }}
              </p>
            </div>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="settingsOpen = false"
            >
              ✕
            </button>
          </div>
          <div class="channels-settings-modal__body">
            <div
              v-if="settingsLoading"
              class="channels-settings-loading"
            >
              {{ t("common.loading") }}
            </div>
            <div
              v-else
              class="channels-settings-field"
            >
              <label class="channels-settings-label">
                {{ t("channels.settings.history.label") }}
                <span
                  class="channels-settings-info"
                  :title="t('channels.settings.history.hint')"
                >ⓘ</span>
              </label>
              <p class="channels-settings-hint">
                {{ t("channels.settings.history.desc") }}
              </p>
              <div class="channels-settings-input-row">
                <input
                  v-model.number="settings.max_history_rounds"
                  type="number"
                  class="config-input"
                  min="1"
                  max="100"
                  step="1"
                  style="width: 90px; text-align: center;"
                />
                <span class="channels-settings-unit">{{ t("channels.settings.history.unit") }}</span>
                <button
                  type="button"
                  class="btn btn-primary btn-sm channels-settings-save-inline"
                  :disabled="settingsSaving || settingsLoading"
                  @click="saveSettings"
                >
                  💾 {{ settingsSaving ? t("common.saving") : t("common.save") }}
                </button>
              </div>
            </div>
          </div>
          <div class="channels-settings-modal__footer">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="settingsOpen = false"
            >
              {{ t("common.close") }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style>/* --- Channel card header badge + info button (V1 parity) --- */
.channel-status-badge {
  font-size: var(--text-xs);
  white-space: nowrap;
}
.channel-info-btn {
  padding: 2px 7px;
  font-size: var(--text-md);
  margin-right: 6px;
  border-radius: 50%;
  line-height: 1;
}

/* --- Channel public settings modal --- */
.channels-settings-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-bg);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}
.channels-settings-modal {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: 100%;
  max-width: 420px;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.channels-settings-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--border);
}
.channels-settings-modal__header h3 {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: 700;
  color: var(--text-primary);
}
.channels-settings-modal__subtitle {
  margin: 2px 0 0 0;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.channels-settings-info {
  cursor: help;
  opacity: 0.5;
  font-size: var(--text-xs);
  margin-left: 3px;
  font-weight: 400;
}
.channels-settings-save-inline {
  margin-left: auto;
}
.channels-settings-modal__body {
  padding: var(--space-4) var(--space-5);
}
.channels-settings-loading {
  color: var(--text-muted);
}
.channels-settings-label {
  display: block;
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: var(--space-1);
}
.channels-settings-hint {
  margin: 0 0 var(--space-2) 0;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.channels-settings-input-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.channels-settings-unit {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.channels-settings-modal__footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--border);
}
</style>
