<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->
<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import { useVlaSimulation } from "@/composables/useVlaSimulation";

const { t } = useI18n();
const { serverUrl } = useVlaSimulation();
const baseSandbox = "allow-scripts allow-forms allow-popups allow-downloads allow-pointer-lock";
const iframeSandbox = computed(() => {
  const url = serverUrl.value;
  if (url === "") return baseSandbox;
  try {
    // A same-origin frame with scripts + allow-same-origin can remove its
    // sandbox. Keep that permission only for the separate configured host.
    return new URL(url).origin === window.location.origin
      ? baseSandbox
      : `${baseSandbox} allow-same-origin`;
  } catch {
    return baseSandbox;
  }
});
</script>

<template>
  <section
    class="vla-viewport"
    data-testid="vla-simulation-viewport"
    :aria-label="t('index.vlaSimulationMode')"
  >
    <!-- Scripts, forms, downloads, popups and pointer lock support the
         interactive UI. Same-origin pages never receive allow-same-origin. -->
    <iframe
      v-if="serverUrl"
      :src="serverUrl"
      class="vla-viewport-frame"
      :title="t('index.vlaSimulationMode')"
      referrerpolicy="no-referrer"
      :sandbox="iframeSandbox"
      data-testid="vla-simulation-frame"
    ></iframe>
    <div v-else class="vla-viewport-empty" data-testid="vla-simulation-unconfigured">
      <strong>{{ t("index.vlaSimulationNotConfigured") }}</strong>
      <p>{{ t("index.vlaSimulationPanelHint") }}</p>
    </div>
  </section>
</template>

<style scoped>
.vla-viewport {
  display: flex;
  flex: 0 1 auto;
  min-width: 0;
  min-height: 200px;
  height: min(58vh, 640px);
  max-height: 58vh;
  margin: 0 12px 8px;
  overflow: hidden;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 10px;
}

.vla-viewport-frame {
  width: 100%;
  height: 100%;
  min-height: 0;
  border: 0;
  background: #0a0e1a;
}

.vla-viewport-empty {
  display: grid;
  flex: 1;
  align-content: center;
  justify-items: center;
  gap: 8px;
  padding: 24px;
  color: var(--text-secondary);
  text-align: center;
}

.vla-viewport-empty strong {
  color: var(--text-primary);
  font-size: 15px;
}

.vla-viewport-empty p {
  max-width: 520px;
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
}
</style>
