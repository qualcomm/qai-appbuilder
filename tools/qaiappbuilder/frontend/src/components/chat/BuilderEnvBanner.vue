<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  BuilderEnvBanner — thin strip at the top of the chat page.

  Shown only when the model-conversion environment (x64 py3.10 venv + QAIRT SDK) is not
  installed, or is being installed / failed to install. A click on the action goes to the
  Downloads page where the install is started and its progress shown. It never blocks the
  chat: it is a plain strip above the message list.

  "Dismiss" hides it for this browser session only (sessionStorage) -- the environment is
  still missing, so the banner returns on the next visit; it is a reminder, not a modal.
  While an install is running it cannot be dismissed (the progress is the point).
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";

import { useBuilderEnv } from "@/composables/useBuilderEnv";

const { t } = useI18n();
const router = useRouter();
const env = useBuilderEnv();

onMounted(env.acquire);
onBeforeUnmount(env.release);

const DISMISS_KEY = "qai.builderEnv.bannerDismissed.v1";

function readDismissed(): boolean {
  try {
    return window.sessionStorage.getItem(DISMISS_KEY) === "1";
  } catch {
    return false;
  }
}

const dismissed = ref(readDismissed());

function dismiss(): void {
  dismissed.value = true;
  try {
    window.sessionStorage.setItem(DISMISS_KEY, "1");
  } catch {
    // storage unavailable: dismissal lasts until this component is destroyed
  }
}

const phase = env.phase;

/** Which phases deserve a strip. `unavailable` is informational only, so no strip. */
const visible = computed(() => {
  switch (phase.value) {
    case "installing":
      return true; //  always: progress matters more than tidiness
    case "missing":
    case "failed":
    case "cancelled":
      return !dismissed.value;
    default:
      return false; // unknown / ready / unavailable
  }
});

const tone = computed(() =>
  phase.value === "failed" ? "error" : phase.value === "installing" ? "info" : "warn",
);

const text = computed(() => {
  switch (phase.value) {
    case "installing":
      return t("builderEnv.banner.installing", { percent: env.percent.value });
    case "failed":
      return t("builderEnv.banner.failed");
    case "cancelled":
      return t("builderEnv.banner.cancelled");
    default:
      return t("builderEnv.banner.missing");
  }
});

const actionLabel = computed(() =>
  phase.value === "installing" ? t("builderEnv.banner.viewProgress") : t("builderEnv.banner.goInstall"),
);

function go(): void {
  void router.push({ path: "/downloads", query: { tab: "converter" } });
}
</script>

<template>
  <transition name="scroll-btn-fade">
    <div
      v-if="visible"
      class="builder-env-banner"
      :class="`builder-env-banner--${tone}`"
      data-testid="builder-env-banner"
      :data-phase="phase"
      role="status"
    >
      <span
        class="builder-env-banner__icon"
        aria-hidden="true"
      >{{ phase === "installing" ? "⏳" : "⚠" }}</span>
      <span
        class="builder-env-banner__text"
        data-testid="builder-env-banner-text"
      >{{ text }}</span>
      <button
        type="button"
        class="builder-env-banner__go"
        data-testid="builder-env-banner-go"
        @click="go"
      >
        {{ actionLabel }}
      </button>
      <button
        v-if="phase !== 'installing'"
        type="button"
        class="builder-env-banner__dismiss"
        :aria-label="t('common.close', '×')"
        data-testid="builder-env-banner-dismiss"
        @click="dismiss"
      >
        ×
      </button>
    </div>
  </transition>
</template>

<style scoped>
/* Same strip geometry as ChatView's fork-lineage banner; tone from the banner tokens. */
.builder-env-banner {
  display: flex;
  align-items: center;
  gap: var(--space-2, 8px);
  padding: var(--space-2, 8px) var(--space-3, 12px);
  margin: 0;
  font-size: var(--text-sm, 13px);
  flex-shrink: 0;
  position: relative;
  z-index: 1;
  border-bottom: 1px solid var(--border-subtle, rgb(255 255 255 / 8%));
}

.builder-env-banner--warn {
  background: var(--banner-warn-bg);
  color: var(--warning);
}

.builder-env-banner--info {
  background: var(--banner-info-bg);
  color: var(--info);
}

.builder-env-banner--error {
  background: var(--banner-error-bg);
  color: var(--error);
}

.builder-env-banner__icon {
  font-size: 14px;
}

.builder-env-banner__text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.builder-env-banner__go {
  background: none;
  border: 1px solid currentcolor;
  border-radius: var(--radius-sm, 4px);
  padding: 2px 8px;
  color: inherit;
  cursor: pointer;
  font-size: var(--text-xs, 12px);
}

.builder-env-banner__go:hover {
  background: rgb(127 127 127 / 15%);
}

.builder-env-banner__dismiss {
  background: none;
  border: none;
  color: inherit;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
}
</style>
