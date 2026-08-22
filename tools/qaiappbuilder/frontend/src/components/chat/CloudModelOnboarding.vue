<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CloudModelOnboarding — non-blocking cloud-model setup hint.
 *
 * V2 enhancement (edition-dual-form-design.md §6.4). Shown on the chat
 * welcome screen when no cloud model is configured (detected by
 * `useCloudModelStatus`, which reuses the existing
 * `/api/model-catalog/entries` source — no new data source). Guides the
 * user to Settings → Cloud Models.
 *
 * - Edition-agnostic: never reads an edition flag; purely "is the cloud
 *   list empty right now?".
 * - Non-blocking: a gentle card under the welcome chips, NOT a modal — it
 *   does not stop the user from chatting with a local model.
 * - No native dialogs (AGENTS.md §3.9.2): navigation is via vue-router,
 *   not window.confirm/alert/prompt.
 * - Theme-aware: all colors come from global CSS variables (no hardcoded
 *   light/dark literals), so it follows light/dark themes (AGENTS.md §8 /
 *   §3.10.6).
 */
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";

const { t } = useI18n();
const router = useRouter();

const CLOUD_SVG =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 12.5a3 3 0 01-.4-5.97 5 5 0 019.8 0A3 3 0 0113.5 12.5h-9z"/></svg>';

/**
 * Land on Settings → Cloud Models. Mirrors the existing V2 deep-link
 * convention (`useOcModels.navigateToCloudModels`,
 * `router.push({ path: "/settings", query: { tab: "cloud-models" } })`),
 * which SettingsView consumes via its `?tab=` watcher.
 */
function goToCloudModels(): void {
  void router.push({ path: "/settings", query: { tab: "cloud-models" } });
}
</script>

<template>
  <div
    class="cloud-onboarding"
    role="note"
    data-testid="cloud-model-onboarding"
  >
    <div
      class="cloud-onboarding__icon"
      aria-hidden="true"
      v-html="CLOUD_SVG"
    />
    <div class="cloud-onboarding__body">
      <p class="cloud-onboarding__title">
        {{ t("cloudModels.onboarding.title") }}
      </p>
      <p class="cloud-onboarding__desc">
        {{ t("cloudModels.onboarding.desc") }}
      </p>
    </div>
    <button
      type="button"
      class="btn btn-ghost btn-sm"
      data-testid="cloud-model-onboarding-cta"
      @click="goToCloudModels"
    >
      {{ t("cloudModels.onboarding.cta") }}
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3l4 4-4 4"/></svg>
    </button>
  </div>
</template>

<style scoped>
.cloud-onboarding {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  max-width: 32rem;
  margin: var(--space-4) auto 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-tertiary);
  text-align: left;
}

.cloud-onboarding__icon {
  flex-shrink: 0;
  font-size: var(--text-2xl);
  line-height: 1;
}

.cloud-onboarding__body {
  flex: 1 1 auto;
  min-width: 0;
}

.cloud-onboarding__title {
  margin: 0;
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--text-primary);
}

.cloud-onboarding__desc {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
}
</style>
