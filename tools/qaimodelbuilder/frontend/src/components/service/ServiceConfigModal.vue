<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ServiceConfigModal — the Service Config modal (Teleported overlay) of the
 * Service page, extracted from ServiceView.vue to keep that view within the
 * cohesion budget.
 *
 * Owns the modal chrome (overlay / dialog / header with Save+Reset+status /
 * body) and the mounted `ServiceConfigPanel`. The parent controls visibility
 * via `v-model:open` and is notified of close via the `close` event (so it can
 * re-sync launch params), and ESC handling stays in the parent (V2 per-overlay
 * self-managed ESC, mirroring ServiceView's previous useEscClose wiring).
 *
 * The modal "chrome" classes (overlay / dialog / header / title / subtitle /
 * save-status / body) come from the global, V1-parity stylesheet
 * `styles/common/settings.css`; only the two header layout helpers below have
 * no global definition, so they stay scoped here.
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import ServiceConfigPanel from "@/components/chat/ServiceConfigPanel.vue";

const open = defineModel<boolean>("open", { required: true });

const emit = defineEmits<{
  (e: "close"): void;
}>();

const { t } = useI18n();

// Ref to the mounted panel so the modal header (single-layer, V1
// index.html:3055-3083) can drive its Save/Reset actions + status pill while
// the panel keeps owning the active-tab → backend dispatch.
const configPanel = ref<InstanceType<typeof ServiceConfigPanel> | null>(null);

function requestClose(): void {
  emit("close");
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="svc-cfg-modal-overlay"
      @click.self="requestClose"
    >
      <div
        class="svc-cfg-modal"
        role="dialog"
        aria-modal="true"
      >
        <div class="svc-cfg-modal-header">
          <div class="svc-cfg-modal-titlebox">
            <span style="font-size: var(--text-xl)">⚙️</span>
            <div>
              <div class="svc-cfg-modal-title">
                {{ t("serviceConfig.title") }}
              </div>
              <div class="svc-cfg-modal-subtitle">
                {{ t("serviceConfig.subtitle") }}
              </div>
            </div>
          </div>
          <div class="svc-cfg-modal-actions">
            <button
              type="button"
              class="btn btn-primary btn-sm"
              :disabled="configPanel?.saving"
              @click="configPanel?.save()"
            >
              <span v-if="configPanel?.saving">⏳</span>
              <span v-else>💾</span>
              {{ configPanel?.saving ? t("serviceConfig.savingDots") : t("serviceConfig.saveChanges") }}
            </button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :disabled="configPanel?.loading || configPanel?.saving"
              :title="t('serviceConfig.resetTitle')"
              @click="configPanel?.reset()"
            >
              ↺ {{ t("serviceConfig.reset") }}
            </button>
            <span
              v-if="configPanel?.saveStatus"
              :class="['svc-cfg-modal-save-status', configPanel.saveStatus.type]"
            >
              {{ configPanel.saveStatus.icon }} {{ configPanel.saveStatus.message }}
            </span>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :title="t('service.close')"
              @click="requestClose"
            >
              ✕
            </button>
          </div>
        </div>
        <div class="svc-cfg-modal-body">
          <ServiceConfigPanel ref="configPanel" />
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* The modal "chrome" (overlay / dialog container / header / title / subtitle /
   save-status / body) is owned by the global, V1-parity stylesheet
   `styles/common/settings.css`. We deliberately do NOT redefine those here so
   we don't drift from V1.

   Only the two header layout helpers below have no global definition (V1 used
   inline-style flex rows, index.html:3056/3063); V2 names them into classes
   for a typed, readable template, so they stay scoped here. */
.svc-cfg-modal-titlebox {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.svc-cfg-modal-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
</style>
