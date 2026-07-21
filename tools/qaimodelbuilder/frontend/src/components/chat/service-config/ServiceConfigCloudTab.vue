<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Cloud Tab — service_config public ``cloud_model`` / enterprise
 * ``enterprise_cloud_model`` / shared ``cloud_shared`` settings.
 *
 * V1 parity (`ServiceConfigPanel.js:271-592`):
 *   • 公网云 / 企业内网云：header 右侧是独立的 enable ToggleSwitch + 折叠箭头
 *     （toggle 点击只切换 enable，不折叠卡片；箭头/标题点击才折叠）；
 *   • 字段两列布局（base_url + model 一行，api_key + context_size 一行）；
 *   • API Key 用 🔑 前缀 + 👁/🙈 visibility toggle（MaskedPasswordInput）；
 *   • 故障转移端点是子卡内的 v-for 逐条编辑（base_url / model / api_key /
 *     ✕ 删除 + ＋ 添加），**不是** textarea + JSON 字符串；
 *   • 共享云端设置：AdvancedCard（dashed border + Advanced 徽章）；
 *     Timeout (s) + Stream Timeout (s) 一行；Retry & Circuit Breaker 子卡（含
 *     Max Retries / Backoff / Max Total Attempts / Switch on 429 / Failure
 *     Threshold / Cooldown）；Rate Limiting 子卡（Max Inf / Max Tokens）。
 *     （Debug Log 已于 2026-06-16 移至「调试」Tab 集中管理。）
 *
 * 设计自查（§重构质量铁律）：
 *   • 复用全局共享组件（ToggleSwitch / MaskedPasswordInput / CollapsibleCard /
 *     AdvancedCard），不复制实现；行为对齐 V1，结构按 V2 composition / slot 设计。
 *   • API-key "treat **** as unchanged" 草稿语义留在本组件（per-call-site，不下沉
 *     到 MaskedPasswordInput，避免无关耦合）。
 *   • 折叠卡片与 enable toggle 共存通过 `@click.stop` + `@keydown.stop` 阻止 toggle
 *     的点击事件冒泡到 CollapsibleCard 的 header（V1 同款用户感知行为）。
 *   • 不引入硬编码颜色 / 全局 ref / 巨石函数；所有 v-model 都走父级响应式 cfg。
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { ServiceConfig } from "./types";
import {
  cloudModel,
  cloudShared,
  cloudUploadPolicy,
  enterpriseCloudModel,
  routing,
} from "./helpers";
import CollapsibleCard from "./CollapsibleCard.vue";
import AdvancedCard from "./AdvancedCard.vue";
import ToggleSwitch from "./ToggleSwitch.vue";
import MaskedPasswordInput from "./MaskedPasswordInput.vue";

const props = defineProps<{
  cfg: ServiceConfig;
  saving: boolean;
  hideSave?: boolean;
}>();

const emit = defineEmits<{ (e: "save"): void }>();

const { t } = useI18n();

// Alias the (reactive) config prop so templates mutate nested properties
// of the parent-owned object via a local computed rather than tripping
// vue/no-mutating-props on the prop path.
const cfg = computed(() => props.cfg);

// API Key masking: if value is "****" treat as unchanged (don't send). // Per call-site draft — kept here, not in MaskedPasswordInput, since the // "**** = unchanged" semantics depends on parent's save flow.
const cloudApiKeyDraft = ref<string | null>(null);
const enterpriseApiKeyDraft = ref<string | null>(null);

watch(
  () => props.cfg.cloud_model?.api_key,
  (v) => { if (cloudApiKeyDraft.value === null && v !== undefined) cloudApiKeyDraft.value = v; },
  { immediate: true },
);
watch(
  () => props.cfg.enterprise_cloud_model?.api_key,
  (v) => { if (enterpriseApiKeyDraft.value === null && v !== undefined) enterpriseApiKeyDraft.value = v; },
  { immediate: true },
);

function onCloudApiKeyChange(value: string): void {
  cloudApiKeyDraft.value = value;
  if (value !== "****") {
    cloudModel(props.cfg).api_key = value;
  }
}

function onEnterpriseApiKeyChange(value: string): void {
  enterpriseApiKeyDraft.value = value;
  if (value !== "****") {
    enterpriseCloudModel(props.cfg).api_key = value;
  }
}

// -- Endpoint helpers (V1 ServiceConfigPanel.js:368 / :386 / :464 / :482) --
// ``endpoints`` is an unknown[] in types.ts to allow legacy / wire-shape
// flexibility; treat each entry as a partial endpoint record locally.
type EndpointRow = { base_url?: string; model?: string; api_key?: string };

function publicEndpoints(): EndpointRow[] {
  const cm = cloudModel(props.cfg);
  if (!Array.isArray(cm.endpoints)) cm.endpoints = [];
  return cm.endpoints as EndpointRow[];
}
function enterpriseEndpoints(): EndpointRow[] {
  const ec = enterpriseCloudModel(props.cfg);
  if (!Array.isArray(ec.endpoints)) ec.endpoints = [];
  return ec.endpoints as EndpointRow[];
}

function addPublicEndpoint(): void {
  publicEndpoints().push({ base_url: "", api_key: "", model: "" });
}
function removePublicEndpoint(idx: number): void {
  publicEndpoints().splice(idx, 1);
}
function addEnterpriseEndpoint(): void {
  enterpriseEndpoints().push({ base_url: "", api_key: "", model: "" });
}
function removeEnterpriseEndpoint(idx: number): void {
  enterpriseEndpoints().splice(idx, 1);
}
</script>

<template>
  <div class="service-config-panel__form">
    <!-- --- 公网云 --- -->
    <CollapsibleCard>
      <template #title>
        <span class="svc-cfg-card-title-main">🌐 {{ t("serviceConfig.publicCloudTitle") }}</span>
        <!-- V1 parity: enable toggle sits in the header next to the title; clicking
             it must NOT collapse the svc-cfg-card. Stop click/keydown propagation so only
             the chevron / title-text can toggle the body. -->
        <span
          class="svc-cfg-card-header-toggle"
          @click.stop
          @keydown.stop
        >
          <ToggleSwitch
            v-model="cloudModel(cfg).enabled"
            :aria-label="t('serviceConfig.enableCloudModel')"
          />
        </span>
      </template>

      <p class="svc-cfg-hint">
        {{ t("serviceConfig.publicCloudHint") }}
      </p>

      <!-- Row 1: base_url + model -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.baseUrlLabel") }}</span>
          <input
            v-model="cloudModel(cfg).base_url"
            type="text"
            class="svc-cfg-input"
            placeholder="https://api.openai.com/v1"
          />
        </label>
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.modelNameLabel") }}</span>
          <input
            v-model="cloudModel(cfg).model"
            type="text"
            class="svc-cfg-input"
            placeholder="gpt-4o"
          />
        </label>
      </div>

      <!-- Row 2: api_key + context_size -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.apiKeyLabel") }}</span>
          <MaskedPasswordInput
            :model-value="cloudApiKeyDraft ?? cloudModel(cfg).api_key ?? ''"
            prefix-icon="🔑"
            placeholder="your-cloud-api-key"
            :aria-label="t('serviceConfig.apiKeyLabel')"
            @update:model-value="onCloudApiKeyChange"
          />
        </label>
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.contextSizeLabel") }}</span>
          <input
            v-model.number="cloudModel(cfg).context_size"
            type="number"
            class="svc-cfg-input"
            min="1024"
            step="1024"
            placeholder="32768"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.promptOptTokenBudget32k") }}</span>
        </label>
      </div>

      <!-- 🛡️ Data Privacy Policy (default-open) -->
      <CollapsibleCard
        :default-open="true"
        class="svc-cfg-card--nested"
      >
        <template #title>
          🛡️ {{ t("serviceConfig.dataPrivacyTitle") }}
        </template>
        <p class="svc-cfg-hint">
          {{ t("serviceConfig.uploadPolicyHint") }}
        </p>

        <div class="svc-cfg-field svc-cfg-field--row">
          <span class="svc-cfg-label">{{ t("serviceConfig.enableSensitivityCheck") }}</span>
          <ToggleSwitch
            v-model="cloudUploadPolicy(cfg).enable_sensitivity_check"
            :aria-label="t('serviceConfig.enableSensitivityCheck')"
          />
        </div>
        <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.enableSensitivityCheckHint") }}</span>

        <div class="svc-cfg-field svc-cfg-field--row">
          <span class="svc-cfg-label">{{ t("serviceConfig.enableDesensitization") }}</span>
          <ToggleSwitch
            v-model="cloudUploadPolicy(cfg).enable_desensitization"
            :aria-label="t('serviceConfig.enableDesensitization')"
          />
        </div>
        <span class="svc-cfg-hint svc-cfg-hint--row">{{ t("serviceConfig.enableDesensitizationS1S2Hint") }}</span>
      </CollapsibleCard>

      <!-- 🔗 Failover Endpoints (V1 svc-cfg-subcard; V1 collapsedGroups empty
           ⇒ default-EXPANDED, useConfig.js:39/44) -->
      <CollapsibleCard
        :default-open="true"
        class="svc-cfg-card--nested"
      >
        <template #title>
          🔗 {{ t("serviceConfig.failoverEndpointsTitle") }}
        </template>
        <p class="svc-cfg-hint">
          {{ t("serviceConfig.failoverEndpointsHint") }}
        </p>

        <div
          v-for="(ep, idx) in publicEndpoints()"
          :key="`pub-ep-${idx}`"
          class="svc-cfg-endpoint"
        >
          <div class="svc-cfg-endpoint-header">
            <span class="svc-cfg-slot-title">
              {{ t("serviceConfig.endpointLabel") }} #{{ idx + 1 }}
            </span>
            <button
              type="button"
              class="btn-ghost btn-ghost--sm"
              @click="removePublicEndpoint(idx)"
            >
              ✕ {{ t("serviceConfig.deleteEndpoint") }}
            </button>
          </div>
          <div class="svc-cfg-row svc-cfg-row--2">
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">{{ t("serviceConfig.baseUrlLabel") }}</span>
              <input
                v-model="ep.base_url"
                type="text"
                class="svc-cfg-input"
                placeholder="https://api.openai.com/v1"
              />
            </label>
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">
                {{ t("serviceConfig.modelNameLabel") }}{{ t("serviceConfig.optionalSuffix") }}
              </span>
              <input
                v-model="ep.model"
                type="text"
                class="svc-cfg-input"
                :placeholder="t('serviceConfig.endpointModelPlaceholder')"
              />
            </label>
          </div>
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">
              {{ t("serviceConfig.apiKeyLabel") }}{{ t("serviceConfig.optionalSuffix") }}
            </span>
            <MaskedPasswordInput
              :model-value="ep.api_key ?? ''"
              prefix-icon="🔑"
              :placeholder="t('serviceConfig.endpointApiKeyPlaceholder')"
              :aria-label="t('serviceConfig.apiKeyLabel')"
              @update:model-value="ep.api_key = $event"
            />
          </label>
        </div>

        <button
          type="button"
          class="btn-ghost btn-ghost--block"
          @click="addPublicEndpoint"
        >
          ＋ {{ t("serviceConfig.addEndpoint") }}
        </button>
      </CollapsibleCard>
    </CollapsibleCard>

    <!-- --- 企业内网云 --- -->
    <CollapsibleCard>
      <template #title>
        <span class="svc-cfg-card-title-main">
          🏢 {{ t("serviceConfig.privateCloudTitle") }}
          <span class="svc-cfg-card-subtitle">{{ t("serviceConfig.privateCloudSubtitle") }}</span>
        </span>
        <span
          class="svc-cfg-card-header-toggle"
          @click.stop
          @keydown.stop
        >
          <ToggleSwitch
            v-model="enterpriseCloudModel(cfg).enabled"
            :aria-label="t('serviceConfig.enableEnterpriseCloudModel')"
          />
        </span>
      </template>

      <p class="svc-cfg-hint">
        {{ t("serviceConfig.privateCloudHint") }}
      </p>

      <!-- Row 1: base_url + model -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.baseUrlLabel") }}</span>
          <input
            v-model="enterpriseCloudModel(cfg).base_url"
            type="text"
            class="svc-cfg-input"
            placeholder="http://192.168.1.100:8080/v1"
          />
        </label>
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.modelNameLabel") }}</span>
          <input
            v-model="enterpriseCloudModel(cfg).model"
            type="text"
            class="svc-cfg-input"
            placeholder="qwen2.5-72b-instruct"
          />
        </label>
      </div>

      <!-- Row 2: api_key + context_size -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.apiKeyLabel") }}</span>
          <MaskedPasswordInput
            :model-value="enterpriseApiKeyDraft ?? enterpriseCloudModel(cfg).api_key ?? ''"
            prefix-icon="🔑"
            placeholder="your-enterprise-api-key"
            :aria-label="t('serviceConfig.apiKeyLabel')"
            @update:model-value="onEnterpriseApiKeyChange"
          />
        </label>
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.contextSizeLabel") }}</span>
          <input
            v-model.number="enterpriseCloudModel(cfg).context_size"
            type="number"
            class="svc-cfg-input"
            min="1024"
            step="1024"
            placeholder="16384"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.promptOptTokenBudget16k") }}</span>
        </label>
      </div>

      <!-- requireDesensS1 toggle -->
      <div class="svc-cfg-field svc-cfg-field--row">
        <span class="svc-cfg-label">{{ t("serviceConfig.requireDesensS1") }}</span>
        <ToggleSwitch
          :model-value="routing(cfg).enterprise_cloud_require_desensitize ?? false"
          :aria-label="t('serviceConfig.requireDesensS1')"
          @update:model-value="routing(cfg).enterprise_cloud_require_desensitize = $event"
        />
      </div>
      <!-- eslint-disable vue/no-v-html -->
      <span
        class="svc-cfg-hint svc-cfg-hint--row"
        v-html="t('serviceConfig.requireDesensS1Hint')"
      />
      <!-- eslint-enable vue/no-v-html -->

      <!-- 🔗 Failover Endpoints (V1 default-EXPANDED, useConfig.js:39/44) -->
      <CollapsibleCard
        :default-open="true"
        class="svc-cfg-card--nested"
      >
        <template #title>
          🔗 {{ t("serviceConfig.failoverEndpointsTitle") }}
        </template>
        <p class="svc-cfg-hint">
          {{ t("serviceConfig.failoverEndpointsHint") }}
        </p>

        <div
          v-for="(ep, idx) in enterpriseEndpoints()"
          :key="`ent-ep-${idx}`"
          class="svc-cfg-endpoint"
        >
          <div class="svc-cfg-endpoint-header">
            <span class="svc-cfg-slot-title">
              {{ t("serviceConfig.endpointLabel") }} #{{ idx + 1 }}
            </span>
            <button
              type="button"
              class="btn-ghost btn-ghost--sm"
              @click="removeEnterpriseEndpoint(idx)"
            >
              ✕ {{ t("serviceConfig.deleteEndpoint") }}
            </button>
          </div>
          <div class="svc-cfg-row svc-cfg-row--2">
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">{{ t("serviceConfig.baseUrlLabel") }}</span>
              <input
                v-model="ep.base_url"
                type="text"
                class="svc-cfg-input"
                placeholder="http://192.168.1.100:8080/v1"
              />
            </label>
            <label class="svc-cfg-field">
              <span class="svc-cfg-label">
                {{ t("serviceConfig.modelNameLabel") }}{{ t("serviceConfig.optionalSuffix") }}
              </span>
              <input
                v-model="ep.model"
                type="text"
                class="svc-cfg-input"
                :placeholder="t('serviceConfig.endpointModelPlaceholder')"
              />
            </label>
          </div>
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">
              {{ t("serviceConfig.apiKeyLabel") }}{{ t("serviceConfig.optionalSuffix") }}
            </span>
            <MaskedPasswordInput
              :model-value="ep.api_key ?? ''"
              prefix-icon="🔑"
              :placeholder="t('serviceConfig.endpointApiKeyPlaceholder')"
              :aria-label="t('serviceConfig.apiKeyLabel')"
              @update:model-value="ep.api_key = $event"
            />
          </label>
        </div>

        <button
          type="button"
          class="btn-ghost btn-ghost--block"
          @click="addEnterpriseEndpoint"
        >
          ＋ {{ t("serviceConfig.addEndpoint") }}
        </button>
      </CollapsibleCard>
    </CollapsibleCard>

    <!-- --- 共享云端设置（Advanced，dashed border + 徽章；V1 default-EXPANDED） --- -->
    <AdvancedCard :badge-text="t('serviceConfig.advancedBadge')">
      <template #title>
        <span class="svc-cfg-card-title-main">⚙️ {{ t("serviceConfig.sharedCloudTitle") }}</span>
      </template>

      <p class="svc-cfg-hint">
        {{ t("serviceConfig.sharedCloudHint") }}
      </p>

      <!-- Row: timeout + stream_timeout -->
      <div class="svc-cfg-row svc-cfg-row--2">
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.timeoutSeconds") }}</span>
          <input
            v-model.number="cloudShared(cfg).timeout_seconds"
            type="number"
            class="svc-cfg-input"
            min="10"
            max="600"
          />
        </label>
        <label class="svc-cfg-field">
          <span class="svc-cfg-label">{{ t("serviceConfig.streamTimeoutSeconds") }}</span>
          <input
            v-model.number="cloudShared(cfg).stream_timeout_seconds"
            type="number"
            class="svc-cfg-input"
            min="60"
            max="7200"
          />
          <span class="svc-cfg-hint">{{ t("serviceConfig.streamTimeoutHint") }}</span>
        </label>
      </div>

      <!-- 🔁 Retry & Circuit Breaker (V1 hard-coded English title; V1 default-EXPANDED) -->
      <CollapsibleCard
        :default-open="true"
        class="svc-cfg-card--nested"
      >
        <template #title>
          🔁 Retry &amp; Circuit Breaker
        </template>

        <div class="svc-cfg-row svc-cfg-row--2">
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.maxRetries") }}</span>
            <input
              v-model.number="cloudShared(cfg).retry.max"
              type="number"
              class="svc-cfg-input"
              min="0"
              max="10"
            />
          </label>
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.retryBackoffMs") }}</span>
            <input
              v-model.number="cloudShared(cfg).retry.backoff_ms"
              type="number"
              class="svc-cfg-input"
              min="0"
              max="5000"
            />
          </label>
        </div>

        <div class="svc-cfg-row svc-cfg-row--2">
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.maxTotalAttempts") }}</span>
            <input
              v-model.number="cloudShared(cfg).retry.max_total_attempts"
              type="number"
              class="svc-cfg-input"
              min="0"
            />
            <span class="svc-cfg-hint">{{ t("serviceConfig.maxTotalAttemptsHint") }}</span>
          </label>
          <div class="svc-cfg-field">
            <div class="svc-cfg-field--row">
              <span class="svc-cfg-label">{{ t("serviceConfig.switchEndpointOn429") }}</span>
              <ToggleSwitch
                v-model="cloudShared(cfg).retry.retry_on_429_switch_endpoint"
                :aria-label="t('serviceConfig.switchEndpointOn429')"
              />
            </div>
            <span class="svc-cfg-hint svc-cfg-hint--no-indent">{{ t("serviceConfig.switch429Hint") }}</span>
          </div>
        </div>

        <div class="svc-cfg-row svc-cfg-row--2">
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.circuitBreakerFailureThreshold") }}</span>
            <input
              v-model.number="cloudShared(cfg).circuit_breaker.failure_threshold"
              type="number"
              class="svc-cfg-input"
              min="1"
              max="20"
            />
          </label>
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.circuitBreakerCooldownSeconds") }}</span>
            <input
              v-model.number="cloudShared(cfg).circuit_breaker.cooldown_seconds"
              type="number"
              class="svc-cfg-input"
              min="10"
              max="600"
            />
          </label>
        </div>
      </CollapsibleCard>

      <!-- 📊 Rate Limiting (V1 hard-coded English title; V1 default-EXPANDED) -->
      <CollapsibleCard
        :default-open="true"
        class="svc-cfg-card--nested"
      >
        <template #title>
          📊 Rate Limiting
        </template>

        <div class="svc-cfg-row svc-cfg-row--2">
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.rateLimitMaxInferences") }}</span>
            <span class="svc-cfg-hint">0 = unlimited</span>
            <input
              v-model.number="cloudShared(cfg).rate_limit.max_inferences_per_task"
              type="number"
              class="svc-cfg-input"
              min="0"
            />
          </label>
          <label class="svc-cfg-field">
            <span class="svc-cfg-label">{{ t("serviceConfig.rateLimitMaxTokens") }}</span>
            <span class="svc-cfg-hint">0 = unlimited</span>
            <input
              v-model.number="cloudShared(cfg).rate_limit.max_tokens_per_task"
              type="number"
              class="svc-cfg-input"
              min="0"
            />
          </label>
        </div>
      </CollapsibleCard>
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

<style scoped>
/* Tab-local styles for V1-parity row slots that aren't already in
   service-config.css. Tokens-only — no hard-coded colours / sizes. */

/* CollapsibleCard #title slot lays out title-main + header-toggle on one row,
   pushing the toggle to the right edge (V1 svc-cfg-card-header). The arrow
   sits to the right of the toggle (provided by CollapsibleCard itself). */
.svc-cfg-card-title-main {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  flex: 1;
  min-width: 0;
}

.svc-cfg-card-header-toggle {
  display: inline-flex;
  align-items: center;
  margin-left: auto;
  margin-right: var(--space-2);
}

/* Endpoint svc-cfg-card (V1 svc-cfg-endpoint): a bordered block grouping the three
   fields per failover endpoint. Tokens only. */
.svc-cfg-endpoint {
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  background: var(--bg-secondary);
}

.svc-cfg-endpoint-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.svc-cfg-slot-title {
  font-weight: 600;
  font-size: var(--text-sm);
}

/* Local ghost-button styles (V1 .btn-ghost). The global btn classes live in
   styles/components/components.css; we add only the size variants we need. */
.btn-ghost {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  border-radius: 4px;
  padding: var(--space-1) var(--space-2);
  cursor: pointer;
  font: inherit;
}
.btn-ghost:hover {
  background: var(--bg-tertiary, var(--bg-secondary));
  color: var(--text-primary);
}
.btn-ghost--sm {
  padding: 2px var(--space-2);
  font-size: var(--text-xs);
}
.btn-ghost--block {
  width: 100%;
  margin-top: var(--space-1);
}

/* Nested collapsible svc-cfg-card visually de-emphasised vs the parent svc-cfg-card. */
.svc-cfg-card--nested {
  border: 1px solid var(--border);
  background: var(--bg-secondary);
}
</style>
