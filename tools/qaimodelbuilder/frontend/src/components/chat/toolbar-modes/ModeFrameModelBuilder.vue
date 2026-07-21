<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModeFrameModelBuilder — chat-input sub-toolbar for `model-build` mode.
 *
 * Full sub-workbench (block-3 spec §1): migrates the V1 model-build
 * controls (index.html:1597-1887) into the V2 chat composer toolbar:
 *
 *   1. Upload-model button   → POST /api/uploads/model
 *   2. Model file management → GET /api/uploads (filter category=model)
 *                              DELETE /api/uploads/{id}
 *   3. Quantization precision picker (7 options, block-3 spec §1.4)
 *   4. Dataset upload (file + directory) → POST /api/uploads/dataset
 *   5. Dataset file management → GET /api/uploads (category=dataset)
 *   6. Promote to App Builder → reuses PromoteToAppBuilderCard
 *      (V2-native /api/app-builder/import/{dry-run,commit} contract).
 *
 * Selected model_path / model_paths / quant_precision / dataset_path are
 * written to the active tab's `toolParams` via the store; the transport
 * (useChatTransport `deriveToolPayload` model-build branch) forwards them
 * as `tool_mode='model-build'` + `tool_params` on send.
 *
 * Upload endpoints return `{id, path, size}` (verified against the live
 * backend — block-3 spec §3.1); we keep `path` (server-side absolute
 * path) for the tool_params and track the `id` for delete.
 */
import { computed, ref, watch, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson, apiUpload, ApiError } from "@/api";
import { useToast } from "@/composables/useToast";
import {
  useChatTabsStore,
  QUANT_PRECISIONS_NEEDING_DATASET,
  type QuantPrecision,
} from "@/stores/chatTabs";
import { useAppBuilderStore } from "@/stores/appBuilder";
import { useForgeConfig } from "@/composables/useForgeConfig";
import {
  extractAllModelWorkdirsFromMessages,
} from "@/utils/modelWorkdir";
import { usePromoteReadyDetection } from "@/composables/usePromoteReadyDetection";
import { useModeFrameTriggers } from "@/composables/useModeFrameTriggers";
import PromoteToAppBuilderCard from "@/components/app-builder/model-builder/PromoteToAppBuilderCard.vue";

const { t } = useI18n();
const toast = useToast();
const store = useChatTabsStore();
const appBuilderStore = useAppBuilderStore();
// Configurable model workspace root (forge_config.workspace.model_root,
// default C:\WoS_AI). Read-only here — drives the conversation scan for the
// `<root>\<model>` workspace path. We `load()` once on mount so the value is
// populated; until then the scan falls back to the default root.
const { config: forgeConfig, load: loadForgeConfig } = useForgeConfig();
const workspaceModelRoot = computed<string>(() => {
  const ws = (forgeConfig.value as Record<string, unknown>)["workspace"];
  if (ws !== null && typeof ws === "object") {
    const root = (ws as Record<string, unknown>)["model_root"];
    if (typeof root === "string" && root.trim() !== "") return root;
  }
  return "";
});

const emit = defineEmits<{
  exit: [];
}>();

// ── Upload-record bookkeeping ───────────────────────────────────────────────
// Verified upload response shape (block-3 spec §3.1): { id, path, size }.
interface UploadResponse {
  id: string;
  path: string;
  size: number;
}
// Dataset upload also returns the server-side directory holding the blobs
// (V1 parity: backend owns the storage layout). `dir` is tail-appended.
interface DatasetUploadResponse extends UploadResponse {
  dir?: string;
  count?: number;
}
// Verified list shape: { uploads: [{ id, category, filename, size_bytes, path, created_at }] }.
interface UploadRecord {
  id: string;
  category: string;
  filename: string;
  size_bytes: number;
  path: string;
  created_at: string;
}
interface UploadListResponse {
  uploads: UploadRecord[];
}

// ── Active tab tool params (source of truth) ─────────────────────────────────
const activeTab = computed(() => store.activeTab);
const modelPath = computed<string>(
  () => activeTab.value?.toolParams.model_path ?? "",
);
const modelPaths = computed<string[]>(
  () => activeTab.value?.toolParams.model_paths ?? [],
);
const datasetPath = computed<string>(
  () => activeTab.value?.toolParams.dataset_path ?? "",
);
const precision = computed<QuantPrecision>(
  () => activeTab.value?.toolParams.quant_precision ?? "fp16",
);

function patchToolParams(
  patch: Partial<{
    model_path: string;
    model_paths: string[];
    quant_precision: QuantPrecision;
    dataset_path: string;
  }>,
): void {
  const tab = store.activeTab;
  if (tab !== null) store.setToolParams(tab.id, patch);
}

// G1 / V1 parity (app.js:1544-1612 activeConvId watch): when the conversation
// changes (session switch, or `/clear` → new session), refresh the model /
// dataset upload lists for the NEW conversation so switching back to a session
// shows the files uploaded there. Note V2 keeps per-tab `toolParams` in the
// store, so model_path / dataset_path / quant_precision already survive
// `/clear` (same tab) WITHOUT V1's manual snapshot-and-restore — an
// architectural improvement over V1's global-ref save/restore dance. Here we
// only reset the transient panel UI + reload the conv-scoped file lists; the
// list refreshers (M3) re-derive model_paths / datasetName from the server.
watch(
  () => activeTab.value?.conversationId ?? null,
  (next, prev) => {
    if (next === prev) return;
    // Reset transient panel UI (V1 reset block); toolParams persist in store.
    modelPanelOpen.value = false;
    datasetPanelOpen.value = false;
    precisionMenuOpen.value = false;
    datasetUploadMenuOpen.value = false;
    promotePanelOpen.value = false;
    // Names re-surface from the list refreshers when the new conv has files.
    if (modelPath.value === "") modelFileName.value = "";
    if (datasetPath.value === "") datasetName.value = "";
    void refreshModelFiles();
    void refreshDatasetFiles();
  },
);

// Load the current conversation's files when the toolbar first mounts (entering
// model-build mode), so a reopened session shows its uploads immediately.
onMounted(() => {
  void refreshModelFiles();
  void refreshDatasetFiles();
  // Populate forge config so the configurable workspace root is available
  // for the conversation scan (no-op if already loaded; shared singleton).
  void loadForgeConfig();
});

// ── Model upload ─────────────────────────────────────────────────────────────
const modelUploading = ref(false);
const modelFileName = ref("");
const modelPanelOpen = ref(false);
// id <-> path index so deletes can target the right upload record.
const modelUploads = ref<UploadRecord[]>([]);

async function handleModelFileSelect(e: Event): Promise<void> {
  const target = e.target as HTMLInputElement;
  const file = target.files?.[0];
  target.value = "";
  if (!file) return;
  modelFileName.value = file.name;
  modelUploading.value = true;
  try {
    const fd = new FormData();
    fd.append("file", file);
    const convId = activeTab.value?.conversationId;
    if (convId) fd.append("conv_id", convId);
    const resp = await apiUpload<UploadResponse>("/api/uploads/model", fd);
    // Append (multi-model parity — V1 modelBuildModelFiles).
    const nextPaths = [...modelPaths.value];
    if (!nextPaths.includes(resp.path)) nextPaths.push(resp.path);
    patchToolParams({ model_path: resp.path, model_paths: nextPaths });
    toast.success(t("index.uploadedToServer"));
    void refreshModelFiles();
  } catch (err) {
    toast.error(err instanceof ApiError ? err.message : String(err));
    // V1 parity (app.js:1672-1675): clear the pending filename on failure so
    // the toolbar doesn't show a name for a model that never uploaded.
    if (modelPath.value === "") modelFileName.value = "";
  } finally {
    modelUploading.value = false;
  }
}

async function refreshModelFiles(): Promise<void> {
  try {
    const convId = activeTab.value?.conversationId;
    const url = convId
      ? `/api/uploads?conv_id=${encodeURIComponent(convId)}`
      : "/api/uploads";
    const resp = await apiJson<UploadListResponse>("GET", url);
    modelUploads.value = resp.uploads.filter((u) => u.category === "model");
    // M3 / V1 parity (app.js:1791-1796): keep model_paths in sync with the
    // server list — V1 derived model_paths from the list on every change so
    // switching back to a session showed its previously-uploaded models. We
    // backfill any list paths missing from toolParams (e.g. after a session
    // switch) and surface a model_path / filename when none is selected yet.
    const listPaths = modelUploads.value.map((u) => u.path);
    if (listPaths.length > 0) {
      const merged = [...modelPaths.value];
      for (const p of listPaths) if (!merged.includes(p)) merged.push(p);
      const patch: { model_paths: string[]; model_path?: string } = {
        model_paths: merged,
      };
      if (modelPath.value === "") {
        patch.model_path = listPaths[0];
        const firstRec = modelUploads.value[0];
        if (firstRec) modelFileName.value = firstRec.filename;
      }
      patchToolParams(patch);
    }
  } catch (err) {
    if (err instanceof ApiError) toast.error(err.message);
  }
}

async function deleteModelFile(rec: UploadRecord): Promise<void> {
  try {
    await apiJson("DELETE", `/api/uploads/${encodeURIComponent(rec.id)}`);
    modelUploads.value = modelUploads.value.filter((u) => u.id !== rec.id);
    const remaining = modelPaths.value.filter((p) => p !== rec.path);
    const nextPath =
      modelPath.value === rec.path ? (remaining[0] ?? "") : modelPath.value;
    patchToolParams({ model_path: nextPath, model_paths: remaining });
    if (remaining.length === 0) {
      modelFileName.value = "";
      modelPanelOpen.value = false;
    }
  } catch (err) {
    if (err instanceof ApiError) toast.error(err.message);
  }
}

function toggleModelPanel(): void {
  modelPanelOpen.value = !modelPanelOpen.value;
  if (modelPanelOpen.value) void refreshModelFiles();
}

// ── Quantization precision ───────────────────────────────────────────────────
const precisionMenuOpen = ref(false);
const precisionOptions: ReadonlyArray<{
  value: QuantPrecision;
  labelKey: string;
  descKey: string;
}> = [
  { value: "fp32", labelKey: "index.quantFp32", descKey: "index.quantFp32Desc" },
  { value: "fp16", labelKey: "index.quantFp16", descKey: "index.quantFp16Desc" },
  { value: "w8a16", labelKey: "index.quantW8a16", descKey: "index.quantW8a16Desc" },
  { value: "w8a8", labelKey: "index.quantW8a8", descKey: "index.quantW8a8Desc" },
  { value: "w8a8b8", labelKey: "index.quantW8a8b8", descKey: "index.quantW8a8b8Desc" },
  { value: "w4a16", labelKey: "index.quantW4a16", descKey: "index.quantW4a16Desc" },
  { value: "w4a8", labelKey: "index.quantW4a8", descKey: "index.quantW4a8Desc" },
];

const precisionLabel = computed(() => {
  const found = precisionOptions.find((o) => o.value === precision.value);
  return found ? t(found.labelKey) : precision.value;
});

const needsDataset = computed(() =>
  QUANT_PRECISIONS_NEEDING_DATASET.has(precision.value),
);

function selectPrecision(value: QuantPrecision): void {
  patchToolParams({ quant_precision: value });
  precisionMenuOpen.value = false;
}

function togglePrecisionMenu(): void {
  precisionMenuOpen.value = !precisionMenuOpen.value;
}

// ── Dataset upload ───────────────────────────────────────────────────────────
const datasetUploading = ref(false);
const datasetUploadMenuOpen = ref(false);
const datasetName = ref("");
const datasetPanelOpen = ref(false);
const datasetUploads = ref<UploadRecord[]>([]);
const datasetFileInput = ref<HTMLInputElement | null>(null);
const datasetDirInput = ref<HTMLInputElement | null>(null);

async function handleDatasetFiles(e: Event, isDir: boolean): Promise<void> {
  const target = e.target as HTMLInputElement;
  const files = Array.from(target.files ?? []);
  target.value = "";
  datasetUploadMenuOpen.value = false;
  // Directory uploads include sub-dir entries (size 0, no type) — skip them
  // (V1 handleDatasetDirSelect parity).
  const real = isDir
    ? files.filter((f) => !(f.size === 0 && f.type === ""))
    : files;
  if (real.length === 0) return;
  datasetUploading.value = true;
  try {
    let lastPath = datasetPath.value;
    let lastDir = "";
    let uploaded = 0;
    // The verified endpoint takes ONE `file` per call → upload sequentially
    // (block-3 spec §3.1). The dataset_path is the dir of the saved blobs.
    const convId = activeTab.value?.conversationId;
    for (const f of real) {
      const fd = new FormData();
      fd.append("file", f);
      if (convId) fd.append("conv_id", convId);
      const resp = await apiUpload<DatasetUploadResponse>(
        "/api/uploads/dataset",
        fd,
      );
      lastPath = resp.path;
      if (resp.dir) lastDir = resp.dir;
      uploaded += 1;
    }
    // dataset_path = the server-side directory holding the uploaded dataset
    // blobs. Prefer the backend-reported `dir` (V1 parity: the store owns the
    // layout); fall back to slicing the last file path for older responses.
    let datasetDir = lastDir;
    if (datasetDir === "") {
      const dirIdx = Math.max(
        lastPath.lastIndexOf("/"),
        lastPath.lastIndexOf("\\"),
      );
      datasetDir = dirIdx > 0 ? lastPath.slice(0, dirIdx) : lastPath;
    }
    patchToolParams({ dataset_path: datasetDir });
    datasetName.value =
      uploaded === 1 && real[0]
        ? real[0].name
        : t("index.nFilesSuffix", { n: uploaded });
    toast.success(t("index.uploadedToServer"));
    void refreshDatasetFiles();
  } catch (err) {
    toast.error(err instanceof ApiError ? err.message : String(err));
  } finally {
    datasetUploading.value = false;
  }
}

async function refreshDatasetFiles(): Promise<void> {
  try {
    const convId = activeTab.value?.conversationId;
    const url = convId
      ? `/api/uploads?conv_id=${encodeURIComponent(convId)}`
      : "/api/uploads";
    const resp = await apiJson<UploadListResponse>("GET", url);
    datasetUploads.value = resp.uploads.filter((u) => u.category === "dataset");
    // V1 parity (app.js:refreshDatasetFiles): when a session has a dataset on
    // the server but the toolbar has no datasetName / dataset_path yet
    // (typical session switch), surface them from the list so the badge shows
    // "N files" / the dataset is wired into tool_params for the next send.
    if (datasetUploads.value.length > 0 && datasetName.value === "") {
      datasetName.value =
        datasetUploads.value.length === 1
          ? (datasetUploads.value[0]?.filename ?? "")
          : t("index.nFilesSuffix", { n: datasetUploads.value.length });
      if (datasetPath.value === "") {
        const lastPath = datasetUploads.value[0]?.path ?? "";
        const dirIdx = Math.max(
          lastPath.lastIndexOf("/"),
          lastPath.lastIndexOf("\\"),
        );
        const datasetDir = dirIdx > 0 ? lastPath.slice(0, dirIdx) : lastPath;
        if (datasetDir !== "") patchToolParams({ dataset_path: datasetDir });
      }
    }
  } catch (err) {
    if (err instanceof ApiError) toast.error(err.message);
  }
}

async function deleteDatasetFile(rec: UploadRecord): Promise<void> {
  try {
    await apiJson("DELETE", `/api/uploads/${encodeURIComponent(rec.id)}`);
    datasetUploads.value = datasetUploads.value.filter((u) => u.id !== rec.id);
    if (datasetUploads.value.length === 0) {
      patchToolParams({ dataset_path: "" });
      datasetName.value = "";
      datasetPanelOpen.value = false;
    }
  } catch (err) {
    if (err instanceof ApiError) toast.error(err.message);
  }
}

function toggleDatasetUploadMenu(): void {
  datasetUploadMenuOpen.value = !datasetUploadMenuOpen.value;
}

function toggleDatasetPanel(): void {
  datasetPanelOpen.value = !datasetPanelOpen.value;
  if (datasetPanelOpen.value) void refreshDatasetFiles();
}

// ── Promote to App Builder ───────────────────────────────────────────────────
const promotePanelOpen = ref(false);

// SINGLE SOURCE OF TRUTH for "which model is promotable" — shared with the
// ChatView promote-ready notice. usePromoteReadyDetection pulls ALL
// `<root>\<model>` candidates from the conversation and picks the FIRST one
// that ACTUALLY has precision variants (.bin/.dlc) on disk. The Promote CARD
// and the notice MUST agree on the workdir; previously the card used its own
// "modelPath || last-mentioned path" logic while the notice used verified
// detection — so the notice could say "inception_v3 ready (2 variants)" while
// the card opened on "yolov8" (last mentioned, no variants). They now share
// this one detection.
const promoteDetection = usePromoteReadyDetection();

// Session model workdir fed to the promote card. Priority:
//   1. promoteDetection.detectedWorkdir — a candidate VERIFIED to have
//      precision variants on disk. This is the MOST trustworthy source and
//      is IDENTICAL to what the promote-ready notice shows, so notice + card
//      always agree. It MUST come first: previously `model_path` won
//      unconditionally, so if the agent set `model_path` to an ONNX source
//      dir (e.g. C:\WoS_AI\yolov8\model.onnx → dir "yolov8") that has NO
//      output/ variants — or doesn't even exist — the card opened on that
//      empty/nonexistent dir while the notice (and Model Hub, which has no
//      model_path branch) correctly pointed at the real converted workdir
//      (e.g. yolov8_det). Verified-variants-first fixes that divergence.
//   2. the uploaded model_path's directory — the manual-upload source, used
//      when nothing has been converted yet (detectedWorkdir still empty), so
//      "upload then promote" keeps working before any variant exists.
//   3. FALLBACK — the most-recently-referenced candidate even without
//      variants, so the card can still scan it and surface the
//      "un-normalized model — normalize it" guidance instead of a bare
//      "no workspace".
const sessionModelWorkdir = computed(() => {
  const verified = promoteDetection.detectedWorkdir.value;
  if (verified !== "") return verified;
  const p = modelPath.value;
  if (p !== "") {
    const idx = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
    if (idx > 0) return p.slice(0, idx);
  }
  const candidates = extractAllModelWorkdirsFromMessages(
    activeTab.value?.messages,
    workspaceModelRoot.value || undefined,
  );
  return candidates[0] ?? "";
});

function togglePromotePanel(): void {
  promotePanelOpen.value = !promotePanelOpen.value;
}

// ── Cross-component trigger from ModeIntroCard ──────────────────────────────
// The intro card's "Promote to App Builder" chip in Model Builder mode
// requests this popover via `useModeFrameTriggers.requestOpenPromote`. We
// only react when Model Builder is the ACTIVE mode — otherwise the App
// Builder mode-frame owns its own Promote panel (see ModeFrameAppBuilder).
const { openPromoteToken } = useModeFrameTriggers();
watch(openPromoteToken, () => {
  if (activeTab.value?.activeMode === "model-build") {
    promotePanelOpen.value = true;
  }
});

// "Ready" badge on the Promote button — visually strengthens the affordance
// when a promote-able model workdir has been detected (either from an
// uploaded model path or by scanning the conversation for a `<root>\<model>`
// path). Purely cosmetic; the actual eligibility check still runs inside
// PromoteToAppBuilderCard on click, so a false-positive dot is harmless.
// "Ready" badge on the Promote button. ON when either:
//   * a model was uploaded via the button (modelPath set — the manual source),
//     OR
//   * usePromoteReadyDetection VERIFIED precision variants on disk for a
//     conversation candidate (identical signal to the promote-ready notice, so
//     dot + notice + card all agree). No longer merely "some path string
//     exists" (State-Truth-First).
const promoteReady = computed<boolean>(
  () =>
    modelPath.value !== "" ||
    promoteDetection.detectedVariants.value.length > 0,
);

function onPromoteImported(): void {
  promotePanelOpen.value = false;
  toast.success(t("modelBuilder.promote.importSuccess"));
  // V1 parity (index.html:1872): @imported callback reloads the model
  // registry, then — if the user happened to be viewing a model in App
  // Builder — re-selects it so the freshly imported data shows (V1
  // `if (selectedModelId === modelId) selectVariant(null)`). The card doesn't
  // forward the imported id, so we conservatively re-select the currently
  // selected model after the refresh to surface its updated variants.
  void appBuilderStore.fetchModels().then(() => {
    const sel = appBuilderStore.selectedModelId;
    if (typeof sel === "string" && sel !== "") {
      appBuilderStore.selectModel(sel);
    }
  });
}

// ── Misc ─────────────────────────────────────────────────────────────────────
function fmtSize(bytes: number): string {
  if (bytes > 1048576) return `${(bytes / 1048576).toFixed(1)}MB`;
  if (bytes > 1024) return `${(bytes / 1024).toFixed(0)}KB`;
  return `${bytes}B`;
}

function onExit(): void {
  // Clear all model-build sub-state (V1 exit-badge parity).
  patchToolParams({
    model_path: "",
    model_paths: [],
    dataset_path: "",
    quant_precision: "fp16",
  });
  modelFileName.value = "";
  datasetName.value = "";
  modelPanelOpen.value = false;
  datasetPanelOpen.value = false;
  promotePanelOpen.value = false;
  emit("exit");
}
</script>

<template>
  <div
    class="rit-left mb-frame"
    data-testid="mode-frame-model-build"
  >
    <button
      type="button"
      class="rit-mode-badge"
      data-testid="mode-frame-exit"
      @click="onExit"
    >
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <path d="M12 2L2 7l10 5 10-5-10-5z" />
        <path d="M2 17l10 5 10-5" />
        <path d="M2 12l10 5 10-5" />
      </svg>
      <span>{{ t("index.modelBuilder") }}</span>
      <span class="rit-close">✕</span>
    </button>

    <span class="rit-sep"></span>

    <!-- 1. 上传模型文件 (.pt / .pth / .onnx)（V1 index.html:1610-1640） -->
    <label
      class="rit-btn rit-model-upload"
      :class="{
        'rit-model-upload--active': modelPath !== '',
        'rit-model-upload--uploading': modelUploading,
      }"
      :title="
        modelPath
          ? t('index.uploadedPathTitle', { name: modelFileName, path: modelPath })
          : t('index.uploadModelHint')
      "
      data-testid="mb-upload-model"
    >
      <svg
        v-if="modelUploading"
        class="rit-spin"
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      ><path d="M21 12a9 9 0 1 1-6.219-8.56" /></svg>
      <svg
        v-else
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      ><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line
        x1="12"
        y1="3"
        x2="12"
        y2="15"
      /></svg>
      <span v-if="modelUploading">{{ t("index.uploadingDots") }}</span>
      <span v-else>{{ t("index.uploadModel") }}</span>
      <span
        v-if="modelPath !== '' && !modelUploading"
        class="rit-upload-ok"
        :title="t('index.uploadedToServer')"
      >✓</span>
      <input
        type="file"
        accept=".pt,.pth,.onnx"
        style="display: none"
        :disabled="modelUploading"
        data-testid="mb-model-input"
        @change="handleModelFileSelect"
      />
    </label>

    <!-- 2. 模型文件管理（有上传路径时显示）（V1 index.html:1642-1688） -->
    <template v-if="modelPath !== ''">
      <span class="rit-sep"></span>
      <div class="rit-submenu-wrap">
        <button
          type="button"
          class="rit-btn"
          :class="{ 'rit-model-upload--active': modelPanelOpen }"
          data-testid="mb-toggle-model-panel"
          :title="t('index.manageUploadedModels')"
          @click="toggleModelPanel"
        >
          <svg
            width="13"
            height="13"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          ><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line
            x1="12"
            y1="3"
            x2="12"
            y2="15"
          /></svg>
          <span>{{ t("index.nModelsSuffix", { n: modelUploads.length }) }}</span>
          <svg
            width="10"
            height="10"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <polyline
              v-if="modelPanelOpen"
              points="18 15 12 9 6 15"
            />
            <polyline
              v-else
              points="6 9 12 15 18 9"
            />
          </svg>
        </button>
        <div
          v-if="modelPanelOpen"
          class="rit-submenu rit-submenu--wide rit-dataset-panel"
          data-testid="mb-model-panel"
        >
          <div
            class="rit-submenu-header"
            style="display: flex; align-items: center; justify-content: space-between"
          >
            <span>{{ t("index.uploadedModelsCountHeader", { n: modelUploads.length }) }}</span>
            <button
              type="button"
              class="rit-dataset-refresh"
              :title="t('index.refreshFileList')"
              @click.stop="refreshModelFiles()"
            >
              ↺
            </button>
          </div>
          <div
            v-for="f in modelUploads"
            :key="f.id"
            class="rit-dataset-file-row"
          >
            <span
              class="rit-dataset-file-name"
              :title="f.filename"
            >{{ f.filename }}</span>
            <span class="rit-dataset-file-size">{{ fmtSize(f.size_bytes) }}</span>
            <button
              type="button"
              class="rit-dataset-file-del"
              :title="t('index.deleteThisFile')"
              @click.stop="deleteModelFile(f)"
            >
              ✕
            </button>
          </div>
          <div
            v-if="modelUploads.length === 0"
            style="padding: 8px 12px; font-size: var(--text-sm); color: var(--text-muted)"
          >
            {{ t("index.noFilesClickRefresh") }}
          </div>
        </div>
        <div
          v-if="modelPanelOpen"
          class="dropdown-overlay"
          @click="modelPanelOpen = false"
        ></div>
      </div>
    </template>

    <!-- 3. 量化精度下拉（V1 index.html:1690-1723） -->
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        :aria-expanded="precisionMenuOpen"
        data-testid="mb-toggle-precision"
        @click="togglePrecisionMenu"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><circle
          cx="12"
          cy="12"
          r="3"
        /><path d="M19.07 4.93a10 10 0 0 1 0 14.14" /><path d="M4.93 4.93a10 10 0 0 0 0 14.14" /></svg>
        <span>{{ t("index.precisionLabel", { p: precisionLabel }) }}</span>
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline
            v-if="precisionMenuOpen"
            points="18 15 12 9 6 15"
          />
          <polyline
            v-else
            points="6 9 12 15 18 9"
          />
        </svg>
      </button>
      <div
        v-if="precisionMenuOpen"
        class="rit-submenu rit-submenu--wide"
        role="menu"
      >
        <div class="rit-submenu-header">
          {{ t("index.quantPrecision") }}
        </div>
        <div
          v-for="opt in precisionOptions"
          :key="opt.value"
          class="rit-submenu-item"
          :class="{ active: precision === opt.value }"
          :data-testid="`mb-precision-${opt.value}`"
          role="menuitem"
          @click="selectPrecision(opt.value)"
        >
          <div class="rit-submenu-item-body">
            <div class="rit-submenu-item-label">
              <span
                class="rit-quant-tag"
                :class="`rit-quant-tag--${opt.value}`"
              >{{ t(opt.labelKey) }}</span>
            </div>
            <div class="rit-submenu-item-desc">
              {{ t(opt.descKey) }}
            </div>
          </div>
          <span
            v-if="precision === opt.value"
            class="rit-submenu-check"
          >✓</span>
        </div>
      </div>
      <div
        v-if="precisionMenuOpen"
        class="dropdown-overlay"
        @click="precisionMenuOpen = false"
      ></div>
    </div>

    <!-- 4. 数据集上传 + 管理（V1 index.html:1725-1807） -->
    <span class="rit-sep"></span>
    <div class="rit-submenu-wrap">
      <label
        class="rit-btn rit-model-upload"
        :class="{
          'rit-model-upload--active': datasetPath !== '',
          'rit-model-upload--uploading': datasetUploading,
        }"
        :title="
          datasetPath
            ? t('index.uploadedDatasetTitle', { name: datasetName, path: datasetPath })
            : t('index.datasetEmptyHint')
        "
        data-testid="mb-toggle-dataset-upload"
        @click.prevent="toggleDatasetUploadMenu"
      >
        <svg
          v-if="datasetUploading"
          class="rit-spin"
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><path d="M21 12a9 9 0 1 1-6.219-8.56" /></svg>
        <svg
          v-else
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></svg>
        <span v-if="datasetUploading">{{ t("index.uploadingDots") }}</span>
        <span
          v-else-if="datasetName !== ''"
          class="rit-model-filename"
          :title="datasetName"
        >{{ datasetName }}</span>
        <span v-else>{{ t("index.dataset") }}</span>
        <span
          v-if="needsDataset && datasetPath === '' && !datasetUploading"
          class="rit-dataset-required"
          :title="t('index.datasetRequiredHint')"
        >*</span>
        <span
          v-if="datasetPath !== '' && !datasetUploading"
          class="rit-upload-ok"
          :title="t('index.uploadedToServer')"
        >✓</span>
      </label>
      <div
        v-if="datasetUploadMenuOpen"
        class="rit-submenu rit-submenu--wide"
        role="menu"
      >
        <div class="rit-submenu-header">
          {{ t("index.selectDatasetUploadMethod") }}
        </div>
        <div
          class="rit-submenu-item"
          style="cursor: pointer"
          role="menuitem"
          data-testid="mb-dataset-upload-files"
          @click.stop="datasetFileInput?.click()"
        >
          <div class="rit-submenu-item-body">
            <div class="rit-submenu-item-label">
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                style="margin-right: 4px; vertical-align: middle"
              ><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line
                x1="12"
                y1="3"
                x2="12"
                y2="15"
              /></svg>
              {{ t("index.uploadFile") }}
            </div>
            <div class="rit-submenu-item-desc">
              {{ t("index.uploadFileMultiHint") }}
            </div>
          </div>
        </div>
        <div
          class="rit-submenu-item"
          style="cursor: pointer"
          role="menuitem"
          data-testid="mb-dataset-upload-dir"
          @click.stop="datasetDirInput?.click()"
        >
          <div class="rit-submenu-item-body">
            <div class="rit-submenu-item-label">
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                style="margin-right: 4px; vertical-align: middle"
              ><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></svg>
              {{ t("index.selectDirectory") }}
            </div>
            <div class="rit-submenu-item-desc">
              {{ t("index.selectDirHint") }}
            </div>
          </div>
        </div>
      </div>
      <div
        v-if="datasetUploadMenuOpen"
        class="dropdown-overlay"
        @click="datasetUploadMenuOpen = false"
      ></div>
      <input
        ref="datasetFileInput"
        type="file"
        multiple
        accept=".zip,.tar,.gz,.tgz,.jpg,.jpeg,.png,.bmp,.webp,.tiff,.tif,.npy,.npz,.json,.jsonl,.txt,.csv"
        style="display: none"
        :disabled="datasetUploading"
        @change="(e) => handleDatasetFiles(e, false)"
      />
      <input
        ref="datasetDirInput"
        type="file"
        multiple
        webkitdirectory
        style="display: none"
        :disabled="datasetUploading"
        @change="(e) => handleDatasetFiles(e, true)"
      />
    </div>

    <!-- 5. 数据集文件管理（有上传路径时显示）（V1 index.html:1809-1852） -->
    <template v-if="datasetPath !== ''">
      <span class="rit-sep"></span>
      <div class="rit-submenu-wrap">
        <button
          type="button"
          class="rit-btn"
          :class="{ 'rit-model-upload--active': datasetPanelOpen }"
          data-testid="mb-toggle-dataset-panel"
          :title="t('index.manageDatasetFiles')"
          @click="toggleDatasetPanel"
        >
          <svg
            width="13"
            height="13"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          ><line
            x1="8"
            y1="6"
            x2="21"
            y2="6"
          /><line
            x1="8"
            y1="12"
            x2="21"
            y2="12"
          /><line
            x1="8"
            y1="18"
            x2="21"
            y2="18"
          /><line
            x1="3"
            y1="6"
            x2="3.01"
            y2="6"
          /><line
            x1="3"
            y1="12"
            x2="3.01"
            y2="12"
          /><line
            x1="3"
            y1="18"
            x2="3.01"
            y2="18"
          /></svg>
          <span>{{ t("index.nFilesSuffix", { n: datasetUploads.length }) }}</span>
          <svg
            width="10"
            height="10"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <polyline
              v-if="datasetPanelOpen"
              points="18 15 12 9 6 15"
            />
            <polyline
              v-else
              points="6 9 12 15 18 9"
            />
          </svg>
        </button>
        <div
          v-if="datasetPanelOpen"
          class="rit-submenu rit-submenu--wide rit-dataset-panel"
          data-testid="mb-dataset-panel"
        >
          <div
            class="rit-submenu-header"
            style="display: flex; align-items: center; justify-content: space-between"
          >
            <span>{{ t("index.datasetFilesCountHeader", { n: datasetUploads.length }) }}</span>
            <button
              type="button"
              class="rit-dataset-refresh"
              :title="t('index.refreshFileList')"
              @click.stop="refreshDatasetFiles()"
            >
              ↺
            </button>
          </div>
          <div
            v-for="f in datasetUploads"
            :key="f.id"
            class="rit-dataset-file-row"
          >
            <span
              class="rit-dataset-file-name"
              :title="f.filename"
            >{{ f.filename }}</span>
            <span class="rit-dataset-file-size">{{ fmtSize(f.size_bytes) }}</span>
            <button
              type="button"
              class="rit-dataset-file-del"
              :title="t('index.deleteThisFile')"
              @click.stop="deleteDatasetFile(f)"
            >
              ✕
            </button>
          </div>
        </div>
        <div
          v-if="datasetPanelOpen"
          class="dropdown-overlay"
          @click="datasetPanelOpen = false"
        ></div>
      </div>
    </template>

    <!-- 6. Promote to App Builder（V1 index.html:1854-1876） -->
    <span class="rit-sep"></span>
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        :class="{
          'rit-model-upload--active': promotePanelOpen,
          'mb-promote-btn--ready': promoteReady,
        }"
        :title="t('modelBuilder.promote.title')"
        data-testid="mb-toggle-promote"
        @click="togglePromotePanel"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><path d="M4 14h6v6H4z" /><path d="M14 4h6v6h-6z" /><path d="M7 14V7h7" /><polyline points="14 10 7 10" /></svg>
        <span>{{ t("modelBuilder.promote.title") }}</span>
        <!-- Ready dot (§14 UX): a subtle 6px accent dot when a promote-able
             model workdir has been detected in the chat / upload. Draws the
             eye without shouting; purely cosmetic. -->
        <span
          v-if="promoteReady"
          class="mb-promote-ready-dot"
          role="status"
          :aria-label="t('modelBuilder.promote.readyBadgeAria')"
        ></span>
      </button>
      <div
        v-show="promotePanelOpen"
        class="rit-submenu rit-submenu--wide"
        style="min-width: 400px; max-height: 500px; overflow-y: auto"
        data-testid="mb-promote-panel"
      >
        <PromoteToAppBuilderCard
          :session-model-workdir="sessionModelWorkdir"
          @imported="onPromoteImported"
        />
      </div>
      <div
        v-if="promotePanelOpen"
        class="dropdown-overlay"
        @click="promotePanelOpen = false"
      ></div>
    </div>
  </div>
</template>

<style scoped>
.mb-frame {
  position: relative;
  flex-wrap: wrap;
}

/* "Ready" dot on the Promote button — accent-colored 6px pill anchored top-
   right of the button. Purely CSS + theme tokens; no i18n text needed
   (aria-label on the dot conveys meaning to AT). */
.mb-promote-ready-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #6d5efc);
  box-shadow: 0 0 0 2px var(--bg-secondary, #1c1c22);
  margin-left: 2px;
  flex: 0 0 auto;
}
</style>
