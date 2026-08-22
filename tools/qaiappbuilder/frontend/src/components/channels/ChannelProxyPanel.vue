<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChannelProxyPanel — per-channel outbound HTTP proxy settings (V1 parity).
 *
 * V1 layout (`channels/wechat/WechatConfigPanel.js:223-251` +
 * `css/channels.css:98-124`): a `.channel-proxy-section` box that the
 * PARENT toggles open via the `⚙ 代理` button in the idle-actions row.
 * The section header (`.channel-proxy-title`) carries a "🔄 Sync global
 * proxy" button in its top-right corner; fields are horizontal
 * `.channel-proxy-row`s (52px `.channel-proxy-label` + flexed input);
 * a single "Save" button sits at the bottom-right.
 *
 * This component is CONTROLLED: the parent owns the open/closed state and
 * the toggle button (so the toggle can live inside the shared action row
 * exactly like V1). We only render the section body when `expanded` is true.
 *
 * Fields: address (url) / username / password (eye toggle). Persists via
 * POST `/api/{kind}/proxy`; the password is SecretStore-backed
 * (AGENTS.md §3.3) so we only ever see a `****` mask on load and never
 * send the mask back unchanged.
 *
 * Owns its own `useChannelSettings(kind, instanceId)` instance.
 */
import { onMounted } from "vue";
import { useI18n } from "vue-i18n";

import { useToastStore } from "@/stores/toast";
import { useProxy } from "@/composables/useProxy";
import { useChannelSettings, type ChannelKind } from "@/composables/useChannelSettings";

const props = defineProps<{
  kind: ChannelKind;
  instanceId: string;
  /** Whether the proxy section is expanded (controlled by the parent). */
  expanded: boolean;
}>();

const { t } = useI18n();
const toast = useToastStore();

const {
  proxyUrl,
  proxyUsername,
  proxyPassword,
  proxyShowPassword,
  saving,
  loadProxy,
  saveProxy,
  syncGlobalProxy,
} = useChannelSettings(props.kind, props.instanceId);

// Global proxy (for the "sync global" button).
const globalProxy = useProxy();

async function onSyncGlobal(): Promise<void> {
  await globalProxy.loadProxy();
  syncGlobalProxy({
    proxy_url: globalProxy.proxyUrl.value,
    proxy_username: globalProxy.proxyUsername.value,
    proxy_password: globalProxy.proxyPassword.value,
  });
  toast.push({
    id: crypto.randomUUID(),
    kind: "success",
    message: t("channels.proxySynced", "Synced from global proxy (remember to save)"),
    timeoutMs: 3000,
  });
}

async function onSave(): Promise<void> {
  const ok = await saveProxy();
  toast.push({
    id: crypto.randomUUID(),
    kind: ok ? "success" : "error",
    message: ok
      ? t("channels.proxySaved", "Proxy settings saved")
      : t("channels.proxySaveFailed", "Failed to save proxy settings"),
    timeoutMs: ok ? 2500 : 5000,
  });
}

onMounted(loadProxy);
</script>

<template>
  <!-- V1 parity: the proxy section is a `.channel-proxy-section` box rendered
       below the action row, only when the parent toggles it open. -->
  <div
    v-if="expanded"
    class="channel-proxy-section"
    :data-testid="`${kind}-proxy-panel`"
  >
    <!-- Title row with the "sync global proxy" button top-right. -->
    <div class="channel-proxy-title channel-proxy-title--row">
      <span>{{ t("channels.proxyTitle", "Proxy Settings") }}</span>
      <button
        type="button"
        class="btn btn-ghost btn-sm channel-proxy-sync-btn"
        :data-testid="`${kind}-proxy-sync-global`"
        :title="t('channels.proxySyncGlobal', 'Sync Global Proxy')"
        @click="onSyncGlobal"
      >
        🔄 {{ t("channels.proxySyncGlobal", "Sync Global Proxy") }}
      </button>
    </div>

    <!-- Address -->
    <div class="channel-proxy-row">
      <label class="channel-proxy-label">{{ t("channels.proxyAddress", "Proxy Address") }}</label>
      <input
        v-model="proxyUrl"
        class="config-input channel-proxy-input"
        type="text"
        :name="`${kind}-proxy-url`"
        autocomplete="off"
        data-1p-ignore
        data-lpignore="true"
        placeholder="http://proxy.company.com:8080"
        :data-testid="`${kind}-proxy-url`"
      />
    </div>

    <!-- Username -->
    <div class="channel-proxy-row">
      <label class="channel-proxy-label">{{ t("channels.proxyUsername", "Username") }}</label>
      <input
        v-model="proxyUsername"
        class="config-input channel-proxy-input"
        type="text"
        :name="`${kind}-proxy-username`"
        autocomplete="off"
        data-1p-ignore
        data-lpignore="true"
        :placeholder="t('channels.proxyUsernamePlaceholder', '(optional)')"
        :data-testid="`${kind}-proxy-username`"
      />
    </div>

    <!-- Password (eye toggle) -->
    <div class="channel-proxy-row">
      <label class="channel-proxy-label">{{ t("channels.proxyPassword", "Password") }}</label>
      <div class="channel-proxy-pwd">
        <input
          v-model="proxyPassword"
          class="config-input channel-proxy-input"
          :type="proxyShowPassword ? 'text' : 'password'"
          :name="`${kind}-proxy-password`"
          autocomplete="new-password"
          data-1p-ignore
          data-lpignore="true"
          :placeholder="t('channels.proxyPasswordPlaceholder', '(unchanged)')"
          :data-testid="`${kind}-proxy-password`"
        />
        <button
          type="button"
          class="btn btn-ghost btn-sm config-eye-btn"
          :data-testid="`${kind}-proxy-toggle-password`"
          @click="proxyShowPassword = !proxyShowPassword"
        >
          {{ proxyShowPassword ? "🙈" : "👁" }}
        </button>
      </div>
    </div>

    <!-- Save -->
    <div class="channel-proxy-save-row">
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        :disabled="saving"
        :data-testid="`${kind}-proxy-save`"
        @click="onSave"
      >
        {{ saving ? t("common.saving", "Saving...") : t("common.save", "Save") }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* V1 parity: the box / title / row / label all reuse the global
   `.channel-proxy-*` classes (styles channels.css). Only the small inline
   layout helpers V1 expressed via inline `style` are defined here, scoped,
   using real CSS-variable names (no --qai-* prefixes). */
.channel-proxy-title--row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px;
}

.channel-proxy-sync-btn {
  font-size: var(--text-xs);
  padding: 2px 8px;
  text-transform: none;
  letter-spacing: 0;
}

.channel-proxy-input {
  flex: 1;
  font-size: var(--text-base);
}

.channel-proxy-pwd {
  flex: 1;
  display: flex;
  gap: 6px;
}

.channel-proxy-save-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
</style>
