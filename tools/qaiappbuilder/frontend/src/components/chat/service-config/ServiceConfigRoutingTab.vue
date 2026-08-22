<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Routing Tab — service_config ``routing.*``: master switch, fallback strategy,
 * agent routing, sticky routing, incremental check, S2 turn cleaning, metrics,
 * detection cache. Operates on the reactive ``cfg`` object; saving is delegated
 * via the ``save`` event.
 *
 * V1 alignment (Wave 3, 2026-06-05):
 *   - V1 service_config schema does NOT define the four ``enabled`` keys
 *     ``routing.fallback.enabled`` / ``routing.fallback.strategy`` /
 *     ``routing.agent_routing.enabled`` / ``routing.metrics.enabled``. They
 *     were V2-only invented toggles with no wire effect; ``helpers.ts`` has
 *     already dropped them from defaults, and the bindings are now removed
 *     here so the rendered config matches V1 byte-for-byte
 *     (V1 ServiceConfigPanel.js:594-905).
 *   - All boolean fields render as ``<ToggleSwitch>`` (V1 ``.toggle`` pill).
 *   - Sticky Routing / Incremental Check / S2 Turn Cleaning / Metrics /
 *     Detection Cache use ``<AdvancedCard>`` (dashed border + Advanced badge)
 *     to match V1 ``.svc-cfg-svc-cfg-card--advanced`` (V1:738/768/827/865/886).
 *   - Numeric short fields are grouped on one line via ``.svc-cfg-row[--2|--3]``
 *     (V1 settings.css:920-925 svc-cfg-row); no inline grid-template-columns.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { ServiceConfig } from "./types";
import { routing } from "./helpers";
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

</script>

<template>
  <div class="service-config-panel__form">
    <!-- 🔀 Routing Master -->
    <CollapsibleCard>
      <template #title>
        🔀 {{ t("serviceConfig.routingTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableRouting") }}</span>
        <ToggleSwitch v-model="routing(cfg).enabled" />
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.preferLocalSimple") }}</span>
        <ToggleSwitch v-model="routing(cfg).prefer_local_for_simple" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.preferLocalSimpleHint") }}</span>
    </CollapsibleCard>

    <!-- 🔄 Fallback Strategy
         V1 has NO master toggle nor strategy select for fallback — it goes
         straight into per-condition policies. -->
    <CollapsibleCard>
      <template #title>
        🔄 {{ t("serviceConfig.fallbackStrategyTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.cloudUnavailableToLocal") }}</span>
        <ToggleSwitch v-model="routing(cfg).fallback.cloud_unavailable_to_local" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.cloudUnavailableFallbackHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.cleanLocalHistoryOnFallback") }}</span>
        <ToggleSwitch v-model="routing(cfg).fallback.clean_local_history_on_fallback" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.cleanLocalHistoryHint") }}</span>

      <!-- row-3col: S0 / S1 / S2 local-unavailable selects (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--3">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.localUnavailableS0") }}</span>
          <select
            v-model="routing(cfg).fallback.local_unavailable.s0"
            class="svc-cfg-input"
          >
            <option value="cloud_if_allowed">cloud_if_allowed</option>
            <option value="fail">fail</option>
          </select>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.localUnavailableS1") }}</span>
          <select
            v-model="routing(cfg).fallback.local_unavailable.s1"
            class="svc-cfg-input"
          >
            <option value="cloud_if_allowed">cloud_if_allowed</option>
            <option value="fail">fail</option>
          </select>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.localUnavailableS2") }}</span>
          <select
            v-model="routing(cfg).fallback.local_unavailable.s2"
            class="svc-cfg-input"
          >
            <option value="fail">fail</option>
            <option value="cloud_if_allowed">cloud_if_allowed</option>
          </select>
        </label>
      </div>

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.maxInputOverflowRetries") }}</span>
        <input
          v-model.number="routing(cfg).fallback.max_input_overflow_retries"
          type="number"
          class="svc-cfg-input"
          min="0"
          max="10"
        />
        <span class="svc-cfg-hint">{{ t("serviceConfig.maxInputOverflowRetriesHint") }}</span>
      </label>

      <!-- row-2col: Enterprise / Public Cloud Unavailable selects (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.enterpriseCloudUnavailable") }}</span>
          <select
            v-model="routing(cfg).fallback.enterprise_cloud_unavailable"
            class="svc-cfg-input"
          >
            <option value="public_cloud_if_allowed">{{ t("serviceConfig.fallbackEcuPubCloud") }}</option>
            <option value="local_if_allowed">{{ t("serviceConfig.fallbackLocalIfAllowed") }}</option>
            <option value="fail">{{ t("serviceConfig.fallbackFail503") }}</option>
          </select>
          <span class="svc-cfg-hint">{{ t("serviceConfig.enterpriseCloudUnavailHint") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.publicCloudUnavailable") }}</span>
          <select
            v-model="routing(cfg).fallback.public_cloud_unavailable"
            class="svc-cfg-input"
          >
            <option value="enterprise_cloud_if_allowed">{{ t("serviceConfig.fallbackPcuEntCloud") }}</option>
            <option value="local_if_allowed">{{ t("serviceConfig.fallbackLocalIfAllowed") }}</option>
            <option value="fail">{{ t("serviceConfig.fallbackFail503") }}</option>
          </select>
          <span class="svc-cfg-hint">{{ t("serviceConfig.publicCloudUnavailHint") }}</span>
        </label>
      </div>
    </CollapsibleCard>

    <!-- 🤖 Agent Routing
         V1 has NO ``agent_routing.enabled`` master toggle — fields are shown
         directly. -->
    <CollapsibleCard>
      <template #title>
        🤖 {{ t("serviceConfig.agentRoutingTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.subAgentPreferLocal") }}</span>
        <ToggleSwitch v-model="routing(cfg).agent_routing.sub_agent_prefer_local" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.subAgentPreferLocalHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.subAgentAllowCloudC2") }}</span>
        <ToggleSwitch v-model="routing(cfg).agent_routing.sub_agent_allow_cloud_on_c2" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.subAgentAllowCloudC2Hint") }}</span>

      <label class="svc-cfg-field">
        <span class="svc-cfg-label">{{ t("serviceConfig.maxToolCallRetries") }}</span>
        <input
          v-model.number="routing(cfg).agent_routing.max_tool_call_retries"
          type="number"
          class="svc-cfg-input"
          min="0"
          max="50"
        />
        <span class="svc-cfg-hint">{{ t("serviceConfig.maxToolCallRetriesHint") }}</span>
      </label>
    </CollapsibleCard>

    <!-- 📌 Sticky Routing (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        📌 {{ t("serviceConfig.stickyRoutingTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableStickyRouting") }}</span>
        <ToggleSwitch v-model="routing(cfg).sticky_routing.enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.stickyRoutingHint") }}</span>

      <!-- row-2col: TTL + Max Sessions (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.ttlSeconds") }}</span>
          <input
            v-model.number="routing(cfg).sticky_routing.ttl_seconds"
            type="number"
            class="svc-cfg-input"
            min="60"
            max="86400"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.stickyTtlHint") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.maxSessions") }}</span>
          <input
            v-model.number="routing(cfg).sticky_routing.max_sessions"
            type="number"
            class="svc-cfg-input"
            min="10"
            max="10000"
          />
        </label>
      </div>
    </AdvancedCard>

    <!-- ⚡ Incremental Check (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        ⚡ {{ t("serviceConfig.incrementalCheckTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableIncrementalCheck") }}</span>
        <ToggleSwitch v-model="routing(cfg).incremental_check.enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.incrementalCheckHint") }}</span>

      <!-- row-2col: Session TTL + Max Sessions (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.sessionTtlSeconds") }}</span>
          <input
            v-model.number="routing(cfg).incremental_check.session_ttl_seconds"
            type="number"
            class="svc-cfg-input"
            min="60"
            max="86400"
          />
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.maxSessions") }}</span>
          <input
            v-model.number="routing(cfg).incremental_check.max_sessions"
            type="number"
            class="svc-cfg-input"
            min="10"
            max="10000"
          />
        </label>
      </div>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.s2AlwaysFullCheck") }}</span>
        <ToggleSwitch v-model="routing(cfg).incremental_check.s2_always_full_check" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.s2AlwaysFullCheckHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.detectSensitiveReference") }}</span>
        <ToggleSwitch v-model="routing(cfg).incremental_check.detect_sensitive_reference" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.detectSensitiveRefHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.detectHistoryTampering") }}</span>
        <ToggleSwitch v-model="routing(cfg).incremental_check.detect_history_tampering" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.detectHistoryTamperingHint") }}</span>
    </AdvancedCard>

    <!-- 🧹 S2 Turn Cleaning (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        🧹 {{ t("serviceConfig.s2TurnCleaningTitle") }}
      </template>
      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.enableS2TurnCleaning") }}</span>
        <ToggleSwitch v-model="routing(cfg).s2_turn_cleaning.enabled" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.s2TurnCleaningHint") }}</span>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.logDetails") }}</span>
        <ToggleSwitch v-model="routing(cfg).s2_turn_cleaning.log_details" />
      </label>

      <label class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.allowCloudRerouteAfterClean") }}</span>
        <ToggleSwitch v-model="routing(cfg).s2_turn_cleaning.allow_cloud_reroute_after_clean" />
      </label>
      <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.allowCloudRerouteHint") }}</span>
    </AdvancedCard>

    <!-- 📈 Metrics (V1: AdvancedCard, dashed border)
         V1 has NO ``metrics.enabled`` master toggle — only the two summary
         cadence numbers shown two-up. -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        📈 {{ t("serviceConfig.metricsTitle") }}
      </template>
      <!-- row-2col: Summary Every N + Summary Every Seconds (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.summaryEveryNRequests") }}</span>
          <input
            v-model.number="routing(cfg).metrics.summary_every_n_requests"
            type="number"
            class="svc-cfg-input"
            min="0"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.zeroDisabled") }}</span>
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.summaryEverySeconds") }}</span>
          <input
            v-model.number="routing(cfg).metrics.summary_every_seconds"
            type="number"
            class="svc-cfg-input"
            min="0"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.zeroDisabled") }}</span>
        </label>
      </div>
    </AdvancedCard>

    <!-- 🗄️ Detection Cache (V1: AdvancedCard, dashed border) -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        🗄️ {{ t("serviceConfig.detectionCacheTitle") }}
      </template>
      <!-- row-2col: TTL + Max Entries (V1 same row) -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.cacheTtlSeconds") }}</span>
          <input
            v-model.number="routing(cfg).cache.ttl_seconds"
            type="number"
            class="svc-cfg-input"
            min="0"
            max="3600"
          />
        </label>

        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.maxEntries") }}</span>
          <input
            v-model.number="routing(cfg).cache.max_entries"
            type="number"
            class="svc-cfg-input"
            min="0"
            max="10000"
          />
        </label>
      </div>
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
