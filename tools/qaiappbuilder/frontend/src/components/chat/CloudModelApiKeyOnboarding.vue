<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CloudModelApiKeyOnboarding — in-place "set API key" prompt card.
 *
 * Internal-edition enhancement: the internal edition ships cloud models
 * PRE-CONFIGURED (a provider with a model list) but with NO API key. This
 * card is shown on the chat welcome screen — via `useCloudModelStatus`'s
 * `showApiKeyPrompt` (models exist but a provider reports
 * `has_api_key === false`) — to nudge the user to set the key.
 *
 * Visual style is intentionally IDENTICAL to `CloudModelOnboarding.vue`
 * (same `.cloud-onboarding*` class structure + design tokens) for
 * consistency; only the icon (a key, to distinguish it) and the copy differ.
 *
 * Unlike the no-models onboarding card, this does NOT navigate to Settings:
 * clicking the CTA emits `configure`, and the parent opens an in-place
 * "set API key" dialog — the whole point is a convenient in-place entry.
 *
 * - Edition-agnostic: never reads an edition flag; purely "does a
 *   configured provider still lack a key right now?".
 * - Non-blocking: a gentle card under the welcome chips, NOT a modal.
 * - Theme-aware: all colors come from global CSS variables.
 */
import { useI18n } from "vue-i18n";
// Shared help-manual affordance — see components/common/HelpButton.vue.
// Docs live under `frontend/src/help-content/cloud-model-api-key.<locale>.md`.
import HelpButton from "@/components/common/HelpButton.vue";

const { t } = useI18n();

const emit = defineEmits<{
  configure: [];
}>();

const KEY_SVG =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="12" r="3"/><path d="M8.1 9.9L15 3M12.5 5.5l2 2M11 7l2 2"/></svg>';
</script>

<template>
  <div
    class="cloud-onboarding"
    role="note"
    data-testid="cloud-model-api-key-onboarding"
  >
    <div
      class="cloud-onboarding__icon"
      aria-hidden="true"
      v-html="KEY_SVG"
    />
    <div class="cloud-onboarding__body">
      <div class="cloud-onboarding__title-row">
        <p class="cloud-onboarding__title">
          {{ t("cloudModels.apiKeyOnboarding.title") }}
        </p>
        <!-- Help entry: explains WHY an API key is needed and lists the
             per-vendor request URLs. Not tied to a single provider so no
             `external-url` is passed. -->
        <HelpButton
          doc-key="cloud-model-api-key"
          size="sm"
        />
      </div>
      <p class="cloud-onboarding__desc">
        {{ t("cloudModels.apiKeyOnboarding.desc") }}
      </p>
    </div>
    <button
      type="button"
      class="btn btn-ghost btn-sm"
      data-testid="cloud-model-api-key-onboarding-cta"
      @click="emit('configure')"
    >
      {{ t("cloudModels.apiKeyOnboarding.cta") }}
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

/* Title + inline HelpButton row inside the onboarding card. Keeps the
 * baseline alignment consistent so the ℹ️ affordance reads as part of the
 * title rather than a floating action. */
.cloud-onboarding__title-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
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
