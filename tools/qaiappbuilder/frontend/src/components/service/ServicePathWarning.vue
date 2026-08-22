<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ServicePathWarning — Region 2 of the Service page (extracted from
 * ServiceView.vue to keep that view within the cohesion budget).
 *
 * Presentational: renders the path-safety warning banner (V1
 * index.html:2756-2775) shown when the service reports a non-ASCII /
 * spaces-in-path warning while stopped. State stays in the page-level
 * `useServiceControl`; this panel only renders + emits the "re-download"
 * navigation intent back to the parent.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

const props = defineProps<{
  /** svc.serviceStatus.path_warning — the backend-reported offending path. */
  pathWarning: string;
}>();

const emit = defineEmits<{
  (e: "redownload"): void;
}>();

const { t } = useI18n();

/**
 * Path-safety migrate sentence (V1 index.html:2766). V1 renders
 * `warnPathMigrate` via `v-html`, interpolating `{bold}` with a `<strong>`
 * wrapping the "pure-English, space-free" phrase. Reproduced here so the
 * banner reads identically. The interpolated phrase is a static locale
 * string (no user input), so the v-html is safe.
 */
const warnPathMigrateHtml = computed<string>(() =>
  t("service.warnPathMigrate", {
    bold: `<strong>${t("service.pureEnglishNoSpaces")}</strong>`,
  }),
);
</script>

<template>
  <div class="service-path-warning">
    <div class="path-warn-title">
      <span>⚠️</span>
      <span>{{ t("service.warnPathChineseSpaces") }}</span>
    </div>
    <div class="path-warn-detail">
      {{ pathWarning }}
    </div>
    <div class="path-warn-body">
      {{ t("service.warnPathQnnDesc1") }}<br />
      <!-- eslint-disable-next-line vue/no-v-html -->
      <span v-html="warnPathMigrateHtml" />
      <code class="path-warn-example">C:\QAI\GenieAPIService\</code>
      {{ t("service.orWord") }}
      <code class="path-warn-example">D:\AI\models\</code>
    </div>
    <div class="path-warn-actions">
      <a
        href="#"
        @click.prevent="emit('redownload')"
      >{{ t("service.redownloadInstall") }}</a>
    </div>
  </div>
</template>

<style scoped>
.service-path-warning {
  background: var(--banner-warn-bg);
  border: 1px solid var(--banner-warn-border);
  border-radius: 8px;
  padding: 12px 16px;
  font-size: var(--text-base);
  line-height: 1.7;
  color: var(--banner-warn-text);
}
.path-warn-title {
  font-weight: 700;
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.path-warn-detail {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  background: var(--bg-tertiary);
  border-radius: 4px;
  padding: 6px 10px;
  margin: 6px 0;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-secondary);
}
.path-warn-body {
  color: var(--banner-warn-text);
}
.path-warn-example {
  background: var(--bg-tertiary);
  padding: 1px 5px;
  border-radius: 3px;
  font-size: var(--text-xs);
  font-family: var(--font-mono);
}
.path-warn-actions {
  margin-top: 8px;
  display: flex;
  gap: 12px;
}
.path-warn-actions a {
  color: var(--accent);
  font-weight: 600;
  text-decoration: none;
  font-size: var(--text-sm);
}
</style>
