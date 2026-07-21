// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useCloudModelStatus` — detect whether any cloud model is configured, and
 * whether a configured cloud provider is still missing its API key.
 *
 * V2 enhancement (edition-dual-form-design.md §6.4 — "无云端模型空状态引导"):
 * the external edition ships with NO cloud provider, and an internal user
 * may also simply not have configured one. When the initial chat surface
 * detects that there is no usable cloud model, it surfaces a (non-blocking)
 * onboarding hint guiding the user to Settings → Cloud Models.
 *
 * Internal-edition enhancement: the internal edition ships the "qgenie"
 * cloud provider PRE-CONFIGURED with a model list but NO API key. In that
 * case cloud models DO exist, but they are unusable until the user sets a
 * key. We surface a second (also non-blocking) prompt — `showApiKeyPrompt`
 * — that opens an in-place "set API key" dialog rather than routing to
 * Settings. Detection is edition-AGNOSTIC: it only asks "does a provider
 * that has models still report `has_api_key === false`?" (presence-only
 * boolean from `GET /api/model-catalog/providers`, never the secret).
 *
 * This is a purely-frontend concern: it does NOT read any edition flag — it
 * only reuses the SAME data sources the rest of the app already uses for the
 * model catalog (`GET /api/model-catalog/cloud-models` via `fetchCloudModels`
 * and `GET /api/model-catalog/providers` via `fetchCloudProviders`). No
 * new/duplicate data source is introduced (AGENTS.md 细则 2：复用 > 重造).
 *
 * The result is cached on a module-level singleton so multiple consumers
 * (e.g. the chat welcome screen) share one fetch + one reactive state, and
 * a successful Cloud Models config change can `refresh()` it so the hint(s)
 * disappear without a full reload.
 */
import { ref, computed, type Ref, type ComputedRef } from "vue";
import { fetchCloudModels, fetchCloudProviders } from "@/api/cloudModels";
import type { CloudProviderMeta } from "@/types/cloudModels";
import { useServiceStore } from "@/stores/service";

// ─── Module-level singleton state (shared across all callers) ───────────────

const hasCloudModels = ref(false);
/** True once a fetch has completed at least once (success or failure). */
const checked = ref(false);
const loading = ref(false);
let inflight: Promise<void> | null = null;

/**
 * The first provider that has models but is missing its API key, or null.
 * `id` is the provider_id used for the PUT; `config` is the provider's full
 * existing config document (base_url / models / pinned) so the save can PUT
 * it back intact (the backend replaces the whole config on write).
 */
const providerNeedingKeyId = ref<string | null>(null);
const providerNeedingKeyConfigRef = ref<Record<string, unknown> | null>(null);

/**
 * ALL provider ids that have models but are missing their API key. The
 * pre-send interception (`useChatTurnSubmit`) needs to check a SPECIFIC
 * provider (the target tab's model provider), not just the first one that
 * `providerNeedingKeyId` tracks — so we keep the full set as well. Also
 * caches each such provider's config so a per-provider open can seed the
 * dialog with the right provider (see `openApiKeyFlowForProvider`).
 */
const providersMissingKeyIds = ref<Set<string>>(new Set());
const providerConfigById = ref<Record<string, Record<string, unknown>>>({});

/**
 * Visibility of the shared in-place "set API key" dialog. Lifted onto the
 * module singleton (rather than a component-local ref) so ALL three entry
 * points — the composer pre-send interception (`useChatTurnSubmit`), the
 * chat error bubble, and the welcome-screen prompt card — open the SAME
 * dialog instance hosted in `ChatMessageList.vue`.
 */
const dialogVisible = ref(false);

/**
 * Programmatic "navigate to Settings → Cloud Models" callback, registered
 * once by `App.vue` (which owns the router). `openApiKeyFlow()` runs outside
 * a component setup (e.g. from `useChatTurnSubmit`), so it cannot call
 * `useRouter()`; it defers navigation to this registered callback instead.
 */
let cloudModelSettingsNavigator: (() => void) | null = null;

/** Register the router-backed "go to Cloud Model Settings" navigation. */
export function registerCloudModelSettingsNavigator(fn: () => void): void {
  cloudModelSettingsNavigator = fn;
}

/**
 * Fetch the cloud-model catalog + provider registry and derive
 * `hasCloudModels` and the "needs API key" state. Failures are non-fatal and
 * treated as "no cloud models / nothing needs a key" — the hints are
 * advisory, never blocking, so a transient catalog error simply shows the
 * same gentle guidance (or nothing) rather than an error state.
 */
async function doFetch(): Promise<void> {
  loading.value = true;
  try {
    // Run both reads together; they feed independent bits of state.
    const [modelsRes, providersRes] = await Promise.all([
      fetchCloudModels(),
      fetchCloudProviders().catch(() => ({ providers: {} as Record<string, CloudProviderMeta> })),
    ]);

    // Only CLOUD models count — local models (is_local === true) are excluded
    // (same filter as the discussion model dropdowns, AGENTS.md 细则 2).
    hasCloudModels.value =
      Array.isArray(modelsRes?.models) &&
      modelsRes.models.filter((m) => m.is_local !== true).length > 0;

    // Detect the first provider that HAS models but whose api key is absent.
    // Keeping it simple per the brief: any provider returned by /providers is
    // considered relevant; we additionally require it to actually carry a
    // models list so an empty stub provider never triggers the prompt.
    providerNeedingKeyId.value = null;
    providerNeedingKeyConfigRef.value = null;
    const missing = new Set<string>();
    const cfgById: Record<string, Record<string, unknown>> = {};
    const providers = providersRes.providers ?? {};
    for (const [id, meta] of Object.entries(providers)) {
      const hasModels =
        Array.isArray((meta as { models?: unknown[] }).models) &&
        ((meta as { models?: unknown[] }).models?.length ?? 0) > 0;
      if (hasModels && meta.has_api_key === false) {
        // Strip the presence-only derived field from the config we keep for
        // the PUT (it is not part of the writable config document).
        const cfg: Record<string, unknown> = { ...(meta as Record<string, unknown>) };
        delete cfg.has_api_key;
        missing.add(id);
        cfgById[id] = cfg;
        // The first such provider seeds the single "needs a key" refs used by
        // the welcome card + dialog default (unchanged existing behaviour).
        if (providerNeedingKeyId.value === null) {
          providerNeedingKeyId.value = id;
          providerNeedingKeyConfigRef.value = cfg;
        }
      }
    }
    providersMissingKeyIds.value = missing;
    providerConfigById.value = cfgById;
  } catch {
    hasCloudModels.value = false;
    providerNeedingKeyId.value = null;
    providerNeedingKeyConfigRef.value = null;
    providersMissingKeyIds.value = new Set();
    providerConfigById.value = {};
  } finally {
    loading.value = false;
    checked.value = true;
  }
}

export interface CloudModelStatus {
  /** Reactive: true when at least one cloud model is configured. */
  hasCloudModels: Ref<boolean>;
  /** Reactive: a fetch has completed at least once. */
  checked: Ref<boolean>;
  /** Reactive: a fetch is currently in flight. */
  loading: Ref<boolean>;
  /**
   * True when the onboarding hint should be shown: a check has completed
   * and there are no cloud models. Stays false while the first fetch is
   * still pending so the hint never flashes before we actually know.
   */
  showOnboarding: ComputedRef<boolean>;
  /**
   * True when at least one provider has models but is missing its API key.
   */
  needsApiKey: ComputedRef<boolean>;
  /** The provider_id that needs a key (first such provider), or null. */
  providerNeedingKey: Ref<string | null>;
  /**
   * The full existing config document of the provider that needs a key, so
   * the save can PUT it back intact (with the real api_key merged in).
   */
  providerNeedingKeyConfig: Ref<Record<string, unknown> | null>;
  /**
   * True when the "set API key" prompt should be shown: a check has
   * completed AND a provider needs a key. Mutually exclusive with
   * `showOnboarding` (models must exist for a provider to need a key), so
   * the no-models card and the missing-key prompt never show together.
   */
  showApiKeyPrompt: ComputedRef<boolean>;
  /**
   * Visibility of the shared in-place "set API key" dialog (hosted in
   * `ChatMessageList.vue`). Bound via `v-model:visible`. Opened only by
   * `openApiKeyFlow()` / `openApiKeyFlowForProvider()` on the internal
   * edition.
   */
  dialogVisible: Ref<boolean>;
  /**
   * True when the given provider currently has models but is missing its
   * API key (per the last fetch). Used by the pre-send interception to check
   * the SPECIFIC provider of the target tab's model, not just the first one.
   */
  providerMissingKey: (providerId: string) => boolean;
  /**
   * The SINGLE edition-aware entry point shared by all three surfaces
   * (composer pre-send, chat error bubble, welcome card):
   *   - internal edition (`service.isInternal === true`) → open the in-place
   *     dialog (`dialogVisible = true`);
   *   - otherwise (external OR unknown) → navigate to Settings → Cloud Models.
   * Unknown edition is treated conservatively as "not internal" so we never
   * open a key dialog for a provider that does not exist on external.
   */
  openApiKeyFlow: () => void;
  /**
   * Like `openApiKeyFlow()` but first points the dialog refs at a SPECIFIC
   * provider (the one that triggered a pre-send block), so the in-place
   * dialog saves the key for the right provider on the internal edition.
   */
  openApiKeyFlowForProvider: (providerId: string) => void;
  /** Trigger a fetch if one has not run yet (de-duplicated). */
  ensureChecked: () => Promise<void>;
  /** Force a re-fetch (e.g. after the user configures a cloud model). */
  refresh: () => Promise<void>;
}

export function useCloudModelStatus(): CloudModelStatus {
  const showOnboarding = computed(
    () => checked.value && !hasCloudModels.value,
  );

  const needsApiKey = computed(() => providerNeedingKeyId.value !== null);

  const showApiKeyPrompt = computed(
    () => checked.value && !showOnboarding.value && needsApiKey.value,
  );

  function providerMissingKey(providerId: string): boolean {
    return providersMissingKeyIds.value.has(providerId);
  }

  /**
   * Decide the edition-appropriate action and perform it. The internal-vs-
   * external decision lives HERE (one shared place) so the composer, error
   * bubble, and welcome card all behave identically.
   */
  function openApiKeyFlow(): void {
    const service = useServiceStore();
    if (service.isInternal === true) {
      // Internal edition: the cloud provider is pre-configured — just set the
      // key in place.
      dialogVisible.value = true;
    } else {
      // External OR unknown edition: the user must add a provider first, so
      // guide them to Settings → Cloud Models instead of opening a dialog for
      // a provider that may not exist. Falls back to a no-op if the navigator
      // has not been registered yet (App.vue registers it at mount).
      cloudModelSettingsNavigator?.();
    }
  }

  function openApiKeyFlowForProvider(providerId: string): void {
    // Seed the dialog refs at the specific provider that triggered the flow
    // so an internal-edition save PUTs the key for the RIGHT provider. Only
    // repoint when we actually have that provider's cached config; otherwise
    // leave the existing (first-missing) refs untouched.
    const cfg = providerConfigById.value[providerId];
    if (cfg !== undefined) {
      providerNeedingKeyId.value = providerId;
      providerNeedingKeyConfigRef.value = cfg;
    }
    openApiKeyFlow();
  }

  async function ensureChecked(): Promise<void> {
    if (checked.value) return;
    if (inflight !== null) {
      await inflight;
      return;
    }
    inflight = doFetch().finally(() => {
      inflight = null;
    });
    await inflight;
  }

  async function refresh(): Promise<void> {
    // Coalesce with any in-flight fetch; otherwise start a fresh one.
    if (inflight !== null) {
      await inflight;
      return;
    }
    inflight = doFetch().finally(() => {
      inflight = null;
    });
    await inflight;
  }

  return {
    hasCloudModels,
    checked,
    loading,
    showOnboarding,
    needsApiKey,
    providerNeedingKey: providerNeedingKeyId,
    providerNeedingKeyConfig: providerNeedingKeyConfigRef,
    showApiKeyPrompt,
    dialogVisible,
    providerMissingKey,
    openApiKeyFlow,
    openApiKeyFlowForProvider,
    ensureChecked,
    refresh,
  };
}
