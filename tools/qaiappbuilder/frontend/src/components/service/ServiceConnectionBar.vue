<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ServiceConnectionBar — Region 1 (collapsible connection bar) of the Service
 * page, extracted from ServiceView.vue to keep that view within the cohesion
 * budget.
 *
 * Presentational: renders the local port input, the "Test" probe
 * + reachable/unreachable result, and the Save button. All state stays in the
 * page-level `useServiceControl`; the port is a v-model binding, and
 * test / save are emitted back up.
 */
import { useI18n } from "vue-i18n";

defineProps<{
  localUrl: string;
  connectionTesting: boolean;
  connectionTestResult: "ok" | "fail" | null;
  svcParamsSaving: boolean;
}>();

const emit = defineEmits<{
  (e: "test"): void;
  (e: "save"): void;
}>();

// Two-way scalar bindings (each backed by the page-level useServiceControl's
// reactive svcParams).
const collapsed = defineModel<boolean>("collapsed", { required: true });
const localPort = defineModel<number>("localPort", { required: true });

const { t } = useI18n();
</script>

<template>
  <div class="service-params-section service-connection-section">
    <div
      class="service-connection-bar"
      @click="collapsed = !collapsed"
    >
      <span
        class="conn-caret"
        :style="{ transform: collapsed ? 'rotate(-90deg)' : 'rotate(0deg)' }"
      >▼</span>
      <span class="conn-label">🖥️ {{ t("service.connection") }}</span>
      <span class="conn-value">
        {{ t("service.localPrefix") }}{{ localUrl }}
      </span>
      <span class="conn-toggle">{{ collapsed ? t("service.editArrow") : t("service.closeArrow") }}</span>
    </div>

    <div
      v-show="!collapsed"
      class="service-connection-body"
    >
      <div class="conn-inputs">
        <div class="conn-field">
          <label class="conn-field-label">{{ t("service.port") }}</label>
          <input
            class="param-input conn-port"
            type="number"
            min="1"
            max="65535"
            :value="localPort"
            @input="(e) => (localPort = +(e.target as HTMLInputElement).value)"
          />
        </div>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="connectionTesting"
          @click="emit('test')"
        >
          <span
            v-if="connectionTesting"
            class="spinner svc-btn-spinner"
            aria-hidden="true"
          ></span>
          <span v-else>↗</span>
          {{ t("service.test") }}
        </button>
        <span
          v-if="connectionTestResult === 'ok'"
          class="conn-result conn-result-ok"
        >✓ {{ t("service.reachable") }}</span>
        <span
          v-if="connectionTestResult === 'fail'"
          class="conn-result conn-result-fail"
        >✗ {{ t("service.unreachable") }}</span>
      </div>

      <div>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="svcParamsSaving"
          @click="emit('save')"
        >
          <span
            v-if="svcParamsSaving"
            class="spinner svc-btn-spinner"
            aria-hidden="true"
          ></span>
          <span v-else>💾</span>
          {{ t("service.save") }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.service-connection-section {
  margin-bottom: var(--space-2);
}
.service-params-section {
  padding: var(--space-4) var(--space-5);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  flex-shrink: 0;
}
.service-connection-bar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  /* V1 service.css:438-448 — low-key chip: tertiary bg + padding + radius. */
  padding: var(--space-2) var(--space-4);
  background: var(--bg-tertiary);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}
.conn-caret {
  font-size: var(--text-xs);
  color: var(--text-muted);
  transition: transform 150ms;
}
.conn-label {
  /* V1 service.css:450-454 — smaller, muted, no shrink. */
  color: var(--text-muted);
  font-size: var(--text-xs);
  font-weight: 600;
  flex-shrink: 0;
}
.conn-value {
  /* V1 service.css:456-463 — mono, primary, ellipsis-truncated. */
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conn-toggle {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--text-muted);
}
.service-connection-body {
  margin-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.conn-inputs {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.conn-field {
  display: flex;
  align-items: center;
  gap: 6px;
}
.conn-field-label {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  white-space: nowrap;
}
.conn-port {
  width: 80px;
}
.conn-result {
  font-size: var(--text-sm);
}
.conn-result-ok {
  color: var(--success);
}
.conn-result-fail {
  color: var(--error);
}
.param-input {
  padding: var(--space-2) var(--space-3);
  /* V1 service.css:128 — semantic input bg token (no hardcoded fallback). */
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: var(--text-sm);
  outline: none;
  transition: border-color var(--duration-fast) var(--ease-out),
    box-shadow var(--duration-fast) var(--ease-out);
}
.param-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-muted);
}
.svc-btn-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-width: 2px;
  vertical-align: -1px;
}
</style>
