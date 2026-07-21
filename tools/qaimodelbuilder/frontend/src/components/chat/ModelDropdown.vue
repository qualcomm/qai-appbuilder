<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModelDropdown — 380px popover for selecting the active chat model.
 *
 * V1 visual parity (T2.5):
 *   - Header "Select Model"
 *   - Search input (debounced filter on model name)
 *   - LOCAL group  → backend GET /api/service/models  (empty → offline placeholder card)
 *   - CLOUD group → backend GET /api/model-catalog/cloud-models (empty → "(no cloud models configured)")
 *
 * Dropdown opens above the trigger button; click outside (overlay) closes it.
 *
 * Reuses V1 CSS classes that were migrated to V2 in PR-802b/c:
 *   .model-dropdown / .model-dropdown-header / .model-search /
 *   .model-list / .model-group-label / .model-item / .model-item-info /
 *   .model-item-name / .model-item-meta / .model-item-icon /
 *   .model-item-check / .ctx-badge /
 *   .model-placeholder-card / .model-placeholder-title /
 *   .model-placeholder-desc / .model-placeholder-btn /
 *   .dropdown-overlay
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import { apiJson } from "@/api";
import { useCloudModelPermissionsStore } from "@/stores/cloudModelPermissions";
import { useModelSelector, type ModelInfo } from "@/composables/useModelSelector";

interface Props {
  /** id of the currently selected model (used for the ✓ marker) */
  selectedModelId: string | null;
  /**
   * V1 parity (index.html:1028, useModels.js:30) — selected model's
   * `provider` slug, used to disambiguate two cloud entries that share
   * the same `model_id` but live under different providers (e.g.
    * "provider_a" vs "cloud_llm"). When empty, the ✓ falls back to the
   * V1 "match by id only" behaviour (matches local models too).
   */
  selectedModelProvider?: string;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  /**
   * V1 parity — when the user picks a model, both `model_id` and
   * `provider` are emitted so the caller can persist them on the
   * active chat tab. `provider` is empty string for local models.
   */
  select: [modelId: string, provider: string];
  close: [];
}>();

// The ✓ marker requires BOTH the `model_id` AND the `provider` to match
// exactly. A model is only "the same model" when its provider and full
// model id both line up — otherwise two entries that merely share a
// `model_id` under different providers (e.g. AICEGROK vs other cloud providers
// "claude-4-6-sonnet") would both light up. We never fall back to an
// id-only match, so the selected provider must always be persisted
// alongside the id (see useComposerModelSelection.ts seed/backfill).
function isSelected(modelId: string, provider: string | undefined): boolean {
  if (props.selectedModelId !== modelId) return false;
  return (props.selectedModelProvider ?? "") === (provider ?? "");
}

const { t } = useI18n();
const router = useRouter();

// Per-``(provider, model_id)`` permission snapshot populated at app mount by
// App.vue. The dropdown reads it to HIDE (not grey out — product decision) any
// model the current cloud API key has been probed as `"denied"`. Fail-open:
// `"unknown"` and `"allowed"` both show, so a not-yet-populated / failed
// snapshot leaves the previous "show all" behaviour intact.
const permissionsStore = useCloudModelPermissionsStore();

const {
  models: localModels,
  loading: localLoading,
  fetchModels: fetchLocalModels,
  getModelLabel,
  getModelId,
} = useModelSelector();

// ─── Cloud models ──────────────────────────────────────────────

interface CloudEntry {
  model_id: string;
  name?: string;
  provider?: string;
  description?: string;
  // Optional ctx hint (e.g. "200K") if the backend ever populates it.
  context_length?: number;
  [key: string]: unknown;
}

interface CloudModelsResponse {
  models: CloudEntry[];
}

const cloudEntries = ref<CloudEntry[]>([]);
const cloudLoading = ref(false);

/**
 * Per-provider metadata (V1 `cloudProviders` lookup, index.html:991) —
 * we only need the `pinned` flag so the cloud group label can render
 * the 📌 marker before pinned providers (V1 parity). Sourced from the
 * dedicated `/api/model-catalog/providers` endpoint that already
 * exposes this on the V2 backend; failures fall back to an empty map
 * so the dropdown still renders without 📌 (degrades gracefully, no
 * error UI — V1 also silently omits 📌 when metadata is absent).
 */
interface ProviderMeta {
  pinned?: boolean;
  [key: string]: unknown;
}
interface ProvidersResponse {
  providers: Record<string, ProviderMeta>;
}
const cloudProviders = ref<Record<string, ProviderMeta>>({});

async function fetchCloudModels(): Promise<void> {
  cloudLoading.value = true;
  try {
    const res = await apiJson<CloudModelsResponse>(
      "GET",
      "/api/model-catalog/cloud-models",
    );
    cloudEntries.value = res.models ?? [];
  } catch {
    cloudEntries.value = [];
  } finally {
    cloudLoading.value = false;
  }
}

async function fetchCloudProviders(): Promise<void> {
  try {
    // Backend returns { providers: Array<{provider_id, config}> } (list form).
    // Convert to dict keyed by provider_id for easy lookup.
    const res = await apiJson<{ providers: Array<{ provider_id?: string; config?: Record<string, unknown> }> }>(
      "GET",
      "/api/model-catalog/providers",
    );
    const dict: Record<string, ProviderMeta> = {};
    for (const row of res.providers ?? []) {
      const id = row.provider_id ?? "";
      if (!id) continue;
      dict[id] = { pinned: (row.config?.pinned as boolean) ?? undefined };
    }
    cloudProviders.value = dict;
  } catch {
    cloudProviders.value = {};
  }
}

// ─── Query services (internal-only) ─────────────────────────────────────────
// A query service is selected like a model but routes via a `query::<id>`
// model hint. The backend `/api/model-catalog/query-services` endpoint is
// edition-gated: it returns the configured services on internal builds and an
// empty list on external — so on external editions the dropdown shows no
// "查询服务" group at all (no extra UI gating needed here). Rendered ABOVE the
// cloud models group (per product placement).

interface QueryServiceEntry {
  service_id: string;
  display_name: string;
  model_id: string; // the `query::<id>` hint selected by clicking the row
}

interface QueryServicesResponse {
  services: QueryServiceEntry[];
}

const queryServices = ref<QueryServiceEntry[]>([]);

async function fetchQueryServices(): Promise<void> {
  try {
    const res = await apiJson<QueryServicesResponse>(
      "GET",
      "/api/model-catalog/query-services",
    );
    queryServices.value = res.services ?? [];
  } catch {
    queryServices.value = [];
  }
}

// ─── Local model run state (V1 index.html:1022-1024) ─────────────────────────
// V1 shows a per-local-model "● Running" / "○ Stopped" indicator derived from
// `m.is_running`. The V2 `/api/service/models` payload lists models on disk
// only (name/path/size/...), so the run state is derived here from the live
// service status (`GET /api/service/status` → `{ running, model }`): a local
// model is "running" when the daemon is running AND its currently-loaded
// `model` matches the model's id/name. No backend change is needed — the
// status endpoint already exposes the loaded model, and deriving the badge in
// a small computed keeps this component the single owner of the LOCAL-group
// presentation (vs. V1's app.js global ref soup).
interface ServiceStatus {
  running?: boolean;
  model?: string | null;
  [key: string]: unknown;
}

const serviceRunning = ref(false);
const loadedModel = ref<string>("");

async function fetchServiceStatus(): Promise<void> {
  try {
    const res = await apiJson<ServiceStatus>("GET", "/api/service/status");
    serviceRunning.value = res.running === true;
    loadedModel.value = (res.model ?? "") || "";
  } catch {
    serviceRunning.value = false;
    loadedModel.value = "";
  }
}

/**
 * Whether a given local model is the one currently loaded/running in the
 * daemon (V1 `m.is_running`). Matches against both the model's id and its
 * display label since the daemon reports the loaded model by name.
 */
function isLocalRunning(model: ModelInfo): boolean {
  // Prefer the backend-authoritative ``is_running`` (V1 ``/api/models`` per
  // entry, now tail-appended to ``/api/service/models``) when present.
  if (typeof model.is_running === "boolean") return model.is_running;
  // Fallback for older payloads: derive from the live service status.
  if (!serviceRunning.value || loadedModel.value === "") return false;
  const loaded = loadedModel.value;
  return loaded === getModelId(model) || loaded === getModelLabel(model);
}

// ─── Search / filter ─────────────────────────────────────────────────────────

const searchTerm = ref("");

function matchesText(haystack: string | undefined): boolean {
  const q = searchTerm.value.trim().toLowerCase();
  if (q === "") return true;
  return (haystack ?? "").toLowerCase().includes(q);
}

// V1 parity (useModels.js:75-78): search name + provider + model_id.
function matchesCloud(entry: CloudEntry): boolean {
  const q = searchTerm.value.trim().toLowerCase();
  if (q === "") return true;
  return (
    (entry.name ?? "").toLowerCase().includes(q) ||
    (entry.model_id ?? "").toLowerCase().includes(q) ||
    (entry.provider ?? "").toLowerCase().includes(q)
  );
}

const filteredLocal = computed<ModelInfo[]>(() =>
  localModels.value.filter((m) => matchesText(getModelLabel(m))),
);

/**
 * Permission gate for a cloud entry.
 *
 * Hides ``"denied"`` models entirely (product decision — no grey/lock UI).
 * Exception: the CURRENTLY SELECTED model is always kept in the list, even
 * when denied, so the user does not lose visual anchoring on what they had
 * selected (e.g. after a probe scan just flipped it to denied). Without this,
 * the dropdown would silently drop the selected row and the user would see
 * their model "vanish" with no explanation. When denied AND selected we
 * keep the row in place; the surrounding chat error card (see
 * chatErrorActions.ts `permission_denied` spec) is where the user learns
 * why turns to this model fail and picks a replacement.
 */
function isCloudEntryVisible(entry: CloudEntry): boolean {
  const status = permissionsStore.getStatus(
    entry.provider,
    entry.model_id,
  );
  if (status !== "denied") return true;
  // Denied — keep only if this is the model the user currently has picked.
  return (
    props.selectedModelId === entry.model_id &&
    (props.selectedModelProvider ?? "") === (entry.provider ?? "")
  );
}

const filteredCloud = computed<CloudEntry[]>(() =>
  cloudEntries.value.filter(
    (entry) => isCloudEntryVisible(entry) && matchesCloud(entry),
  ),
);

function matchesQueryService(entry: QueryServiceEntry): boolean {
  const q = searchTerm.value.trim().toLowerCase();
  if (q === "") return true;
  return (
    entry.display_name.toLowerCase().includes(q) ||
    entry.service_id.toLowerCase().includes(q)
  );
}

const filteredQueryServices = computed<QueryServiceEntry[]>(() =>
  queryServices.value.filter(matchesQueryService),
);

/**
 * Sub-group the cloud entries by their `provider` field so the V1
 * "CLOUD" group renders one `.model-group-label` per provider
 * (matches V1 `useModels.js:groupedModels`). Insertion order of the
 * Map is preserved, so providers appear in first-seen order from the
 * backend list.
 *
 * Key is the **raw** `provider` id (lowercase, as the backend
 * surfaces it) so the `cloudProviders[providerKey]?.pinned` lookup
 * in the template hits the providers-metadata map directly. The
 * group label uppercases the key for display only (V1 parity:
 * `groupedModels` keys are the raw provider names, the label
 * styling does the visual uppercase via CSS / template).
 */
const filteredCloudByProvider = computed<Map<string, CloudEntry[]>>(() => {
  const groups = new Map<string, CloudEntry[]>();
  for (const entry of filteredCloud.value) {
    const key = (entry.provider ?? "").trim() === ""
      ? "cloud"
      : (entry.provider as string);
    const existing = groups.get(key);
    if (existing === undefined) {
      groups.set(key, [entry]);
    } else {
      existing.push(entry);
    }
  }
  return groups;
});

// ─── ctx_length formatter (matches V1 `formatCtx`) ───────────────────────────

function formatCtx(n: number | undefined): string {
  if (typeof n !== "number" || !Number.isFinite(n) || n <= 0) return "";
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return `${n}`;
}

// Provider → emoji icon (V1 parity: `useModels.js:providerIcon`). Unknown
// providers (e.g. cloud model services / custom providers) fall back to the 🤖 robot, which
// is what V1 shows for cloud models under a cloud LLM service.
function providerIcon(provider: string | undefined): string {
  const icons: Record<string, string> = {
    OpenAI: "\u{1F7E2}",
    Anthropic: "\u{1F7E3}",
    Google: "\u{1F535}",
    Alibaba: "\u{1F7E0}",
    Local: "\u{1F5A5}\uFE0F",
    "Cloud (configured)": "\u2601\uFE0F",
  };
  return icons[provider ?? ""] ?? "\u{1F916}";
}

// ─── Actions ─────────────────────────────────────────────────────────────────

function close(): void {
  emit("close");
}

function pickLocal(model: ModelInfo): void {
  // Local models have no upstream provider concept — emit empty string,
  // matching V1 `selectedModelProvider = ''` for local picks.
  emit("select", getModelId(model), "");
  close();
}

function pickCloud(entry: CloudEntry): void {
  // Emit provider alongside model_id so the caller can disambiguate
  // identical model_ids living under different providers (V1 parity:
  // useModels.js:264 stores both selectedModelId + selectedModelProvider).
  emit("select", entry.model_id, entry.provider ?? "");
  close();
}

function pickQueryService(entry: QueryServiceEntry): void {
  // A query service is selected via its `query::<id>` model hint; the routing
  // layer (ProviderRoutingLLMStream) dispatches `query::*` to the internal
  // query-service transport. No provider slug (empty, like local picks).
  emit("select", entry.model_id, "");
  close();
}

function gotoService(): void {
  close();
  void router.push("/service");
}

// ─── Escape key closes dropdown (V1 parity) ─────────────────────────────────

function onKeydown(e: KeyboardEvent): void {
  if (e.key === "Escape") {
    e.stopPropagation();
    emit("close");
  }
}

onMounted(() => {
  document.addEventListener("keydown", onKeydown);
  void fetchLocalModels();
  void fetchCloudModels();
  void fetchCloudProviders();
  void fetchQueryServices();
  void fetchServiceStatus();
});

onBeforeUnmount(() => {
  document.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <!-- Overlay catches outside clicks (z-index sits between page and the
       popover so the dropdown itself stays interactive). -->
  <div
    class="dropdown-overlay"
    @click="close"
  ></div>

  <div
    class="model-dropdown"
    role="listbox"
    :aria-label="t('chat.modelDropdownHeader')"
    @click.stop
  >
    <div class="model-dropdown-header">
      {{ t('chat.modelDropdownHeader') }}
    </div>
    <div class="model-search">
      <input
        v-model="searchTerm"
        type="text"
        :placeholder="t('chat.modelDropdownSearchPlaceholder')"
        autofocus
      />
    </div>

    <div class="model-list">
      <!-- ── LOCAL group ─────────────────────────────────────────────── -->
      <div class="model-group-label">
        {{ t('chat.modelDropdownGroupLocal') }}
      </div>

      <div
        v-if="localLoading"
        class="chat-model-dropdown-loading"
      >
        {{ t('chat.modelDropdownLoading') }}
      </div>

      <!-- Empty / offline → V1-style placeholder card -->
      <div
        v-else-if="filteredLocal.length === 0"
        class="model-item placeholder"
      >
        <div class="model-placeholder-card">
          <div class="model-placeholder-title">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 1.5L1 14h14L8 1.5z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M8 6v4M8 11.5v.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
            <span>{{ t('chat.modelDropdownLocalOfflineTitle') }}</span>
          </div>
          <div class="model-placeholder-desc">
            {{ t('chat.modelDropdownLocalOfflineDesc') }}
          </div>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            data-testid="model-dropdown-goto-service"
            @click="gotoService"
          >
            {{ t('chat.modelDropdownGotoServiceBtn') }}
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3l4 4-4 4"/></svg>
          </button>
        </div>
      </div>

      <!-- Real local models -->
      <div
        v-for="m in filteredLocal"
        v-else
        :key="getModelId(m)"
        class="model-item"
        :class="{ selected: isSelected(getModelId(m), '') }"
        role="option"
        :aria-selected="isSelected(getModelId(m), '')"
        @click="pickLocal(m)"
      >
        <span
          class="model-item-icon"
          aria-hidden="true"
        >🖥️</span>
        <div class="model-item-info">
          <div class="model-item-name">
            {{ getModelLabel(m) }}
          </div>
          <div class="model-item-meta">
            <!-- V1 parity (index.html:1019) — coloured Local badge:
                 `.badge.badge-local` (green) lives in the global
                 components.css; here we only compose the classes. -->
            <span class="badge badge-local">Local</span>
            <!-- Per-model run state (V1 index.html:1022-1024). Green dot +
                 "Running" when this local model is the one currently loaded
                 in the daemon; muted "Stopped" otherwise. -->
            <span
              class="model-item-status"
              :class="{ 'model-item-status--running': isLocalRunning(m) }"
            >{{ isLocalRunning(m) ? '● Running' : '○ Stopped' }}</span>
            <!-- V1 parity (index.html:1025) — `formatCtx(m.context_length)`
                 returns '' when the field is missing, so the v-if guard
                 keeps the badge hidden until the backend `/api/service/
                 models` payload starts including `context_length`. The
                 markup is in place so no further frontend change is
                 needed once the field lands. -->
            <span
              v-if="formatCtx(m.context_length) !== ''"
              class="ctx-badge"
            >{{ formatCtx(m.context_length) }}</span>
          </div>
        </div>
        <span
          v-if="isSelected(getModelId(m), '')"
          class="model-item-check"
        >✓</span>
      </div>

      <!-- ── 查询服务 / Query Services group (internal-only) ──────────
           Rendered ABOVE the cloud models group (product placement). The
           backend `/api/model-catalog/query-services` endpoint returns an
           empty list on external editions, so this whole group is absent
           there (no extra gating needed). A query service is selected via its
           `query::<id>` model hint; the routing layer dispatches it to the
           internal query-service transport. -->
      <template v-if="filteredQueryServices.length > 0">
        <div class="model-group-label">
          {{ t('chat.modelDropdownGroupQueryServices') }}
        </div>
        <div
          v-for="svc in filteredQueryServices"
          :key="svc.model_id"
          class="model-item"
          :class="{ selected: isSelected(svc.model_id, '') }"
          role="option"
          :aria-selected="isSelected(svc.model_id, '')"
          @click="pickQueryService(svc)"
        >
          <span
            class="model-item-icon"
            aria-hidden="true"
          >🔍</span>
          <div class="model-item-info">
            <div class="model-item-name">
              {{ svc.display_name }}
            </div>
            <div class="model-item-meta">
              <span class="badge badge-cloud">{{ t('chat.modelDropdownQueryServiceBadge') }}</span>
            </div>
          </div>
          <span
            v-if="isSelected(svc.model_id, '')"
            class="model-item-check"
          >✓</span>
        </div>
      </template>

      <!-- ── Cloud groups ──────────────────────────── -->
      <div
        v-if="cloudLoading"
        class="chat-model-dropdown-loading"
      >
        {{ t('chat.modelDropdownLoading') }}
      </div>

      <template v-else-if="filteredCloud.length === 0">
        <div class="model-group-label">
          {{ t('chat.modelDropdownGroupCloud') }}
        </div>
        <div class="chat-model-dropdown-empty">
          {{ t('chat.modelDropdownCloudEmpty') }}
        </div>
      </template>

      <!-- One `.model-group-label` per cloud provider (V1 parity:
           `groupedModels` in useModels.js). When entries lack a
           `provider` field they are grouped under the default provider key.
           V1 inserts a `.divider` between adjacent provider groups
           (index.html:988) and prefixes pinned providers with 📌
           (index.html:991, lookup `cloudProviders[provider].pinned`). -->
      <template
        v-for="([providerKey, entries], idx) in [...filteredCloudByProvider.entries()]"
        v-else
        :key="providerKey"
      >
        <!-- V1 parity (index.html:988) — visual separator between
             adjacent provider groups; suppressed before the first
             group. The global `.divider` rule lives in
             components.css. -->
        <div
          v-if="idx > 0"
          class="divider"
          aria-hidden="true"
        ></div>
        <div class="model-group-label">
          <!-- V1 parity (index.html:991) — pinned providers get a 📌
               prefix. `cloudProviders` comes from
               /api/model-catalog/providers; absence of metadata or a
               falsy `pinned` simply skips the marker (graceful no-op). -->
          <span
            v-if="cloudProviders[providerKey]?.pinned === true"
            class="cloud-pin"
            aria-hidden="true"
          >📌</span>{{ providerKey.toUpperCase() }}
        </div>
        <div
          v-for="entry in entries"
          :key="`${entry.provider ?? ''}::${entry.model_id}`"
          class="model-item"
          :class="{ selected: isSelected(entry.model_id, entry.provider) }"
          role="option"
          :aria-selected="isSelected(entry.model_id, entry.provider)"
          @click="pickCloud(entry)"
        >
          <span
            class="model-item-icon"
            aria-hidden="true"
          >{{ providerIcon(entry.provider) }}</span>
          <div class="model-item-info">
            <div class="model-item-name">
              {{ entry.name ?? entry.model_id }}
            </div>
            <div class="model-item-meta">
              <!-- V1 parity (index.html:1019) — coloured Cloud badge:
                   `.badge.badge-cloud` (blue) is defined in the global
                   components.css; here we only compose the classes. -->
              <span class="badge badge-cloud">Cloud</span>
              <span
                v-if="formatCtx(entry.context_length) !== ''"
                class="ctx-badge"
              >{{ formatCtx(entry.context_length) }}</span>
            </div>
          </div>
          <span
            v-if="isSelected(entry.model_id, entry.provider)"
            class="model-item-check"
          >✓</span>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
/*
 * Layout-only deltas on top of the global .model-dropdown rules in
 * frontend/src/styles/components/components.css (which already defines
 * the 380px width, glass background, header / search / list / item /
 * placeholder visuals migrated from V1). Anything below is a small
 * helper for the loading / empty rows that V1 rendered ad-hoc.
 */
.chat-model-dropdown-loading,
.chat-model-dropdown-empty {
  padding: var(--space-3) var(--space-4);
  font-size: var(--text-xs);
  color: var(--text-muted);
  text-align: center;
}

/*
 * Per-local-model run-state indicator (V1 index.html:1022-1024 inline style
 * → token-driven class here, no hardcoded colours). Muted by default;
 * `--running` switches to the global success colour.
 */
.model-item-status {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.model-item-status--running {
  color: var(--success);
}

/*
 * Pinned-provider marker (V1 index.html:991 inline `style="margin-right:3px"`
 * → token-driven class here). Pure layout: emoji renders at the parent's
 * text colour, no hardcoded values.
 */
.cloud-pin {
  margin-right: var(--space-1);
}
</style>
