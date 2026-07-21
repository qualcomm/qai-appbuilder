<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AppBuilderWorkbenchOverlay — App Builder chat-mode workbench (V1
 * `AppBuilderWorkbench.js` parity, rendered above the chat message list when
 * `activeToolMode === 'app-builder'`).
 *
 * Layout (V1 `.ab-workbench`):
 *   sticky header (taxonomy-driven TaskRail trigger + privacy badge + collapse
 *     + history + info + exit)
 *   → progress bar (while live)
 *   → body: model gallery (when no model picked) OR three-column stage
 *     (input + params + Run/Cancel + weights pill | output | metrics+
 *     classification)
 *   → info drawer + weights drawer + run-history modal + compare tray.
 *
 * Heavy view-model logic lives in `useAppBuilderWorkbench` (card VMs, taxonomy
 * tree, filtering) so this shell stays cohesive (§重构质量铁律). Run / stream /
 * selection state is the Pinia `appBuilder` store. Styling reuses the global
 * `.ab-*` classes from styles/app-builder/app-builder.css (real design tokens).
 */
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useAppBuilderStore } from "@/stores/appBuilder";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useAppBuilderChatBridge } from "@/composables/chat/useAppBuilderChatBridge";
import { useUiStore } from "@/stores/ui";
import { useConfirm } from "@/composables/useConfirm";
import {
  workbenchOpen,
  historyOpen,
  toggleHistory,
  promptDialogOpen,
} from "@/composables/app-builder/useAppBuilderModeUi";
import {
  useAppBuilderWorkbench,
} from "@/composables/app-builder/useAppBuilderWorkbench";
import { useDisplayedRun } from "@/composables/app-builder/useDisplayedRun";
import { useRunHistoryPanel } from "@/composables/app-builder/useRunHistoryPanel";
import type { AppModelCardVM } from "@/components/app-builder/types";
import ModelStrip from "@/components/app-builder/ModelStrip.vue";
import VariantSwitcher from "@/components/app-builder/VariantSwitcher.vue";
import TaxonomyPickerDropdown from "@/components/app-builder/TaxonomyPickerDropdown.vue";
import HeaderModelPicker from "@/components/app-builder/HeaderModelPicker.vue";
import DynamicInput from "@/components/app-builder/DynamicInput.vue";
import DynamicParams from "@/components/app-builder/DynamicParams.vue";
import DynamicOutput from "@/components/app-builder/DynamicOutput.vue";
import MetricsView from "@/components/app-builder/MetricsView.vue";
import type {
  CurrentRunMetrics,
  StageEntry,
} from "@/components/app-builder/MetricsView.vue";
import ModelInfoDrawer from "@/components/app-builder/ModelInfoDrawer.vue";
import CompareTray from "@/components/app-builder/CompareTray.vue";
import HistoryPanel from "@/components/app-builder/HistoryPanel.vue";
import AudioInput from "@/components/app-builder/inputs/AudioInput.vue";
import ImageDropzone from "@/components/app-builder/inputs/ImageDropzone.vue";
import TextEditor from "@/components/app-builder/inputs/TextEditor.vue";
import { resolveAppBuilderAssetUrl } from "@/utils/appBuilderAssetUrl";

const { t, locale } = useI18n();
const store = useAppBuilderStore();
const wb = useAppBuilderWorkbench();
const tabs = useChatTabsStore();
const ui = useUiStore();
const { confirm } = useConfirm();
const bridge = useAppBuilderChatBridge();

// Exit App Builder mode (V1 `AppBuilderWorkbench` exit button → parent sets
// activeToolMode = null). Mirrors ChatComposer.exitMode so the chip ✕ in the
// composer and this header button behave identically.
function onExit(): void {
  const tab = tabs.activeTab;
  if (tab !== null) {
    tabs.setActiveMode(tab.id, null);
  }
  ui.setActiveToolMode(null);
}

/**
 * Resolve a run OUTPUT path written by a Pack runner to a browser-usable URL
 * (V1 `resolveAssetUrl` parity, `frontend/js/utils/appbuilder-url.js`).
 *
 * Runner OUTPUT artifacts land in the flat `data/outputs/` tree (e.g. MeloTTS
 * `audio_path` = `data/outputs/tts-<run_id>.wav`, super-resolution / segmentation
 * `image_path` = `data/outputs/sr-<run_id>.png`) — NOT in the per-run artifact
 * blob store. `resolveAppBuilderAssetUrl` rewrites such prefixed paths onto the
 * backend static mount `/api/appbuilder/files/outputs/…` (mounted in
 * `apps/api/_spa_mount.py :: _mount_app_builder_files`). Only when the path does
 * not match any known static-mount prefix (i.e. a genuine blob-store relative
 * path persisted through `ArtifactStorePort`) do we fall back to the dedicated
 * artifact-blob route `/api/app-builder/artifacts/<run_id>/<path>/blob`.
 */
function resolveOutputUrl(p: string): string {
  const mapped = resolveAppBuilderAssetUrl(p);
  // `resolveAppBuilderAssetUrl` returns the input unchanged when no prefix
  // matches, so a differing result means a static mount claimed it.
  if (mapped !== p && mapped !== "") return mapped;
  const runId = displayed.value?.id;
  return runId != null ? store.artifactBlobUrl(runId, p) : p;
}

// ── local UI state ──────────────────────────────────────────────────────
const drawerOpen = ref(false);
const drawerModel = ref<AppModelCardVM | null>(null);
const weightsDrawerOpen = ref(false);
// V1 parity (AppBuilderWorkbench.js:93-96, 770, 806, 845, 874): each side
// panel can be independently collapsed to a narrow rail, and the center
// output can be maximized to take over the full width. Pure view toggles —
// not in the store.
const leftCollapsed = ref(false);
const rightCollapsed = ref(false);
const outputMaximized = ref(false);

// Schema → input/param/variant projections live in the workbench composable
// (keeps this shell cohesive — §重构质量铁律).
const inputFields = computed(() => wb.inputFields.value);
const paramDefs = computed(() => wb.paramDefs.value);
const variantOptions = computed(() => wb.variantOptions.value);

// V1-parity stage details: input kind (drives the dropzone vs form dispatch),
// the input-constraints hint line, and the right-column CLASSIFICATION block.
const inputKind = computed(() => wb.selectedInputKind.value);
const inputConstraintsHint = computed(() => wb.inputConstraintsHint.value);
const textConstraints = computed(() => wb.textConstraints.value);
const classificationRows = computed(() => wb.classificationRows.value);

// ── TaxonomyPickerDropdown + HeaderModelPicker data (V1 setup bar parity) ──
// Map the composable's taskGroups into the shape TaxonomyPickerDropdown expects.
const GROUP_ICON_MAP: Readonly<Record<string, string>> = {
  audio: "audio",
  "computer-vision": "vision",
  "generative-ai": "spark",
  multimodal: "stack",
};
const taxonomyForPicker = computed(() => ({
  groups: wb.taskGroups.value.map((g) => ({
    id: g.id,
    label: g.label,
    icon: g.icon ?? GROUP_ICON_MAP[g.id] ?? "dot",
    tasks: g.tasks.map((t) => ({ id: t.id, label: t.label })),
  })),
}));
const modelCountsForPicker = computed<Record<string, number>>(() => {
  const out: Record<string, number> = {};
  for (const g of wb.taskGroups.value) {
    for (const t of g.tasks) out[t.id] = t.modelCount;
  }
  return out;
});
// Currently browsed group/task (the picker selections drive model filtering).
const selectedGroupId = computed<string | null>(
  () => store.selectedGroupId ?? (wb.taskGroups.value[0]?.id ?? null),
);
const selectedTaskId = computed<string | null>(() => store.selectedTaskId ?? null);

function onTaxonomySelect(payload: { groupId: string; taskId: string }): void {
  store.setTaxonomyFilter(payload.groupId, payload.taskId);
}

// Gallery title label (V1 `selectionLabel`, AppBuilderWorkbench.js:178-191):
// the human-readable label of the currently-filtered task (else group), used
// to fill the `Choose a {category} model` heading. Falls back to the humanized
// task/group id so the heading is never blank.
const selectionLabel = computed<string>(() => {
  const taskId = store.selectedTaskId;
  const groupId = store.selectedGroupId;
  for (const g of wb.taskGroups.value) {
    if (taskId !== null) {
      const task = g.tasks.find((t) => t.id === taskId);
      if (task !== undefined) return task.label;
    }
  }
  if (groupId !== null) {
    const g = wb.taskGroups.value.find((x) => x.id === groupId);
    if (g !== undefined) return g.label;
  }
  return "";
});

// HeaderModelPicker needs the lean models list.
const modelsForPicker = computed(() =>
  store.models.map((m) => ({
    id: m.id,
    title: m.title ?? m.id,
    enabled: m.enabled !== false,
    taxonomy: m.taxonomy ?? [],
  })),
);

// Per-kind input value bound to the matching `store.inputs` bucket. The runner
// reads `inputs.<kind>` (audio / image / text); the dropzone / editor emit the
// data-URL / text which we write back through `store.setInputs`.
const audioValue = computed<string | null>(
  () => (store.inputs.audio as string | undefined) ?? null,
);
const imageValue = computed<string | null>(
  () => (store.inputs.image as string | undefined) ?? null,
);
const textValue = computed<string>(
  () => (store.inputs.text as string | undefined) ?? "",
);
function setInputKey(key: string, value: unknown): void {
  store.setInputs({ ...store.inputs, [key]: value });
}

// ── derived ───────────────────────────────────────────────────────────────
// The live "displayed run" projection (local ref kept in sync with the store
// across non-live changes + live SSE proxy mutations) lives in
// `useDisplayedRun` (cohesion split — keeps this shell a thin layout host).
const { displayed, runStatus, isLive, runProgress } = useDisplayedRun(store);
void runStatus;
void isLive;

/**
 * Map a backend device string to a friendly display label (V1
 * MetricsView.js:60-68 `prettyDevice` parity).
 */
function prettyDevice(d: string): string {
  const lower = d.toLowerCase();
  if (lower.includes("htp") || lower.includes("npu")) return "NPU (HTP)";
  if (lower === "gpu") return "GPU";
  if (lower === "cpu") return "CPU";
  if (lower === "qnn") return "QNN · NPU";
  return d.toUpperCase();
}

const metricsRows = computed(() => {
  const m = displayed.value?.metrics ?? null;
  const rows: Array<{ label: string; value: string | number; unit?: string }> = [];
  // V1 parity (MetricsView.js:384-393): the KV simple-table shows Device +
  // Quantization. Latency is promoted to the 32px headline (see currentRun),
  // so it is intentionally NOT duplicated here.
  const cr = currentRunMetrics.value;
  if (cr !== null) {
    if (cr.device != null && cr.device !== "") {
      rows.push({ label: t("appBuilder.metrics.device"), value: prettyDevice(cr.device) });
    }
    if (cr.quantization != null && cr.quantization !== "") {
      rows.push({ label: t("appBuilder.metrics.quantization"), value: cr.quantization });
    }
  }
  // Keep the run provenance rows (Started / artifact count) only when there is
  // no richer current-run telemetry, preserving the lean look on bare runs.
  if (rows.length === 0 && m !== null) {
    if (m.duration_ms != null) {
      rows.push({ label: t("appBuilder.metrics.latency"), value: Math.round(m.duration_ms), unit: "ms" });
    }
    rows.push({ label: t("appBuilder.history.colStartedAt"), value: m.started_at ?? "—" });
    rows.push({ label: t("appBuilder.metrics.runs"), value: m.artifact_count });
  }
  return rows;
});

/**
 * V1-compatible current-run telemetry (CurrentRunMetrics for MetricsView).
 *
 * Data source (V1 useAppBuilder.js:600-606 parity): the runner emits a
 * `metrics` NDJSON frame carrying `{ latencyMs, memoryMB, device, stages?,
 * loadStages?, latencyDistribution? }`. V2 `frames.ts` records every raw
 * payload on `run.frames[].payload` but does not promote the `metrics` frame
 * to a typed slot, so we scan the frames here (component-local, no store
 * change) for the latest `event === "metrics"` payload. Latency falls back to
 * the HTTP `RunMetricsResponse.duration_ms` (always present), and quantization
 * falls back to the selected variant's runtime quantization (V1
 * MetricsView.js:123-128: `selectedVariant.runtime.quantization`).
 */
const currentRunMetrics = computed<CurrentRunMetrics | null>(() => {
  const run = displayed.value;
  if (run === null) return null;

  // Latest `metrics` frame payload (newest wins).
  let frameMetrics: Record<string, unknown> | null = null;
  for (let i = run.frames.length - 1; i >= 0; i--) {
    const p = run.frames[i]?.payload as Record<string, unknown> | undefined;
    if (p === undefined) continue;
    if (p.event === "metrics") {
      frameMetrics = p;
      break;
    }
  }

  const num = (v: unknown): number | null => {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const str = (v: unknown): string | null =>
    typeof v === "string" && v !== "" ? v : null;
  const stageArr = (v: unknown): StageEntry[] | null => {
    if (!Array.isArray(v)) return null;
    const out: StageEntry[] = [];
    for (const it of v) {
      if (it === null || typeof it !== "object") continue;
      const o = it as Record<string, unknown>;
      const name = str(o.name);
      const lat = num(o.latencyMs ?? o.latency_ms);
      if (name === null || lat === null) continue;
      out.push({ name, latencyMs: lat, model: str(o.model) ?? null });
    }
    return out.length > 0 ? out : null;
  };

  const fm = frameMetrics ?? {};
  // Latency: prefer the metrics frame, fall back to HTTP duration_ms.
  const latencyMs =
    num(fm.latencyMs ?? fm.latency_ms) ?? num(run.metrics?.duration_ms);
  // Quantization: prefer frame, fall back to selected variant runtime.
  const quantization =
    str(fm.quantization) ?? (selectedVariantQuant.value || null);

  const distRaw = fm.latencyDistribution as Record<string, unknown> | undefined;
  const latencyDistribution =
    distRaw !== undefined && distRaw !== null && typeof distRaw === "object"
      ? {
          p50: num(distRaw.p50),
          p90: num(distRaw.p90),
          std: num(distRaw.std),
        }
      : null;

  const cr: CurrentRunMetrics = {
    latencyMs,
    device: str(fm.device),
    quantization,
    peakMemoryMB: num(fm.memoryMB ?? fm.memory_mb),
    modelSizeMB: num(fm.modelSizeMB ?? fm.model_size_mb),
    stages: stageArr(fm.stages),
    loadStages: stageArr(fm.loadStages ?? fm.load_stages),
    latencyDistribution,
  };

  // All-empty guard: nothing meaningful → null so MetricsView hides the block.
  const hasAny =
    cr.latencyMs !== null ||
    cr.device !== null ||
    (cr.quantization !== null && cr.quantization !== "") ||
    cr.peakMemoryMB !== null ||
    cr.modelSizeMB !== null ||
    (cr.stages?.length ?? 0) > 0 ||
    (cr.loadStages?.length ?? 0) > 0 ||
    latencyDistribution !== null;
  return hasAny ? cr : null;
});

const weightsInstallPath = computed<string>(() =>
  store.selectedModelId !== null ? `models/${store.selectedModelId}/` : "",
);

// ── handlers ────────────────────────────────────────────────────────────
function onSelectModel(id: string): void {
  store.selectModel(id);
}
function onSelectVariant(id: string): void {
  store.selectVariant(id);
}
function onApplyExample(example: { name?: string | null; license?: string | null; inputs?: Record<string, unknown>; paramsOverride?: Record<string, unknown> }): void {
  // V1 parity (useAppBuilder.js:349-357): apply the example's preset inputs
  // and params to the workbench, then close the drawer so the user sees the
  // filled-in stage and can immediately press Run.
  store.applyExample({
    inputs: example.inputs,
    paramsOverride: example.paramsOverride,
  });
  drawerOpen.value = false;
}
function onDeleteModel(modelId: string): void {
  // Close the detail drawer immediately, then delete + refresh from the
  // backend. ``store.deleteModel`` clears the selection and re-fetches the
  // authoritative model list, so the gallery/taxonomy drop the model and the
  // workbench stops showing it. If the model being deleted is the one the
  // workbench currently shows, fall back to the gallery (no model selected)
  // so the user is not left staring at a now-deleted model's stage.
  drawerOpen.value = false;
  const wasShowingDeleted = store.selectedModelId === modelId;
  void (async () => {
    const { ok, warnings } = await store.deleteModel(modelId);
    // 缺陷 P4: surface non-fatal file-cleanup warnings (e.g. an AV-locked
    // ``.bin`` left on disk) via a toast so the user knows disk state may
    // diverge from DB state (State-Truth-First — reflect reality).
    if (ok && warnings.length > 0) {
      // eslint-disable-next-line no-console
      console.warn(
        "[app-builder] delete completed with warnings:",
        warnings,
      );
    }
    if (ok && wasShowingDeleted && store.selectedModelId === null) {
      // No model auto-selected (e.g. it was the last one) → return to the
      // gallery view instead of an empty stage.
      workbenchOpen.value = store.models.length > 0 ? workbenchOpen.value : false;
    }
  })();
}
function onDeleteVariants(modelId: string, variantIds: string[]): void {
  // V1 parity (handleVariantsDeleted): a partial delete keeps the drawer OPEN
  // showing the thinned pack — only a full-pack delete closes it. Refresh the
  // gallery + manifest so the surviving variants are reflected in place.
  void (async () => {
    await store.deleteVariants(modelId, variantIds);
    await store.fetchModels();
    const refreshed = wb.cardVMFor(modelId);
    if (refreshed !== null) {
      drawerModel.value = refreshed;
    }
  })();
}
function onShowInfo(model: AppModelCardVM): void {
  drawerModel.value = model;
  drawerOpen.value = true;
}
function onShowInfoById(id: string): void {
  const card = wb.cardVMFor(id);
  if (card !== null) onShowInfo(card);
}
function onToggleInfo(): void {
  if (wb.selectedCard.value !== null) onShowInfo(wb.selectedCard.value);
}
function onInputUpdate(value: Record<string, unknown>): void {
  store.setInputs(value);
}
function onParamsUpdate(value: Record<string, unknown>): void {
  store.setParams(value);
}
function onRun(): void {
  if (!store.canRun) return;
  void store.startRun();
}
function onCancel(): void {
  void store.cancelRun();
}
function onAddToCompare(): void {
  const run = store.currentRun;
  if (run !== null) store.addToCompare(run);
}
function onSendToChat(): void {
  // The output-toolbar "Send to Chat" button (and the error "ask LLM" button)
  // dispatch a real chat turn through the bridge — same path as the bottom
  // toolbar's Send to Chat (V1 index.html:1402 `@send-to-chat="appBuilder.
  // sendToChat"` → handler → sendMessage()). Previously this only reset the
  // prompt and never sent, so the in-output Send button did nothing (缺陷 11/16).
  if (store.currentRun === null || store.currentRun.output === null) return;
  bridge.sendToChat();
}
// V1 `useAppBuilder._resolveDefaultPrompt` (useAppBuilder.js:321-335): the
// default send-to-chat prompt template with the current model name filled in.
// Kept i18n-resolution in the component (store stays i18n-free / domain-pure).
//
// Precedence (manifest-first, generic-fallback): if the selected model's pack
// manifest ships a per-model trilingual `send_to_chat_prompt` block (the
// locale-keyed dict `{ en, zh-CN, zh-TW }` exposed on `PackManifestResponse`
// via `store.manifestCache[id]`), prefer that curated prompt for the current
// UI locale (falling back to the `en` entry). Only when no usable per-model
// string exists do we fall back to the existing generic
// `appBuilder.sendResultPromptDefault` template with the model title filled in.
// Pure function — no side effects (safe for the reactive `watch` below).
function resolveDefaultPrompt(): string {
  const id = store.selectedModelId;
  const perModel = id ? store.manifestCache[id]?.send_to_chat_prompt : undefined;
  // Current locale first, then English; guard against a non-string / empty
  // value defensively (the field is optional & only loosely typed as a dict).
  const custom = perModel?.[locale.value] ?? perModel?.["en"];
  if (typeof custom === "string" && custom.length > 0) return custom;
  return t("appBuilder.sendResultPromptDefault", {
    model: store.selectedModel?.title ?? "",
  });
}
function onResetPrompt(): void {
  store.resetSendToChatPrompt(resolveDefaultPrompt());
}
// A2 (Bug 2-B): re-resolve the editable send-to-chat prompt whenever the
// selected model OR the UI locale changes, mirroring V1's
// `watch([selectedModel, locale], … sendToChatPrompt = _resolveDefaultPrompt())`
// (useAppBuilder.js:337-343, `{ immediate: true }`). Without this the prompt was
// resolved once on mount — before `fetchModels()` had selected a model — so
// `{model}` was empty and the sent message read "通过 [] 模型…". V1 reset
// unconditionally on model/locale change (no dirty guard); we match that.
watch(
  [() => store.selectedModelId, locale],
  () => {
    store.resetSendToChatPrompt(resolveDefaultPrompt());
  },
  { immediate: true },
);

// ── Run-history view-model (cohesion split → useRunHistoryPanel) ────────────
// The list / row formatting / expansion lives in HistoryPanel.vue; the
// overlay-side glue (subtitle assembly, current-run highlight, delete confirm
// via useConfirm §3.9, export/share/add-to-compare wiring) lives in the
// composable. `variantOptions` (from the workbench composable) is injected
// because the subtitle needs the selected variant.
const {
  selectedVariant,
  selectedVariantQuant,
  selectedModelTitle,
  historyLoading,
  historyError,
  currentHistoryRunId,
  onSelectHistory,
  onDeleteHistoryRun,
  onExportRun,
  onShareRun,
  onAddToCompareFromHistory,
} = useRunHistoryPanel({ store, confirm, t, variantOptions });
void selectedVariant;

function onKeydown(e: KeyboardEvent): void {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    onRun();
  } else if (e.key === "Escape") {
    if (historyOpen.value) {
      historyOpen.value = false;
    } else if (drawerOpen.value) {
      drawerOpen.value = false;
    } else if (weightsDrawerOpen.value) {
      weightsDrawerOpen.value = false;
    }
  }
}

const compareTrayItems = computed(() =>
  // Pass the structured item straight through (CompareTray needs the raw
  // output object + modelName/status/runtime/metrics for its Table / Radar /
  // Cards views — V1 parity). Stringifying here would break avgConf etc.
  store.compareItems.map((c) => ({
    id: c.id,
    modelId: c.modelId,
    modelName: c.modelName,
    status: c.status,
    rating: c.rating,
    variant: c.variant,
    runtime: c.runtime ?? undefined,
    output: c.output,
    metrics: c.metrics ?? undefined,
  })),
);

// V1 parity (HistoryPanel.js:57-78 → per-model `GET /history/{model_id}/runs`):
// the Run History panel shows ONLY the currently-selected model's runs, not
// the global cross-model list. The store's working set may hold other models'
// in-memory live runs, so we filter by the selected model id here. The store
// fetch is scoped server-side (缺口 #4) and this filter is the display guard.
const currentModelRuns = computed(() => {
  const id = store.selectedModelId;
  if (id === null) return store.runs;
  return store.runs.filter((r) => r.modelId === id);
});

// V1 scoped refresh: re-fetch only the selected model's history (缺口 #4).
function onRefreshHistory(): void {
  if (store.selectedModelId !== null) {
    void store.fetchHistory(50, 0, store.selectedModelId);
  } else {
    void store.fetchHistory();
  }
}

onMounted(() => {
  // V1 parity (app.js:640): entering App Builder mode always expands the
  // workbench, so a previous collapse doesn't persist across re-entry. The
  // overlay re-mounts whenever the mode is (re)activated, so doing it here is
  // equivalent to V1's mode-enter watcher.
  workbenchOpen.value = true;
  if (store.models.length === 0) void store.fetchModels();
  void store.fetchWorkerStatus();
  void store.fetchTaxonomy();
  void store.fetchTaxonomyTree();
  // V1 deps-status 逐 pack 进度 parity (useAppBuilder.js:852-853): start the
  // background dependency-install polling once AppBuilder opens so the gallery
  // ModelCard badges flip "installing → ready / missing + error" live. The
  // overlay re-mounts on every mode (re)entry, so reset the one-shot "done"
  // guard first; the poller auto-stops once all packs are satisfied.
  store.resetDepsPolling();
  void store.pollDepsStatus({ intervalMs: 5000, maxAttempts: 60 });
  // The send-to-chat prompt is initialized + kept in sync by the immediate
  // `watch([selectedModelId, locale])` above (A2 / V1 useAppBuilder.js:337-343),
  // so no one-shot init is needed here.
});

onUnmounted(() => {
  // Stop the deps polling when AppBuilder closes so no resident 5s timer
  // keeps running in chat-only mode (State-Truth: poll only while visible).
  store.stopDepsPolling();
});
</script>

<template>
  <!-- V1 parity: .ab-workbench-host provides the deep-dark background gradient
       + the CSS custom-property scope (--ab-panel, --ab-text, --ab-teal-line
       etc.) that all child ab-* classes inherit. Without this wrapper the
       variables defined on `.ab-workbench-host` in app-builder.css never
       cascade, causing broken colors / invisible buttons / missing badge
       backgrounds (root cause of visual diffs 1/2/3). -->
  <div
    v-if="workbenchOpen"
    class="ab-workbench-host"
  >
    <div
      class="ab-workbench"
      tabindex="0"
      data-testid="app-builder-workbench"
      @keydown="onKeydown"
    >
      <!-- ── sticky header / setup bar (V1 parity: pickers + badge in one row) ── -->
      <header class="ab-header ab-setup-bar">
        <div class="ab-header-left">
          <TaxonomyPickerDropdown
            :taxonomy="taxonomyForPicker"
            :selected-group-id="selectedGroupId"
            :selected-task-id="selectedTaskId"
            :model-counts="modelCountsForPicker"
            @update:selection="onTaxonomySelect"
          />
          <HeaderModelPicker
            v-if="store.models.length > 0"
            :selected-id="store.selectedModelId"
            :models="modelsForPicker"
            @update:selected-id="onSelectModel"
            @info="onShowInfoById"
          />
          <VariantSwitcher
            v-if="store.selectedModel && variantOptions.length >= 2"
            :variants="variantOptions"
            :model-value="store.selectedVariantId ?? ''"
            @update:model-value="onSelectVariant"
          />
          <span
            class="ab-privacy-badge ab-privacy-badge--inline"
            role="img"
            :title="t('appBuilder.privacyTooltip')"
            :aria-label="t('appBuilder.privacyBadge')"
          >
            <svg
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            ><rect
              x="3"
              y="11"
              width="18"
              height="11"
              rx="2"
              ry="2"
            /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>
            <span>{{ t("appBuilder.privacyBadge") }}</span>
          </span>
        </div>
        <div class="ab-header-right">
          <button
            type="button"
            class="ab-icon-btn ab-collapse-btn"
            :title="t('appBuilder.collapsePanel')"
            @click="workbenchOpen = false"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            ><polyline points="6 9 12 15 18 9" /></svg>
            <span class="ab-collapse-label">{{ t("appBuilder.collapsePanel") }}</span>
          </button>
          <button
            type="button"
            class="ab-icon-btn"
            :title="t('appBuilder.history.title')"
            :disabled="!store.selectedModel"
            data-testid="app-builder-history-toggle"
            @click="toggleHistory"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            ><polyline points="1 4 1 10 7 10" /><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" /></svg>
          </button>
          <button
            type="button"
            class="ab-icon-btn"
            :title="t('appBuilder.info')"
            :disabled="!store.selectedModel"
            @click="onToggleInfo"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            ><circle
              cx="12"
              cy="12"
              r="10"
            /><line
              x1="12"
              y1="16"
              x2="12"
              y2="12"
            /><line
              x1="12"
              y1="8"
              x2="12.01"
              y2="8"
            /></svg>
          </button>
          <button
            type="button"
            class="ab-icon-btn ab-exit-btn"
            :title="t('appBuilder.exit')"
            data-testid="app-builder-exit"
            @click="onExit"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            ><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16 17 21 12 16 7" /><line
              x1="21"
              y1="12"
              x2="9"
              y2="12"
            /></svg>
            <span class="ab-exit-label">{{ t("appBuilder.exit") }}</span>
          </button>
        </div>
      </header>

      <!-- ── snapshot banner ───────────────────────────────────────────── -->
      <div
        v-if="store.snapshotRun !== null"
        class="ab-snapshot-banner"
        role="status"
        data-testid="app-builder-snapshot-banner"
      >
        <span
          class="ab-snapshot-banner-icon"
          aria-hidden="true"
        >📜</span>
        <span class="ab-snapshot-banner-text">{{ t("appBuilder.snapshot.banner") }}</span>
        <code
          v-if="store.snapshotRun?.id"
          class="ab-snapshot-banner-runid"
        >{{ store.snapshotRun.id }}</code>
        <button
          type="button"
          class="ab-snapshot-banner-exit"
          @click="store.exitSnapshot()"
        >
          ✕ {{ t("appBuilder.snapshot.exit") }}
        </button>
      </div>

      <!-- ── progress bar (live) ───────────────────────────────────────── -->
      <div
        v-if="isLive"
        class="ab-progress"
        role="progressbar"
        :aria-valuenow="runProgress.pct ?? undefined"
        aria-valuemin="0"
        aria-valuemax="100"
      >
        <div class="ab-progress-bar">
          <div
            class="ab-progress-fill"
            :class="{ 'ab-progress-fill--indeterminate': runProgress.pct === null }"
            :style="runProgress.pct !== null ? { width: runProgress.pct + '%' } : {}"
          ></div>
        </div>
        <div class="ab-progress-label">
          <span>{{ runProgress.phase || t("appBuilder.statusRunning") }}</span>
          <span
            v-if="runProgress.pct !== null"
            class="ab-progress-pct"
          >{{ Math.round(runProgress.pct) }}%</span>
        </div>
      </div>

      <!-- ── body ──────────────────────────────────────────────────────── -->
      <div class="ab-body">
        <div class="ab-main ab-stage">
          <!-- loading -->
          <div
            v-if="store.loading && store.models.length === 0"
            class="ab-empty-state"
          >
            {{ t("appBuilder.loading") }}
          </div>

          <!-- model gallery (no model picked) -->
          <div
            v-else-if="!store.selectedModel"
            class="ab-model-gallery"
          >
            <div class="ab-model-gallery-head">
              <h3 class="ab-model-gallery-title">
                {{
                  selectionLabel
                    ? t("appBuilder.gallery.title", { category: selectionLabel })
                    : t("appBuilder.choosingModel")
                }}
              </h3>
              <p class="ab-model-gallery-hint">
                {{ t("appBuilder.gallery.hint", { count: wb.cardsForSelection.value.length }) }}
              </p>
            </div>
            <!-- V1 parity (AppBuilderWorkbench.js:736-743): a task may have no
               models registered yet, or the registry may be empty entirely.
               Surface explicit empty states so the user gets actionable text
               instead of a silent gap. -->
            <div
              v-if="store.models.length === 0"
              class="ab-empty-state"
            >
              {{ t("appBuilder.empty") }}
            </div>
            <div
              v-else-if="wb.cardsForSelection.value.length === 0"
              class="ab-empty-state ab-empty-state--task"
            >
              {{ t("appBuilder.gallery.taskEmpty", { task: selectionLabel || t("appBuilder.task") }) }}
            </div>
            <ModelStrip
              v-else
              class="ab-model-gallery-strip"
              :selected-id="null"
              @select="onSelectModel"
              @info="onShowInfo"
            />
          </div>

          <!-- three-column stage -->
          <div
            v-else
            class="ab-content"
            :class="{ 'ab-left-collapsed': leftCollapsed, 'ab-right-collapsed': rightCollapsed }"
          >
            <!-- left rail (V1 AppBuilderWorkbench.js:874): shown when the
               input/params panel is collapsed; click to restore. -->
            <div
              v-if="leftCollapsed && !outputMaximized"
              class="ab-panel-rail ab-panel-rail-left"
              role="button"
              tabindex="0"
              :title="t('appBuilder.expandPanel')"
              data-testid="app-builder-rail-left"
              @click="leftCollapsed = false"
              @keydown.enter.prevent="leftCollapsed = false"
              @keydown.space.prevent="leftCollapsed = false"
            >
              ▶ {{ t("appBuilder.section.input") }}
            </div>

            <!-- left: input + params + run -->
            <section
              v-show="!leftCollapsed && !outputMaximized"
              class="ab-panel ab-panel-left"
            >
              <div class="ab-panel-body">
                <div class="ab-panel-head">
                  <h4 class="ab-section-h4">
                    {{ t("appBuilder.section.input") }}
                  </h4>
                  <button
                    type="button"
                    class="ab-panel-collapse-btn"
                    :title="t('appBuilder.collapsePanel')"
                    data-testid="app-builder-collapse-left"
                    @click="leftCollapsed = true"
                  >
                    ◀
                  </button>
                </div>
                <p
                  v-if="inputConstraintsHint"
                  class="ab-input-constraints"
                >
                  {{ inputConstraintsHint }}
                </p>
                <!-- Input dispatch by kind (V1 parity): audio → recorder/upload
                   dropzone, image → drag-drop dropzone, text → editor; any
                   other / schema-driven shape → the generic form fields. -->
                <AudioInput
                  v-if="inputKind === 'audio'"
                  :model-value="audioValue"
                  @update:model-value="setInputKey('audio', $event)"
                />
                <ImageDropzone
                  v-else-if="inputKind === 'image'"
                  :model-value="imageValue"
                  @update:model-value="setInputKey('image', $event)"
                />
                <TextEditor
                  v-else-if="inputKind === 'text'"
                  :model-value="textValue"
                  :constraints="textConstraints"
                  @update:model-value="setInputKey('text', $event)"
                />
                <DynamicInput
                  v-else-if="inputFields.length > 0"
                  :schema="inputFields"
                  :model-value="store.inputs"
                  @update:model-value="onInputUpdate"
                />
                <p
                  v-else
                  class="ab-input-constraints"
                >
                  {{ t("appBuilder.inputUnsupported") }}
                </p>
              </div>
              <details
                v-if="paramDefs.length > 0"
                class="ab-pane-footer ab-collapsible"
              >
                <summary>{{ t("appBuilder.advancedParams") }}</summary>
                <div class="ab-collapsible-body">
                  <DynamicParams
                    :params="paramDefs"
                    :model-value="store.params"
                    @update:model-value="onParamsUpdate"
                  />
                </div>
              </details>
              <div class="ab-panel-actions">
                <button
                  type="button"
                  class="ab-btn ab-btn-primary ab-run-btn"
                  :disabled="!store.canRun"
                  :aria-busy="isLive ? 'true' : 'false'"
                  :title="t('appBuilder.kbdRunHint')"
                  data-testid="app-builder-run"
                  @click="onRun"
                >
                  <span v-if="isLive">{{ t("appBuilder.running") }}</span>
                  <span v-else>{{ t("appBuilder.run") }}</span>
                </button>
                <button
                  v-if="isLive"
                  type="button"
                  class="ab-btn ab-btn-cancel"
                  data-testid="app-builder-cancel"
                  @click="onCancel"
                >
                  {{ t("appBuilder.cancel") }}
                </button>
                <button
                  v-if="store.weightsMissing"
                  type="button"
                  class="ab-weights-pill"
                  :title="t('appBuilder.weightsMissingTip')"
                  data-testid="app-builder-weights-missing"
                  @click="weightsDrawerOpen = true"
                >
                  <span aria-hidden="true">&#9888;</span>
                  {{ t("appBuilder.weightsMissing") }}
                </button>
              </div>
            </section>

            <!-- center: output -->
            <section
              class="ab-panel ab-panel-center"
              :class="{ 'ab-output-maximized': outputMaximized }"
            >
              <button
                type="button"
                class="ab-maximize-btn"
                :title="outputMaximized ? t('appBuilder.exitMaximize') : t('appBuilder.maximizeOutput')"
                data-testid="app-builder-maximize-output"
                @click="outputMaximized = !outputMaximized"
              >
                {{ outputMaximized ? "✕" : "⤢" }}
              </button>
              <div class="ab-panel-body ab-panel-result">
                <h4 class="ab-section-h4 ab-section-h4--output">
                  {{ t("appBuilder.section.output") }}
                  <span class="ab-section-h4-sep">·</span>
                  <span class="ab-section-h4-sub">{{ wb.outputSubtypeLabel.value }}</span>
                </h4>
                <DynamicOutput
                  :run="displayed"
                  :status="runStatus"
                  :subtype="wb.outputSubtypeLabel.value"
                  :resolve-url="(p: string) => resolveOutputUrl(p)"
                  @send-to-chat="onSendToChat"
                  @re-run="onRun"
                  @add-to-compare="onAddToCompare"
                />
              </div>
            </section>

            <!-- right: metrics + classification -->
            <section
              v-show="!rightCollapsed && !outputMaximized"
              class="ab-panel ab-panel-right"
            >
              <div class="ab-panel-body">
                <div class="ab-panel-head">
                  <h4 class="ab-section-h4">
                    {{ t("appBuilder.section.performance") }}
                  </h4>
                  <button
                    type="button"
                    class="ab-panel-collapse-btn"
                    :title="t('appBuilder.collapsePanel')"
                    data-testid="app-builder-collapse-right"
                    @click="rightCollapsed = true"
                  >
                    ▶
                  </button>
                </div>
                <MetricsView
                  v-if="metricsRows.length > 0 || store.runs.length > 0 || currentRunMetrics !== null"
                  :metrics="metricsRows"
                  :runs="store.runs"
                  :current-run="currentRunMetrics"
                  :model-id="store.selectedModelId"
                  :variant-id="store.selectedVariantId"
                  :rating="displayed?.rating ?? null"
                  :title="t('appBuilder.metrics.title')"
                />
                <div
                  v-else
                  class="ab-metrics-empty"
                >
                  {{ t("appBuilder.metrics.idle") }}
                </div>
                <!-- CLASSIFICATION block (V1 parity): Source / Group / Task /
                   Tags derived from the manifest + taxonomy. Reuses the V1
                   `.ab-classification-card` / `.ab-class-row` classes. -->
                <div
                  v-if="classificationRows.length > 0"
                  class="ab-classification-card"
                  data-testid="app-builder-classification"
                >
                  <h4 class="ab-section-h4">
                    {{ t("appBuilder.classification.title") }}
                  </h4>
                  <div
                    v-for="row in classificationRows"
                    :key="row.labelKey"
                    class="ab-class-row"
                  >
                    <span>{{ t(row.labelKey) }}</span>
                    <span class="ab-class-value ab-class-value--mono">{{ row.value }}</span>
                  </div>
                </div>
              </div>
            </section>

            <!-- right rail (V1 AppBuilderWorkbench.js:874): shown when the
               metrics/classification panel is collapsed; click to restore. -->
            <div
              v-if="rightCollapsed && !outputMaximized"
              class="ab-panel-rail ab-panel-rail-right"
              role="button"
              tabindex="0"
              :title="t('appBuilder.expandPanel')"
              data-testid="app-builder-rail-right"
              @click="rightCollapsed = false"
              @keydown.enter.prevent="rightCollapsed = false"
              @keydown.space.prevent="rightCollapsed = false"
            >
              ◀ {{ t("appBuilder.section.performance") }}
            </div>
          </div>
        </div>
      </div>

      <!-- ── info drawer ───────────────────────────────────────────────── -->
      <ModelInfoDrawer
        :open="drawerOpen"
        :model="drawerModel"
        :selected-variant-id="store.selectedVariantId"
        @close="drawerOpen = false"
        @select-variant="onSelectVariant"
        @apply-example="onApplyExample"
        @delete-model="onDeleteModel"
        @delete-variants="onDeleteVariants"
      />

      <!-- ── weights-missing drawer ────────────────────────────────────── -->
      <template v-if="weightsDrawerOpen && store.selectedModel && store.weightsMissing">
        <div
          class="ab-drawer-backdrop"
          @click="weightsDrawerOpen = false"
        ></div>
        <aside
          class="ab-info-drawer"
          role="dialog"
          aria-modal="true"
        >
          <header class="ab-drawer-header">
            <strong>{{ t("appBuilder.weightsMissing") }}</strong>
            <button
              type="button"
              class="ab-drawer-close"
              :title="t('appBuilder.close')"
              @click="weightsDrawerOpen = false"
            >
              ×
            </button>
          </header>
          <div class="ab-drawer-body">
            <p>
              {{ t("appBuilder.weightsMissingBody", { modelId: store.selectedModelId, installPath: weightsInstallPath }) }}
            </p>
            <pre class="ab-drawer-code">{{ weightsInstallPath }}</pre>
          </div>
        </aside>
      </template>

      <!-- ── run history modal ─────────────────────────────────────────── -->
      <div
        v-if="historyOpen && store.selectedModel"
        class="ab-history-overlay"
        data-testid="app-builder-history-modal"
        @click.self="historyOpen = false"
      >
        <div
          class="ab-history-modal"
          role="dialog"
          aria-modal="true"
        >
          <div class="ab-history-modal-header">
            <div class="ab-history-modal-title-wrap">
              <span class="ab-history-modal-title">{{ t("appBuilder.history.title") }}</span>
              <span class="ab-history-modal-subtitle">
                <span class="ab-history-modal-model">{{ selectedModelTitle }}</span>
                <span
                  v-if="selectedVariant && selectedVariant.id"
                  class="ab-history-modal-variant"
                >
                  {{ selectedVariant.label || selectedVariant.id }}
                  <span
                    v-if="selectedVariantQuant"
                    class="ab-history-modal-quant"
                  >
                    · {{ selectedVariantQuant }}
                  </span>
                </span>
              </span>
            </div>
            <button
              type="button"
              class="ab-history-modal-close"
              :title="t('appBuilder.history.close')"
              @click="historyOpen = false"
            >
              ×
            </button>
          </div>
          <div class="ab-history-modal-body">
            <HistoryPanel
              :runs="currentModelRuns"
              :scoped="true"
              :selected-model-title="selectedModelTitle"
              :selected-variant-label="selectedVariant?.label ?? ''"
              :selected-variant-quant="selectedVariantQuant"
              :selected-run-id="currentHistoryRunId"
              :is-loading="historyLoading"
              :error="historyError"
              @select-run="onSelectHistory"
              @delete-run="onDeleteHistoryRun"
              @refresh="onRefreshHistory"
              @export-run="onExportRun"
              @share-run="onShareRun"
              @add-to-compare="onAddToCompareFromHistory"
            />
          </div>
        </div>
      </div>

      <!-- ── edit Send-to-Chat prompt dialog: moved OUT of the
           `v-if="workbenchOpen"` host (see the teleported block at the end of
           the template) so it stays openable when the workbench is collapsed
           (A1 / Bug 1). ──────────────────────────────────────────────────── -->

      <!-- ── compare tray ──────────────────────────────────────────────── -->
      <CompareTray
        :is-open="store.compareOpen"
        :items="compareTrayItems"
        @close="store.compareOpen = false"
        @clear="store.clearCompare()"
        @remove-run="store.removeFromCompare($event)"
      />
    </div>
  </div>

  <!-- ── collapsed bar (V1 index.html:1359-1382 parity; shown when the
       workbench is collapsed instead of hidden entirely) ─────────────── -->
  <div
    v-else
    class="ab-collapsed-bar"
    data-testid="app-builder-collapsed-bar"
  >
    <span class="ab-collapsed-bar-text">
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
        style="opacity: 0.6"
      >
        <rect
          x="3"
          y="3"
          width="7"
          height="7"
          rx="1"
        /><rect
          x="14"
          y="3"
          width="7"
          height="7"
          rx="1"
        /><rect
          x="3"
          y="14"
          width="7"
          height="7"
          rx="1"
        /><rect
          x="14"
          y="14"
          width="7"
          height="7"
          rx="1"
        />
      </svg>
      <span>{{ t("appBuilder.collapsedHint") }}</span>
      <span
        v-if="store.selectedModel"
        class="ab-collapsed-bar-model"
      >— {{ store.selectedModel.title }}</span>
    </span>
    <button
      type="button"
      class="ab-collapsed-bar-btn"
      data-testid="app-builder-expand-panel"
      :title="t('appBuilder.expandPanel')"
      @click="workbenchOpen = true"
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <polyline points="18 15 12 9 6 15" />
      </svg>
      <span>{{ t("appBuilder.expandPanel") }}</span>
    </button>
    <button
      v-if="store.compareItems.length > 0"
      type="button"
      class="ab-collapsed-bar-btn"
      :title="t('appBuilder.compare.title')"
      @click="store.toggleCompare()"
    >
      {{ t("appBuilder.compare.title") }} ({{ store.compareItems.length }})
    </button>
  </div>

  <!-- ── edit Send-to-Chat prompt dialog (V1 index.html:1558-1584) ──────────
       A1 (Bug 1): teleported to <body> and controlled ONLY by
       `promptDialogOpen`, NOT nested inside the `v-if="workbenchOpen"` host.
       V1 rendered this dialog in the chat-input toolbar context (decoupled
       from the workbench collapse state), so the 提示词 button worked whether
       the workbench was expanded, collapsed, or auto-collapsed after a Send to
       Chat. Nesting it under `workbenchOpen` (the previous V2 bug) meant the
       dialog DOM did not exist while collapsed → clicking 提示词 did nothing. -->
  <Teleport to="body">
    <div
      v-if="promptDialogOpen"
      class="ab-prompt-dialog ab-prompt-dialog--floating"
      data-testid="app-builder-prompt-dialog"
    >
      <div class="ab-prompt-dialog-header">
        <span>{{ t("appBuilder.sendToChatPromptLabel") }}</span>
        <div class="ab-prompt-dialog-actions">
          <button
            type="button"
            class="ab-prompt-reset-btn"
            :title="t('appBuilder.resetPrompt')"
            @click="onResetPrompt"
          >
            ↺
          </button>
          <button
            type="button"
            class="ab-prompt-close-btn"
            @click="promptDialogOpen = false"
          >
            ✕
          </button>
        </div>
      </div>
      <textarea
        v-model="store.sendToChatPrompt"
        class="ab-prompt-dialog-textarea"
        :placeholder="t('appBuilder.sendToChatPromptPlaceholder')"
        rows="3"
      ></textarea>
      <div class="ab-prompt-dialog-hint">
        {{ t("appBuilder.promptHint") }}
      </div>
    </div>
  </Teleport>
</template>
