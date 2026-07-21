<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Local Model Tab — service_config ``local_model`` master switch + 3 model
 * slots (NPU / GPU / CPU). V1 reference: ``ServiceConfigPanel.js:151-269``.
 *
 * Layout (V1-parity):
 *   • 🖥️ 本地模型 svc-cfg-card — single ToggleSwitch (enableLocalModel) + hint.
 *   • 📦 模型实例 svc-cfg-card — free-text input for ``default_model`` (with a
 *     <datalist> suggesting all scanned model names; users may type any
 *     name they expect the backend to load), then three slots:
 *       ─ 🔷 NPU 槽位 1: select from QNN models  + 上下文 (number)
 *       ─ 🟢 GPU 槽位 2: select from GGUF models + 上下文 (number)
 *       ─ 🔵 CPU 槽位 3: select from MNN models  + 上下文 (number)
 *     Per-slot enabled/disabled is rendered as a right-aligned ToggleSwitch
 *     in the slot header (no text label — V1 has the slider only).
 *
 * Per-format model lists arrive via ``availableModelsByFormat`` (client-side
 * bucketed in the parent from ``/api/service/models`` — see
 * ``ServiceConfigPanel.vue:loadAvailableModels``). The legacy flat
 * ``availableModels`` is kept for the default-model datalist.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { LocalModelsByFormat, ServiceConfig } from "./types";
import { localModel, modelSlot } from "./helpers";
import CollapsibleCard from "./CollapsibleCard.vue";
import ToggleSwitch from "./ToggleSwitch.vue";

const props = defineProps<{
  cfg: ServiceConfig;
  availableModels: string[];
  availableModelsByFormat: LocalModelsByFormat;
  saving: boolean;
  hideSave?: boolean;
}>();

const emit = defineEmits<{ (e: "save"): void }>();

const { t } = useI18n();

// Alias the (reactive) config prop so templates mutate nested properties of the
// parent-owned object via a local ref rather than tripping vue/no-mutating-props
// on the prop path; the computed always reflects the current prop value even
// when the parent reassigns ``svcCfg``.
const cfg = computed(() => props.cfg);

// V1 (``ServiceConfigPanel.js:190``) gates slot rendering on
// ``cfg.models && cfg.models.length >= 3``; when the backend has not yet
// produced the models[] array (e.g. defaults-only config), V1 shows the
// ``modelsListEmpty`` hint instead. Mirror that here so users get the same
// guidance on a fresh install rather than an empty triple-slot block.
const slotsReady = computed(() => Array.isArray(cfg.value.models) && cfg.value.models.length >= 3);
</script>

<template>
  <div class="service-config-panel__form">
    <!-- 🖥️ Local Model master switch -->
    <CollapsibleCard>
      <template #title>
        🖥️ {{ t("serviceConfig.localModelTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableLocalModel") }}</span>
        <ToggleSwitch
          v-model="localModel(cfg).enabled"
          :aria-label="t('serviceConfig.enableLocalModel')"
        />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.localModelHint") }}</span>
    </CollapsibleCard>

    <!-- 📦 Model Instances -->
    <CollapsibleCard>
      <template #title>
        📦 {{ t("serviceConfig.modelInstancesTitle") }}
      </template>

      <!-- Default model: free-text input (V1 parity) with datalist of
           scanned model names as type-ahead suggestions. -->
      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.defaultModelLabel") }}</span>
        <input
          v-model="cfg.default_model"
          type="text"
          class="svc-cfg-input mono"
          list="svc-cfg-default-model-options"
          :placeholder="t('serviceConfig.defaultModelPlaceholder')"
        />
        <datalist id="svc-cfg-default-model-options">
          <option
            v-for="m in props.availableModels"
            :key="m"
            :value="m"
          />
        </datalist>
        <span class="svc-cfg-hint">{{ t("serviceConfig.defaultModelHint") }}</span>
      </label>

      <template v-if="slotsReady">
        <!-- 槽位 1：NPU (QNN) -->
        <div class="svc-cfg-slot">
          <div class="svc-cfg-slot-header">
            <span class="svc-cfg-slot-title">
              🔷 {{ t("serviceConfig.npuModelSlot") }}
              <span class="svc-cfg-slot-badge">
                <span
                  class="svc-cfg-slot-dot svc-cfg-slot-dot--npu"
                  aria-hidden="true"
                ></span>
                {{ t("serviceConfig.slot") }} 1
              </span>
            </span>
            <ToggleSwitch
              v-model="modelSlot(cfg, 0).enabled"
              :aria-label="t('serviceConfig.npuModelSlot')"
            />
          </div>
          <div class="svc-cfg-row">
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">Model (QNN)</span>
              <select
                v-model="modelSlot(cfg, 0).name"
                class="svc-cfg-select mono"
              >
                <option value="">
                  {{ t("serviceConfig.selectQnnModel") }}
                </option>
                <option
                  v-for="m in props.availableModelsByFormat.qnn"
                  :key="m"
                  :value="m"
                >
                  {{ m }}
                </option>
              </select>
            </label>
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">{{ t("serviceConfig.contextSizeLabel") }}</span>
              <input
                v-model.number="modelSlot(cfg, 0).context_size"
                type="number"
                class="svc-cfg-input mono"
                min="512"
                max="131072"
              />
            </label>
          </div>
        </div>

        <!-- 槽位 2：GPU (GGUF) -->
        <div class="svc-cfg-slot">
          <div class="svc-cfg-slot-header">
            <span class="svc-cfg-slot-title">
              🟢 {{ t("serviceConfig.gpuModelSlot") }}
              <span class="svc-cfg-slot-badge">
                <span
                  class="svc-cfg-slot-dot svc-cfg-slot-dot--gpu"
                  aria-hidden="true"
                ></span>
                {{ t("serviceConfig.slot") }} 2
              </span>
            </span>
            <ToggleSwitch
              v-model="modelSlot(cfg, 1).enabled"
              :aria-label="t('serviceConfig.gpuModelSlot')"
            />
          </div>
          <div class="svc-cfg-row">
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">Model (GGUF)</span>
              <select
                v-model="modelSlot(cfg, 1).name"
                class="svc-cfg-select mono"
              >
                <option value="">
                  {{ t("serviceConfig.selectGgufModel") }}
                </option>
                <option
                  v-for="m in props.availableModelsByFormat.gguf"
                  :key="m"
                  :value="m"
                >
                  {{ m }}
                </option>
              </select>
            </label>
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">{{ t("serviceConfig.contextSizeLabel") }}</span>
              <input
                v-model.number="modelSlot(cfg, 1).context_size"
                type="number"
                class="svc-cfg-input mono"
                min="512"
                max="131072"
              />
            </label>
          </div>
        </div>

        <!-- 槽位 3：CPU (MNN) -->
        <div class="svc-cfg-slot">
          <div class="svc-cfg-slot-header">
            <span class="svc-cfg-slot-title">
              🔵 {{ t("serviceConfig.cpuModelSlot") }}
              <span class="svc-cfg-slot-badge">
                <span
                  class="svc-cfg-slot-dot svc-cfg-slot-dot--cpu"
                  aria-hidden="true"
                ></span>
                {{ t("serviceConfig.slot") }} 3
              </span>
            </span>
            <ToggleSwitch
              v-model="modelSlot(cfg, 2).enabled"
              :aria-label="t('serviceConfig.cpuModelSlot')"
            />
          </div>
          <div class="svc-cfg-row">
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">Model (MNN)</span>
              <select
                v-model="modelSlot(cfg, 2).name"
                class="svc-cfg-select mono"
              >
                <option value="">
                  {{ t("serviceConfig.selectMnnModel") }}
                </option>
                <option
                  v-for="m in props.availableModelsByFormat.mnn"
                  :key="m"
                  :value="m"
                >
                  {{ m }}
                </option>
              </select>
            </label>
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">{{ t("serviceConfig.contextSizeLabel") }}</span>
              <input
                v-model.number="modelSlot(cfg, 2).context_size"
                type="number"
                class="svc-cfg-input mono"
                min="512"
                max="131072"
              />
            </label>
          </div>
        </div>
      </template>

      <!-- V1 fallback hint when models[] hasn't been seeded by the backend -->
      <div
        v-else
        class="svc-cfg-hint"
      >
        {{ t("serviceConfig.modelsListEmpty") }}
      </div>
    </CollapsibleCard>

    <button
      v-if="!props.hideSave"
      type="button"
      class="service-config-panel__save"
      :disabled="props.saving"
      @click="emit('save')"
    >
      {{ props.saving ? t("common.saving") : t("common.save") }}
    </button>
  </div>
</template>

<style scoped src="./service-config.css"></style>
