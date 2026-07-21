<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * WechatConfigPanel — single-instance WeChat channel (V1-aligned).
 *
 * UI mirrors the V1 verified single-instance WeChat panel
 * (`channels/wechat/WechatConfigPanel.js`): an idle "Connect WeChat" state,
 * then a QR code with countdown + manual refresh while waiting to scan, a
 * "scanned" hint, and connected/expired/error branches. There is NO instance
 * list or register form — WeChat is a single instance and the backend
 * instance_id is handled transparently by `useWechat`.
 *
 * Reuses the V1 CSS classes from styles/channels/channels.css
 * (.channel-qr / .channel-qr-footer / .channel-qr-countdown /
 * .channel-qr-refresh / .channel-status-text / .channel-idle-* /
 * .channel-footer-row / .channel-status-inline / .channel-status-dot).
 */
import { onMounted, onBeforeUnmount, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useWechatShared } from "@/composables/useWechat";
import ChannelModelSelector from "./ChannelModelSelector.vue";
import ChannelProxyPanel from "./ChannelProxyPanel.vue";
// NOTE: the HelpButton for this channel lives in the channel-card HEADER
// (see `ChannelsView.vue` — directly to the left of the existing ℹ️
// info button), not inside this panel. That placement keeps the two
// discoverable-help affordances side-by-side (Help + Info) at the top
// of the card, matching the pattern users already know from the info
// button. See `ChannelsView.vue::channel-card-header` for the wiring.

const { t } = useI18n();

// V1 parity: the proxy section is toggled by the `⚙ 代理` button inside the
// idle-actions row (WechatConfigPanel.js:206-209); the section expands below
// the action row. The parent owns this state so the toggle can live in the
// shared action row.
const proxyExpanded = ref(false);

const {
  wechatStatus,
  wechatQrUrl,
  wechatQrLoading,
  wechatQrCountdown,
  wechatAutoConnect,
  wechatConfigSaving,
  wechatLogin,
  wechatLogout,
  requestQr,
  loadWechatStatus,
  loadWechatConfig,
  saveWechatConfig,
  dispose,
  getInstanceId,
  resolveInstanceId,
} = useWechatShared();

// V1 parity: on mount, sync the real connection status + config so the badge
// and the auto-connect toggle reflect the persisted state after a reload.
onMounted(() => {
  void loadWechatStatus();
  void loadWechatConfig();
});
// The shared instance may be owned by the parent view; only the owner should
// tear down timers. When injected, the parent disposes; the fallback (own)
// instance disposes here. Disposing twice is a safe no-op.
onBeforeUnmount(dispose);
</script>

<template>
  <div
    class="qai-wechat-config"
    data-testid="wechat-config-panel"
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
      kind="wechat"
      :instance-id="getInstanceId() ?? ''"
      :resolve-instance-id="resolveInstanceId"
    />

    <!-- connected -->
    <div
      v-if="wechatStatus === 'connected'"
      data-testid="wechat-connected"
    >
      <div class="channel-footer-row">
        <div class="channel-status-inline">
          <span class="channel-status-dot" />
          <span>{{ t("channels.wechat.connectedMsg") }}</span>
        </div>
        <!-- Kept consistent with the Feishu card (V2 enhancement): a
             refresh-status button + running-hint banner in the connected
             state. v0.5 had neither on either channel; the user asked to
             keep both channels consistent, so WeChat mirrors Feishu. -->
        <button
          class="btn btn-ghost btn-sm"
          data-testid="wechat-refresh-running-btn"
          type="button"
          @click="loadWechatStatus"
        >
          🔄 {{ t("channels.wechat.refreshStatus", "刷新状态") }}
        </button>
        <button
          class="btn btn-ghost btn-sm channel-disconnect-btn"
          data-testid="wechat-disconnect-btn"
          type="button"
          @click="wechatLogout"
        >
          {{ t("channels.disconnect") }}
        </button>
      </div>
      <div class="wechat-running-hint">
        ✅ {{ t("channels.wechat.runningHint", "微信通道运行中，机器人已连接，可接收消息。") }}
      </div>
    </div>

    <!-- logging_in / scanned: QR image + countdown + manual refresh -->
    <div
      v-else-if="wechatStatus === 'logging_in' || wechatStatus === 'scanned'"
      data-testid="wechat-qr-section"
    >
      <div
        v-if="wechatQrUrl"
        style="text-align: center"
      >
        <img
          :src="wechatQrUrl"
          class="channel-qr"
          data-testid="wechat-qr-image"
          :alt="t('channels.scan_wechat')"
        />
        <div
          class="channel-status-text"
          data-testid="wechat-qr-status"
        >
          <span
            v-if="wechatStatus === 'scanned'"
            style="color: #4caf50"
          >{{ t("channels.qr_scanned") }}</span>
          <span v-else>{{ t("channels.scan_wechat") }}</span>
        </div>
        <div
          class="channel-qr-footer"
        >
          <span
            v-if="wechatStatus === 'logging_in' && wechatQrCountdown > 0"
            class="channel-qr-countdown"
            data-testid="wechat-qr-countdown"
          >{{ t("channels.qr_countdown", { seconds: wechatQrCountdown }) }}</span>
          <button
            v-if="wechatStatus === 'logging_in'"
            class="btn btn-ghost btn-sm channel-qr-refresh"
            data-testid="wechat-qr-refresh-btn"
            type="button"
            @click="requestQr"
          >
            {{ t("channels.qr_refresh") }}
          </button>
          <!-- Escape hatch — v0.5 缺失导致 `scanned`/`logging_in` 状态下
               (尤其是 SDK 请求上游失败拿到 broken image 时) 用户没有任何
               按钮回到 idle 去改代理/其它设置。用 wechatLogout 复用
               既有语义（清 challenge → status 回 idle），不新增抽象。
               `expired`/`error` 分支各自有出口按钮，这里补齐对称。 -->
          <button
            class="btn btn-ghost btn-sm"
            data-testid="wechat-qr-cancel-btn"
            type="button"
            @click="wechatLogout"
          >
            {{ t("common.cancel") }}
          </button>
        </div>
      </div>
      <div
        v-else
        style="display: flex; flex-direction: column; align-items: center; gap: var(--space-3); padding: 32px"
      >
        <div
          class="spinner"
          style="width: 28px; height: 28px; border-width: 3px"
          data-testid="wechat-qr-loading"
        />
        <!-- Escape hatch during the spinner phase — SDK could be stuck
             (e.g. proxy misconfigured → upstream returns HTML → JSON decode
             fails → qr_url never published → spinner spins forever). User
             needs a way back to idle to fix config. Same wechatLogout
             semantic as the footer button above. -->
        <button
          class="btn btn-ghost btn-sm"
          data-testid="wechat-qr-cancel-btn-loading"
          type="button"
          @click="wechatLogout"
        >
          {{ t("common.cancel") }}
        </button>
      </div>
    </div>

    <!-- expired -->
    <div
      v-else-if="wechatStatus === 'expired'"
      style="text-align: center; padding: 16px 0"
      data-testid="wechat-qr-expired"
    >
      <div style="font-size: var(--text-2xl); margin-bottom: 8px">
        ⏰
      </div>
      <div class="channel-status-text">
        {{ t("channels.qr_expired") }}
      </div>
      <button
        class="btn btn-primary btn-sm"
        data-testid="wechat-qr-reget-btn"
        type="button"
        @click="requestQr"
      >
        {{ t("channels.qr_reget") }}
      </button>
    </div>

    <!-- error -->
    <div
      v-else-if="wechatStatus === 'error'"
      style="text-align: center; padding: 16px 0"
      data-testid="wechat-error"
    >
      <div style="font-size: var(--text-2xl); margin-bottom: 8px">
        ⚠️
      </div>
      <div class="channel-status-text">
        {{ t("channels.wechat.errorMsg") }}
      </div>
      <div style="display: flex; gap: 8px; justify-content: center; margin-top: 8px; flex-wrap: wrap">
        <button
          class="btn btn-primary btn-sm"
          data-testid="wechat-retry-btn"
          type="button"
          @click="wechatLogin(false)"
        >
          {{ t("common.retry") }}
        </button>
        <button
          class="btn btn-ghost btn-sm"
          type="button"
          @click="wechatLogout"
        >
          {{ t("channels.disconnect") }}
        </button>
      </div>
    </div>

    <!-- idle -->
    <div
      v-else
      data-testid="wechat-idle"
    >
      <div class="channel-idle-body">
        <div class="channel-idle-hint">
          {{ t("channels.wechat.idleHint") }}
        </div>

        <!-- Auto-connect on service start (V1 wechatAutoConnect) -->
        <div
          class="channel-proxy-row"
          style="margin-top: 10px"
        >
          <label
            style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: var(--text-base); color: var(--text-secondary)"
            data-testid="wechat-autoconnect"
          >
            <input
              v-model="wechatAutoConnect"
              type="checkbox"
              style="width: 15px; height: 15px; cursor: pointer"
              data-testid="wechat-autoconnect-checkbox"
            />
            {{ t("wechat.autoConnect") }}
          </label>
        </div>

        <div
          class="channel-idle-actions"
          style="margin-top: 12px"
        >
          <button
            class="btn btn-primary btn-sm"
            data-testid="wechat-connect-btn"
            type="button"
            @click="wechatLogin(false)"
          >
            {{ t("channels.wechat.connect") }}
          </button>
          <!-- Re-scan login (force=true): bypass any stored credentials and
               force a fresh QR scan. v0.5 parity — v0.5 reached this via
               "断开连接(清凭证) → 连接", but the user-facing intent ("我要重新
               扫码/换个微信号") is served more directly by an explicit force
               button. -->
          <button
            class="btn btn-ghost btn-sm"
            data-testid="wechat-rescan-btn"
            type="button"
            :title="t('channels.wechat.rescanTitle', '强制重新扫码登录（忽略已保存凭证）')"
            @click="wechatLogin(true)"
          >
            🔄 {{ t("channels.wechat.rescan", "重新扫码登录") }}
          </button>
          <!-- V1 parity: ⚙ proxy toggle inline in the action row
               (WechatConfigPanel.js:206-209) -->
          <button
            class="btn btn-ghost btn-sm channel-proxy-toggle"
            :class="{ active: proxyExpanded }"
            data-testid="wechat-proxy-toggle"
            type="button"
            :title="t('channels.proxyTitle')"
            @click="proxyExpanded = !proxyExpanded"
          >
            ⚙ {{ t("channels.proxyLabel") }}
          </button>
          <button
            class="btn btn-ghost btn-sm"
            data-testid="wechat-save-btn"
            type="button"
            :disabled="wechatConfigSaving"
            :title="t('wechat.btn.saveTitle')"
            @click="saveWechatConfig"
          >
            💾 {{ t("common.save") }}
          </button>
        </div>
      </div>

      <!-- Per-instance proxy settings — V1 parity: the proxy section expands
           below the action row when the ⚙ proxy toggle is on. It is available
           in the idle (not-yet-connected) state too, so the user can configure
           an outbound proxy before connecting. The empty string lets the
           read-only GET degrade gracefully until an instance is registered
           (see useChannelSettings). -->
      <ChannelProxyPanel
        kind="wechat"
        :instance-id="getInstanceId() ?? ''"
        :expanded="proxyExpanded"
      />
    </div>
  </div>
</template>

<style scoped>
.qai-wechat-config {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

/* Running-hint banner in the connected state — mirrors the Feishu card
   (.feishu-running-hint) so both channels look consistent. */
.wechat-running-hint {
  margin-top: var(--space-3);
  background: var(--banner-success-bg);
  border: 1px solid var(--banner-success-border);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  color: var(--banner-success-text);
  font-size: var(--text-base);
}
</style>
