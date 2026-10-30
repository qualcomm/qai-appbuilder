// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useComposerModelSelection` — chat-composer model selector logic
 * (ARCH-1 cohesion split, extracted verbatim from `ChatComposer.vue`).
 *
 * Owns the model-selection surface with ZERO behaviour change:
 *   - status-dot 5-state colour + placeholder detection (V1 index.html:946-954)
 *   - `selectedModelLabel` resolving local + cloud names (useModels.js)
 *   - `currentModelIsCloud` for the persona switcher (app.js:531-538)
 *   - `selectModel` + `maybeAutoLoadLocalModel` (useModels.js:117-145/264)
 *   - the cloud-model auto-select / provider-backfill watcher
 *     (useModels.js:182-195) and the mount-time preference restore
 *   - the model-list auto-switch on mount (useModels.js:197) and the
 *     streaming→idle ctx-badge refresh
 *
 * The composer wires in the shared composables (model selector, cloud
 * names, auto-switch, CC/OC) and its own
 * `modelDropdownOpen` ref so the dropdown still closes on pick. The mount
 * + streaming watchers are registered here so the composer's `onMounted`
 * stays thin; `useContextUsage.refresh` is threaded in for the
 * streaming→idle ctx refresh.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useServiceStore } from "@/stores/service";
import { useToast } from "@/composables/useToast";
import { useModelSelector } from "@/composables/useModelSelector";
import { useCloudModelNames } from "@/composables/chat/useCloudModelNames";
import {
  loadChatModelPreference,
  saveChatModelPreference,
} from "@/composables/chat/useChatModelPreference";
import { useServiceRuntimeStore } from "@/stores/serviceRuntime";
import {
  useChatModelAutoSwitch,
  type AutoSwitchModel,
} from "@/composables/chat/useChatModelAutoSwitch";
import type { useClaudeCode } from "@/composables/useClaudeCode";
import type { useOpenCode } from "@/composables/useOpenCode";

type ClaudeCode = ReturnType<typeof useClaudeCode>;
type OpenCode = ReturnType<typeof useOpenCode>;

export function useComposerModelSelection(opts: {
  modelDropdownOpen: Ref<boolean>;
  claudeCode: ClaudeCode;
  openCode: OpenCode;
  /** ctx-badge refresh, run on each streaming→idle transition (V1 parity). */
  refreshCtx: () => void;
}) {
  const { modelDropdownOpen, claudeCode, openCode, refreshCtx } = opts;
  const { t } = useI18n();
  const store = useChatTabsStore();
  const toast = useToast();
  const serviceStore = useServiceStore();

  // V1 parity (useModels.js:13 + 182-195) — the globally persisted model
  // selection (`/api/preferences` selected_model_id/provider), loaded once on
  // mount and updated whenever the user picks a model. Declared up-front so the
  // auto-switch `applySelection` callback (registered below) can capture it;
  // that callback only fires post-mount, so the forward reference is safe.
  const persistedModelPref = ref<{ id: string; provider: string }>({
    id: "",
    provider: "",
  });

  const {
    models,
    hasModels,
    loading: modelsLoading,
    fetchModels: loadModels,
    getModelLabel,
    getModelId,
  } = useModelSelector();

  // V1 `useModels.js:219-329` parity — auto-switch the chat model when the
  // selected LOCAL model stops running, with a loading guard so a just-picked
  // model isn't yanked away mid-load. Getters are lazy so they read the live
  // active-tab selection at call time (post-mount).
  const { loadingLocalModelId, autoSwitchStoppedModel } =
    useChatModelAutoSwitch({
      selection: () => ({
        id: store.activeTab?.modelId ?? "",
        provider: store.activeTab?.modelProvider ?? "",
      }),
      applySelection: (id, provider) => {
        const tab = store.activeTab;
        if (tab) store._patchTab(tab.id, { modelId: id, modelProvider: provider });
        persistedModelPref.value = { id, provider };
      },
    });

  // Cloud-model id ↔ display-name index — extracted to a composable so
  // the chip label and auto-select watch share the same loader.
  const { cloudModelMap, cloudModelEntries, cloudModelsLoaded, loadCloudModelNames } =
    useCloudModelNames();

  // V1 parity: the model-dropdown "online" state tracks inference-service
  // / model availability (V1 keyed off the model list's `is_placeholder`,
  // useModels.js:42-48), NOT the generic system-health probe alone. So we
  // require both: system health OK AND at least one real model present.
  const serviceOnline = computed(
    () => serviceStore.health !== null && !serviceStore.error && hasModels.value,
  );

  // V1 parity (index.html:946-954) — model selector status dot has FIVE
  // states. `selectedModelIsPlaceholder` mirrors V1: true when no real
  // model is selected (modelId is null/empty/"qai-default"). Reads
  // `tab.modelId` directly: for both ordinary chat tabs and sub-agent tabs
  // (whose model is seeded from the session's `subagentMeta.modelId` by
  // the watcher below) this IS the truth source.
  const selectedModelIsPlaceholder = computed<boolean>(() => {
    const mid = store.activeTab?.modelId ?? null;
    return mid === null || mid === "" || mid === "qai-default";
  });

  const selectedModelIsLocal = computed<boolean>(() =>
    (store.activeTab?.modelId ?? "").startsWith("local::"),
  );

  // Shared with the Service page: the dot is derived from the one lifecycle
  // store, so start / stop / chat-triggered auto-start show up here at once.
  const runtimeStore = useServiceRuntimeStore();
  const localServiceState = computed<"unknown" | "stopped" | "loading" | "ready">(() => {
    if (!selectedModelIsLocal.value) return "unknown";
    const selectedId = store.activeTab?.modelId ?? "";
    const wanted = selectedId.slice("local::".length).toLowerCase();
    const loaded = runtimeStore.activeModel.toLowerCase();
    // Same rule as the dropdown: the daemon reports a bare name that may match
    // the entry's id / model_id / name rather than the selected id verbatim.
    const entry = models.value.find((m) => getModelId(m) === selectedId);
    const aliases = [wanted];
    if (entry) {
      for (const v of [entry.id, entry.model_id, entry.name]) {
        if (typeof v !== "string") continue;
        const lower = v.toLowerCase();
        aliases.push(lower.includes("::") ? lower.slice(lower.indexOf("::") + 2) : lower);
      }
    }
    const matches = loaded !== "" && aliases.includes(loaded);
    switch (runtimeStore.lifecycle) {
      case "starting":
        return loaded === "" || matches ? "loading" : "stopped";
      case "loading":
        return matches ? "loading" : "stopped";
      case "running":
        // The daemon reports no model name when it was started without one;
        // it is up and ready, so do not paint it as stopped.
        return loaded === "" || matches ? "ready" : "stopped";
      default:
        return "stopped";
    }
  });

  onMounted(() => runtimeStore.acquire());
  onBeforeUnmount(() => runtimeStore.release());
  // The per-model `is_running` dots in the dropdown come from a snapshot of the
  // model list; refetch it whenever the daemon lifecycle changes (from any
  // surface).
  watch(
    () => runtimeStore.lifecycle,
    () => void loadModels(),
  );

  // `modelDotStyle` returns an inline `background:` declaration so we can
  // pre-compute the colour without adding 5 new CSS classes for the same
  // visual effect (V1 itself uses inline `:style` on the dot).
  // Colour coding:
  //   - CC/OC mode: their brand colours
  //   - loading: muted (grey)
  //   - service offline (no models): muted (grey)
  //   - no model selected but service online: amber (warning)
  //   - LOCAL model: live daemon lifecycle (stopped/unknown grey, loading amber)
  //   - model selected & online: green (success)
  const modelDotStyle = computed<string>(() => {
    if (claudeCode.isCCMode.value) return "background:#7eb8f7";
    if (openCode.isOCMode.value) return "background:#63b3ed";
    // A selected LOCAL model is judged purely by the daemon lifecycle: the
    // cloud-oriented health probe and the model-list fetch must not gray it.
    if (selectedModelIsLocal.value) {
      if (localServiceState.value === "loading") return "background:var(--warning)";
      if (localServiceState.value === "ready") return "background:var(--success)";
      return "background:var(--text-muted)";
    }
    if (modelsLoading.value) return "background:var(--text-muted)";
    if (!serviceOnline.value) return "background:var(--text-muted)";
    if (selectedModelIsPlaceholder.value) return "background:var(--warning)";
    return "background:var(--success)";
  });

  const selectedModelLabel = computed(() => {
    // V1 parity (index.html:961-969) — when CC / OC mode is active, the
    // toolbar trigger reflects that mode's current model with a leading
    // emoji (🤖 for CC / 🔷 for OC) instead of the chat-tab's model.
    if (claudeCode.isCCMode.value) {
      const ccModel = claudeCode.currentModel.value;
      return `🤖 ${ccModel !== "" ? ccModel : t("index.claudeCodeMode")}`;
    }
    if (openCode.isOCMode.value) {
      const ocModel = openCode.currentModel.value;
      return `🔷 ${ocModel !== "" && ocModel !== null ? ocModel : t("index.openCodeMode")}`;
    }

    // Read from `tab.modelId` directly: for a sub-agent tab the watcher
    // below seeds this from the session's `subagentMeta.modelId` (the
    // authoritative source); for an ordinary chat tab this is the user's
    // own selection.
    const selectedId = store.activeTab?.modelId ?? null;

    // V1 parity (`useModels.js:selectedModelName`): the chip shows the NAME of
    // the currently-selected model, resolved against BOTH local on-device
    // models and the cloud catalog.
    if (selectedId !== null && selectedId !== "" && selectedId !== "qai-default") {
      const local = models.value.find((m) => getModelId(m) === selectedId);
      if (local) return getModelLabel(local);
      const cloudName = cloudModelMap.value[selectedId];
      if (cloudName !== undefined) return cloudName;
      // Selected id not found in either list: strip any "provider::" prefix.
      return selectedId.includes("::")
        ? selectedId.split("::").slice(1).join("::")
        : selectedId;
    }

    // No explicit selection: show a concise placeholder based on service state.
    // When service is online (models available) → "Select model" placeholder.
    // When service is offline → "Service offline" short label.
    // The verbose "本机 — 服务未启动" style is retired in favour of dot colour
    // encoding (grey = offline, amber = no selection, green = ready).
    if (serviceOnline.value) return t("chat.modelSelectorPlaceholder");
    return t("chat.modelSelectorOffline");
  });

  // V1 parity (`app.js:531-538` `currentModelIsCloud`): the code-mode persona
  // switcher only applies to cloud models (on-device models don't support
  // personas). Reads `tab.modelId` directly: for a sub-agent tab the watcher
  // below seeds it from the session's `subagentMeta.modelId`.
  const currentModelIsCloud = computed<boolean>(() => {
    const id = store.activeTab?.modelId ?? null;
    if (id === null || id === "" || id === "qai-default") return true;
    if (models.value.some((m) => getModelId(m) === id)) return false;
    return true;
  });

  function selectModel(modelId: string, provider: string): void {
    const tab = store.activeTab;
    // Sub-agent tab owns its model PER SUB-AGENT: persist it to the
    // sub-agent's session (PATCH) so the budget recomputes against the new
    // model while `used` is untouched, and do NOT write the global chat-model
    // preference nor touch the parent — switching a sub-agent's model affects
    // ONLY that sub-agent (State-Truth-First: the session is the truth source).
    // The store action `setSubAgentModel` writes back to `tab.modelId` +
    // `subagentMeta` + the mirrored ref so all surfaces agree.
    if (tab && tab.kind === "subagent" && tab.subagentMeta?.subagentId) {
      modelDropdownOpen.value = false;
      void store.setSubAgentModel(tab.id, modelId, provider).catch((e: unknown) => {
        const m = e instanceof Error ? e.message : String(e);
        // Project toast — never window.alert (AGENTS.md §3.9.2). Local state
        // is left unchanged on failure (the PATCH never patched the tab).
        toast.error(`${t("models.startFailed")}: ${m}`);
      });
      // No global pref write, no auto-load: a sub-agent's model is a pure
      // session denominator switch (the sub-agent runs server-side).
      return;
    }
    if (tab) {
      store._patchTab(tab.id, { modelId, modelProvider: provider });
    }
    // V1 parity (useModels.js:111-115): persist the global selection so a
    // refresh / new session restores it instead of snapping back to the first
    // cloud model. Also remember it in-memory for the auto-select watcher.
    persistedModelPref.value = { id: modelId, provider };
    saveChatModelPreference(modelId, provider);
    modelDropdownOpen.value = false;

    // Selecting a LOCAL model only records the choice. GenieAPIService is
    // started / the model loaded when a message is actually sent
    // (`useChatTurnSubmit` → `ensureLocalModelReady`). Keep the guard so the
    // auto-switch does not yank a just-picked, not-yet-loaded model back.
    if (modelId.startsWith("local::")) loadingLocalModelId.value = modelId;
  }

  onMounted(() => {
    // V1 parity (useModels.js:197): after each model-list load, run the
    // auto-switch check so a selected-but-stopped LOCAL model falls back to a
    // running one (LOCAL mode), with the loading guard respected.
    void loadModels().then(() => {
      autoSwitchStoppedModel(models.value as unknown as AutoSwitchModel[]);
    });
    void loadCloudModelNames();
    // V1 parity (useModels.js:182-195): restore the globally persisted model
    // selection so the auto-select watcher can prefer it over the first cloud
    // model fallback.
    void loadChatModelPreference().then((pref) => {
      persistedModelPref.value = {
        id: pref.selected_model_id,
        provider: pref.selected_model_provider,
      };
    });
  });

  // Bug fix: `models.value` was previously only fetched once on mount, so
  // its per-entry `is_running` snapshot goes stale the moment the running
  // model changes (e.g. after switching to another local model). Re-fetching
  // whenever the dropdown opens (right before the user can pick anything)
  // keeps `is_running` accurate for the status dots and the auto-switch.
  watch(modelDropdownOpen, (isOpen) => {
    if (isOpen) {
      void loadModels();
    }
  });

  // V1 parity (`useModels.js:loadModels` auto-selects models[0]): once
  // the cloud model map is populated, if the active tab still has the
  // "qai-default" placeholder (no real model selected), auto-select the
  // first available cloud model so the backend receives a concrete
  // model_hint and can route to the correct provider endpoint.
  watch(
    [
      cloudModelEntries,
      cloudModelsLoaded,
      models,
      () => store.activeTab?.id,
      // React to the sub-agent's session model arriving / changing (detail
      // fetch, live refresh, or a PATCH) so the dropdown re-seeds to it.
      () => store.activeTab?.subagentMeta?.modelId,
      () => store.activeTab?.subagentMeta?.modelProvider,
      persistedModelPref,
    ],
    () => {
      const tab = store.activeTab;
      if (tab === null) return;
      // Sub-agent tabs own their model from the SESSION (State-Truth-First):
      // seed `tab.modelId` from `subagentMeta.modelId` (the authoritative
      // session value), NOT from the global default — otherwise the
      // auto-seed below would clobber the sub-agent's real model with the
      // user's last global pick (the D11 split). Only act when the meta's
      // model is present AND differs from the tab; if the meta has no model
      // yet, fall through to the standard fallback so the tab is never
      // stranded on an empty selection.
      if (tab.kind === "subagent") {
        const metaModelId = tab.subagentMeta?.modelId;
        if (metaModelId !== undefined && metaModelId !== "") {
          if (
            tab.modelId !== metaModelId ||
            (tab.modelProvider ?? "") !== (tab.subagentMeta?.modelProvider ?? "")
          ) {
            store._patchTab(tab.id, {
              modelId: metaModelId,
              modelProvider: tab.subagentMeta?.modelProvider ?? "",
            });
          }
          return;
        }
        // No session model yet: fall through to the shared fallback so the
        // dropdown still shows a routable default.
      }
      const cur = tab.modelId;
      const hasRealSelection = cur !== "" && cur !== "qai-default";
      if (!hasRealSelection) {
        // V1 parity (useModels.js:182-195): prefer the globally persisted
        // selection (the user's last pick, e.g. a local model) before falling
        // back to "first cloud model". Only adopt it when it still resolves to
        // a known local or cloud entry, so a stale preference for a removed
        // model doesn't strand the tab on an unroutable id.
        const pref = persistedModelPref.value;
        if (pref.id !== "" && pref.id !== "qai-default") {
          const isKnownLocal = models.value.some(
            (m) => getModelId(m) === pref.id,
          );
          const isKnownCloud = cloudModelEntries.value.some(
            (e) =>
              e.id === pref.id &&
              (pref.provider === "" || e.provider === pref.provider),
          );
          if (isKnownLocal || isKnownCloud) {
            store._patchTab(tab.id, {
              modelId: pref.id,
              modelProvider: pref.provider,
            });
            return;
          }
          // The persisted pref points at a cloud model that hasn't loaded yet:
          // wait for the cloud fetch to settle rather than overwriting it with
          // a local default we'd then never replace (the seed below flips
          // `hasRealSelection` to true and this branch never re-runs).
          if (!cloudModelsLoaded.value) return;
        }
        // Prefer first cloud model; fall back to first local.
        // V1 parity (useModels.js:192-194): seed both modelId AND
        // modelProvider so the ✓ in the dropdown only marks the picked
        // entry, not every entry that happens to share `model_id`.
        const firstCloud = cloudModelEntries.value[0];
        if (firstCloud !== undefined) {
          store._patchTab(tab.id, {
            modelId: firstCloud.id,
            modelProvider: firstCloud.provider,
          });
          return;
        }
        // No cloud model available. Only fall back to a LOCAL model once the
        // cloud fetch has actually SETTLED — otherwise an in-flight cloud list
        // would let us prematurely seed an on-device model and (because that
        // flips `hasRealSelection`) we'd never switch to the cloud model that
        // arrives a moment later. This is the root cause of "界面默认选了端侧
        // 模型" when cloud models exist but resolve slightly later than local.
        if (!cloudModelsLoaded.value) return;
        if (models.value.length > 0) {
          store._patchTab(tab.id, {
            modelId: getModelId(models.value[0]!),
            modelProvider: "",
          });
        }
        return;
      }
      // V1 parity (useModels.js:185-194) — when modelId is set but
      // modelProvider is empty (e.g. tab restored from older state, or
      // first auto-seed before this fix), look up the first cloud entry
      // whose model_id matches and adopt its provider.
      if (tab.modelProvider === "") {
        const match = cloudModelEntries.value.find((e) => e.id === cur);
        if (match !== undefined && match.provider !== "") {
          store._patchTab(tab.id, { modelProvider: match.provider });
        }
      }
    },
    { immediate: true },
  );

  // Refresh ctx after each streaming turn completes (status leaves
  // "streaming" → "idle").
  watch(
    () => store.activeTab?.status ?? null,
    (next, prev) => {
      if (prev === "streaming" && next === "idle") {
        refreshCtx();
      }
    },
  );

  return {
    models,
    hasModels,
    modelsLoading,
    getModelLabel,
    getModelId,
    selectedModelIsPlaceholder,
    modelDotStyle,
    selectedModelLabel,
    currentModelIsCloud,
    selectModel,
  };
}
