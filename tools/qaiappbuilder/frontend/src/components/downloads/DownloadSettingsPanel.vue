<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  DownloadSettingsPanel — V1 collapsible "Download Settings" region.

  Edits the forge_config download section:
    save_dir / version_list_url / catalog_url / fetch_timeout_seconds /
    download_timeout_seconds / ssl_verify

  Shows a yellow warning banner above + an inline warning under the
  save_dir input when the save_dir contains non-ASCII or whitespace
  characters (V1 `hasUnsafePath`, prevents QNN load failures).

  V1 reference: DownloadCenterPanel.js:155-163 (warn banner) +
  DownloadCenterPanel.js:165-234 (collapsible settings region).
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";

import type { DownloadSettings } from "@/types/downloads";
import { hasUnsafePath } from "@/composables/downloads/format";
import ToggleSwitch from "@/components/chat/service-config/ToggleSwitch.vue";
// Shared help-manual affordance — see components/common/HelpButton.vue.
// Docs live under `frontend/src/help-content/download-settings.<locale>.md`.
import HelpButton from "@/components/common/HelpButton.vue";

interface Props {
  settings: DownloadSettings;
  saving: boolean;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  "update:settings": [next: DownloadSettings];
  save: [];
}>();

const { t } = useI18n();

/** Local-collapsed flag (V1 default: collapsed). */
const expanded = ref(false);
function toggle(): void {
  expanded.value = !expanded.value;
}

const isSaveDirUnsafe = computed<boolean>(() =>
  hasUnsafePath(props.settings.save_dir),
);

function patch(partial: Partial<DownloadSettings>): void {
  emit("update:settings", { ...props.settings, ...partial });
}

function clamp(n: number, min: number, max: number): number {
  if (!Number.isFinite(n)) return min;
  return Math.min(max, Math.max(min, Math.round(n)));
}
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html renders only our own static, trusted i18n catalog strings (no user/remote input); not an XSS vector. -->
  <div class="dc-settings">
    <!-- V1 yellow warn banner above the collapsed area when save_dir is unsafe.
         Reuses the global `.dc-info-banner.warning` chrome from
         `styles/downloads/downloads.css:88-121` (V1 parity:
         DownloadCenterPanel.js:155-163 used `.dc-info-banner.warning`). -->
    <div
      v-if="isSaveDirUnsafe"
      class="dc-info-banner warning"
      role="alert"
    >
      <span
        class="dc-settings__warn-icon"
        aria-hidden="true"
      >⚠️</span>
      <div class="dc-settings__warn-body">
        <strong>{{ t("downloads.saveDirUnsafeTitle") }}</strong>
        <code>{{ settings.save_dir }}</code>
        <!-- V1 parity (DownloadCenterPanel.js:161): the warn banner renders the
             full `saveDirUnsafeMsg` body explaining WHY the path is unsafe and
             HOW to fix it (the message contains markup, so v-html as in V1). -->
        <span
          class="dc-settings__warn-msg"
          v-html="t('downloads.saveDirUnsafeMsg')"
        ></span>
        <button
          type="button"
          class="dc-settings__warn-link"
          @click="expanded = true"
        >
          {{ t("downloads.settings") }} →
        </button>
      </div>
    </div>

    <!-- V1 .dc-settings-panel: bordered card; header is a full-width
         clickable bar; body (border-top) appears when expanded. -->
    <div class="dc-settings__panel">
      <div class="dc-settings__header-row">
        <button
          type="button"
          class="dc-settings__toggle"
          :aria-expanded="expanded"
          @click="toggle"
        >
          <span
            class="dc-settings__chevron"
            :class="{ 'is-open': expanded }"
          >▶</span>
          {{ t("downloads.settings") }}
        </button>
        <!-- Help affordance for aria2c / proxy / SSL / save_dir safety.
             Sits outside the toggle button so clicking ℹ️ never
             accidentally collapses/expands the settings section. External
             link points at the aria2 project homepage. -->
        <HelpButton
          doc-key="download-settings"
          external-url="https://aria2.github.io/"
          size="sm"
        />
      </div>

      <div
        v-if="expanded"
        class="dc-settings__form"
      >
        <!-- save_dir -->
        <div class="dc-settings__field">
          <label
            class="dc-settings__label"
            for="dc-save-dir"
          >
            {{ t("downloads.saveDir") }}
          </label>
          <input
            id="dc-save-dir"
            type="text"
            class="dc-settings__input"
            :class="{ 'is-unsafe': isSaveDirUnsafe }"
            :value="settings.save_dir"
            placeholder="(default: QAIModelBuilder/downloads/)"
            spellcheck="false"
            @input="(e) => patch({ save_dir: (e.target as HTMLInputElement).value })"
          />
          <p class="dc-settings__hint">
            {{ t("downloads.saveDirDesc") }}
          </p>
          <p
            v-if="isSaveDirUnsafe"
            class="dc-settings__inline-warn"
            role="alert"
          >
            {{ t("downloads.saveDirInputUnsafe") }}
          </p>
        </div>

        <!-- version_list_url -->
        <div class="dc-settings__field">
          <label
            class="dc-settings__label"
            for="dc-version-url"
          >
            {{ t("downloads.versionListUrl") }}
          </label>
          <input
            id="dc-version-url"
            type="url"
            class="dc-settings__input"
            :value="settings.version_list_url"
            placeholder="https://github.com/qualcomm/qai-appbuilder/releases/download/v2.34.0/release_manifest.json"
            spellcheck="false"
            @input="
              (e) =>
                patch({
                  version_list_url: (e.target as HTMLInputElement).value,
                })
            "
          />
          <p class="dc-settings__hint">
            {{ t("downloads.versionListUrlDesc") }}
          </p>
        </div>

        <!-- catalog_url -->
        <div class="dc-settings__field">
          <label
            class="dc-settings__label"
            for="dc-catalog-url"
          >
            {{ t("downloads.modelCatalogUrl") }}
          </label>
          <input
            id="dc-catalog-url"
            type="url"
            class="dc-settings__input"
            :value="settings.catalog_url"
            placeholder="https://github.com/qualcomm/qai-appbuilder/releases/download/v2.34.0/model_catalog.json"
            spellcheck="false"
            @input="
              (e) =>
                patch({ catalog_url: (e.target as HTMLInputElement).value })
            "
          />
          <p class="dc-settings__hint">
            {{ t("downloads.modelCatalogUrlDesc") }}
          </p>
        </div>

        <!-- timeouts (5..120 / 30..3600 per V1 input bounds) -->
        <div class="dc-settings__row">
          <div class="dc-settings__field dc-settings__field--inline">
            <label
              class="dc-settings__label"
              for="dc-fetch-timeout"
            >
              {{ t("downloads.fetchTimeout") }}
            </label>
            <input
              id="dc-fetch-timeout"
              type="number"
              min="5"
              max="120"
              class="dc-settings__input dc-settings__input--narrow"
              :value="settings.fetch_timeout_seconds"
              @input="
                (e) =>
                  patch({
                    fetch_timeout_seconds: clamp(
                      Number((e.target as HTMLInputElement).value),
                      5,
                      120,
                    ),
                  })
              "
            />
          </div>
          <div class="dc-settings__field dc-settings__field--inline">
            <label
              class="dc-settings__label"
              for="dc-download-timeout"
            >
              {{ t("downloads.downloadTimeout") }}
            </label>
            <input
              id="dc-download-timeout"
              type="number"
              min="30"
              max="3600"
              class="dc-settings__input dc-settings__input--narrow"
              :value="settings.download_timeout_seconds"
              @input="
                (e) =>
                  patch({
                    download_timeout_seconds: clamp(
                      Number((e.target as HTMLInputElement).value),
                      30,
                      3600,
                    ),
                  })
              "
            />
          </div>
        </div>

        <!-- ssl_verify — V1 parity: global `.toggle / .toggle-slider` pill,
           not a browser-default checkbox (DownloadCenterPanel.js:218-221). -->
        <div class="dc-settings__field dc-settings__field--toggle">
          <label class="dc-settings__toggle-label">
            <ToggleSwitch
              :model-value="settings.ssl_verify"
              :aria-label="t('downloads.verifySsl')"
              @update:model-value="(v) => patch({ ssl_verify: v })"
            />
            <span>{{ t("downloads.verifySsl") }}</span>
          </label>
          <p class="dc-settings__hint">
            {{ t("downloads.verifySslDesc") }}
          </p>
        </div>

        <div class="dc-settings__actions">
          <button
            type="button"
            class="btn btn-primary btn-sm"
            :disabled="saving"
            @click="emit('save')"
          >
            <span
              v-if="saving"
              class="spinner"
              style="width: 12px; height: 12px; border-width: 2px; margin-right: 4px"
            ></span>
            {{ saving ? t("downloads.saving") : t("downloads.saveSettings") }}
          </button>
        </div>
      </div>
    </div>
  </div>
  <!-- eslint-enable vue/no-v-html -->
</template>

<style scoped>
.dc-settings {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

/* `.dc-info-banner.warning` chrome comes from the global download styles
   (downloads.css:88-121). Below are only the BEM children specific to the
   "save_dir is unsafe" warn slot inside the banner. */
.dc-settings__warn-icon {
  font-size: var(--text-lg);
  line-height: 1.2;
  flex-shrink: 0;
}

.dc-settings__warn-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1 1 auto;
  min-width: 0;
}

.dc-settings__warn-body code {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  background: var(--bg-code);
  padding: 1px 6px;
  border-radius: 3px;
  word-break: break-all;
}

.dc-settings__warn-msg {
  font-size: var(--text-xs);
  color: inherit;
  line-height: 1.6;
}

.dc-settings__warn-msg :deep(code) {
  font-family: var(--font-mono);
  background: var(--bg-code);
  padding: 1px 5px;
  border-radius: 3px;
}

.dc-settings__warn-link {
  align-self: flex-start;
  background: transparent;
  border: none;
  color: var(--accent);
  cursor: pointer;
  padding: 0;
  font-weight: 600;
  font-size: var(--text-sm);
}

.dc-settings__warn-link:hover {
  text-decoration: underline;
}

/*
  V1 .dc-settings-panel (downloads.css:990-1027): a bordered card whose
  header is a FULL-WIDTH clickable bar (not a small pill). Rounded corners
  live on the card with overflow:hidden so the header/body share them.
*/
.dc-settings__panel {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

/* Header row: the collapsible toggle takes all remaining width, HelpButton
 * sits at the right edge. Sharing the header bar background so the ℹ️
 * affordance reads as part of the header rather than as an orphaned icon
 * floating above the panel. */
.dc-settings__header-row {
  display: flex;
  align-items: stretch;
  background: var(--bg-secondary);
}

.dc-settings__header-row :deep(.help-btn) {
  align-self: center;
  margin-right: var(--space-2);
}

/* V1 .dc-settings-header: full-width clickable bar, bg-secondary, hover bg-tertiary */
.dc-settings__toggle {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  align-items: center;
  gap: var(--space-2);
  background: var(--bg-secondary);
  border: none;
  border-radius: 0;
  padding: var(--space-3) var(--space-4);
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--text-primary);
  cursor: pointer;
  text-align: left;
  user-select: none;
  transition: background 0.15s;
}

.dc-settings__toggle:hover {
  background: var(--bg-tertiary);
}

.dc-settings__chevron {
  display: inline-block;
  transition: transform 0.18s ease;
  font-size: 0.7em;
}

.dc-settings__chevron.is-open {
  transform: rotate(90deg);
}

/* V1 .dc-settings-body: padding + a top rule separating it from the header bar */
.dc-settings__form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  border-top: 1px solid var(--border);
  background: var(--bg-secondary);
}

.dc-settings__row {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.dc-settings__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.dc-settings__field--inline {
  flex: 0 0 auto;
}

.dc-settings__field--toggle {
  flex-direction: row;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.dc-settings__label {
  font-weight: 500;
  font-size: var(--text-sm);
}

.dc-settings__input {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-primary);
  font-size: var(--text-sm);
  font-family: var(--font-mono);
}

.dc-settings__input--narrow {
  width: 9em;
  font-family: inherit;
}

.dc-settings__input.is-unsafe {
  border-color: var(--banner-warn-border);
  background: rgba(255, 245, 224, 0.4);
}

.dc-settings__hint {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.dc-settings__inline-warn {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--warning);
}

.dc-settings__toggle-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  font-size: var(--text-sm);
  font-weight: 500;
}

.dc-settings__actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
</style>
