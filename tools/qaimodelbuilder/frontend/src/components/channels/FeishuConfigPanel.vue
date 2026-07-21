<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * FeishuConfigPanel — single-instance Feishu channel (V1-aligned).
 *
 * UI mirrors the V1 verified single-instance Feishu panel
 * (`channels/feishu/FeishuConfigPanel.js`): a `stopped` idle state shows
 * the App-credential form (App ID / App Secret / Encrypt Key /
 * Verification Token) + auto-start toggle + a "Connect Feishu" button and
 * a separate "Save" button; `starting` shows a spinner + "Connecting to
 * Feishu server…"; `running` shows a green dot + "Feishu connected" +
 * Disconnect; `error` shows ⚠️ + the error detail + Retry / Disconnect.
 *
 * Feishu has NO QR scan — it is pure App-credential + webhook/WebSocket.
 * It is a single-instance channel; the backend instance_id is handled
 * transparently by `useFeishu` (no instance list / register form).
 *
 * Reuses the V1 CSS classes from styles/channels/channels.css
 * (.channel-idle-body / .channel-idle-hint / .channel-idle-actions /
 * .channel-footer-row / .channel-status-inline / .channel-status-dot /
 * .channel-status-text / .channel-proxy-row / .channel-proxy-label).
 */
import { onMounted, onBeforeUnmount, ref } from "vue";
import { useI18n } from "vue-i18n";

import { useFeishuShared } from "@/composables/useFeishu";
import ChannelModelSelector from "./ChannelModelSelector.vue";
import ChannelProxyPanel from "./ChannelProxyPanel.vue";
// NOTE: the HelpButton for this channel lives in the channel-card HEADER
// (see `ChannelsView.vue` — directly to the left of the existing ℹ️
// info button), not inside this panel. Symmetric with WechatConfigPanel;
// see that file's script-header comment for the rationale.

const { t } = useI18n();

// V1 parity: the proxy section is toggled by the `⚙ 代理` button inside the
// idle-actions row (FeishuConfigPanel.js:247-251); the section expands below
// the action row. The parent owns this state so the toggle can live in the
// shared action row.
const proxyExpanded = ref(false);

const {
  feishuStatus,
  feishuError,
  feishuConfig,
  loading,
  saving,
  loadFeishuStatus,
  loadFeishuConfig,
  saveFeishuConfig,
  startFeishu,
  stopFeishu,
  dispose,
  getInstanceId,
  resolveInstanceId,
} = useFeishuShared();

// V1 parity: on mount, sync the real connection status + config so the
// badge does not wrongly show "Not connected" after a reload.
onMounted(() => {
  void loadFeishuStatus();
  void loadFeishuConfig();
});
onBeforeUnmount(dispose);
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <div
    class="qai-feishu-config"
    data-testid="feishu-config-panel"
  >
    <!-- HelpButton for this channel lives in the channel-card header (see
         `ChannelsView.vue`), directly to the left of the ℹ️ info button —
         two paired discoverable-help affordances at the top of the card. -->

    <!-- Model selector — V1 parity: the "AI Model → Default (follow global
         settings)" row is ALWAYS visible above the status-specific body,
         regardless of connection/registration, so the user can pick a model
         before connecting. The single-instance backend id is handled
         transparently (`getInstanceId()` is null until the first
         register/connect; the empty string lets the read-only GETs degrade
         to the "follow global" default per useChannelSettings). -->
    <ChannelModelSelector
      kind="feishu"
      :instance-id="getInstanceId() ?? ''"
      :resolve-instance-id="resolveInstanceId"
    />

    <!-- running — V1 现役 (channels/feishu/FeishuConfigPanel.js:144-151):
         green dot + connected text + Disconnect only. -->
    <div
      v-if="feishuStatus === 'running'"
      data-testid="feishu-connected"
    >
      <div class="channel-footer-row">
        <div class="channel-status-inline">
          <span class="channel-status-dot" />
          <span>{{ t("channels.feishu.connectedMsg") }}</span>
        </div>
        <!-- Enhancement merged from V1 legacy panel (components/FeishuConfigPanel.js:219-226):
             refresh status button also available in running state. The active V1
             card (channels/feishu/FeishuConfigPanel.js) omits it; we keep it as a
             non-regressing usability enhancement. -->
        <button
          class="btn btn-ghost btn-sm"
          data-testid="feishu-refresh-running-btn"
          type="button"
          :disabled="loading"
          @click="loadFeishuStatus"
        >
          🔄 {{ t("channels.feishu.refreshStatus") }}
        </button>
        <button
          class="btn btn-ghost btn-sm channel-disconnect-btn"
          data-testid="feishu-disconnect-btn"
          type="button"
          :disabled="loading"
          @click="stopFeishu"
        >
          {{ t("channels.disconnect") }}
        </button>
      </div>
      <!-- Enhancement merged from V1 legacy panel (components/FeishuConfigPanel.js:230-236):
           running hint banner. Non-regressing usability enhancement. -->
      <div class="feishu-running-hint">
        ✅ {{ t("channels.feishu.runningHint") }}
      </div>
    </div>

    <!-- starting -->
    <div
      v-else-if="feishuStatus === 'starting'"
      style="text-align: center; padding: 16px 0"
      data-testid="feishu-starting"
    >
      <div style="display: flex; justify-content: center; margin-bottom: 10px">
        <div
          class="spinner"
          style="
            width: 28px;
            height: 28px;
            border-width: 3px;
            border-color: var(--accent-light);
            border-top-color: var(--accent);
          "
          data-testid="feishu-starting-spinner"
        />
      </div>
      <div
        class="channel-status-text"
        style="color: var(--warning)"
      >
        {{ t("channels.feishu.connectingMsg") }}
      </div>
      <button
        class="btn btn-ghost btn-sm"
        style="margin-top: 8px"
        data-testid="feishu-cancel-btn"
        type="button"
        @click="stopFeishu"
      >
        {{ t("feishu.btn.cancel") }}
      </button>
    </div>

    <!-- error -->
    <div
      v-else-if="feishuStatus === 'error'"
      data-testid="feishu-error"
    >
      <!-- Error banner -->
      <div
        style="text-align: center; padding: 12px 0 8px"
      >
        <div style="font-size: var(--text-2xl); margin-bottom: 8px">
          ⚠️
        </div>
        <div class="channel-status-text">
          {{ t("channels.feishu.errorMsg") }}
        </div>
        <div
          v-if="feishuError"
          style="font-size: var(--text-sm); color: var(--error); margin: 6px 16px; word-break: break-all"
          data-testid="feishu-error-detail"
        >
          {{ feishuError }}
        </div>
      </div>

      <!-- Credential form (same as stopped state so user can fix config) -->
      <div class="channel-idle-body">
        <div class="channel-idle-hint">
          <span v-html="t('channels.feishu.introLine1')" />
          <span v-html="t('channels.feishu.introLine2Prefix')" />
        </div>
        <!-- Decision 6 (Plan §1.4): promote the "Feishu Open Platform" link
             out of the inline sentence into an independent, button-styled
             external link so it is impossible to miss and its click target
             is a full button hit area rather than 4-character inline text. -->
        <a
          class="btn btn-ghost btn-sm feishu-open-platform-btn"
          href="https://open.feishu.cn/app"
          target="_blank"
          rel="noopener noreferrer"
          :title="t('channels.feishu.openPlatformTooltip')"
          data-testid="feishu-open-platform-link"
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
            <polyline points="15 3 21 3 21 9" />
            <line x1="10" y1="14" x2="21" y2="3" />
          </svg>
          {{ t("channels.feishu.openPlatformLabel") }}
        </a>

        <!-- App ID -->
        <div
          class="channel-proxy-row"
          style="margin-top: 10px"
        >
          <label class="channel-proxy-label">
            {{ t("feishu.label.appId") }} <span style="color: var(--error)">*</span>
          </label>
          <input
            v-model="feishuConfig.appId"
            class="config-input mono"
            style="flex: 1; font-size: var(--text-base)"
            data-testid="feishu-error-app-id"
            placeholder="cli_xxxxxxxxxxxxxxxx"
            name="feishu-app-id"
            autocomplete="off"
            data-1p-ignore
            data-lpignore="true"
          />
        </div>

        <!-- App Secret -->
        <div class="channel-proxy-row">
          <label class="channel-proxy-label">
            {{ t("feishu.label.appSecret") }} <span style="color: var(--error)">*</span>
          </label>
          <div class="feishu-secret-field">
            <input
              v-model="feishuConfig.appSecret"
              class="config-input mono"
              style="flex: 1; font-size: var(--text-base)"
              data-testid="feishu-error-app-secret"
              type="password"
              :placeholder="feishuConfig.hasAppSecret ? t('feishu.placeholder.saved') : t('feishu.placeholder.appSecret')"
              name="feishu-app-secret"
              autocomplete="new-password"
              data-1p-ignore
              data-lpignore="true"
            />
            <span
              v-if="feishuConfig.hasAppSecret && feishuConfig.appSecret === ''"
              class="feishu-secret-saved-badge"
              data-testid="feishu-error-app-secret-saved"
            >
              ✓ {{ t("feishu.label.appSecretSaved") }}
            </span>
          </div>
        </div>

        <!-- Encrypt Key -->
        <div class="channel-proxy-row">
          <label class="channel-proxy-label">{{ t("feishu.label.encryptKey") }}</label>
          <input
            v-model="feishuConfig.encryptKey"
            class="config-input mono"
            style="flex: 1; font-size: var(--text-base)"
            data-testid="feishu-error-encrypt-key"
            type="password"
            :placeholder="t('feishu.placeholder.encryptKey')"
            autocomplete="new-password"
          />
        </div>

        <!-- Verification Token -->
        <div class="channel-proxy-row">
          <label class="channel-proxy-label">{{ t("feishu.label.verifyToken") }}</label>
          <input
            v-model="feishuConfig.verificationToken"
            class="config-input mono"
            style="flex: 1; font-size: var(--text-base)"
            data-testid="feishu-error-verification-token"
            type="password"
            :placeholder="t('feishu.placeholder.verifyToken')"
            autocomplete="new-password"
          />
        </div>

        <!-- Actions: Save + Retry + Proxy toggle -->
        <div
          class="channel-idle-actions"
          style="margin-top: 12px"
        >
          <button
            class="btn btn-primary btn-sm"
            data-testid="feishu-retry-btn"
            type="button"
            :disabled="loading"
            @click="startFeishu"
          >
            {{ t("feishu.btn.retry") }}
          </button>
          <button
            class="btn btn-ghost btn-sm channel-proxy-toggle"
            :class="{ active: proxyExpanded }"
            data-testid="feishu-error-proxy-toggle"
            type="button"
            :title="t('channels.proxyTitle')"
            @click="proxyExpanded = !proxyExpanded"
          >
            ⚙ {{ t("channels.proxyLabel") }}
          </button>
          <button
            class="btn btn-ghost btn-sm"
            data-testid="feishu-error-save-btn"
            type="button"
            :disabled="saving"
            :title="t('feishu.btn.saveTitle')"
            @click="saveFeishuConfig"
          >
            💾 {{ t("common.save") }}
          </button>
          <button
            class="btn btn-ghost btn-sm channel-disconnect-btn"
            data-testid="feishu-error-disconnect-btn"
            type="button"
            :disabled="loading"
            @click="stopFeishu"
          >
            {{ t("channels.disconnect") }}
          </button>
        </div>
      </div>

      <!-- Proxy panel (same as stopped state) -->
      <ChannelProxyPanel
        kind="feishu"
        :instance-id="getInstanceId() ?? ''"
        :expanded="proxyExpanded"
      />
    </div>

    <!-- stopped (idle): credential form + connect -->
    <div
      v-else
      data-testid="feishu-idle"
    >
      <div class="channel-idle-body">
        <!-- V1 parity (FeishuConfigPanel.js:110-113): two-line intro with
             v-html to render the link and code tags. -->
        <div class="channel-idle-hint">
          <span v-html="t('channels.feishu.introLine1')" />
          <span v-html="t('channels.feishu.introLine2Prefix')" />
        </div>
        <!-- Decision 6 (Plan §1.4): promote the "Feishu Open Platform" link
             out of the inline sentence into an independent, button-styled
             external link so it is impossible to miss and its click target
             is a full button hit area rather than 4-character inline text. -->
        <a
          class="btn btn-ghost btn-sm feishu-open-platform-btn"
          href="https://open.feishu.cn/app"
          target="_blank"
          rel="noopener noreferrer"
          :title="t('channels.feishu.openPlatformTooltip')"
          data-testid="feishu-open-platform-link-idle"
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
            <polyline points="15 3 21 3 21 9" />
            <line x1="10" y1="14" x2="21" y2="3" />
          </svg>
          {{ t("channels.feishu.openPlatformLabel") }}
        </a>

        <!-- App ID (required) -->
        <div
          class="channel-proxy-row"
          style="margin-top: 10px"
        >
          <label class="channel-proxy-label">
            {{ t("feishu.label.appId") }} <span style="color: var(--error)">*</span>
          </label>
          <input
            v-model="feishuConfig.appId"
            class="config-input mono"
            style="flex: 1; font-size: var(--text-base)"
            data-testid="feishu-app-id"
            placeholder="cli_xxxxxxxxxxxxxxxx"
            name="feishu-app-id"
            autocomplete="off"
            data-1p-ignore
            data-lpignore="true"
          />
        </div>

        <!-- App Secret (required, password) -->
        <div class="channel-proxy-row">
          <label class="channel-proxy-label">
            {{ t("feishu.label.appSecret") }} <span style="color: var(--error)">*</span>
          </label>
          <div class="feishu-secret-field">
            <input
              v-model="feishuConfig.appSecret"
              class="config-input mono"
              style="flex: 1; font-size: var(--text-base)"
              data-testid="feishu-app-secret"
              type="password"
              :placeholder="feishuConfig.hasAppSecret ? t('feishu.placeholder.saved') : t('feishu.placeholder.appSecret')"
              name="feishu-app-secret"
              autocomplete="new-password"
              data-1p-ignore
              data-lpignore="true"
            />
            <span
              v-if="feishuConfig.hasAppSecret && feishuConfig.appSecret === ''"
              class="feishu-secret-saved-badge"
              data-testid="feishu-app-secret-saved"
            >
              ✓ {{ t("feishu.label.appSecretSaved") }}
            </span>
          </div>
        </div>

        <!-- Encrypt Key (optional) -->
        <div class="channel-proxy-row">
          <label class="channel-proxy-label">{{ t("feishu.label.encryptKey") }}</label>
          <input
            v-model="feishuConfig.encryptKey"
            class="config-input mono"
            style="flex: 1; font-size: var(--text-base)"
            data-testid="feishu-encrypt-key"
            type="password"
            :placeholder="t('feishu.placeholder.encryptKey')"
            autocomplete="new-password"
          />
        </div>

        <!-- Verification Token (optional) -->
        <div class="channel-proxy-row">
          <label class="channel-proxy-label">{{ t("feishu.label.verifyToken") }}</label>
          <input
            v-model="feishuConfig.verificationToken"
            class="config-input mono"
            style="flex: 1; font-size: var(--text-base)"
            data-testid="feishu-verification-token"
            type="password"
            :placeholder="t('feishu.placeholder.verifyToken')"
            autocomplete="new-password"
          />
        </div>

        <!-- Auto-start -->
        <div
          class="channel-proxy-row"
          style="margin-top: 4px"
        >
          <label
            style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: var(--text-base); color: var(--text-secondary)"
            data-testid="feishu-autostart"
          >
            <input
              v-model="feishuConfig.autoStart"
              type="checkbox"
              style="width: 15px; height: 15px; cursor: pointer"
              data-testid="feishu-autostart-checkbox"
            />
            {{ t("feishu.autoConnect") }}
          </label>
        </div>

        <!-- Actions (V1 layout: Connect + ⚙ Proxy + Save) -->
        <div
          class="channel-idle-actions"
          style="margin-top: 12px"
        >
          <button
            class="btn btn-primary btn-sm"
            data-testid="feishu-connect-btn"
            type="button"
            :disabled="loading"
            @click="startFeishu"
          >
            {{ t("feishu.btn.connect") }}
          </button>
          <!-- V1 parity: ⚙ proxy toggle inline in the action row
               (FeishuConfigPanel.js:247-251) -->
          <button
            class="btn btn-ghost btn-sm channel-proxy-toggle"
            :class="{ active: proxyExpanded }"
            data-testid="feishu-proxy-toggle"
            type="button"
            :title="t('channels.proxyTitle')"
            @click="proxyExpanded = !proxyExpanded"
          >
            ⚙ {{ t("channels.proxyLabel") }}
          </button>
          <!-- Enhancement merged from V1 legacy panel (components/FeishuConfigPanel.js:219-226):
               refresh status button in idle-actions row so user can re-check
               connection state. Non-regressing usability enhancement. -->
          <button
            class="btn btn-ghost btn-sm"
            data-testid="feishu-refresh-btn"
            type="button"
            :disabled="loading"
            @click="loadFeishuStatus"
          >
            🔄 {{ t("feishu.refreshStatus") }}
          </button>
          <button
            class="btn btn-ghost btn-sm"
            data-testid="feishu-save-btn"
            type="button"
            :disabled="saving"
            :title="t('feishu.btn.saveTitle')"
            @click="saveFeishuConfig"
          >
            💾 {{ t("common.save") }}
          </button>
        </div>
      </div>

      <!-- Per-instance proxy settings — V1 parity: the proxy section expands
           below the action row when the ⚙ proxy toggle is on. It is available
           in the stopped (not-yet-connected) state too, so the user can
           configure an outbound proxy before connecting. The empty string lets
           the read-only GET degrade gracefully until an instance is registered
           (see useChannelSettings). -->
      <ChannelProxyPanel
        kind="feishu"
        :instance-id="getInstanceId() ?? ''"
        :expanded="proxyExpanded"
      />
    </div>
  </div>
  <!-- eslint-enable vue/no-v-html -->
</template>

<style scoped>
.qai-feishu-config {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

/* Running hint banner (enhancement merged from V1 legacy components/FeishuConfigPanel.js:230-236) */
.feishu-running-hint {
  margin-top: var(--space-3);
  background: var(--banner-success-bg);
  border: 1px solid var(--banner-success-border);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  color: var(--banner-success-text);
  font-size: var(--text-base);
}

/* App Secret field wrapper: input + "saved" badge inline (proxy-range parity). */
.feishu-secret-field {
  flex: 1;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}

/*
 * "Open Feishu Open Platform" external link — promoted from an inline <a>
 * inside introLine2 to a standalone button-style affordance (decision 6).
 * `align-self: flex-start` keeps it left-aligned as its own row inside the
 * `.channel-idle-body` flex column without stretching to full width.
 * All colours flow from existing `.btn.btn-ghost.btn-sm` tokens.
 */
.feishu-open-platform-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  align-self: flex-start;
  margin: 4px 0 var(--space-2);
  text-decoration: none;
}
.feishu-open-platform-btn:hover {
  text-decoration: none;
}

/* Explicit "已保存" badge so the saved state is unambiguous (not relying on
   the grey placeholder alone). Reuses the running-hint green token set. */
.feishu-secret-saved-badge {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--banner-success-bg);
  border: 1px solid var(--banner-success-border);
  border-radius: var(--radius-sm);
  padding: 2px 8px;
  color: var(--banner-success-text);
  font-size: var(--text-sm);
  font-weight: 600;
  white-space: nowrap;
}
</style>
