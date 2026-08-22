<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * RebootOverlay — full-screen service-restart transition overlay.
 *
 * V1 parity (`index.html:128-135` `.reboot-overlay` + the `<style>` block at
 * `index.html:40-59`). Shown while the service is restarting: a dimmed
 * full-screen backdrop, a spinner, the `reboot.message` text and the
 * `reboot.hint` ("page will auto-refresh once the service recovers").
 *
 * State is owned by the shared `useReboot` composable (`isRebooting`), which
 * also drives the `/api/system/health` polling + `location.reload()`. This
 * component is purely presentational (判据 1: thin component, logic in the
 * composable). Mounted once in `App.vue`.
 */
import { useI18n } from "vue-i18n";
import { useReboot } from "@/composables/useReboot";

const { t } = useI18n();
const { isRebooting } = useReboot();
</script>

<template>
  <div
    v-if="isRebooting"
    class="reboot-overlay"
    role="alertdialog"
    aria-modal="true"
    :aria-label="t('reboot.message')"
  >
    <div class="reboot-box">
      <div
        class="reboot-spinner"
        aria-hidden="true"
      />
      <div>{{ t('reboot.message') }}</div>
      <div class="reboot-hint">
        {{ t('reboot.hint') }}
      </div>
    </div>
  </div>
</template>

<style scoped>
/* V1 index.html:40-59 — .reboot-overlay / .reboot-box / .reboot-spinner. */
.reboot-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
  background: rgba(0, 0, 0, 0.78);
  display: flex;
  align-items: center;
  justify-content: center;
}

.reboot-box {
  text-align: center;
  color: #e0e6f0;
  font-size: 1.1rem;
  line-height: 2;
}

.reboot-spinner {
  width: 52px;
  height: 52px;
  border: 4px solid rgba(255, 255, 255, 0.15);
  border-top-color: #7eb8f7;
  border-radius: 50%;
  animation: reboot-spin 0.85s linear infinite;
  margin: 0 auto 18px;
}

@keyframes reboot-spin {
  to {
    transform: rotate(360deg);
  }
}

.reboot-hint {
  font-size: 0.83rem;
  color: #8a9ab5;
  margin-top: 4px;
}
</style>
