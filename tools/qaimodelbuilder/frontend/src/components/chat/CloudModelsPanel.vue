<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CloudModelsPanel — V1-parity cloud model catalog management with full
 * per-model CRUD (Settings → Cloud Models, backed by `cloud_models.json`).
 *
 * Aligns with V1 `CloudModelsPanel.js` + `useCloudModels.js`:
 *   - "+ Add Model" + search toolbar
 *   - provider groups (📌pin + base_url + api_key inputs)
 *   - per-model cards with ✏️ edit / ⧉ clone / 🗑️ delete
 *   - right-hand "Edit Model" side panel (Model ID / Display Name / Provider /
 *     Provider Base URL / Provider API Key / Context Length / Description /
 *     Supports Streaming) + Save / Cancel
 *
 * Backend contract (V2, measured):
 *   GET  /api/model-catalog/providers
 *     → { providers: [{ provider_id, config: { base_url, pinned,
 *          models: [{ model_id, name, context_length?, description?,
 *          supports_streaming? }] } }] }   (api_key NOT returned — secrets)
 *   PUT  /api/model-catalog/providers/{provider_id}  body { config }
 *     → upserts the whole provider config (creates the provider if new).
 *
 * Since V2 models live INSIDE a provider's `config.models`, model CRUD is
 * expressed as: mutate the target provider's `config.models` array and PUT
 * that provider. Moving a model to a different provider PUTs both the old
 * (model removed) and the new (model added) provider. This matches V1's
 * net behaviour (add/edit/clone/delete a cloud model) over the V2 wire
 * shape without needing a new backend endpoint.
 */
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { useToast } from "@/composables/useToast";
import { useConfirm } from "@/composables/useConfirm";
import ToggleSwitch from "@/components/chat/service-config/ToggleSwitch.vue";
// Shared help-manual affordance — see components/common/HelpButton.vue.
// Docs live under `frontend/src/help-content/cloud-models.<locale>.md`.
import HelpButton from "@/components/common/HelpButton.vue";

// ─── Types ───────────────────────────────────────────────────────────────────

interface CloudModel {
  model_id: string;
  name?: string;
  context_length?: number;
  description?: string;
  supports_streaming?: boolean;
  api_model_id?: string;
  params?: Record<string, unknown>;
  [key: string]: unknown;
}

interface ProviderConfig {
  base_url?: string;
  api_key?: string;
  pinned?: boolean;
  models?: CloudModel[];
  [key: string]: unknown;
}

interface ProviderRow {
  provider_id: string;
  config: ProviderConfig;
}

interface ProvidersResponse {
  providers: ProviderRow[];
}

interface ModelForm {
  model_id: string;
  name: string;
  provider: string;
  provider_base_url: string;
  provider_api_key: string;
  context_length: number;
  description: string;
  supports_streaming: boolean;
  api_model_id: string;
  // Per-model sampling-parameter support flags (cloud_models.json
  // ``models[].params``). ``true`` = the model accepts the parameter
  // (default); ``false`` = the param is dropped from the wire so the model
  // does not 400. These let the user override the hard-coded family-regex
  // defaults — e.g. when a renamed model id no longer matches the regex.
  param_temperature_supported: boolean;
  param_top_p_supported: boolean;
  param_max_tokens_supported: boolean;
  // Vertex AI thinking models require the prior tool_calls' thought_signature
  // to be echoed back; turning this on enables the history-flatten safety net.
  param_thought_signature_required: boolean;
}

//: Raw ``params`` object preserved verbatim across an edit so user-authored
//: fine-grained constraints (min / max / default) are never clobbered when
//: only the support toggles change.
type RawParams = Record<string, unknown>;

// ─── State ───────────────────────────────────────────────────────────────────

const toast = useToast();
const { confirm } = useConfirm();
const { t } = useI18n();

const providers = ref<ProviderRow[]>([]);
const loading = ref(false);
const search = ref("");
const keyVisible = ref<Record<string, boolean>>({});

// Side-panel edit state (V1 `cloudModelEditKey` / `cloudModelForm`).
const panelOpen = ref(false);
// { provider, model_id } of the row being edited, or null for add/clone.
const editKey = ref<{ provider: string; model_id: string } | null>(null);
const saving = ref(false);
const formError = ref("");
const showFormKey = ref(false);
const form = ref<ModelForm>(emptyForm());
//: The model entry's original ``params`` object (or {}), preserved so a
//: subsequent save keeps fine-grained constraints (min/max/default) the UI
//: does not expose as toggles.
const formRawParams = ref<RawParams>({});

function emptyForm(): ModelForm {
  return {
    model_id: "",
    name: "",
    provider: "",
    provider_base_url: "",
    provider_api_key: "",
    context_length: 128000,
    description: "",
    supports_streaming: true,
    api_model_id: "",
    param_temperature_supported: true,
    param_top_p_supported: true,
    param_max_tokens_supported: true,
    param_thought_signature_required: false,
  };
}

/** Read a ``params[key].supported`` flag, defaulting to ``true`` (supported). */
function readSupported(params: RawParams | undefined, key: string): boolean {
  const slice = params?.[key];
  if (slice !== null && typeof slice === "object") {
    const s = (slice as Record<string, unknown>).supported;
    if (typeof s === "boolean") return s;
  }
  return true;
}

/** Read ``params.thought_signature.required``, defaulting to ``false``. */
function readThoughtSigRequired(params: RawParams | undefined): boolean {
  const slice = params?.thought_signature;
  if (slice !== null && typeof slice === "object") {
    const r = (slice as Record<string, unknown>).required;
    if (typeof r === "boolean") return r;
  }
  return false;
}

// ─── Computed ────────────────────────────────────────────────────────────────

const filteredProviders = computed<ProviderRow[]>(() => {
  const q = search.value.trim().toLowerCase();
  if (q === "") return providers.value;
  return providers.value
    .map((p) => {
      const models = (p.config.models ?? []).filter(
        (m) =>
          (m.name ?? "").toLowerCase().includes(q) ||
          m.model_id.toLowerCase().includes(q) ||
          p.provider_id.toLowerCase().includes(q),
      );
      return { ...p, config: { ...p.config, models } };
    })
    .filter((p) => (p.config.models ?? []).length > 0);
});

const isEmpty = computed(() => !loading.value && providers.value.length === 0);

const knownProviderIds = computed(() =>
  providers.value.map((p) => p.provider_id),
);

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatCtx(n: number | undefined): string {
  // V1 parity (`useCloudModels.js:formatCtx`): no value → "?"; ≥1M → "{n}M";
  // ≥1K → "{n}K" (no "ctx" suffix); else the raw number.
  if (typeof n !== "number" || !Number.isFinite(n) || n <= 0) return "?";
  if (n >= 1000000) return `${Math.round(n / 1000000)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return String(n);
}

function findProvider(id: string): ProviderRow | undefined {
  return providers.value.find((p) => p.provider_id === id);
}

// ─── Load ────────────────────────────────────────────────────────────────────

async function fetchProviders(): Promise<void> {
  loading.value = true;
  try {
    const res = await apiJson<ProvidersResponse>(
      "GET",
      "/api/model-catalog/providers",
    );
    providers.value = res.providers ?? [];
  } catch {
    providers.value = [];
    toast.error(t("cloudModels.loadFailed"));
  } finally {
    loading.value = false;
  }
}

// ─── Provider field save (base_url / api_key / pinned) ───────────────────────

async function putProviderConfig(
  providerId: string,
  config: ProviderConfig,
): Promise<void> {
  await apiJson("PUT", `/api/model-catalog/providers/${providerId}`, {
    config,
  });
}

async function saveProviderField(
  providerId: string,
  field: "base_url" | "pinned",
  value: string | boolean,
): Promise<void> {
  const row = findProvider(providerId);
  if (row === undefined) return;
  const nextConfig: ProviderConfig = { ...row.config, [field]: value };
  // Never echo a secret we don't have; drop api_key from the sent config so
  // the backend keeps the stored secret untouched.
  delete nextConfig.api_key;
  try {
    await putProviderConfig(providerId, nextConfig);
    row.config = nextConfig;
    // V1 parity (`useCloudModels.js:saveProviderField`): toast is
    // `{provider} {API Key|Base URL} {saved}` — non-api_key fields use the
    // "Base URL" label.
    toast.success(`${providerId} ${t("cloudModels.providerBaseUrl")} ${t("cloudModels.providerFieldSaved")}`);
  } catch (e) {
    toast.error(
      `${t("cloudModels.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  }
}

async function saveProviderApiKey(providerId: string, ev: Event): Promise<void> {
  const value = (ev.target as HTMLInputElement).value;
  if (value.trim() === "") return; // blank = keep existing secret
  const row = findProvider(providerId);
  if (row === undefined) return;
  try {
    await putProviderConfig(providerId, { ...row.config, api_key: value });
    toast.success(`${providerId} ${t("cloudModels.providerApiKey")} ${t("cloudModels.providerFieldSaved")}`);
  } catch (e) {
    toast.error(
      `${t("cloudModels.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  }
}

function toggleKeyVisible(providerId: string): void {
  keyVisible.value = {
    ...keyVisible.value,
    [providerId]: !keyVisible.value[providerId],
  };
}

function togglePinned(row: ProviderRow): void {
  void saveProviderField(
    row.provider_id,
    "pinned",
    !(row.config.pinned ?? false),
  );
}

function onBaseUrlChange(providerId: string, ev: Event): void {
  void saveProviderField(
    providerId,
    "base_url",
    (ev.target as HTMLInputElement).value,
  );
}

// ─── Form helpers ────────────────────────────────────────────────────────────

/**
 * V1 parity (`CloudModelsPanel.js:138`): when the user types or picks a known
 * Provider in the form's Provider input, auto-fill the Provider Base URL from
 * the already-loaded providers list, so they don't have to retype it.
 *
 * V2 safety improvement (B-7/B-8) preserved: api_key is NOT echoed by the
 * backend (secret-store) and we deliberately do NOT auto-fill the API Key
 * field here — V1 used to copy api_key into the form, which was the very
 * leak V2 fixed. The user must retype the key if they want to change it;
 * blank means "keep existing".
 */
function onProviderInputChange(): void {
  const id = form.value.provider.trim();
  if (id === "") return;
  const known = findProvider(id);
  if (known === undefined) return;
  const presetBaseUrl = known.config.base_url ?? "";
  // Only overwrite when the form's base_url is still empty or matches a known
  // provider's preset — never clobber a value the user typed manually.
  const current = form.value.provider_base_url.trim();
  const looksLikePreset =
    current === "" ||
    providers.value.some((p) => (p.config.base_url ?? "") === current);
  if (looksLikePreset) {
    form.value.provider_base_url = presetBaseUrl;
  }
}

// ─── Model CRUD (side panel) ─────────────────────────────────────────────────

function openAddForm(): void {
  formError.value = "";
  showFormKey.value = false;
  editKey.value = null;
  form.value = emptyForm();
  formRawParams.value = {};
  panelOpen.value = true;
}

function openEditForm(providerId: string, model: CloudModel): void {
  formError.value = "";
  showFormKey.value = false;
  editKey.value = { provider: providerId, model_id: model.model_id };
  const prov = findProvider(providerId);
  const rawParams: RawParams =
    model.params !== null && typeof model.params === "object"
      ? { ...(model.params as RawParams) }
      : {};
  formRawParams.value = rawParams;
  form.value = {
    model_id: model.model_id,
    name: model.name ?? "",
    provider: providerId,
    provider_base_url: prov?.config.base_url ?? "",
    provider_api_key: "",
    context_length:
      typeof model.context_length === "number" ? model.context_length : 128000,
    description: model.description ?? "",
    supports_streaming: model.supports_streaming ?? true,
    api_model_id: typeof model.api_model_id === "string" ? model.api_model_id : "",
    param_temperature_supported: readSupported(rawParams, "temperature"),
    param_top_p_supported: readSupported(rawParams, "top_p"),
    param_max_tokens_supported: readSupported(rawParams, "max_tokens"),
    param_thought_signature_required: readThoughtSigRequired(rawParams),
  };
  panelOpen.value = true;
}

function cloneForm(providerId: string, model: CloudModel): void {
  formError.value = "";
  showFormKey.value = false;
  editKey.value = null; // add mode
  const prov = findProvider(providerId);
  const rawParams: RawParams =
    model.params !== null && typeof model.params === "object"
      ? { ...(model.params as RawParams) }
      : {};
  formRawParams.value = rawParams;
  form.value = {
    model_id: "", // clear so the user supplies a new id
    name: model.name ?? "",
    provider: providerId,
    provider_base_url: prov?.config.base_url ?? "",
    provider_api_key: "",
    context_length:
      typeof model.context_length === "number" ? model.context_length : 128000,
    description: model.description ?? "",
    supports_streaming: model.supports_streaming ?? true,
    api_model_id: typeof model.api_model_id === "string" ? model.api_model_id : "",
    param_temperature_supported: readSupported(rawParams, "temperature"),
    param_top_p_supported: readSupported(rawParams, "top_p"),
    param_max_tokens_supported: readSupported(rawParams, "max_tokens"),
    param_thought_signature_required: readThoughtSigRequired(rawParams),
  };
  panelOpen.value = true;
}

function closePanel(): void {
  panelOpen.value = false;
  editKey.value = null;
  formError.value = "";
}

function buildModelFromForm(f: ModelForm): CloudModel {
  const m: CloudModel = {
    model_id: f.model_id.trim(),
    name: f.name.trim(),
    context_length: f.context_length,
    supports_streaming: f.supports_streaming,
  };
  if (f.description.trim() !== "") m.description = f.description.trim();
  // V1 ``api_model_id`` override: only persist when the user supplied a
  // distinct wire id so the stored JSON stays clean.
  if (f.api_model_id.trim() !== "") m.api_model_id = f.api_model_id.trim();
  // Per-model sampling-param constraints. Start from the preserved raw params
  // so user-authored min/max/default survive, then overlay the UI toggles.
  // A param is only written when it deviates from the implicit default
  // (supported=true / required=false) so the stored JSON stays minimal.
  const params: RawParams = { ...formRawParams.value };
  applySupportedToggle(params, "temperature", f.param_temperature_supported);
  applySupportedToggle(params, "top_p", f.param_top_p_supported);
  applySupportedToggle(params, "max_tokens", f.param_max_tokens_supported);
  applyRequiredToggle(
    params,
    "thought_signature",
    f.param_thought_signature_required,
  );
  if (Object.keys(params).length > 0) m.params = params;
  return m;
}

/**
 * Overlay a ``supported`` toggle onto ``params[key]``. ``true`` (the default)
 * removes an explicit ``supported`` flag — and prunes the slice entirely when
 * nothing else remains — so the stored JSON only records deviations.
 */
function applySupportedToggle(
  params: RawParams,
  key: string,
  supported: boolean,
): void {
  const existing =
    params[key] !== null && typeof params[key] === "object"
      ? { ...(params[key] as Record<string, unknown>) }
      : {};
  if (supported) {
    delete existing.supported;
  } else {
    existing.supported = false;
  }
  if (Object.keys(existing).length === 0) {
    delete params[key];
  } else {
    params[key] = existing;
  }
}

/** Overlay a ``required`` toggle onto ``params[key]`` (same minimisation). */
function applyRequiredToggle(
  params: RawParams,
  key: string,
  required: boolean,
): void {
  const existing =
    params[key] !== null && typeof params[key] === "object"
      ? { ...(params[key] as Record<string, unknown>) }
      : {};
  if (required) {
    existing.required = true;
  } else {
    delete existing.required;
  }
  if (Object.keys(existing).length === 0) {
    delete params[key];
  } else {
    params[key] = existing;
  }
}

async function submitForm(): Promise<void> {
  const f = form.value;
  if (f.model_id.trim() === "") {
    formError.value = t("cloudModels.modelIdRequired");
    return;
  }
  if (f.name.trim() === "") {
    formError.value = t("cloudModels.displayNameRequired");
    return;
  }
  const providerId = f.provider.trim();
  if (providerId === "") {
    formError.value = t("cloudModels.providerRequired");
    return;
  }
  formError.value = "";
  saving.value = true;
  try {
    const newModel = buildModelFromForm(f);
    // Duplicate (provider, model_id) check. Skip when editing in-place
    // and the model_id hasn't changed (it's obviously already there).
    const target = findProvider(providerId);
    const existing = target?.config.models ?? [];
    const isInPlaceEdit =
      editKey.value !== null &&
      editKey.value.provider === providerId &&
      editKey.value.model_id === newModel.model_id;
    if (!isInPlaceEdit) {
      const dup = existing.some((m) => m.model_id === newModel.model_id);
      if (dup) {
        formError.value = t("cloudModels.modelIdDuplicate", {
          model_id: newModel.model_id,
        });
        saving.value = false;
        return;
      }
    }

    // If editing and the provider changed, remove the model from the old
    // provider first (PUT old provider without it).
    if (
      editKey.value !== null &&
      editKey.value.provider !== providerId
    ) {
      const old = findProvider(editKey.value.provider);
      if (old !== undefined) {
        const oldModels = (old.config.models ?? []).filter(
          (m) => m.model_id !== editKey.value!.model_id,
        );
        const oldConfig: ProviderConfig = { ...old.config, models: oldModels };
        delete oldConfig.api_key;
        await putProviderConfig(old.provider_id, oldConfig);
        old.config = oldConfig;
      }
    }

    // Build the target provider's next models list.
    let nextModels: CloudModel[];
    if (
      editKey.value !== null &&
      editKey.value.provider === providerId
    ) {
      // In-place edit within same provider.
      nextModels = (target?.config.models ?? []).map((m) =>
        m.model_id === editKey.value!.model_id ? newModel : m,
      );
    } else {
      nextModels = [...(target?.config.models ?? []), newModel];
    }

    // Target provider config: keep existing base_url/pinned, apply the
    // form's base_url, set models. api_key only if the user typed one.
    const baseConfig: ProviderConfig = target?.config ?? {};
    const nextConfig: ProviderConfig = {
      ...baseConfig,
      base_url: f.provider_base_url.trim(),
      models: nextModels,
    };
    delete nextConfig.api_key;
    if (f.provider_api_key.trim() !== "") {
      nextConfig.api_key = f.provider_api_key.trim();
    }
    await putProviderConfig(providerId, nextConfig);

    const wasEdit = editKey.value !== null;
    await fetchProviders();
    closePanel();
    toast.success(wasEdit ? t("cloudModels.modelUpdated") : t("cloudModels.modelAdded"));
  } catch (e) {
    formError.value = e instanceof Error ? e.message : String(e);
  } finally {
    saving.value = false;
  }
}

async function confirmDelete(providerId: string, model: CloudModel): Promise<void> {
  // V1 parity (`useCloudModels.js:confirmDeleteCloudModel`): use the global
  // confirm dialog (danger style) rather than a native window.confirm.
  const ok = await confirm({
    icon: "🗑️",
    title: t("cloudModels.delete"),
    message: t("cloudModels.confirmDeleteMsg", {
      name: model.name ?? model.model_id,
      model_id: model.model_id,
    }),
    confirmText: t("common.delete"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
  });
  if (!ok) return;
  const row = findProvider(providerId);
  if (row === undefined) return;
  try {
    const nextModels = (row.config.models ?? []).filter(
      (m) => m.model_id !== model.model_id,
    );
    const nextConfig: ProviderConfig = { ...row.config, models: nextModels };
    delete nextConfig.api_key;
    await putProviderConfig(providerId, nextConfig);
    row.config = nextConfig;
    if (
      editKey.value !== null &&
      editKey.value.provider === providerId &&
      editKey.value.model_id === model.model_id
    ) {
      closePanel();
    }
    toast.success(t("cloudModels.deleted"));
  } catch (e) {
    toast.error(
      `${t("cloudModels.deleteFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  }
}

onMounted(() => {
  void fetchProviders();
  // V1 parity (app.js:2295-2298): Escape closes the cloud-model side panel.
  window.addEventListener("keydown", onGlobalKeydown);
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onGlobalKeydown);
});

function onGlobalKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && panelOpen.value) {
    event.stopPropagation();
    closePanel();
  }
}
</script>

<template>
  <div class="cloud-models-panel">
    <!-- Toolbar: Add + search (full width, above the two-column layout) -->
    <div class="cloud-models-toolbar">
      <button
        type="button"
        class="btn btn-primary btn-sm"
        @click="openAddForm"
      >
        + {{ t("cloudModels.add") }}
      </button>
      <input
        v-model="search"
        class="cloud-models-search"
        :placeholder="'🔍 ' + t('cloudModels.search')"
      />
      <!-- Help affordance for provider / base_url / API key onboarding.
           Placed at the end of the toolbar so it sits alongside Add/Search
           without disrupting the current visual weight. -->
      <HelpButton
        doc-key="cloud-models"
        external-url="https://platform.openai.com/docs/api-reference"
        size="sm"
      />
    </div>

    <div
      class="cloud-models-layout"
      :class="{ 'panel-open': panelOpen }"
    >
      <div class="cloud-models-list-col">
        <div
          v-if="loading && providers.length === 0"
          style="display:flex;justify-content:center;padding:40px"
        >
          <div
            class="spinner"
            style="width:32px;height:32px;border-width:3px"
          ></div>
        </div>

        <template v-else>
          <div
            v-if="isEmpty || filteredProviders.length === 0"
            class="cloud-models-empty"
          >
            <div style="font-size:32px;margin-bottom:8px">
              ☁️
            </div>
            <div>{{ t("cloudModels.noModels") }}</div>
            <div class="cloud-models-empty-hint">
              {{ t("cloudModels.addFirst") }}
            </div>
          </div>

          <div
            v-else
            class="cloud-models-list"
          >
            <div
              v-for="row in filteredProviders"
              :key="row.provider_id"
              class="cloud-model-provider-group"
            >
              <!-- Provider header: pin + base_url + api_key -->
              <div
                class="cloud-model-provider-label"
                style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"
              >
                <span style="white-space:nowrap;font-weight:600">
                  <span
                    v-if="row.config.pinned"
                    :title="t('cloudModels.pinned')"
                    style="margin-right:2px"
                  >📌</span>
                  {{ row.provider_id }}
                </span>
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  style="padding:2px 6px;flex-shrink:0"
                  :title="row.config.pinned ? t('cloudModels.unpin') : t('cloudModels.pinProvider')"
                  @click="togglePinned(row)"
                >
                  {{ row.config.pinned ? "📌" : "📍" }}
                </button>
                <input
                  class="cloud-models-search"
                  style="flex:2;min-width:160px;font-size:var(--text-xs);padding:3px 8px;font-family:var(--font-mono)"
                  :value="row.config.base_url ?? ''"
                  :placeholder="t('cloudModels.baseUrlOptional')"
                  @change="onBaseUrlChange(row.provider_id, $event)"
                />
                <div style="flex:2;min-width:160px;display:flex;align-items:center;gap:4px">
                  <input
                    class="cloud-models-search"
                    style="flex:1;font-size:var(--text-xs);padding:3px 8px;font-family:var(--font-mono)"
                    :type="keyVisible[row.provider_id] ? 'text' : 'password'"
                    :placeholder="t('cloudModels.apiKeyKeepHint')"
                    @change="saveProviderApiKey(row.provider_id, $event)"
                  />
                  <button
                    type="button"
                    class="config-eye-btn"
                    :title="keyVisible[row.provider_id] ? t('cloudModels.hide') : t('cloudModels.show')"
                    @click="toggleKeyVisible(row.provider_id)"
                  >
                    {{ keyVisible[row.provider_id] ? "🙈" : "👁" }}
                  </button>
                </div>
              </div>

              <!-- Model cards -->
              <div style="display:flex;flex-direction:column;gap:8px">
                <div
                  v-for="m in (row.config.models ?? [])"
                  :key="row.provider_id + '::' + m.model_id"
                  class="cloud-model-card"
                  :class="{ editing: editKey && editKey.provider === row.provider_id && editKey.model_id === m.model_id }"
                >
                  <div class="cloud-model-card-info">
                    <div class="cloud-model-card-name">
                      {{ m.name ?? m.model_id }}
                    </div>
                    <div class="cloud-model-card-id">
                      {{ m.model_id }}
                    </div>
                    <div
                      v-if="m.description"
                      class="cloud-model-card-desc"
                    >
                      {{ m.description }}
                    </div>
                    <div class="cloud-model-card-meta">
                      <span
                        v-if="formatCtx(m.context_length) !== ''"
                        class="cloud-model-badge"
                      >{{ formatCtx(m.context_length) }}</span>
                      <span
                        v-if="m.supports_streaming"
                        class="cloud-model-badge"
                      >{{ t("cloudModels.supportsStreaming") }}</span>
                    </div>
                  </div>
                  <div class="cloud-model-card-actions">
                    <button
                      type="button"
                      class="btn btn-ghost btn-sm"
                      :title="t('common.edit')"
                      @click="openEditForm(row.provider_id, m)"
                    >
                      ✏️
                    </button>
                    <button
                      type="button"
                      class="btn btn-ghost btn-sm"
                      :title="t('cloudModels.clone')"
                      @click="cloneForm(row.provider_id, m)"
                    >
                      ⧉
                    </button>
                    <button
                      type="button"
                      class="btn btn-ghost btn-sm"
                      style="color: var(--error)"
                      :title="t('common.delete')"
                      @click="confirmDelete(row.provider_id, m)"
                    >
                      🗑️
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- Edit / Add side panel (only rendered when open so it doesn't
           reserve the right-hand column while closed) -->
      <div
        v-if="panelOpen"
        class="cloud-model-side-panel open"
      >
        <div class="cloud-model-side-panel-header">
          <span v-if="editKey">✏️ {{ t("cloudModels.edit") }}</span>
          <span v-else>+ {{ t("cloudModels.add") }}</span>
          <button
            type="button"
            class="config-eye-btn"
            :title="t('common.close')"
            @click="closePanel"
          >
            ✕
          </button>
        </div>
        <!-- Error banner: fixed at top of panel, always visible without scrolling -->
        <div
          v-if="formError !== ''"
          style="padding:8px 16px;background:var(--error-bg, rgba(211,47,47,0.1));border-bottom:1px solid var(--error, #d32f2f);color:var(--error, #d32f2f);font-size:var(--text-sm);font-weight:500"
        >
          ⚠️ {{ formError }}
        </div>
        <div class="cloud-model-side-panel-body">
          <div class="config-field">
            <label class="config-label">{{ t("cloudModels.modelId") }} <span style="color:var(--error)">*</span></label>
            <input
              v-model="form.model_id"
              class="config-input mono"
            />
            <div class="config-comment">
              {{ t("cloudModels.modelIdDesc") }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("cloudModels.apiModelId") }}</label>
            <input
              v-model="form.api_model_id"
              class="config-input mono"
              :placeholder="form.model_id"
            />
            <div class="config-comment">
              {{ t("cloudModels.apiModelIdDesc") }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("cloudModels.name") }} <span style="color:var(--error)">*</span></label>
            <input
              v-model="form.name"
              class="config-input"
            />
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("cloudModels.provider") }}</label>
            <input
              v-model="form.provider"
              class="config-input"
              list="cloud-model-providers"
              @change="onProviderInputChange"
            />
            <datalist id="cloud-model-providers">
              <option
                v-for="p in knownProviderIds"
                :key="p"
                :value="p"
              />
            </datalist>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("cloudModels.providerBaseUrl") }}</label>
            <input
              v-model="form.provider_base_url"
              class="config-input mono"
            />
            <div class="config-comment">
              {{ t("cloudModels.providerBaseUrlDesc") }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("cloudModels.providerApiKey") }}</label>
            <div class="config-input-wrap">
              <input
                v-model="form.provider_api_key"
                :type="showFormKey ? 'text' : 'password'"
                class="config-input mono"
                :placeholder="t('cloudModels.apiKeyKeepHint')"
              />
              <button
                type="button"
                class="config-eye-btn"
                @click="showFormKey = !showFormKey"
              >
                {{ showFormKey ? "🙈" : "👁" }}
              </button>
            </div>
            <div class="config-comment">
              {{ t("cloudModels.providerApiKeyDesc") }}
            </div>
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("cloudModels.contextLength") }}</label>
            <input
              v-model.number="form.context_length"
              type="number"
              min="1024"
              class="config-input config-number"
            />
          </div>
          <div class="config-field">
            <label class="config-label">{{ t("cloudModels.description") }}</label>
            <input
              v-model="form.description"
              class="config-input"
            />
          </div>
          <div class="config-field">
            <label
              class="config-label"
              style="flex-direction:row;align-items:center;gap:8px"
            >
              {{ t("cloudModels.supportsStreaming") }}
              <ToggleSwitch
                v-model="form.supports_streaming"
                :aria-label="t('cloudModels.supportsStreaming')"
                style="margin-left:auto"
              />
            </label>
          </div>

          <!-- Supported parameters: let the user declare which sampling
               params this model accepts, so an unsupported one is dropped
               from the wire instead of triggering an upstream 400. -->
          <div class="config-field cloud-model-params">
            <label class="config-label">{{ t("cloudModels.paramsTitle") }}</label>
            <div class="config-comment" style="margin-bottom:8px">
              {{ t("cloudModels.paramsDesc") }}
            </div>
            <label class="cloud-model-param-row">
              <span class="cloud-model-param-name">temperature</span>
              <ToggleSwitch
                v-model="form.param_temperature_supported"
                aria-label="temperature"
              />
            </label>
            <label class="cloud-model-param-row">
              <span class="cloud-model-param-name">top_p</span>
              <ToggleSwitch
                v-model="form.param_top_p_supported"
                aria-label="top_p"
              />
            </label>
            <label class="cloud-model-param-row">
              <span class="cloud-model-param-name">max_tokens</span>
              <ToggleSwitch
                v-model="form.param_max_tokens_supported"
                aria-label="max_tokens"
              />
            </label>
            <label class="cloud-model-param-row">
              <span class="cloud-model-param-name">
                thought_signature
                <span class="cloud-model-param-hint">{{ t("cloudModels.paramThoughtSigHint") }}</span>
              </span>
              <ToggleSwitch
                v-model="form.param_thought_signature_required"
                aria-label="thought_signature"
              />
            </label>
          </div>
        </div>
        <div class="cloud-model-side-panel-footer">
          <button
            type="button"
            class="btn btn-ghost"
            @click="closePanel"
          >
            {{ t("common.cancel") }}
          </button>
          <button
            type="button"
            class="btn btn-primary"
            :disabled="saving"
            @click="submitForm"
          >
            <span
              v-if="saving"
              class="spinner"
            ></span>
            <span v-else>💾</span>
            {{ t("cloudModels.save") }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cloud-models-empty-hint {
  margin-top: var(--space-1, 4px);
  font-size: var(--text-sm, 0.85rem);
  color: var(--text-secondary);
}

.cloud-model-params {
  border-top: 1px solid var(--border, rgba(255, 255, 255, 0.08));
  padding-top: var(--space-3, 12px);
  margin-top: var(--space-2, 8px);
}

.cloud-model-param-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2, 8px);
  padding: var(--space-1, 4px) 0;
  cursor: pointer;
}

.cloud-model-param-name {
  font-family: var(--font-mono);
  font-size: var(--text-sm, 0.85rem);
  color: var(--text-primary);
}

.cloud-model-param-hint {
  display: block;
  font-family: var(--font-sans);
  font-size: var(--text-xs, 0.75rem);
  color: var(--text-secondary);
  margin-top: 2px;
}
</style>
