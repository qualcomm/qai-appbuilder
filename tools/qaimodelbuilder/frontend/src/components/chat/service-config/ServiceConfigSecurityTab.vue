<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Security Tab — service_config sensitivity detection, detection rules,
 * extended rules, desensitization (+ entity switches), and complexity
 * assessment. Operates on the reactive ``cfg`` object; saving is delegated via
 * the ``save`` event.
 *
 * V1 alignment (Wave 3, 2026-06-04):
 *   - All boolean fields render as ``<ToggleSwitch>`` (V1 pill toggle), not raw
 *     checkboxes (V1 ServiceConfigPanel.js:907-1245).
 *   - Extended Rules / Entity Switches / Complexity Assessment are rendered as
 *     ``<AdvancedCard>`` (dashed border + "Advanced" badge), matching V1 which
 *     keeps these expert-mode groups visually de-emphasised.
 *   - Numeric short fields are grouped on one line via ``.svc-cfg-row .svc-cfg-row--3``
 *     (V1 settings.css:920-925 svc-cfg-row).
 *   - ``desensitization.strategies`` is ``string[]`` in the typed config, but
 *     V1 (useConfig.js:107-109 ``desensitizationStrategiesStr``) lets the user
 *     edit it as a comma-separated string. We bridge with a ``computed`` that
 *     joins/splits on ``,`` so the underlying typed array stays consistent.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { ServiceConfig } from "./types";
import {
  complexity,
  desensitization,
  keywordsToText,
  sensitivityDetection,
  textToKeywords,
} from "./helpers";
import CollapsibleCard from "./CollapsibleCard.vue";
import AdvancedCard from "./AdvancedCard.vue";
import ToggleSwitch from "./ToggleSwitch.vue";

const props = defineProps<{
  cfg: ServiceConfig;
  saving: boolean;
  hideSave?: boolean;
}>();

const emit = defineEmits<{ (e: "save"): void }>();

const { t } = useI18n();

// Alias the (reactive) config prop so templates mutate nested properties
// of the parent-owned object via a local ref rather than tripping
// vue/no-mutating-props on the prop path; the computed always reflects the
// current prop value even when the parent reassigns ``svcCfg``.
const cfg = computed(() => props.cfg);

// Bridge ``desensitization.strategies`` (typed as ``string[]``) ↔ a
// comma-separated string the user types into a single text input. V1 uses
// the same trick (useConfig.js:107-109 ``desensitizationStrategiesStr``).
//
// In ``<script setup>`` we must unwrap the computed ref via ``.value`` to
// hand a plain ``ServiceConfig`` to the helper; the template auto-unwraps so
// other call sites can keep using ``desensitization(cfg)`` directly.
const strategiesStr = computed<string>({
  get: () => (desensitization(cfg.value).strategies ?? []).join(", "),
  set: (value: string) => {
    desensitization(cfg.value).strategies = value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  },
});

const detectionRuleKeys = ['phone', 'email', 'id_card', 'bank_card', 'api_key', 'private_key', 'token', 'password'] as const;
const entityKeys = ['phone', 'email', 'id_card', 'bank_card', 'api_key', 'private_key', 'token', 'password', 'internal_url', 'local_path', 'device_id', 'image_data'] as const;

function pascal(s: string): string {
  return s.split('_').map((w) => (w[0] ?? '').toUpperCase() + w.slice(1)).join('');
}
</script>

<template>
  <div class="service-config-panel__form">
    <!-- 🔍 Sensitivity Detection -->
    <CollapsibleCard>
      <template #title>
        🔍 {{ t("serviceConfig.sensitivityDetectionTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableDetection") }}</span>
        <ToggleSwitch v-model="sensitivityDetection(cfg).enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.detectionMasterSwitchHint") }}</span>

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.detectionMethod") }}</span>
        <select
          v-model="sensitivityDetection(cfg).method"
          class="svc-cfg-input"
        >
          <option value="rule_first">{{ t("serviceConfig.detectionMethodRuleFirst") }}</option>
          <option value="model_first">model_first</option>
        </select>
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.useLocalModelFallback") }}</span>
        <ToggleSwitch v-model="sensitivityDetection(cfg).use_local_model_fallback" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.useLocalModelFallbackHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.strictS2Union") }}</span>
        <ToggleSwitch v-model="sensitivityDetection(cfg).strict_s2_union" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.strictS2UnionHint") }}</span>

      <!-- row-3col: Timeout / Model Input Max Chars / Max Gen Tokens (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--3">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.timeoutMs") }}</span>
          <input
            v-model.number="sensitivityDetection(cfg).timeout_ms"
            type="number"
            class="svc-cfg-input"
            min="1000"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.localModelTimeoutHint") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.modelInputMaxChars") }}</span>
          <input
            v-model.number="sensitivityDetection(cfg).model_input_max_chars"
            type="number"
            class="svc-cfg-input"
            min="100"
          />
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.maxGenTokens") }}</span>
          <input
            v-model.number="sensitivityDetection(cfg).max_gen_tokens"
            type="number"
            class="svc-cfg-input"
            min="64"
          />
        </label>
      </div>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.debugLogMatches") }}</span>
        <ToggleSwitch v-model="sensitivityDetection(cfg).debug_log_matches" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.debugLogMatchesHint") }}</span>

      <!-- row-2col: Keywords Dict Path + Reload Interval (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.keywordsDictPath") }}</span>
          <input
            v-model="sensitivityDetection(cfg).keywords_dict_path"
            type="text"
            class="svc-cfg-input"
            placeholder="./sensitive_keywords.json"
          />
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.keywordsReloadIntervalSeconds") }}</span>
          <input
            v-model.number="sensitivityDetection(cfg).keywords_reload_interval_seconds"
            type="number"
            class="svc-cfg-input"
            min="10"
          />
        </label>
      </div>
    </CollapsibleCard>

    <!-- 📋 Detection Rules -->
    <CollapsibleCard>
      <template #title>
        📋 {{ t("serviceConfig.detectionRulesTitle") }}
      </template>
      <p class="svc-cfg-hint">
        {{ t("serviceConfig.detectionRulesHint") }}
      </p>

      <div
        v-for="rule in detectionRuleKeys"
        :key="rule"
        class="detection-rule-row"
      >
        <label class="svc-cfg-field svc-cfg-field--row detection-rule-row__toggle">
          <span class="svc-cfg-label">{{ t(`serviceConfig.ruleLabel${pascal(rule)}`) }}</span>
          <ToggleSwitch
            v-model="sensitivityDetection(cfg).detection_rules[`enable_${rule}`]"
          />
        </label>
        <select
          v-model="sensitivityDetection(cfg).detection_rules[`level_${rule}`]"
          class="svc-cfg-input detection-rule-row__level"
        >
          <option value="S0">
            S0
          </option>
          <option value="S1">
            S1
          </option>
          <option value="S2">
            S2
          </option>
        </select>
      </div>
    </CollapsibleCard>

    <!-- 🔌 Extended Rules (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        🔌 {{ t("serviceConfig.extendedRulesTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.localPathLabel") }}</span>
        <ToggleSwitch v-model="sensitivityDetection(cfg).extended_rules.enable_local_path" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.localPathHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.internalUrlLabel") }}</span>
        <ToggleSwitch v-model="sensitivityDetection(cfg).extended_rules.enable_internal_url" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.internalUrlHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.deviceIdLabel") }}</span>
        <ToggleSwitch v-model="sensitivityDetection(cfg).extended_rules.enable_device_id" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.deviceIdHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.imageDataLabel") }}</span>
        <ToggleSwitch v-model="sensitivityDetection(cfg).extended_rules.enable_image_data" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.imageDataHint") }}</span>
    </AdvancedCard>

    <!-- 🧽 Desensitization -->
    <CollapsibleCard>
      <template #title>
        🧽 {{ t("serviceConfig.desensitizationTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableDesensitization") }}</span>
        <ToggleSwitch v-model="desensitization(cfg).enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.desensS1Hint") }}</span>

      <!-- ``strategies`` is ``string[]`` in the typed config; bridge to a
           comma-separated string the user types here (V1 useConfig.js:107-109). -->
      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.strategiesLabel") }}</span>
        <input
          v-model="strategiesStr"
          type="text"
          class="svc-cfg-input"
          placeholder="structured_placeholder"
        />
        <span class="svc-cfg-hint">{{ t("serviceConfig.strategiesHint") }}</span>
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.formatPreserving") }}</span>
        <ToggleSwitch v-model="desensitization(cfg).format_preserving_enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.formatPreservingHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.restoreResponse") }}</span>
        <ToggleSwitch v-model="desensitization(cfg).restore_response_enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.restoreResponseHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.restoreStream") }}</span>
        <ToggleSwitch v-model="desensitization(cfg).restore_stream_enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.restoreStreamHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.iterative") }}</span>
        <ToggleSwitch v-model="desensitization(cfg).iterative" />
      </label>

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.maxRounds") }}</span>
        <input
          v-model.number="desensitization(cfg).max_rounds"
          type="number"
          class="svc-cfg-input"
          min="1"
          max="10"
        />
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.logDesensitizationDetails") }}</span>
        <ToggleSwitch v-model="desensitization(cfg).log_desensitization_details" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.logDesensDetailsHint") }}</span>
    </CollapsibleCard>

    <!-- 🧽 Desensitization Entity Switches (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        🧽 {{ t("serviceConfig.entitySwitchesTitle") }}
      </template>
      <p class="svc-cfg-hint">
        {{ t("serviceConfig.entitySwitchesHint") }}
      </p>

      <div class="entity-switches-grid">
        <label
          v-for="entity in entityKeys"
          :key="entity"
          class="svc-cfg-field svc-cfg-field--row"
        >
          <span class="svc-cfg-label">{{ t(`serviceConfig.entity${pascal(entity)}`) }}</span>
          <ToggleSwitch
            v-model="desensitization(cfg).entity_switches[`enable_${entity}`]"
          />
        </label>
      </div>
    </AdvancedCard>

    <!-- 🧠 Complexity Assessment (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        🧠 {{ t("serviceConfig.complexityAssessmentTitle") }}
      </template>
      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.methodLabel") }}</span>
        <select
          v-model="complexity(cfg).method"
          class="svc-cfg-input"
        >
          <option value="heuristic_first">{{ t("serviceConfig.complexityHeuristicFirst") }}</option>
          <option value="model_first">model_first</option>
        </select>
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.useLocalModelFallback") }}</span>
        <ToggleSwitch v-model="complexity(cfg).use_local_model_fallback" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.useLocalModelFallbackHint") }}</span>

      <!-- row-3col: Timeout / Model Input / Tool Calls Threshold (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--3">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.timeoutMs") }}</span>
          <input
            v-model.number="complexity(cfg).timeout_ms"
            type="number"
            class="svc-cfg-input"
            min="1000"
          />
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.modelInputMaxChars") }}</span>
          <input
            v-model.number="complexity(cfg).model_input_max_chars"
            type="number"
            class="svc-cfg-input"
            min="100"
          />
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.toolCallsThresholdC1") }}</span>
          <input
            v-model.number="complexity(cfg).thresholds.tool_calls"
            type="number"
            class="svc-cfg-input"
            min="1"
            max="50"
          />
        </label>
      </div>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.toolCallsThresholdHint") }}</span>

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.c1KeywordsLabel") }}</span>
        <textarea
          :value="keywordsToText(complexity(cfg).keywords_c1)"
          class="svc-cfg-input svc-cfg-textarea"
          rows="3"
          :placeholder="t('serviceConfig.c1KeywordsPlaceholder')"
          @change="complexity(cfg).keywords_c1 = textToKeywords(($event.target as HTMLTextAreaElement).value)"
        />
        <!-- eslint-disable vue/no-v-html -->
        <span
          class="svc-cfg-hint"
          v-html="t('serviceConfig.c1KeywordsHint')"
        />
        <!-- eslint-enable vue/no-v-html -->
      </label>

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.c2KeywordsLabel") }}</span>
        <textarea
          :value="keywordsToText(complexity(cfg).keywords_c2)"
          class="svc-cfg-input svc-cfg-textarea"
          rows="3"
          :placeholder="t('serviceConfig.c2KeywordsPlaceholder')"
          @change="complexity(cfg).keywords_c2 = textToKeywords(($event.target as HTMLTextAreaElement).value)"
        />
        <!-- eslint-disable vue/no-v-html -->
        <span
          class="svc-cfg-hint"
          v-html="t('serviceConfig.c2KeywordsHint')"
        />
        <!-- eslint-enable vue/no-v-html -->
      </label>
    </AdvancedCard>

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
