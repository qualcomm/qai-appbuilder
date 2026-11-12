<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CloudModelQuickAdd — add your own cloud models in three steps:
 *   1. pick a provider preset (or "Custom") -> Base URL is filled in,
 *   2. paste the API key and press "Fetch models",
 *   3. tick the models you want and press "Add".
 *
 * Nothing is saved until step 3. The probe (`POST /api/model-catalog/providers/probe`)
 * only lists the models; it never persists the key. Provider id, display name and
 * context length are derived (the backend also falls back to its family table when a
 * model has no `context_length`), so the user is never asked for them.
 * Sampling parameters (temperature / top_p / max_tokens) are intentionally not offered.
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { useToast } from "@/composables/useToast";

interface ExistingProvider {
  provider_id: string;
  config: {
    base_url?: string;
    models?: { model_id: string; [key: string]: unknown }[];
    [key: string]: unknown;
  };
}
interface ProbeResponse {
  ok: boolean;
  status: number | null;
  code: string | null;
  model_ids: string[];
  context_lengths: Record<string, number>;
}

const props = defineProps<{ providers: ExistingProvider[] }>();
const emit = defineEmits<{
  (e: "saved"): void;
  (e: "close"): void;
  (e: "advanced"): void;
}>();

const { t } = useI18n();
const toast = useToast();

interface Preset {
  id: string;
  labelKey: string;
  baseUrl: string;
}
/** `custom` has no fixed address; the others are OpenAI-compatible endpoints. */
const PRESETS: Preset[] = [
  { id: "deepseek", labelKey: "cloudModels.quick.presetDeepseek", baseUrl: "https://api.deepseek.com/v1" },
  { id: "dashscope", labelKey: "cloudModels.quick.presetDashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  { id: "zhipu", labelKey: "cloudModels.quick.presetZhipu", baseUrl: "https://open.bigmodel.cn/api/paas/v4" },
  { id: "custom", labelKey: "cloudModels.quick.presetCustom", baseUrl: "" },
];

const presetId = ref<string>("deepseek");
const baseUrl = ref<string>(PRESETS[0]!.baseUrl);
const apiKey = ref("");
const showKey = ref(false);
const fetching = ref(false);
const saving = ref(false);
const errorKey = ref<string | null>(null);
const fetched = ref(false);
const modelIds = ref<string[]>([]);
const contexts = ref<Record<string, number>>({});
const selected = ref<Set<string>>(new Set());
const filter = ref("");
const manualId = ref("");

const isCustom = computed(() => presetId.value === "custom");
const urlValid = computed(() => /^https?:\/\/[^\s/?#]+/i.test(baseUrl.value.trim()));
const visibleIds = computed(() => {
  const q = filter.value.trim().toLowerCase();
  return q === "" ? modelIds.value : modelIds.value.filter((m) => m.toLowerCase().includes(q));
});
const canFetch = computed(() => urlValid.value && !fetching.value && !saving.value);
const canSave = computed(() => selected.value.size > 0 && urlValid.value && !saving.value);

/** Provider id: the preset id, or one derived from the host for "Custom". */
const providerId = computed(() => {
  if (!isCustom.value) return presetId.value;
  try {
    const host = new URL(baseUrl.value.trim()).hostname.toLowerCase();
    const slug = host.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    return slug === "" ? "custom" : `custom-${slug}`;
  } catch {
    return "custom";
  }
});
const existing = computed(() => props.providers.find((p) => p.provider_id === providerId.value));
const existingIds = computed(
  () => new Set((existing.value?.config.models ?? []).map((m) => m.model_id)),
);

function pickPreset(id: string): void {
  presetId.value = id;
  const p = PRESETS.find((x) => x.id === id);
  baseUrl.value = p?.baseUrl ?? "";
  fetched.value = false;
  modelIds.value = [];
  selected.value = new Set();
  errorKey.value = null;
}

function codeToKey(code: string | null): string {
  switch (code) {
    case "unauthorized": return "cloudModels.quick.errUnauthorized";
    case "not_found": return "cloudModels.quick.errNotFound";
    case "unreachable": return "cloudModels.quick.errUnreachable";
    case "invalid_url": return "cloudModels.quick.errInvalidUrl";
    default: return "cloudModels.quick.errOther";
  }
}

async function fetchModels(): Promise<void> {
  if (!canFetch.value) return;
  fetching.value = true;
  errorKey.value = null;
  try {
    const res = await apiJson<ProbeResponse>("POST", "/api/model-catalog/providers/probe", {
      base_url: baseUrl.value.trim(),
      api_key: apiKey.value.trim(),
    });
    fetched.value = true;
    if (!res.ok) {
      errorKey.value = codeToKey(res.code);
      return;
    }
    modelIds.value = res.model_ids;
    contexts.value = { ...contexts.value, ...res.context_lengths };
    if (res.model_ids.length === 0) errorKey.value = "cloudModels.quick.errEmpty";
  } catch {
    errorKey.value = "cloudModels.quick.errOther";
  } finally {
    fetching.value = false;
  }
}

function toggle(id: string): void {
  if (existingIds.value.has(id)) return;
  const next = new Set(selected.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selected.value = next;
}

function addManual(): void {
  const id = manualId.value.trim();
  if (id === "") return;
  if (!modelIds.value.includes(id)) modelIds.value = [...modelIds.value, id];
  if (!existingIds.value.has(id)) selected.value = new Set(selected.value).add(id);
  manualId.value = "";
}

async function save(): Promise<void> {
  if (!canSave.value) return;
  saving.value = true;
  try {
    const base = existing.value?.config ?? {};
    const keep = base.models ?? [];
    const added = [...selected.value]
      .filter((id) => !existingIds.value.has(id))
      .map((id) => {
        const m: Record<string, unknown> = { model_id: id, name: id, supports_streaming: true };
        const ctx = contexts.value[id];
        if (typeof ctx === "number" && ctx > 0) m.context_length = ctx;
        return m;
      });
    const config: Record<string, unknown> = {
      ...base,
      base_url: baseUrl.value.trim(),
      models: [...keep, ...added],
    };
    delete config.api_key;
    if (apiKey.value.trim() !== "") config.api_key = apiKey.value.trim();
    await apiJson("PUT", `/api/model-catalog/providers/${providerId.value}`, { config });
    toast.success(t("cloudModels.quick.added", { n: added.length }));
    emit("saved");
    emit("close");
  } catch {
    errorKey.value = "cloudModels.quick.errSave";
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <section class="quick-add">
    <header class="quick-head">
      <strong>{{ t("cloudModels.quick.title") }}</strong>
      <button type="button" class="btn btn-sm" @click="emit('close')">{{ t("common.close") }}</button>
    </header>

    <div class="quick-step">
      <label class="quick-label">{{ t("cloudModels.quick.step1") }}</label>
      <div class="quick-presets">
        <button
          v-for="p in PRESETS"
          :key="p.id"
          type="button"
          class="btn btn-sm"
          :class="{ 'btn-primary': presetId === p.id }"
          @click="pickPreset(p.id)"
        >{{ t(p.labelKey) }}</button>
      </div>
      <input
        v-model="baseUrl"
        class="quick-input"
        spellcheck="false"
        :readonly="!isCustom"
        :placeholder="t('cloudModels.quick.urlPlaceholder')"
      />
    </div>

    <div class="quick-step">
      <label class="quick-label">{{ t("cloudModels.quick.step2") }}</label>
      <div class="quick-row">
        <input
          v-model="apiKey"
          class="quick-input"
          :type="showKey ? 'text' : 'password'"
          autocomplete="off"
          spellcheck="false"
          :placeholder="t('cloudModels.quick.keyPlaceholder')"
          @keydown.enter.prevent="fetchModels"
        />
        <button type="button" class="btn btn-sm" @click="showKey = !showKey">{{ showKey ? "🙈" : "👁" }}</button>
        <button type="button" class="btn btn-primary btn-sm" :disabled="!canFetch" @click="fetchModels">
          {{ fetching ? t("cloudModels.quick.fetching") : t("cloudModels.quick.fetch") }}
        </button>
      </div>
      <p v-if="errorKey !== null" class="quick-hint quick-warn">{{ t(errorKey) }}</p>
    </div>

    <div class="quick-step">
      <label class="quick-label">{{ t("cloudModels.quick.step3") }}</label>
      <input
        v-if="modelIds.length > 8"
        v-model="filter"
        class="quick-input"
        :placeholder="t('cloudModels.quick.filter')"
      />
      <ul v-if="visibleIds.length > 0" class="quick-list">
        <li v-for="id in visibleIds" :key="id">
          <label :class="{ 'quick-have': existingIds.has(id) }">
            <input
              type="checkbox"
              :checked="selected.has(id) || existingIds.has(id)"
              :disabled="existingIds.has(id)"
              @change="toggle(id)"
            />
            <span class="quick-mono">{{ id }}</span>
            <span v-if="existingIds.has(id)" class="quick-hint">{{ t("cloudModels.quick.alreadyAdded") }}</span>
          </label>
        </li>
      </ul>
      <p v-else-if="!fetched" class="quick-hint">{{ t("cloudModels.quick.notFetched") }}</p>
      <div class="quick-row">
        <input
          v-model="manualId"
          class="quick-input"
          spellcheck="false"
          :placeholder="t('cloudModels.quick.manualPlaceholder')"
          @keydown.enter.prevent="addManual"
        />
        <button type="button" class="btn btn-sm" :disabled="manualId.trim() === ''" @click="addManual">
          {{ t("cloudModels.quick.manualAdd") }}
        </button>
      </div>
    </div>

    <footer class="quick-foot">
      <a href="#" class="quick-hint" @click.prevent="emit('advanced')">{{ t("cloudModels.quick.advanced") }}</a>
      <button type="button" class="btn btn-primary btn-sm" :disabled="!canSave" @click="save">
        {{ saving ? t("cloudModels.quick.saving") : t("cloudModels.quick.addBtn", { n: selected.size }) }}
      </button>
    </footer>
  </section>
</template>

<style scoped>
.quick-add {
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
  border-radius: var(--radius-md, 8px);
  padding: var(--space-3, 12px);
  margin-bottom: var(--space-3, 12px);
  display: flex;
  flex-direction: column;
  gap: var(--space-3, 12px);
}
.quick-head,
.quick-foot,
.quick-row {
  display: flex;
  align-items: center;
  gap: var(--space-2, 8px);
}
.quick-head,
.quick-foot {
  justify-content: space-between;
}
.quick-step {
  display: flex;
  flex-direction: column;
  gap: var(--space-2, 8px);
}
.quick-label {
  font-weight: 600;
  color: var(--text-primary);
}
.quick-presets {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2, 8px);
}
.quick-input {
  flex: 1;
  min-width: 0;
}
.quick-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 220px;
  overflow: auto;
}
.quick-list label {
  display: flex;
  align-items: center;
  gap: var(--space-2, 8px);
  padding: 2px 0;
  cursor: pointer;
}
.quick-have {
  opacity: 0.6;
  cursor: default;
}
.quick-mono {
  font-family: var(--font-mono);
}
.quick-hint {
  margin: 0;
  font-size: var(--text-sm, 0.85rem);
  color: var(--text-secondary);
}
.quick-warn {
  color: var(--warning, #d29922);
}
</style>
