// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useChannelSettings` — per-instance channel settings (V1 parity).
 *
 * Wraps the 6 per-channel settings endpoints that already exist in
 * `interfaces/http/routes/channels.py` (`_register_kind`, PR-202):
 *
 *   GET  /api/{kind}/config?instance_id=…  → { auto_start, kind_specific }
 *   POST /api/{kind}/config   body { instance_id, auto_start, kind_specific }
 *   GET  /api/{kind}/proxy?instance_id=…   → { url, username, has_password }
 *   POST /api/{kind}/proxy    body { instance_id, url, username, password }
 *   GET  /api/{kind}/model?instance_id=…   → { model_id, model_provider }
 *   POST /api/{kind}/model    body { instance_id, model_id, model_provider }
 *
 * Backend semantics (verified against channels.py + manage_settings.py):
 *   - All three GETs load the aggregate via `instances.get(instance_id)`
 *     and 404 (NotFoundError) when the instance was never registered;
 *     callers degrade gracefully (defaults stay) so an unregistered
 *     Feishu/WeChat instance does not break the panel.
 *   - `config.kind_specific` is a flat `dict[str, str]` of NON-secret
 *     fields, *merged* server-side (absent keys preserved). Plaintext
 *     secrets (app_secret) must NEVER be written here (AGENTS.md §3.3).
 *   - `proxy.password`: empty string = "preserve existing SecretStore
 *     record" (NOT clear). A non-empty value rotates it. The GET only
 *     exposes `has_password`; we surface a `****` mask in the UI and
 *     skip sending it back unchanged.
 *   - `model.{model_id,model_provider}` empty = follow global default.
 *
 * Model candidates come from BOTH the on-device service
 * (`GET /api/service/models`) and the model_catalog cloud list
 * (`GET /api/model-catalog/cloud-models`), merged into a single
 * provider-grouped dropdown — v0.5 parity (`models_registry.list_models()`
 * merges local + cloud; the channel dropdown shows both, see
 * `backend/channels/wechat/channel.py:1116`). Cloud-only would leave the
 * dropdown empty when no cloud provider is configured (the V2 regression).
 */
import { computed, ref, type Ref } from "vue";

import { apiJson, ApiError } from "@/api";
import { fetchCloudModels, fetchCloudProviders } from "@/api/cloudModels";
import type { CloudModelEntry } from "@/types/cloudModels";

// ─── Wire types (mirror channels.py response shapes) ──────────────────────────

/** On-device model entry shape from `GET /api/service/models`. */
interface LocalModelInfo {
  id?: string;
  model_id?: string;
  name?: string;
  [key: string]: unknown;
}

interface LocalModelsResponse {
  models: LocalModelInfo[];
}

interface ChannelConfigResponse {
  auto_start: boolean;
  kind_specific: Record<string, string>;
}

interface ChannelProxyResponse {
  url: string;
  username: string;
  has_password: boolean;
}

interface ChannelModelResponse {
  model_id: string;
  model_provider: string;
}

/** Mask shown for a set-but-hidden proxy password. */
export const CHANNEL_PROXY_PASSWORD_MASK = "****";

export type ChannelKind = "feishu" | "wechat";

/** A model candidate grouped under its provider. */
export interface ModelGroup {
  provider: string;
  models: CloudModelEntry[];
  /** Whether this provider is "pinned" in the catalog (📌 marker, V1 parity). */
  pinned: boolean;
  /** Sentinel group rendered as a divider between local and cloud groups. */
  isSeparator?: boolean;
}

// ─── Composable ───────────────────────────────────────────────────────────────

export function useChannelSettings(
  kind: ChannelKind,
  instanceId: string,
  /**
   * Optional resolver that registers / returns the single-instance ULID on
   * demand. v0.5 parity: the channel is single-instance and the user may pick
   * a model BEFORE connecting (`getInstanceId()` is null until then). When
   * provided, `saveModel` resolves (and registers if needed) the instance id
   * so the selection persists instead of failing with "保存通道模型失败".
   */
  resolveInstanceId?: () => Promise<string | null>,
) {
  // The effective instance id: starts from the prop and is upgraded once the
  // resolver registers a real ULID (so subsequent reads/writes address it).
  let effectiveInstanceId = instanceId;

  /** Resolve the instance id, registering via the resolver if still empty. */
  async function ensureInstanceId(): Promise<string | null> {
    if (effectiveInstanceId) return effectiveInstanceId;
    if (resolveInstanceId) {
      const resolved = await resolveInstanceId();
      if (resolved) effectiveInstanceId = resolved;
      return resolved;
    }
    return effectiveInstanceId || null;
  }

  // ── config (auto_start + kind_specific) ──────────────────────────────────
  const autoStart = ref(false);
  const kindSpecific = ref<Record<string, string>>({});

  // ── proxy ────────────────────────────────────────────────────────────────
  const proxyUrl = ref("");
  const proxyUsername = ref("");
  const proxyPassword = ref("");
  const proxyHasPassword = ref(false);
  const proxyShowPassword = ref(false);

  // ── model ──────────────────────────────────────────────────────────────────
  const modelId = ref("");
  const modelProvider = ref("");

  // ── model candidates ───────────────────────────────────────────────────────
  const allModels: Ref<CloudModelEntry[]> = ref([]);
  /** Provider name → pinned flag (from the catalog provider registry). */
  const pinnedProviders: Ref<Set<string>> = ref(new Set());
  const modelSearch = ref("");

  // ── shared flags ───────────────────────────────────────────────────────────
  const loading = ref(false);
  const saving = ref(false);
  const error = ref<string | null>(null);

  function setError(e: unknown): void {
    error.value = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
  }

  /**
   * Filtered + provider-grouped candidate list for the dropdown.
   *
   * Ordering mirrors V1 `wechatGroupedModels` / `feishuGroupedModels`:
   *   1. providers that have at least one LOCAL model (top),
   *   2. a `__separator__` divider when both local + cloud groups exist,
   *   3. PINNED cloud providers (📌),
   *   4. the remaining cloud providers.
   * Insertion order within each bucket follows first-seen order (stable).
   */
  const modelGroups = computed<ModelGroup[]>(() => {
    const q = modelSearch.value.trim().toLowerCase();
    const matched = q
      ? allModels.value.filter(
          (m) =>
            m.model_id.toLowerCase().includes(q) ||
            m.name.toLowerCase().includes(q) ||
            m.provider.toLowerCase().includes(q),
        )
      : allModels.value;

    const byProvider = new Map<string, CloudModelEntry[]>();
    for (const m of matched) {
      const list = byProvider.get(m.provider) ?? [];
      list.push(m);
      byProvider.set(m.provider, list);
    }

    const local: string[] = [];
    const pinned: string[] = [];
    const rest: string[] = [];
    for (const provider of byProvider.keys()) {
      const models = byProvider.get(provider) ?? [];
      if (models.some((m) => m.is_local)) local.push(provider);
      else if (pinnedProviders.value.has(provider)) pinned.push(provider);
      else rest.push(provider);
    }

    const groups: ModelGroup[] = [];
    for (const provider of local) {
      groups.push({ provider, models: byProvider.get(provider) ?? [], pinned: false });
    }
    if (local.length > 0 && (pinned.length > 0 || rest.length > 0)) {
      groups.push({ provider: "__separator__", models: [], pinned: false, isSeparator: true });
    }
    for (const provider of pinned) {
      groups.push({ provider, models: byProvider.get(provider) ?? [], pinned: true });
    }
    for (const provider of rest) {
      groups.push({ provider, models: byProvider.get(provider) ?? [], pinned: false });
    }
    return groups;
  });

  /** Display label for the currently selected model (or "follow global"). */
  const selectedModelLabel = computed<string>(() => {
    if (!modelId.value) return "";
    const found = allModels.value.find(
      (m) => m.model_id === modelId.value && (!modelProvider.value || m.provider === modelProvider.value),
    );
    return found ? found.name : modelId.value;
  });

  function isSelected(entry: CloudModelEntry): boolean {
    // A model is only "the same" when BOTH its full model_id AND its
    // provider match — never fall back to an id-only match, otherwise two
    // entries sharing a model_id under different providers would both light
    // up. Local models carry an empty provider, matched by (entry.provider
    // ?? "") === "" when no provider is persisted.
    return (
      entry.model_id === modelId.value &&
      (entry.provider ?? "") === (modelProvider.value ?? "")
    );
  }

  // ── loaders ────────────────────────────────────────────────────────────────

  /**
   * Load the dropdown candidates: on-device (local) models + cloud models,
   * merged into one list. v0.5 parity — `models_registry.list_models()`
   * returns both, and the channel dropdown groups local providers above
   * cloud providers (see `modelGroups`). Fetching cloud-only (the prior V2
   * behaviour) left the dropdown empty whenever no cloud provider was set.
   *
   * @param localGroupLabel Provider label shown for the on-device group
   *   (the component passes the localized "本地模型 / Local Models" string).
   */
  async function loadModelCandidates(localGroupLabel = "local"): Promise<void> {
    try {
      const [local, cloud, providers] = await Promise.all([
        // On-device models. Best-effort: the daemon may be down; an empty
        // local group must not block the cloud list.
        apiJson<LocalModelsResponse>("GET", "/api/service/models").catch(
          () => ({ models: [] as LocalModelInfo[] }),
        ),
        fetchCloudModels(),
        // Pinned flags are best-effort: if the providers call fails we still
        // render the list, just without the 📌 ordering hint.
        fetchCloudProviders().catch(() => ({ providers: {} })),
      ]);

      // Map on-device entries to the CloudModelEntry shape. `/api/service/models`
      // returns just a `name` (no id/provider), so synthesise the `local::`
      // prefixed id (V1 convention, matches useModelSelector.getModelId) and
      // tag them `is_local` so `modelGroups` floats them to the top group.
      const localEntries: CloudModelEntry[] = (local.models ?? []).map((m) => {
        const raw = m.id ?? m.model_id ?? m.name ?? "unknown";
        const id = raw.includes("::") ? raw : `local::${raw}`;
        return {
          model_id: id,
          name: m.name ?? raw,
          provider: localGroupLabel,
          is_local: true,
        };
      });

      allModels.value = [...localEntries, ...cloud.models];
      const pinned = new Set<string>();
      for (const [name, meta] of Object.entries(providers.providers ?? {})) {
        if (meta?.pinned) pinned.add(name);
      }
      pinnedProviders.value = pinned;
    } catch (e) {
      // Non-fatal: the dropdown simply shows no candidates.
      setError(e);
    }
  }

  async function loadConfig(): Promise<void> {
    // Guard: skip the GET when instance_id is not yet resolved (empty string
    // means the channel has never been registered; the panel degrades to
    // defaults, matching V1 behaviour for a fresh install).
    if (!instanceId) return;
    try {
      const res = await apiJson<ChannelConfigResponse>("GET", `/api/${kind}/config`, undefined, {
        query: { instance_id: instanceId },
      });
      autoStart.value = !!res.auto_start;
      kindSpecific.value = { ...(res.kind_specific ?? {}) };
    } catch (e) {
      // Unregistered instance → keep defaults (graceful, V1 parity).
      if (!(e instanceof ApiError)) setError(e);
    }
  }

  async function loadProxy(): Promise<void> {
    // Guard: skip when instance_id is not yet resolved.
    if (!instanceId) return;
    try {
      const res = await apiJson<ChannelProxyResponse>("GET", `/api/${kind}/proxy`, undefined, {
        query: { instance_id: instanceId },
      });
      proxyUrl.value = res.url ?? "";
      proxyUsername.value = res.username ?? "";
      proxyHasPassword.value = !!res.has_password;
      // Show a mask so the user knows a password is set without leaking it.
      proxyPassword.value = res.has_password ? CHANNEL_PROXY_PASSWORD_MASK : "";
    } catch (e) {
      if (!(e instanceof ApiError)) setError(e);
    }
  }

  async function loadModel(): Promise<void> {
    // Guard: skip when instance_id is not yet resolved.
    if (!instanceId) return;
    try {
      const res = await apiJson<ChannelModelResponse>("GET", `/api/${kind}/model`, undefined, {
        query: { instance_id: instanceId },
      });
      modelId.value = res.model_id ?? "";
      modelProvider.value = res.model_provider ?? "";
    } catch (e) {
      if (!(e instanceof ApiError)) setError(e);
    }
  }

  async function loadAll(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      await Promise.all([loadModelCandidates(), loadConfig(), loadProxy(), loadModel()]);
    } finally {
      loading.value = false;
    }
  }

  // ── savers ───────────────────────────────────────────────────────────────

  /**
   * Save config (auto_start + merged kind_specific).
   *
   * `extraKindSpecific` lets callers patch non-secret provider fields
   * (e.g. Feishu app_id/encrypt_key) atomically with auto_start. Keys
   * are merged server-side; absent keys are preserved.
   */
  async function saveConfig(extraKindSpecific: Record<string, string> = {}): Promise<boolean> {
    // Guard: cannot save without a resolved instance_id.
    if (!instanceId) return false;
    saving.value = true;
    error.value = null;
    try {
      const merged = { ...kindSpecific.value, ...extraKindSpecific };
      await apiJson("POST", `/api/${kind}/config`, {
        instance_id: instanceId,
        auto_start: autoStart.value,
        kind_specific: merged,
      });
      kindSpecific.value = merged;
      return true;
    } catch (e) {
      setError(e);
      return false;
    } finally {
      saving.value = false;
    }
  }

  async function saveProxy(): Promise<boolean> {
    // Guard: cannot save without a resolved instance_id.
    if (!instanceId) return false;
    saving.value = true;
    error.value = null;
    try {
      // Unchanged mask → send empty so the backend preserves the
      // existing SecretStore record (channels.py "blank = preserve").
      const pwd =
        proxyPassword.value === CHANNEL_PROXY_PASSWORD_MASK ? "" : proxyPassword.value;
      await apiJson("POST", `/api/${kind}/proxy`, {
        instance_id: instanceId,
        url: proxyUrl.value.trim(),
        username: proxyUsername.value.trim(),
        password: pwd,
      });
      await loadProxy();
      return true;
    } catch (e) {
      setError(e);
      return false;
    } finally {
      saving.value = false;
    }
  }

  async function saveModel(entry: CloudModelEntry | null): Promise<boolean> {
    // Resolve (and register, single-instance) the instance id on demand so a
    // model can be picked before connecting — v0.5 parity (the channel is
    // single-instance; picking a model registered it transparently).
    const id = await ensureInstanceId();
    if (!id) return false;
    saving.value = true;
    error.value = null;
    try {
      const next = entry
        ? { model_id: entry.model_id, model_provider: entry.provider }
        : { model_id: "", model_provider: "" };
      await apiJson("POST", `/api/${kind}/model`, {
        instance_id: id,
        ...next,
      });
      modelId.value = next.model_id;
      modelProvider.value = next.model_provider;
      return true;
    } catch (e) {
      setError(e);
      return false;
    } finally {
      saving.value = false;
    }
  }

  /** Copy the global network proxy into this channel's proxy fields. */
  function syncGlobalProxy(global: {
    proxy_url: string;
    proxy_username: string;
    proxy_password: string;
  }): void {
    proxyUrl.value = global.proxy_url ?? "";
    proxyUsername.value = global.proxy_username ?? "";
    // Global GET masks the password as "****"; if the global proxy has a
    // real password set, re-typing is required to rotate the channel one.
    proxyPassword.value =
      global.proxy_password && global.proxy_password !== CHANNEL_PROXY_PASSWORD_MASK
        ? global.proxy_password
        : "";
  }

  return {
    // config
    autoStart,
    kindSpecific,
    // proxy
    proxyUrl,
    proxyUsername,
    proxyPassword,
    proxyHasPassword,
    proxyShowPassword,
    syncGlobalProxy,
    // model
    modelId,
    modelProvider,
    allModels,
    modelSearch,
    modelGroups,
    selectedModelLabel,
    isSelected,
    // flags
    loading,
    saving,
    error,
    // actions
    loadAll,
    loadConfig,
    loadProxy,
    loadModel,
    loadModelCandidates,
    saveConfig,
    saveProxy,
    saveModel,
  };
}
