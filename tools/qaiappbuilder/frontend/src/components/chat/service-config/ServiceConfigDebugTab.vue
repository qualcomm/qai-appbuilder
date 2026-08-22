<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Debug Tab — consolidated debugging settings.
 *
 * 2026-06-16 整合（用户明令）：将原「通用」Tab 的 🐛 服务调试（forge-config
 * ``service_launch``: show_prompt_in_ui + service_log_buffer_size）与原「云端连接」
 * Tab 底部的 Debug Log（service_config ``cloud_shared.log_debug``）一并并入本
 * 「调试」Tab，使所有调试相关设置集中在一处。原「通用」Tab 已删除。
 *
 * 本 Tab 现在跨两个后端持久化：
 *   • 🐛 服务调试        → forge-config ``service_launch``（通过 ``form`` prop）
 *   • 🐛 调试诊断        → service_config ``debug``（通过 ``cfg`` prop）
 *   • 云端连接调试日志   → service_config ``cloud_shared.log_debug``（``cfg`` prop）
 * 保存时父组件（ServiceConfigPanel.vue）需同时写入两个后端（见 ``save`` 事件
 * 在父级 ``saveDebugTab`` 的派发）。
 *
 * Wave 3 V1-parity realignment (2026-06-05):
 *   • All boolean fields render as ``<ToggleSwitch>`` (V1 pill toggle)
 *     instead of raw ``<input type="checkbox">``.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { ServiceConfig } from "./types";
import { cloudShared, debugCfg } from "./helpers";
import CollapsibleCard from "./CollapsibleCard.vue";
import ToggleSwitch from "./ToggleSwitch.vue";

export interface ServiceDebugForm {
  show_prompt_in_ui: boolean;
  prompt_debug: boolean;
  service_log_buffer_size: number;
}

const props = defineProps<{
  cfg: ServiceConfig;
  form: ServiceDebugForm;
  saving: boolean;
  hideSave?: boolean;
}>();

const emit = defineEmits<{ (e: "save"): void }>();

const { t } = useI18n();

// Alias the (reactive) props so templates mutate nested properties of the
// parent-owned objects via local refs rather than tripping vue/no-mutating-props
// on the prop path; the computeds always reflect the current prop value even
// when the parent reassigns ``svcCfg``.
const cfg = computed(() => props.cfg);
const form = computed(() => props.form);
</script>

<template>
  <div class="service-config-panel__form">
    <!-- 🐛 服务调试（原「通用」Tab，forge-config service_launch） -->
    <CollapsibleCard>
      <template #title>
        🐛 {{ t("serviceConfig.serviceDebugTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.showPromptInUi") }}</span>
        <ToggleSwitch v-model="form.show_prompt_in_ui" />
      </label>
      <!-- eslint-disable vue/no-v-html -->
      <!-- Trusted static locale string (no user input). -->
      <span
        class="svc-cfg-hint svc-cfg-hint--row"
        v-html="t('serviceConfig.showPromptInUiHint')"
      />
      <!-- eslint-enable vue/no-v-html -->

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.promptDebug") }}</span>
        <ToggleSwitch v-model="form.prompt_debug" />
      </label>
      <!-- eslint-disable vue/no-v-html -->
      <!-- Trusted static locale string (no user input). -->
      <span
        class="svc-cfg-hint svc-cfg-hint--row"
        v-html="t('serviceConfig.promptDebugHint')"
      />
      <!-- eslint-enable vue/no-v-html -->

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.logBufferSizeLabel") }}</span>
        <input
          v-model.number="form.service_log_buffer_size"
          type="number"
          class="svc-cfg-input"
          min="1000"
          max="20000"
          placeholder="6000"
        />
        <!-- eslint-disable vue/no-v-html -->
        <!-- Trusted static locale string (no user input). -->
        <span
          class="svc-cfg-hint"
          v-html="t('serviceConfig.logBufferSizeHint')"
        />
        <!-- eslint-enable vue/no-v-html -->
        <span class="svc-cfg-hint">{{ t("serviceConfig.logBufferSizeDefault") }}</span>
      </label>
    </CollapsibleCard>

    <!-- 🐛 调试诊断（service_config debug） -->
    <CollapsibleCard>
      <template #title>
        🐛 {{ t("serviceConfig.debugDiagnosticsTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.statusUpdateContentVisible") }}</span>
        <ToggleSwitch v-model="debugCfg(cfg).status_update_content_visible" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.statusUpdateContentHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.logRuleMatches") }}</span>
        <ToggleSwitch v-model="debugCfg(cfg).log_rule_matches" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.logRuleMatchesHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.logInferenceStream") }}</span>
        <ToggleSwitch v-model="debugCfg(cfg).log_inference_stream" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.logInferenceStreamHint") }}</span>

      <!-- Debug Log（原「云端连接」Tab 底部，service_config cloud_shared.log_debug） -->
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.debugLogLabel") }}</span>
        <ToggleSwitch
          v-model="cloudShared(cfg).log_debug"
          :aria-label="t('serviceConfig.debugLogLabel')"
        />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.debugLogHint") }}</span>
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
