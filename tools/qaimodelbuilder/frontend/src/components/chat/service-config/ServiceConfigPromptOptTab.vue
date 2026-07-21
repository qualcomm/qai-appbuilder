<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Prompt Opt. Tab — service_config ``prompt_optimization``: skill & tool,
 * context window, compression, emergency truncation, long text summarization
 * (+ cache), system prompt sections, few-shot examples. Operates on the
 * reactive ``cfg`` object; saving is delegated via the ``save`` event.
 *
 * V1 alignment (Wave 3, 2026-06-05):
 *   - All boolean fields render as ``<ToggleSwitch>`` (V1 ``.toggle`` pill).
 *   - Compression / Emergency Truncation / System Prompt Sections / Few-Shot
 *     Examples render as ``<AdvancedCard>`` (V1 svc-cfg-svc-cfg-card--advanced
 *     ServiceConfigPanel.js:1332 / 1370 / 1513 / 1553).
 *   - Long Text Summarization stays a regular ``CollapsibleCard`` (V1:1400)
 *     and **all** sub-fields (trigger / chunk / max_chunks / summarize toggles
 *     / Map+Reduce Instruction textarea / Summary Cache subcard) live inside
 *     a single ``<template v-if="...enabled">`` wrapper that mirrors V1:1417
 *     — previously the V2 fix was scoped only to the Trigger Ratio row,
 *     leaking the rest when ``enabled=false``.
 *   - Map / Reduce Instruction textarea are now wired (V1:1466-1474) — these
 *     were missing entirely from the V2 form.
  *   - Summary Cache uses ``<details class="sub-card">`` for native collapse
 *     (V1 used a custom toggle group; the visual is a small nested svc-cfg-card).
 *   - ``.svc-cfg-row[--2|--3]`` groups numeric fields on one line (V1).
 *   - ``allowed_tools`` keeps the V2 textarea + onChange split helpers (V1
 *     used a single ``input v-model``) — this is a strict ergonomic win for
 *     long lists; the underlying value stays a ``string[]`` so wire/payload
 *     is identical to V1 (functional behaviour preserved).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { ServiceConfig } from "./types";
import {
  allowedToolsToText,
  fewShotExamples,
  promptOpt,
  systemPromptSections,
  textToAllowedTools,
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

// Bridge ``map_instruction`` / ``reduce_instruction`` (V1 schema fields on
// ``long_text_summarization``; not declared in V2 ``LongTextSummarization``
// type so types.ts stays untouched per file-domain discipline). The runtime
// shape is ``Record<string, unknown>``; we expose a typed string get/set
// wrapper to keep v-model strict.
type LtsExtras = { map_instruction?: string; reduce_instruction?: string };
function ltsExtras(): LtsExtras {
  return promptOpt(cfg.value).long_text_summarization as unknown as LtsExtras;
}
const mapInstruction = computed<string>({
  get: () => ltsExtras().map_instruction ?? "",
  set: (v) => { ltsExtras().map_instruction = v; },
});
const reduceInstruction = computed<string>({
  get: () => ltsExtras().reduce_instruction ?? "",
  set: (v) => { ltsExtras().reduce_instruction = v; },
});

// Narrow optional-boolean wrappers for the system_prompts.sections_enabled
// and few_shot_examples_enabled groups: helpers.ts lazily fills V1-parity
// defaults so the values are always defined at runtime, but the typed
// shape declares them ``boolean | undefined`` (types.ts is shared and must
// not change in this file-domain) — these computeds keep ToggleSwitch
// v-model strict-typed against a clean ``boolean``.
function makeBoolBridge(
  obj: () => Record<string, unknown>,
  key: string,
  fallback: boolean,
) {
  return computed<boolean>({
    get: () => (obj()[key] as boolean | undefined) ?? fallback,
    set: (v) => { obj()[key] = v; },
  });
}
const sectionsRef = () => systemPromptSections(cfg.value) as unknown as Record<string, unknown>;
const fewShotRef = () => fewShotExamples(cfg.value) as unknown as Record<string, unknown>;
const sectCriticalRule = makeBoolBridge(sectionsRef, "critical_rule", false);
const sectToolsIntro = makeBoolBridge(sectionsRef, "tools_intro", true);
const sectCatalogStructuredIntro = makeBoolBridge(sectionsRef, "catalog_structured_intro", true);
const fsEnabled = makeBoolBridge(fewShotRef, "enabled", true);
const fsSkillCorrectCall = makeBoolBridge(fewShotRef, "skill_correct_call", true);
const fsNoSkillNeeded = makeBoolBridge(fewShotRef, "no_skill_needed", true);
</script>

<template>
  <div class="service-config-panel__form">
    <!-- ✨ Skill & Tool -->
    <CollapsibleCard>
      <template #title>
        ✨ {{ t("serviceConfig.skillToolTitle") }}
      </template>
      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.skillCatalogFormat") }}</span>
        <select
          v-model="promptOpt(cfg).skill_catalog_format"
          class="svc-cfg-input"
        >
          <option value="structured">structured</option>
          <option value="plain">plain</option>
        </select>
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableSkillAutoCorrection") }}</span>
        <ToggleSwitch v-model="promptOpt(cfg).enable_skill_auto_correction" />
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableToolWhitelist") }}</span>
        <ToggleSwitch v-model="promptOpt(cfg).enable_tool_whitelist" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.toolWhitelistHint") }}</span>

      <label
        v-if="promptOpt(cfg).enable_tool_whitelist"
        class="svc-cfg-field"
      >
        <span class="svc-cfg-label">{{ t("serviceConfig.allowedToolsLabel") }}</span>
        <textarea
          :value="allowedToolsToText(promptOpt(cfg).allowed_tools)"
          class="svc-cfg-input svc-cfg-textarea"
          rows="4"
          :placeholder="t('serviceConfig.allowedToolsHint')"
          @change="promptOpt(cfg).allowed_tools = textToAllowedTools(($event.target as HTMLTextAreaElement).value)"
        />
        <span class="svc-cfg-hint">{{ t("serviceConfig.allowedToolsHint") }}</span>
      </label>

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.toolCallTemperature") }}</span>
        <input
          v-model.number="promptOpt(cfg).tool_call_temperature"
          type="number"
          class="svc-cfg-input"
          min="0"
          max="2"
          step="0.05"
        />
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.spawnGuardLabel") }}</span>
        <ToggleSwitch v-model="promptOpt(cfg).spawn_guard.enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.spawnGuardHint") }}</span>
    </CollapsibleCard>

    <!-- 📏 Context Window -->
    <CollapsibleCard>
      <template #title>
        📏 {{ t("serviceConfig.contextWindowTitle") }}
      </template>
      <!-- row-2col: Max Messages Limit + Recent Window (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.maxMessagesLimit") }}</span>
          <input
            v-model.number="promptOpt(cfg).max_messages_limit"
            type="number"
            class="svc-cfg-input"
            min="4"
            max="64"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.maxMessagesLimitHint") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.recentWindow") }}</span>
          <input
            v-model.number="promptOpt(cfg).recent_window"
            type="number"
            class="svc-cfg-input"
            min="2"
            max="20"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.recentWindowHint") }}</span>
        </label>
      </div>

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.outputReserveRatio") }}</span>
        <input
          v-model.number="promptOpt(cfg).output_reserve_ratio"
          type="number"
          class="svc-cfg-input"
          min="0.05"
          max="0.5"
          step="0.05"
        />
        <span class="svc-cfg-hint">{{ t("serviceConfig.outputReserveRatioHint") }}</span>
      </label>
    </CollapsibleCard>

    <!-- ✂️ Compression (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        ✂️ {{ t("serviceConfig.compressionTitle") }}
      </template>
      <!-- row-3col: Old / Recent / Tool Compress Len (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--3">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.oldCompressLen") }}</span>
          <input
            v-model.number="promptOpt(cfg).old_compress_len"
            type="number"
            class="svc-cfg-input"
            min="20"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.oldCompressLenHint") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.recentCompressLen") }}</span>
          <input
            v-model.number="promptOpt(cfg).recent_compress_len"
            type="number"
            class="svc-cfg-input"
            min="50"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.recentCompressLenHint") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.toolCompressLen") }}</span>
          <input
            v-model.number="promptOpt(cfg).tool_compress_len"
            type="number"
            class="svc-cfg-input"
            min="50"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.toolCompressLenHint") }}</span>
        </label>
      </div>

      <!-- row-2col: Min Compress Threshold + Tool Min Length (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.minCompressThreshold") }}</span>
          <input
            v-model.number="promptOpt(cfg).min_compress_threshold"
            type="number"
            class="svc-cfg-input"
            min="0"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.minCompressThresholdHint") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.toolMinLength") }}</span>
          <input
            v-model.number="promptOpt(cfg).tool_min_length"
            type="number"
            class="svc-cfg-input"
            min="0"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.toolMinLengthHint") }}</span>
        </label>
      </div>
    </AdvancedCard>

    <!-- 🚨 Emergency Truncation (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        🚨 {{ t("serviceConfig.emergencyTruncationTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableEmergencyTruncation") }}</span>
        <ToggleSwitch v-model="promptOpt(cfg).emergency_truncation.enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.emergencyTruncationHint") }}</span>

      <!-- row-2col: Max Truncation Ratio + Safety Margin Tokens (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.maxTruncationRatio") }}</span>
          <input
            v-model.number="promptOpt(cfg).emergency_truncation.max_truncation_ratio"
            type="number"
            class="svc-cfg-input"
            min="0.1"
            max="1.0"
            step="0.05"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.maxTruncationRatioHint") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.safetyMarginTokens") }}</span>
          <input
            v-model.number="promptOpt(cfg).emergency_truncation.safety_margin_tokens"
            type="number"
            class="svc-cfg-input"
            min="0"
          />
        </label>
      </div>
    </AdvancedCard>

    <!-- 📄 Long Text Summarization
         V1 (1400-1511) keeps this as a regular svc-cfg-card (no advanced badge)
         and wraps **the entire** sub-form in `v-if="...enabled"` so when
         the master toggle is off, only the toggle stays visible. -->
    <CollapsibleCard>
      <template #title>
        📄 {{ t("serviceConfig.longTextSumTitle") }}
      </template>
      <p class="svc-cfg-hint">
        {{ t("serviceConfig.longTextSumHint") }}
      </p>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.longTextSumTitle") }}</span>
        <ToggleSwitch v-model="promptOpt(cfg).long_text_summarization.enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.longTextSumMasterHint") }}</span>

      <template v-if="promptOpt(cfg).long_text_summarization.enabled">
        <!-- row-3col: Trigger Ratio / Chunk Ratio / Max Chunks (V1 same row) -->
        <div class="svc-cfg-row svc-cfg-row--3">
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.triggerRatio") }}</span>
            <input
              v-model.number="promptOpt(cfg).long_text_summarization.trigger_ratio"
              type="number"
              class="svc-cfg-input"
              min="0.1"
              max="0.9"
              step="0.05"
            />
            <span class="svc-cfg-hint">{{ t("serviceConfig.triggerRatioHint") }}</span>
          </label>

          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.chunkRatio") }}</span>
            <input
              v-model.number="promptOpt(cfg).long_text_summarization.chunk_ratio"
              type="number"
              class="svc-cfg-input"
              min="0.1"
              max="0.8"
              step="0.05"
            />
            <span class="svc-cfg-hint">{{ t("serviceConfig.chunkRatioHint") }}</span>
          </label>

          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.maxChunks") }}</span>
            <input
              v-model.number="promptOpt(cfg).long_text_summarization.max_chunks"
              type="number"
              class="svc-cfg-input"
              min="1"
              max="16"
            />
            <span class="svc-cfg-hint">{{ t("serviceConfig.maxChunksHint") }}</span>
          </label>
        </div>

        <label class="svc-cfg-field svc-cfg-field--row">
          <span class="svc-cfg-label">{{ t("serviceConfig.summarizeUserMessages") }}</span>
          <ToggleSwitch v-model="promptOpt(cfg).long_text_summarization.summarize_user_messages" />
        </label>
        <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.summarizeUserMsgsHint") }}</span>

        <label class="svc-cfg-field svc-cfg-field--row">
          <span class="svc-cfg-label">{{ t("serviceConfig.summarizeToolResponses") }}</span>
          <ToggleSwitch v-model="promptOpt(cfg).long_text_summarization.summarize_tool_responses" />
        </label>
        <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.summarizeToolRespHint") }}</span>

        <label class="svc-cfg-field svc-cfg-field--row">
          <span class="svc-cfg-label">{{ t("serviceConfig.verboseLogging") }}</span>
          <ToggleSwitch v-model="promptOpt(cfg).long_text_summarization.verbose_logging" />
        </label>
        <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.verboseLoggingHint") }}</span>

        <!-- Map / Reduce Instruction (V1:1466-1474). The V1 schema persists
             these as ``map_instruction`` / ``reduce_instruction`` on
             ``long_text_summarization``; svc-cfg-field is optional in the typed
             config so we coerce via index-signature to keep ``v-model``
             reactive without a helper. -->
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.mapInstruction") }}</span>
          <textarea
            v-model="mapInstruction"
            class="svc-cfg-input svc-cfg-textarea"
            rows="3"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.mapInstructionHint") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.reduceInstruction") }}</span>
          <textarea
            v-model="reduceInstruction"
            class="svc-cfg-input svc-cfg-textarea"
            rows="3"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.reduceInstructionHint") }}</span>
        </label>

        <!-- Summary Cache sub-card (collapsible via native <details>).
             V1 (ServiceConfigPanel.js:1480 + useConfig.js:39/44) starts with an
             empty collapsedGroups map ⇒ the `lts_cache` group is EXPANDED by
             default, so the native <details> must carry `open` to match. -->
        <details
          class="sub-card"
          open
        >
          <summary class="sub-card__title">
            🗄️ {{ t("serviceConfig.summaryCacheTitle") }}
          </summary>
          <div class="sub-card__body">
            <label class="svc-cfg-field svc-cfg-field--row">
              <span class="svc-cfg-label">{{ t("serviceConfig.enableCache") }}</span>
              <ToggleSwitch v-model="promptOpt(cfg).long_text_summarization.cache.enabled" />
            </label>
            <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.summaryCacheLruHint") }}</span>

            <!-- row-3col: Max Entries / Max Memory / TTL (V1 same row) -->
            <div class="svc-cfg-row svc-cfg-row--3">
              <label class="svc-cfg-field">
                <span class="svc-cfg-label">{{ t("serviceConfig.maxEntries") }}</span>
                <input
                  v-model.number="promptOpt(cfg).long_text_summarization.cache.max_entries"
                  type="number"
                  class="svc-cfg-input"
                  min="10"
                  max="5000"
                />
              </label>

              <label class="svc-cfg-field">
                <span class="svc-cfg-label">{{ t("serviceConfig.maxMemoryMb") }}</span>
                <input
                  v-model.number="promptOpt(cfg).long_text_summarization.cache.max_memory_mb"
                  type="number"
                  class="svc-cfg-input"
                  min="1"
                  max="500"
                />
              </label>

              <label class="svc-cfg-field">
                <span class="svc-cfg-label">{{ t("serviceConfig.ttlMinutes") }}</span>
                <input
                  v-model.number="promptOpt(cfg).long_text_summarization.cache.ttl_minutes"
                  type="number"
                  class="svc-cfg-input"
                  min="1"
                  max="1440"
                />
              </label>
            </div>
          </div>
        </details>
      </template>
    </CollapsibleCard>

    <!-- 📝 System Prompt Sections (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        📝 {{ t("serviceConfig.systemPromptSectionsTitle") }}
      </template>
      <p class="svc-cfg-hint">
        {{ t("serviceConfig.systemPromptSectionsHint") }}
      </p>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.criticalRuleSection") }}</span>
        <ToggleSwitch v-model="sectCriticalRule" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.criticalRuleSectionHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.toolsIntroSection") }}</span>
        <ToggleSwitch v-model="sectToolsIntro" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.toolsIntroSectionHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.catalogStructuredIntroSection") }}</span>
        <ToggleSwitch v-model="sectCatalogStructuredIntro" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.catalogStructuredIntroHint") }}</span>
    </AdvancedCard>

    <!-- 💡 Few-Shot Examples (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        💡 {{ t("serviceConfig.fewShotExamplesTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableFewShotExamples") }}</span>
        <ToggleSwitch v-model="fsEnabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.fewShotMasterHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.skillCorrectCallExample") }}</span>
        <ToggleSwitch v-model="fsSkillCorrectCall" />
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.noSkillNeededExample") }}</span>
        <ToggleSwitch v-model="fsNoSkillNeeded" />
      </label>

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.maxSkillExamples") }}</span>
        <input
          v-model.number="fewShotExamples(cfg).max_skill_examples"
          type="number"
          class="svc-cfg-input"
          min="0"
          max="5"
        />
        <span class="svc-cfg-hint">{{ t("serviceConfig.maxSkillExamplesHint") }}</span>
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
