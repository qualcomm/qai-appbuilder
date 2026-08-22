<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ServiceLaunchParams — Region 5 (collapsible "Launch Parameters") of the
 * Service page, extracted from ServiceView.vue to keep that view within the
 * cohesion budget.
 *
 * Presentational: renders the download/install in-flight hints, the
 * empty-model guidance cards, the model optgroup / port / log-level grid, and
 * the command preview. All state stays in the page-level `useServiceControl`;
 * two-way scalars (collapsed / selected model / port / log level) are exposed
 * as v-model bindings, while save + navigation are emitted back up.
 */
import { useI18n } from "vue-i18n";
import type { ServiceModelEntry } from "@/types/service";

interface ModelsByAccel {
  npu: ServiceModelEntry[];
  gpu: ServiceModelEntry[];
  cpu: ServiceModelEntry[];
}

defineProps<{
  svcParamsSaving: boolean;
  serviceModels: ServiceModelEntry[];
  serviceModelsLoading: boolean;
  serviceModelsByAccel: ModelsByAccel;
  isAnyModelInstalling: boolean;
  isAnyModelDownloading: boolean;
  /** svc.serviceStatus.exe_path — shown in the command preview. */
  exePath: string | undefined;
  serviceCommandPreview: string;
}>();

const emit = defineEmits<{
  (e: "save"): void;
  (e: "model-change", name: string): void;
  (e: "download-models"): void;
}>();

// Two-way scalar bindings (each backed by the page-level useServiceControl).
const collapsed = defineModel<boolean>("collapsed", { required: true });
const selectedModel = defineModel<string>("selectedModel", { required: true });
const localPort = defineModel<number>("localPort", { required: true });
const logLevel = defineModel<number>("logLevel", { required: true });

const { t } = useI18n();

function onModelChange(): void {
  emit("model-change", selectedModel.value);
}
</script>

<template>
  <div class="service-params-section">
    <div
      class="svc-params-header"
      @click="collapsed = !collapsed"
    >
      <div class="svc-params-title">
        <span
          class="conn-caret"
          :style="{ transform: collapsed ? 'rotate(-90deg)' : 'rotate(0deg)' }"
        >▼</span>
        ⚙ {{ t("service.launchParams") }}
      </div>
      <button
        v-show="!collapsed"
        type="button"
        class="btn btn-ghost btn-sm"
        :disabled="svcParamsSaving"
        @click.stop="emit('save')"
      >
        <span
          v-if="svcParamsSaving"
          class="spinner svc-btn-spinner"
          aria-hidden="true"
        ></span>
        <span v-else>💾</span>
        {{ t("service.saveParams") }}
      </button>
    </div>
    <div
      v-show="!collapsed"
      class="svc-params-body"
    >
      <!-- download/install in-flight hint (V1 svc-dl-hint, index.html:2877-2886) -->
      <div
        v-if="isAnyModelInstalling || isAnyModelDownloading"
        class="svc-dl-hint"
      >
        <span class="svc-dl-hint-dot" />
        <span>
          {{ isAnyModelInstalling ? t("service.modelInstallingHint") : t("service.modelDownloadingHint") }}
          <a
            href="#"
            @click.prevent="emit('download-models')"
          >{{ t("service.viewProgressArrow") }}</a>
        </span>
      </div>

      <!-- empty-model + downloading/installing → info guidance card (V1 index.html:2890,
           takes priority over the "no usable models" warn below) -->
      <div
        v-if="(isAnyModelInstalling || isAnyModelDownloading) && serviceModels.length === 0 && !serviceModelsLoading"
        class="svc-notice info"
      >
        <div class="svc-notice-title">
          <span>{{ isAnyModelInstalling ? "📦" : "📥" }}</span>
          {{ isAnyModelInstalling ? t("service.modelInstallingTitle") : t("service.modelDownloadingTitle") }}
        </div>
        <div class="svc-notice-body">
          {{ isAnyModelInstalling ? t("service.modelInstallingBody") : t("service.modelDownloadingBody") }}
        </div>
        <div class="svc-notice-actions">
          <a
            href="#"
            class="svc-notice-action-btn primary"
            @click.prevent="emit('download-models')"
          >
            {{ t("service.viewInDownloadCenter") }} →
          </a>
        </div>
      </div>

      <!-- empty-model guidance -->
      <div
        v-else-if="serviceModels.length === 0 && !serviceModelsLoading"
        class="svc-notice warn"
      >
        <div class="svc-notice-title">
          <span>📭</span>
          {{ t("service.noUsableModelsTitle") }}
        </div>
        <div class="svc-notice-body">
          {{ t("service.noUsableModelsBody") }}
        </div>
        <div class="svc-notice-actions">
          <a
            href="#"
            class="svc-notice-action-btn primary"
            @click.prevent="emit('download-models')"
          >
            {{ t("service.gotoDownloadCenterModels") }} →
          </a>
        </div>
      </div>

      <!-- model / port / loglevel -->
      <div
        v-else
        class="service-params-grid"
      >
        <div class="param-cell param-cell-model">
          <div class="param-label">
            {{ t("service.selectModel") }}
            <span class="param-flag">-c / --config_file</span>
            <span class="param-required">*</span>
            <span class="param-count">({{ t("service.modelsCount", { n: serviceModels.length }) }})</span>
          </div>
          <!-- loading hint inline (V1 index.html:2945-2948) -->
          <div
            v-if="serviceModelsLoading && (isAnyModelInstalling || isAnyModelDownloading)"
            class="svc-dl-hint"
            style="margin-bottom: 4px"
          >
            <span class="svc-dl-hint-dot" />
            {{ isAnyModelInstalling ? t("service.installingShort") : t("service.downloadingShort") }}
          </div>
          <select
            v-model="selectedModel"
            class="param-select"
            :disabled="serviceModelsLoading"
            @change="onModelChange"
          >
            <option
              v-if="serviceModelsLoading"
              value=""
            >
              {{ isAnyModelInstalling ? t("service.installingPleaseWait") : isAnyModelDownloading ? t("service.downloadingPleaseWait") : t("service.loadingDots") }}
            </option>
            <template v-else>
              <optgroup
                v-if="serviceModelsByAccel.npu.length"
                label="⚡ NPU (QNN)"
              >
                <option
                  v-for="m in serviceModelsByAccel.npu"
                  :key="m.name"
                  :value="m.name"
                >
                  {{ m.name }}
                </option>
              </optgroup>
              <optgroup
                v-if="serviceModelsByAccel.gpu.length"
                label="🎮 GPU (GGUF)"
              >
                <option
                  v-for="m in serviceModelsByAccel.gpu"
                  :key="m.name"
                  :value="m.name"
                >
                  {{ m.name }}
                </option>
              </optgroup>
              <optgroup
                v-if="serviceModelsByAccel.cpu.length"
                label="🖥️ CPU (MNN)"
              >
                <option
                  v-for="m in serviceModelsByAccel.cpu"
                  :key="m.name"
                  :value="m.name"
                >
                  {{ m.name }}
                </option>
              </optgroup>
            </template>
          </select>
        </div>
        <div class="param-cell param-cell-port">
          <div class="param-label">
            {{ t("service.port") }}
            <span class="param-flag">-p / --port</span>
          </div>
          <input
            v-model.number="localPort"
            class="param-input"
            type="number"
            min="1"
            max="65535"
          />
        </div>
        <div class="param-cell param-cell-loglevel">
          <div class="param-label">
            {{ t("service.logLevel") }}
            <span class="param-flag">-d / --loglevel</span>
          </div>
          <select
            v-model.number="logLevel"
            class="param-select"
          >
            <option :value="1">
              1 - {{ t("service.logLevelError") }}
            </option>
            <option :value="2">
              2 - {{ t("service.logLevelWarning") }}
            </option>
            <option :value="3">
              3 - {{ t("service.logLevelInfo") }}
            </option>
            <option :value="4">
              4 - {{ t("service.logLevelDebug") }}
            </option>
            <option :value="5">
              5 - {{ t("service.logLevelVerbose") }}
            </option>
          </select>
        </div>
      </div>

      <!-- command preview — V1 parity (index.html:2990-2996):
           `.cmd-path` (info-tinted, dotted underline) renders the exe
           path; `.cmd-flag` (accent-tinted) renders the rest of the
           argv string. Previously the flag segment was a plain text
           interpolation, which left the args uncoloured. -->
      <div class="service-cmd-preview">
        <div class="cmd-label">
          {{ t("service.commandPreview") }}
        </div>
        <span class="cmd-path">{{ exePath || 'GenieAPIService.exe' }}</span>
        <!-- V1 parity (index.html:2994): a leading space separates the exe
             path from the flag string. The space is inside the interpolation
             expression so Vue's whitespace condensing cannot strip it (a bare
             leading text space or &#32; between inline spans gets trimmed). -->
        <span
          v-if="serviceCommandPreview"
          class="cmd-flag"
        >{{ ' ' + serviceCommandPreview }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── Params section ── */
.service-params-section {
  /* V1 service.css:54-55 — wider padding to match status card; flex-shrink:0
     prevents the params section from being compressed when log area grows. */
  padding: var(--space-4) var(--space-5);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  flex-shrink: 0;
}
.svc-params-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  user-select: none;
}
.svc-params-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.conn-caret {
  font-size: var(--text-xs);
  color: var(--text-muted);
  transition: transform 150ms;
}
.svc-params-body {
  margin-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.service-params-grid {
  /* V1 service.css:81-101 (.param-row-inline-3) — model flex:2, port 120px,
     loglevel 160px, bottom-aligned, collapsing to a single column ≤900px. */
  display: flex;
  flex-direction: row;
  gap: var(--space-4);
  align-items: flex-end;
}
.param-cell {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.param-cell-model {
  flex: 2;
  min-width: 0;
}
.param-cell-port {
  flex: 0 0 120px;
}
.param-cell-loglevel {
  flex: 0 0 160px;
}
@media (max-width: 900px) {
  .service-params-grid {
    flex-direction: column;
    align-items: stretch;
  }
  .param-cell-port,
  .param-cell-loglevel {
    flex: none;
  }
}
.param-label {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-bottom: 4px;
}
.param-flag {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-left: 4px;
}
.param-required {
  color: var(--error);
}
.param-count {
  color: var(--text-muted);
  margin-left: 6px;
}
.param-input,
.param-select {
  width: 100%;
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
.param-input:focus,
.param-select:focus {
  /* V1 service.css:139-142,157 — accent ring on focus. */
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-muted);
}
.service-cmd-preview {
  /* V1 service.css:209 — command preview uses primary bg. */
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-3);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.cmd-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-bottom: 4px;
  font-family: var(--font-sans, sans-serif);
}
.cmd-path {
  /* V1 service.css:483-488 — path uses info color + dotted underline. */
  color: var(--info);
  text-decoration: underline;
  text-decoration-style: dotted;
  text-underline-offset: 2px;
}
.cmd-flag {
  /* V1 service.css:475-477 — flags use accent color. */
  color: var(--accent);
}

/* ── Notice cards (shared with status card; redefined scoped here) ── */
.svc-notice {
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.svc-notice.warn {
  background: var(--banner-warn-bg);
  border: 1px solid var(--banner-warn-border);
}
.svc-notice.info {
  background: var(--banner-info-bg);
  border: 1px solid var(--banner-info-border);
}
.svc-notice-title {
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 6px;
}
.svc-notice-body {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.svc-notice-actions {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
}
.svc-notice-action-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  text-decoration: none;
  border: 1px solid transparent;
  transition: background var(--transition), border-color var(--transition),
    color var(--transition);
  width: fit-content;
}
.svc-notice-action-btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #ffffff;
}
.svc-notice-action-btn.primary:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}

/* ── Download / install in-flight hint (V1 .svc-dl-hint) ── */
.svc-dl-hint {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--banner-info-bg);
  border: 1px solid var(--banner-info-border);
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.svc-dl-hint a {
  color: var(--accent);
  text-decoration: none;
  font-weight: 600;
}
.svc-dl-hint-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--info);
  flex-shrink: 0;
  animation: svc-dl-pulse 2s ease-in-out infinite;
}
/* V1 service.css:351-370 — opacity-only pulse (no scale). */
@keyframes svc-dl-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.svc-btn-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-width: 2px;
  vertical-align: -1px;
}
</style>
