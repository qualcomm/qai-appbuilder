// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * App Builder Pinia store.
 *
 * S5 PR-055: wraps /api/app-builder/* routes via apiJson.
 * Block-4 (Mode sub-workbench): extends the store into the full V1
 * `useAppBuilder.js` singleton surface so the App Builder chat mode is a
 * usable workbench backed by the real backend:
 *
 *   - taxonomy / schema fetch (GET /taxonomy, GET /models/{id}/schema)
 *   - two-stage streaming Run: POST /runs (id) → GET /runs/{id}/stream (SSE)
 *   - run cancel = DELETE /runs/{id} + abort stream
 *   - run history list (GET /runs?limit&offset) + metrics (GET /metrics/{id})
 *   - artifacts (GET /runs/{id}/artifacts) + blob URL helper
 *   - selectedModel / selectedVariant + per-model inputs/params buckets
 *   - compare tray (front-end only, FIFO 4) + snapshot view
 *   - send-to-chat prompt + tool-params projection
 *
 * Backend contract notes (verified 2026-06-01 against
 * interfaces/http/routes/app_builder.py + use_cases/run_app.py):
 *   - RunCreateRequest = { model_id, inputs }.  variantId / params /
 *     options are NOT top-level fields; the runner reads them OUT of the
 *     `inputs` bag: `inputs.variant_id` (str) and `inputs.params` (dict).
 *     So we fold variant + params + options into `inputs` on create.
 *   - SSE frame contract (app_builder): event names are
 *     `state` | `frame` | `error` | `done` (NOT V1's status/progress/...).
 *     `state.status` is a RunStatus value (pending/running/streaming/
 *     completed/failed/cancelled).  `frame.payload` carries the runner's
 *     output chunk.
 *   - install status: `AppModelResponse.status` (Ready / NotInstalled /
 *     Error — weights present on disk?) + `deps_status` (ready / missing /
 *     installing) + `variant_status[]` are the V1-parity install-state fields
 *     (V1 `GET /api/appbuilder/models` augmented rows). A model that cannot be
 *     run is also reflected as `enabled === false` (domain
 *     `is_runnable === enabled`).
 */
import { defineStore } from "pinia";
import { ref, computed, reactive } from "vue";
import { apiJson, apiWsStream } from "@/api";
import { peekAbortController } from "@/stores/chatTabs";
import {
  COMPARE_MAX,
  deriveCategoryCode,
  extractRequiredFields,
  isTerminal,
  summariseOutput,
} from "@/stores/_appBuilderHelpers";
import { applyFrameToRun } from "@/stores/app-builder/frames";
import { readLastModel, writeLastModel } from "@/stores/app-builder/persistence";
import {
  hydrateHistoryRun,
  mergeHistoryRuns,
} from "@/stores/app-builder/history";
import {
  artifactBlobUrl as buildArtifactBlobUrl,
  buildCompareItem,
} from "@/stores/app-builder/compare";
import type {
  AppModelResponse,
  AppModelListResponse,
  RunResponse,
  ArtifactListResponse,
  WorkerStatusResponse,
  PackManifestResponse,
  ModelSchemaResponse,
  RunsListResponse,
  TaxonomyNodeResponse,
  TaxonomyTreeResponse,
  RunMetricsResponse,
  RunStateFrame,
  RunStreamFrame,
  AppRun,
  CompareItem,
  AppBuilderToolParams,
  AppEntry,
  AppListResponse,
  AppDetailResponse,
  RunAppRequest,
  RunAppResponse,
  AppLogsResponse,
  AppRunState,
  PackageJobResponse,
  PackageState,
} from "@/stores/app-builder/types";
import { ApiError } from "@/api";
import type { DepsProgressEntry } from "@/components/app-builder/types";

/**
 * Per-model weight-download state machine (App Builder model-strip DOWNLOAD UI).
 *
 * Backing the "Download → live progress bar → ✓ ready" row affordance:
 *   POST /api/app-builder/weights/download → { job_id }, then
 *   GET  .../weights/download/{job_id}/progress (SSE `progress`/`done`/`error`).
 *
 * `status` machine: idle → downloading → (extracting) → done | error.
 * `extracting` is derived from a `progress` frame's `is_complete === true`
 * (bytes finished; server is unpacking just before `event: done`).
 *
 * The live `AbortController` is kept on the reactive object so the row's
 * cancel ✕ can abort the in-flight SSE stream (and best-effort DELETE the job).
 * Holding it here (rather than a side map) keeps cancel a pure per-id lookup.
 */
export interface DownloadState {
  jobId: string | null;
  status: "idle" | "downloading" | "extracting" | "done" | "error";
  percent: number | null;
  speedBps: number;
  etaSeconds: number | null;
  bytesDownloaded: number;
  totalBytes: number | null;
  error: string | null;
  controller: AbortController | null;
}

/** A fresh, inert download slot (status "idle"). */
function makeIdleDownloadState(): DownloadState {
  return {
    jobId: null,
    status: "idle",
    percent: null,
    speedBps: 0,
    etaSeconds: null,
    bytesDownloaded: 0,
    totalBytes: null,
    error: null,
    controller: null,
  };
}

/** Shape of a `progress` SSE frame's parsed `data` (defensive-narrowed). */
interface WeightsProgressFrame {
  bytes_downloaded?: unknown;
  total_bytes?: unknown;
  speed_bps?: unknown;
  eta_seconds?: unknown;
  percent?: unknown;
  is_complete?: unknown;
}

/** `POST .../weights/download` response envelope. */
interface WeightsDownloadStartResponse {
  job_id: string;
}

/** Shape of a package `progress` SSE frame's parsed `data` (defensive-narrowed). */
interface PackageProgressFrame {
  phase?: unknown;
  percent?: unknown;
  message?: unknown;
  size_bytes?: unknown;
  zip_path?: unknown;
  is_complete?: unknown;
}

/** Narrow `unknown` → finite number, else `fallback`. */
function asFiniteNumber(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

/** Narrow `unknown` → finite number or null (for nullable numeric fields). */
function asNullableNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// Re-export the store's public types so existing consumers can keep importing
// them from "@/stores/appBuilder" (these were moved to ./app-builder/types.ts
// during the cohesion split with zero behavior change).
export type {
  AppModelResponse,
  AppModelListResponse,
  RunResponse,
  ArtifactListResponse,
  WorkerStatusResponse,
  PackManifestResponse,
  ModelSchemaResponse,
  RunsListResponse,
  TaxonomyNodeResponse,
  TaxonomyTreeResponse,
  RunMetricsResponse,
  RunStateFrame,
  RunStreamFrame,
  AppRun,
  CompareItem,
  AppBuilderToolParams,
  AppEntry,
  AppListResponse,
  AppDetailResponse,
  RunAppRequest,
  RunAppResponse,
  AppLogsResponse,
  AppRunState,
  PackageJobResponse,
  PackageState,
} from "@/stores/app-builder/types";

export const useAppBuilderStore = defineStore("appBuilder", () => {
  // ─── State ─────────────────────────────────────────────────────────────────
  const models = ref<AppModelResponse[]>([]);
  const workerStatus = ref<WorkerStatusResponse | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);

  const taxonomy = ref<TaxonomyNodeResponse[]>([]);
  // Full static taxonomy tree (group/task label/icon/description/io + counts)
  // from GET /taxonomy/tree — V1 parity so the picker shows every selectable
  // category (incl. zero-pack tasks) with human-readable labels.
  const taxonomyTree = ref<TaxonomyTreeResponse | null>(null);
  const schemaCache = ref<Record<string, ModelSchemaResponse>>({});
  // Rich pack manifest (display_name/runtime/metrics/variants/...), lazily
  // fetched per model so the gallery cards + info drawer can show V1-parity
  // fields the lightweight /models list omits.
  const manifestCache = ref<Record<string, PackManifestResponse>>({});

  // V1 deps-status 逐 pack 进度 parity (useAppBuilderRegistry.js:269-342):
  // live dependency-install progress per model, merged into the gallery
  // ModelCard so the badge flips "installing → ready / missing + error".
  // Keyed by modelId; populated by `pollDepsStatus` polling
  // `GET /api/app-builder/deps-status/packs` every 5s while AppBuilder is open.
  const depsProgress = ref<Record<string, DepsProgressEntry>>({});
  // Polling lifecycle (module-private to the store instance). Mirrors V1's
  // `_depsPollingTimer` / `_depsPollingDone` so the interval auto-stops once
  // every pack is satisfied (or after `maxAttempts`) and never runs twice.
  let _depsPollingTimer: ReturnType<typeof setInterval> | null = null;
  let _depsPollingDone = false;
  let _depsPollingAttempts = 0;

  // Per-model weight-download state (App Builder model-strip DOWNLOAD UI).
  // Keyed by model id; a model with no entry is treated as "idle" by the row.
  // Drives the "Download button → live progress bar → ✓ ready" affordance.
  const downloads = ref<Record<string, DownloadState>>({});

  const selectedModelId = ref<string | null>(null);
  const selectedVariantId = ref<string | null>(null);
  // Multi-select of imported models chosen in the chat-input strip
  // (App Builder mode). Independent of the single `selectedModelId` used by
  // the workbench — both stay valid. Projected into chat tool_params as
  // `selected_model_ids` so the backend injects those Packs into the system
  // prompt. Order is preserved (insertion order = user click order).
  const selectedModelIds = ref<string[]>([]);
  // Active taxonomy task filter for the gallery / TaskRail (V1 activeCategory).
  // null = show all models. Holds a task id (taxonomy leaf segment).
  const selectedTaskId = ref<string | null>(null);
  // Active taxonomy group filter (V1 TaxonomyPicker selected group).
  const selectedGroupId = ref<string | null>(null);

  // Per-model inputs / params buckets (V1 `_inputsByModel`/`_paramsByModel`).
  const inputsByModel = ref<Record<string, Record<string, unknown>>>({});
  const paramsByModel = ref<Record<string, Record<string, unknown>>>({});

  // Run history (newest first) + the live/current run.
  const runs = ref<AppRun[]>([]);
  const currentRunIndex = ref(-1);
  const snapshotRun = ref<AppRun | null>(null);
  const live = ref(false); // a run is in-flight
  /** Timestamp (epoch ms) when `live` was last set to true. Used to detect stuck runs. */
  let liveStartedAt = 0;
  /** Safety threshold: if live has been true longer than this, force-reset on model select. */
  const LIVE_STUCK_THRESHOLD_MS = 5 * 60 * 1000; // 5 minutes

  // Compare tray.
  const compareItems = ref<CompareItem[]>([]);
  const compareOpen = ref(false);

  // Send-to-chat editable prompt.
  const sendToChatPrompt = ref("");

  // ─── Standalone-app hosting (Phase 4, plan §5.1/§5.3/§5.5) ─────────────────
  // Generated *app projects* (`data/app_builder/<app_id>/`) and their managed
  // process state, as opposed to the *models* + *runs* above. Backed by the
  // /api/app-builder/apps/* routes; state is per-app_id.
  //
  //   apps:          the flat list returned by GET /apps (empty on failure).
  //   selectedAppId: which app the UI is currently focused on (menu highlight
  //                  / preview target); null when no selection.
  //   appRunStates:  per-app managed-run snapshot last returned by run/stop
  //                  (or synthesized from GET /logs' status). error carries a
  //                  localizable code (e.g. app_builder.port_in_use) — never a
  //                  raw English message — so the UI can render translated
  //                  text (plan §5.7).
  //   appLogs:       per-app retained stdout/stderr tail from GET /logs.
  const apps = ref<AppEntry[]>([]);
  const selectedAppId = ref<string | null>(null);
  const appRunStates = ref<Record<string, AppRunState>>({});
  const appLogs = ref<Record<string, string>>({});

  // Per-app packaging job state (Phase 5 "Package to ZIP" UI). Keyed by app id;
  // an app with no entry is treated as "not packaging" by the row. Drives the
  // "Package button → live progress bar → done ✓ / error" affordance, mirroring
  // the weight-download slot machine (POST → { job_id } → SSE progress/done).
  const appPackageStates = ref<Record<string, PackageState>>({});

  // ─── Derived ─────────────────────────────────────────────────────────────
  const selectedModel = computed<AppModelResponse | null>(
    () => models.value.find((m) => m.id === selectedModelId.value) ?? null,
  );

  /**
   * Resolved model rows for the multi-select strip (App Builder mode), in
   * selection order. Ids that no longer resolve to a loaded model are dropped
   * (never fabricated) so the strip only ever shows real imported models.
   */
  const selectedModelInfos = computed<AppModelResponse[]>(() =>
    selectedModelIds.value
      .map((id) => models.value.find((m) => m.id === id))
      .filter((m): m is AppModelResponse => m !== undefined),
  );

  const selectedSchema = computed<ModelSchemaResponse | null>(() =>
    selectedModelId.value !== null
      ? (schemaCache.value[selectedModelId.value] ?? null)
      : null,
  );

  /** Rich manifest for the selected model (lazily fetched, cached). */
  const selectedManifest = computed<PackManifestResponse | null>(() =>
    selectedModelId.value !== null
      ? (manifestCache.value[selectedModelId.value] ?? null)
      : null,
  );

  /** weightsMissing: V2 expresses "not runnable" via enabled===false. */
  const weightsMissing = computed<boolean>(
    () => selectedModel.value !== null && selectedModel.value.enabled === false,
  );

  const currentRun = computed<AppRun | null>(
    () => runs.value[currentRunIndex.value] ?? null,
  );

  /** Run displayed in output / metrics (snapshot takes precedence). */
  const displayedRun = computed<AppRun | null>(
    () => snapshotRun.value ?? currentRun.value,
  );

  const inputs = computed<Record<string, unknown>>(() =>
    selectedModelId.value !== null
      ? (inputsByModel.value[selectedModelId.value] ?? {})
      : {},
  );

  const params = computed<Record<string, unknown>>(() =>
    selectedModelId.value !== null
      ? (paramsByModel.value[selectedModelId.value] ?? {})
      : {},
  );

  /**
   * canRun parity (V1 `useAppBuilder.js:360-378`): a model is selected, no live
   * run, weights present, and every required input field has a value.
   *
   * V1 input-readiness rules:
   *   - multi schema  → every `required !== false` field has a value;
   *   - single schema → the field named after `schema.kind`
   *     (audio/image/text/…) has a value (`inputs[kind]`).
   */
  const canRun = computed<boolean>(() => {
    if (selectedModelId.value === null) return false;
    if (live.value) return false;
    if (weightsMissing.value) return false;
    const schema = selectedSchema.value?.input_schema ?? null;
    const bag = inputs.value;
    const hasValue = (v: unknown): boolean => {
      if (v === undefined || v === null) return false;
      if (typeof v === "string") return v.trim() !== "";
      if (Array.isArray(v)) return v.length > 0;
      return true;
    };
    // Single-field schema: V1 convention is the input field name === kind.
    const kind =
      schema !== null && typeof schema.kind === "string" ? schema.kind : null;
    const isMultiShape =
      schema !== null &&
      (Array.isArray((schema as Record<string, unknown>).fields) ||
        Array.isArray((schema as Record<string, unknown>).required) ||
        (schema as Record<string, unknown>).properties !== undefined);
    if (kind !== null && !isMultiShape) {
      return hasValue(bag[kind]);
    }
    const fields = extractRequiredFields(schema);
    return fields.every((name) => hasValue(bag[name]));
  });

  /** tool_params projection for chat send (V1 `toolParamsForChat`). */
  const toolParamsForChat = computed<AppBuilderToolParams>(() => {
    const last = currentRun.value;
    const model = selectedModel.value;
    // Backward-compat single id: prefer the multi-select's first entry, else
    // fall back to the workbench single selection.
    const primaryId = selectedModelIds.value[0] ?? selectedModelId.value;
    const primaryModel =
      primaryId !== null && primaryId !== undefined
        ? (models.value.find((m) => m.id === primaryId) ?? model)
        : model;
    return {
      selected_model_id: primaryId ?? null,
      selected_model_name: primaryModel?.title ?? null,
      selected_model_ids: [...selectedModelIds.value],
      category: primaryModel?.taxonomy?.[0] ?? null,
      variant_id: selectedVariantId.value,
      last_run_summary:
        last !== null && last.output !== null
          ? summariseOutput(last.output, {
              modelName: model?.title ?? null,
              category: deriveCategoryCode(model?.taxonomy),
            })
          : null,
    };
  });

  // ─── Actions: registry / taxonomy / schema ─────────────────────────────────
  async function fetchModels(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<AppModelListResponse>(
        "GET",
        "/api/app-builder/models",
      );
      models.value = res.items;
      // V1 init parity (useAppBuilder.js:806-844) — three-step model fallback:
      //   1. last-used (localStorage)
      //   2. first model whose taxonomy task has at least one model
      //      (in taxonomy declaration order, V1 picks "first non-empty task")
      //   3. globally first model
      // Step 1 already runs above; if it didn't fire, run 2 then 3.
      const remembered = readLastModel();
      if (
        selectedModelId.value === null &&
        remembered !== null &&
        res.items.some((m) => m.id === remembered)
      ) {
        selectModel(remembered);
      } else if (selectedModelId.value === null && res.items.length > 0) {
        // Step 2: pick first model in declaration order whose task has models.
        // res.items is already in registry order (server returns
        // declaration-stable ordering); V1's behavior collapses to "the first
        // model in the first task that has models" — equivalent to picking
        // res.items[0] when ordering is preserved per-task. Fall back to
        // res.items[0] (step 3) when grouping yields nothing else.
        const tree = taxonomyTree.value;
        let pick: string | null = null;
        if (tree !== null) {
          for (const g of tree.groups) {
            for (const tk of g.tasks) {
              const m = res.items.find((mm) => {
                const seg = mm.taxonomy;
                return seg.length >= 2 && seg[0] === g.id && seg[1] === tk.id;
              });
              if (m !== undefined) {
                pick = m.id;
                break;
              }
            }
            if (pick !== null) break;
          }
        }
        const firstId = pick ?? res.items[0]?.id ?? null;
        if (firstId !== null) selectModel(firstId);
      }
      // Prefetch the rich manifest for every gallery model so the cards
      // render the full V1 fieldset (category / IO / runtime · delegate ·
      // precision / latency · memory) on first paint instead of the lean
      // "? → ?" placeholders. V1's `/models` list already carried these
      // fields inline; V2 splits them into the lazily-loaded manifest, so we
      // warm the cache up-front here. Best-effort + parallel + de-duped by
      // `fetchManifest`'s own cache guard — a slow/failing manifest just
      // leaves that one card lean, never blocking the gallery.
      void Promise.allSettled(
        res.items.map((m) => fetchManifest(m.id)),
      );
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  async function fetchTaxonomy(): Promise<void> {
    error.value = null;
    try {
      taxonomy.value = await apiJson<TaxonomyNodeResponse[]>(
        "GET",
        "/api/app-builder/taxonomy",
      );
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  /**
   * Fetch the full static taxonomy tree (V1 parity). Best-effort: a failure
   * leaves `taxonomyTree` null and the workbench falls back to deriving the
   * (narrower) tree from the registered-model counts.
   */
  async function fetchTaxonomyTree(): Promise<void> {
    try {
      taxonomyTree.value = await apiJson<TaxonomyTreeResponse>(
        "GET",
        "/api/app-builder/taxonomy/tree",
      );
    } catch {
      // Optional enrichment; keep the lean count-only taxonomy as fallback.
    }
  }

  async function fetchSchema(modelId: string): Promise<ModelSchemaResponse | null> {
    if (schemaCache.value[modelId] !== undefined) {
      return schemaCache.value[modelId];
    }
    try {
      const res = await apiJson<ModelSchemaResponse>(
        "GET",
        `/api/app-builder/models/${encodeURIComponent(modelId)}/schema`,
      );
      schemaCache.value = { ...schemaCache.value, [modelId]: res };
      return res;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      return null;
    }
  }

  async function fetchWorkerStatus(): Promise<void> {
    error.value = null;
    try {
      workerStatus.value = await apiJson<WorkerStatusResponse>(
        "GET",
        "/api/app-builder/worker/status",
      );
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  /**
   * Lazily fetch the rich pack manifest for a model (cached). Powers the
   * gallery cards' runtime/metrics/variants badges + the info drawer +
   * long-description, which the lightweight /models list does not carry.
   */
  async function fetchManifest(
    modelId: string,
  ): Promise<PackManifestResponse | null> {
    if (manifestCache.value[modelId] !== undefined) {
      return manifestCache.value[modelId];
    }
    try {
      const res = await apiJson<PackManifestResponse>(
        "GET",
        `/api/app-builder/models/${encodeURIComponent(modelId)}/manifest`,
      );
      manifestCache.value = { ...manifestCache.value, [modelId]: res };
      return res;
    } catch {
      // Manifest is optional enrichment; a 404/503 just means cards stay lean.
      return null;
    }
  }

  // ─── Actions: selection ─────────────────────────────────────────────────────
  function selectModel(modelId: string): void {
    if (live.value) {
      // If live has been stuck for longer than the safety threshold, force-reset.
      // Otherwise, attempt a graceful cancel before proceeding.
      if (Date.now() - liveStartedAt > LIVE_STUCK_THRESHOLD_MS) {
        // Force-reset: the run is almost certainly dead/stuck.
        live.value = false;
        liveStartedAt = 0;
      } else {
        // Graceful cancel: fire-and-forget the DELETE to terminate the backend
        // run, and force live=false NOW so selection proceeds immediately.
        // cancelRun is async (awaits the DELETE) but we cannot block selectModel
        // on it — force the flag synchronously here.
        live.value = false;
        liveStartedAt = 0;
        void cancelRun();
      }
    }
    selectedModelId.value = modelId;
    selectedVariantId.value = null;
    snapshotRun.value = null;
    writeLastModel(modelId);
    // Pull schema (variants + input/output) lazily; default the variant.
    void fetchSchema(modelId).then((schema) => {
      if (selectedModelId.value !== modelId) return; // selection moved on
      const variants = (schema?.variants ?? []) as Record<string, unknown>[];
      const def =
        variants.find((v) => v.is_default === true) ?? variants[0] ?? null;
      const defId = def !== null ? def.id : null;
      if (typeof defId === "string" && selectedVariantId.value === null) {
        selectedVariantId.value = defId;
      }
    });
    // Point current run at this model's most-recent run, else clear.
    const idx = runs.value.findIndex((r) => r.modelId === modelId);
    currentRunIndex.value = idx;
    // Enrich card / drawer with the rich manifest (cached, best-effort).
    void fetchManifest(modelId);
    // V1 parity (useAppBuilder.js:650-731 / :221-223): load this model's
    // persisted run history (DB-backed, scoped) and restore the most recent
    // completed run + its inputs so re-entering App Builder shows "last time's
    // result + inputs". Best-effort; never blocks selection. Skipped when a
    // run is in flight (guarded above) so we don't disturb a live stream.
    void loadModelHistoryAndRestore(modelId);
    // Auto-sync taxonomy filter to this model's group/task so the header
    // TaxonomyPicker reflects the selected model's category (V1 parity).
    const m = models.value.find((x) => x.id === modelId);
    if (m !== undefined && Array.isArray(m.taxonomy) && m.taxonomy.length >= 1) {
      selectedGroupId.value = m.taxonomy[0] ?? null;
      selectedTaskId.value = m.taxonomy.length >= 2 ? (m.taxonomy[1] ?? null) : null;
    }
  }

  /**
   * Toggle an imported model in the chat-input multi-select (App Builder mode).
   * Additive to the single `selectedModelId` (workbench selection is left
   * untouched). Toggling an id adds it when absent, removes it when present.
   * Order preserved (append on add).
   */
  function toggleSelectedModelId(modelId: string): void {
    const idx = selectedModelIds.value.indexOf(modelId);
    if (idx >= 0) {
      selectedModelIds.value = selectedModelIds.value.filter(
        (id) => id !== modelId,
      );
    } else {
      selectedModelIds.value = [...selectedModelIds.value, modelId];
    }
  }

  /** Replace the chat-input multi-select wholesale (de-duped, order kept). */
  function setSelectedModelIds(ids: string[]): void {
    const seen = new Set<string>();
    const next: string[] = [];
    for (const id of ids) {
      if (!seen.has(id)) {
        seen.add(id);
        next.push(id);
      }
    }
    selectedModelIds.value = next;
  }

  /** Clear the chat-input multi-select. */
  function clearSelectedModelIds(): void {
    selectedModelIds.value = [];
  }

  /**
   * Select the active taxonomy task filter (V1 `selectCategory`). Passing
   * `null` clears the filter (gallery shows all models). Toggling the same
   * task off (re-selecting it) also clears, matching V1's rail UX.
   */
  function selectTask(taskId: string | null): void {
    selectedTaskId.value =
      taskId !== null && selectedTaskId.value === taskId ? null : taskId;
  }

  /**
   * Set both group + task filter at once (V1 TaxonomyPicker `update:selection`).
   * Clears the task if null; always sets the group.
   * V1 parity: if the currently-selected model does not belong to the new
   * task, deselect it so the UI shows the gallery again for that task.
   */
  function setTaxonomyFilter(groupId: string | null, taskId: string | null): void {
    selectedGroupId.value = groupId;
    selectedTaskId.value = taskId;
    // V1 behavior: selecting a taxonomy node returns to gallery if the
    // current model is not in the new task.
    if (taskId !== null && selectedModelId.value !== null) {
      const m = models.value.find((x) => x.id === selectedModelId.value);
      if (m !== undefined && !m.taxonomy.includes(taskId)) {
        selectedModelId.value = null;
        selectedVariantId.value = null;
        snapshotRun.value = null;
        currentRunIndex.value = -1;
      }
    }
  }

  function selectVariant(variantId: string): void {
    if (live.value) {
      // Same recovery as selectModel: force-reset if stuck, else cancel.
      if (Date.now() - liveStartedAt > LIVE_STUCK_THRESHOLD_MS) {
        live.value = false;
        liveStartedAt = 0;
      } else {
        live.value = false;
        liveStartedAt = 0;
        void cancelRun();
      }
    }
    selectedVariantId.value = variantId;
    // Re-point currentRun to "same model + same variant most-recent".
    const idx = runs.value.findIndex(
      (r) => r.modelId === selectedModelId.value && r.variantId === variantId,
    );
    currentRunIndex.value = idx;
  }

  function setInput(key: string, value: unknown): void {
    const id = selectedModelId.value;
    if (id === null) return;
    const bag = { ...(inputsByModel.value[id] ?? {}), [key]: value };
    inputsByModel.value = { ...inputsByModel.value, [id]: bag };
  }

  function setInputs(bag: Record<string, unknown>): void {
    const id = selectedModelId.value;
    if (id === null) return;
    inputsByModel.value = { ...inputsByModel.value, [id]: { ...bag } };
  }

  function setParam(key: string, value: unknown): void {
    const id = selectedModelId.value;
    if (id === null) return;
    const bag = { ...(paramsByModel.value[id] ?? {}), [key]: value };
    paramsByModel.value = { ...paramsByModel.value, [id]: bag };
  }

  function setParams(bag: Record<string, unknown>): void {
    const id = selectedModelId.value;
    if (id === null) return;
    paramsByModel.value = { ...paramsByModel.value, [id]: { ...bag } };
  }

  function applyExample(example: {
    inputs?: Record<string, unknown>;
    paramsOverride?: Record<string, unknown>;
  }): void {
    if (example.inputs !== undefined) setInputs(example.inputs);
    if (example.paramsOverride !== undefined) setParams(example.paramsOverride);
  }

  // ─── Actions: two-stage streaming Run ───────────────────────────────────────
  /**
   * Trigger reactivity after mutating a run object in place. `runs` is a
   * deep `ref`, so nested property mutations are already reactive; this
   * helper additionally re-assigns the array slot with the SAME object
   * reference so list-level watchers (e.g. history) re-evaluate without
   * invalidating the `run` reference held by the caller.
   */
  function patchRun(run: AppRun): AppRun {
    // `run` is a reactive() proxy (created in startRun).  Direct property
    // mutations on it are tracked by Vue 3 regardless of execution context
    // (including SSE callbacks outside the Pinia action).  This function is
    // kept as a no-op identity so call-sites don't need to change; the actual
    // reactivity is driven by the mutations callers perform on `run` before
    // calling patchRun().
    return run;
  }

  /**
   * Build the `inputs` payload the backend expects. variant + params +
   * options are folded into the inputs bag (backend reads
   * `inputs.variant_id` / `inputs.params`).
   */
  function buildRunInputs(): Record<string, unknown> {
    const base: Record<string, unknown> = { ...inputs.value };
    if (selectedVariantId.value !== null) {
      base.variant_id = selectedVariantId.value;
    }
    const p = params.value;
    if (Object.keys(p).length > 0) {
      base.params = { ...p };
    }
    // V1 always sent noCache:true on a manual Run.
    base.options = { noCache: true };
    return base;
  }

  /**
   * Start a run: POST /runs to obtain the id, then subscribe to
   * GET /runs/{id}/stream (SSE). Pushes a fresh AppRun to the front of
   * `runs` and drives its state machine from the stream.
   *
   * `signalTabId` (optional): when provided, the chat tab's
   * AbortController is reused so cancel() can abort the stream fetch.
   */
  async function startRun(signalTabId?: string): Promise<void> {
    const modelId = selectedModelId.value;
    if (modelId === null || live.value) return;
    error.value = null;

    const runInit: AppRun = {
      id: null,
      modelId,
      variantId: selectedVariantId.value,
      status: "queued",
      inputs: { ...inputs.value },
      params: { ...params.value },
      frames: [],
      output: null,
      logs: [],
      metrics: null,
      artifacts: [],
      error: null,
      createdAt: Date.now(),
      pid: null,
      exitCode: null,
      statusHint: null,
      queuePosition: null,
      errorDetail: null,
      rating: null,
    };
    // Wrap in reactive() so that property mutations (run.output = ...) are
    // tracked by Vue 3 even when performed from SSE callbacks outside the
    // Pinia action context.  Without reactive(), `runs.value[0]` is a plain
    // object and Vue cannot detect property changes on it.
    const reactiveRun = reactive(runInit) as AppRun;
    runs.value = [reactiveRun, ...runs.value];
    currentRunIndex.value = 0;
    snapshotRun.value = null;
    live.value = true;
    liveStartedAt = Date.now();
    let run = runs.value[0] as AppRun;

    let created: RunResponse;
    try {
      created = await apiJson<RunResponse>("POST", "/api/app-builder/runs", {
        model_id: modelId,
        inputs: buildRunInputs(),
      });
    } catch (e) {
      run.status = "failed";
      run.error = e instanceof Error ? e.message : "create failed";
      error.value = run.error;
      live.value = false;
      patchRun(run);
      return;
    }
    run.id = created.id;
    run.status = created.status;
    run = patchRun(run);

    const signal =
      signalTabId !== undefined
        ? (peekAbortController(signalTabId)?.signal ?? undefined)
        : undefined;

    try {
      await apiWsStream(
        `/api/app-builder/runs/${encodeURIComponent(created.id)}/ws`,
        {
          onState: (data) => {
            const s = data as RunStateFrame;
            if (typeof s?.status === "string") {
              run.status = s.status;
              run = patchRun(run);
            }
          },
          onFrame: (data) => {
            const f = data as RunStreamFrame;
            run.frames = [...run.frames, f];
            const payload = f?.payload;
            if (payload !== undefined && payload !== null) {
              applyFrameToRun(run, payload);
            }
            run = patchRun(run);
          },
          onError: (err) => {
            run.status = "failed";
            run.error = err.message;
            error.value = err.message;
            run = patchRun(run);
          },
          onDone: () => {
            if (!isTerminal(run.status)) {
              run.status = "completed";
            }
            run = patchRun(run);
          },
        },
        {
          signal,
          sseFallbackPath: `/api/app-builder/runs/${encodeURIComponent(created.id)}/stream`,
          sseOptions: signal !== undefined ? { signal } : {},
        },
      );
    } catch {
      // apiSSE rejects on error frame (already handled) / abort.
      if (!isTerminal(run.status)) {
        run.status = run.error !== null ? "failed" : "cancelled";
        run = patchRun(run);
      }
    } finally {
      live.value = false;
      // Pull artifacts + metrics for terminal completed runs.
      if (run.id !== null && run.status === "completed") {
        const arts = await fetchArtifacts(run.id);
        if (arts !== null) {
          run.artifacts = arts.items;
        }
        run.metrics = await fetchMetrics(run.id);
        patchRun(run);
      }
    }
  }

  /** Cancel = DELETE /runs/{id} (terminate worker) + abort stream. */
  async function cancelRun(signalTabId?: string): Promise<void> {
    const run = currentRun.value;
    if (run === null || run.id === null) return;
    error.value = null;
    try {
      await apiJson("DELETE", `/api/app-builder/runs/${encodeURIComponent(run.id)}`);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
    // Abort the front-end stream so apiSSE returns.
    if (signalTabId !== undefined) {
      try {
        peekAbortController(signalTabId)?.abort();
      } catch {
        // ignore
      }
    }
    run.status = "cancelled";
    live.value = false;
    patchRun(run);
  }

  /** Retry the current run (re-issue with the same selection). */
  async function retryRun(signalTabId?: string): Promise<void> {
    await startRun(signalTabId);
  }

  // ─── Actions: artifacts / metrics ───────────────────────────────────────────
  async function fetchArtifacts(
    runId: string,
  ): Promise<ArtifactListResponse | null> {
    error.value = null;
    try {
      return await apiJson<ArtifactListResponse>(
        "GET",
        `/api/app-builder/runs/${encodeURIComponent(runId)}/artifacts`,
      );
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      return null;
    }
  }

  async function fetchMetrics(runId: string): Promise<RunMetricsResponse | null> {
    try {
      return await apiJson<RunMetricsResponse>(
        "GET",
        `/api/app-builder/metrics/${encodeURIComponent(runId)}`,
      );
    } catch {
      return null;
    }
  }

  /** Build a blob URL for an artifact path (G6). */
  function artifactBlobUrl(runId: string, relativePath: string): string {
    return buildArtifactBlobUrl(runId, relativePath);
  }

  // ─── Actions: history (runs list) ───────────────────────────────────────────
  /**
   * Fetch run history, newest first. When `modelId` is supplied the list is
   * scoped to that model (+ optional `variantId`) — V1 parity for the
   * per-model Run History panel (`HistoryPanel.js:57-78` →
   * `GET /history/{model_id}/runs?variant_id=`). Without `modelId` the legacy
   * global cross-model list is fetched (unchanged). The backend filter is an
   * append-only query param on `GET /runs` (`_runs.py` list_runs).
   */
  async function fetchHistory(
    limit = 50,
    offset = 0,
    modelId?: string | null,
    variantId?: string | null,
    opts?: { silent?: boolean },
  ): Promise<void> {
    if (opts?.silent !== true) {
      error.value = null;
    }
    try {
      const query: Record<string, string | number> = { limit, offset };
      if (modelId !== undefined && modelId !== null && modelId !== "") {
        query.model_id = modelId;
      }
      if (variantId !== undefined && variantId !== null && variantId !== "") {
        query.variant_id = variantId;
      }
      const res = await apiJson<RunsListResponse>(
        "GET",
        "/api/app-builder/runs",
        undefined,
        { query },
      );
      // Hydrate runs[] from the server view (newest first as returned).
      const hydrated: AppRun[] = res.runs.map(hydrateHistoryRun);
      // Merge strategy: for runs already in memory (matched by id), preserve
      // SSE-streamed fields (frames / output / logs / pid / exitCode /
      // statusHint) that the history API never returns, and only update the
      // persistent fields that the server owns (status / metrics / artifacts /
      // error / startedAt / finishedAt).
      // Runs with id === null are still in-flight (pending run_id assignment)
      // and must be kept at the front unchanged.
      const existingById = new Map(
        runs.value.filter((r) => r.id !== null).map((r) => [r.id, r]),
      );
      const merged: AppRun[] = mergeHistoryRuns(hydrated, existingById);
      const pendingRuns = runs.value.filter((r) => r.id === null);
      // When the fetch is scoped to a single model, only THAT model's rows
      // were returned — keep the in-memory runs of OTHER models so a scoped
      // refresh never discards another model's live/streamed history. The
      // HistoryPanel then filters the working set by the selected model
      // (V1 per-model panel). The global (unscoped) fetch replaces wholesale.
      const scoped =
        modelId !== undefined && modelId !== null && modelId !== "";
      const mergedIds = new Set(merged.map((r) => r.id));
      const otherModelRuns = scoped
        ? runs.value.filter(
            (r) =>
              r.id !== null &&
              r.modelId !== modelId &&
              !mergedIds.has(r.id),
          )
        : [];
      runs.value = [...pendingRuns, ...merged, ...otherModelRuns];
    } catch (e) {
      // A history-fetch failure during the best-effort model-select restore
      // must not surface as the workbench's top-level error (``silent``).
      if (opts?.silent !== true) {
        error.value = e instanceof Error ? e.message : "Unknown error";
      }
    }
  }

  /**
   * Reconstruct a minimal `output` dict from a run's persisted artifacts so a
   * restored historical run renders its real result in the viewer. V2 does NOT
   * persist the structured runner output dict (only artifacts on disk +
   * inputs), so we surface the truthful subset we DO have — image / audio
   * artifact paths — and leave richer shapes (predictions / segments /
   * fullText) absent (the viewer falls back to the artifact-backed media or a
   * neutral state). Never fabricates data: only real persisted artifact paths.
   */
  function reconstructOutputFromArtifacts(
    artifacts: ArtifactListResponse["items"],
  ): Record<string, unknown> | null {
    if (!Array.isArray(artifacts) || artifacts.length === 0) return null;
    const out: Record<string, unknown> = {};
    for (const a of artifacts) {
      if (a.kind === "image" && out.image_path === undefined) {
        out.image_path = a.path;
      } else if (a.kind === "audio" && out.audio_path === undefined) {
        out.audio_path = a.path;
      }
    }
    return Object.keys(out).length > 0 ? out : null;
  }

  /**
   * V1 parity (`useAppBuilder.js:650-731` last-results restore +
   * `:203-236` selectModel): when a model is selected, load its run history
   * (DB-backed, scoped) and restore the most recent COMPLETED run as the
   * current displayed run, re-feeding that run's persisted inputs back into
   * the input area so the user re-enters App Builder seeing "last time's
   * result + inputs". Truthful (§真实状态优先): the run, its artifacts and its
   * inputs all come from the DB — nothing is fabricated; the structured output
   * is reconstructed only from real persisted artifact paths.
   *
   * Best-effort: a fetch/restore failure leaves the selection intact and just
   * shows the empty/idle output card.
   */
  async function loadModelHistoryAndRestore(modelId: string): Promise<void> {
    // Restoring a model's last result is a best-effort enhancement layered on
    // top of selecting the model — a history fetch failure must NOT surface as
    // the workbench's top-level error (the model itself selected fine), so the
    // fetch runs ``silent``. (Using a silent fetch instead of snapshot/restore
    // avoids clobbering a later, legitimate error written by another action.)
    await fetchHistory(50, 0, modelId, null, { silent: true });
    // Selection may have moved on while the fetch was in flight.
    if (selectedModelId.value !== modelId) return;
    // Most-recent COMPLETED run for this model (newest first already).
    const lastDone = runs.value.find(
      (r) => r.modelId === modelId && r.status === "completed" && r.id !== null,
    );
    if (lastDone === undefined) return;
    // Point currentRun at the restored run.
    const idx = runs.value.findIndex((r) => r.id === lastDone.id);
    if (idx >= 0) currentRunIndex.value = idx;
    snapshotRun.value = null;
    // Hydrate artifacts + metrics + reconstruct the viewer output.
    if (lastDone.id !== null) {
      const arts = await fetchArtifacts(lastDone.id);
      if (selectedModelId.value !== modelId) return; // moved on
      if (arts !== null) {
        lastDone.artifacts = arts.items;
        const recon = reconstructOutputFromArtifacts(arts.items);
        if (recon !== null) lastDone.output = recon;
      }
      lastDone.metrics = await fetchMetrics(lastDone.id);
    }
    // Re-feed the persisted inputs back into the input area (V1 回灌 inputs).
    // Only when the user has not already typed something for this model in
    // the current session (do not clobber unsaved edits).
    const existingInputs = inputsByModel.value[modelId];
    const hasUserInput =
      existingInputs !== undefined && Object.keys(existingInputs).length > 0;
    if (!hasUserInput && lastDone.inputs !== null) {
      // Strip the internal variant_id / params / options keys folded into
      // inputs on the run path so only user-facing input fields are restored.
      const { variant_id: _vid, params: _params, options: _opts, ...clean } =
        lastDone.inputs as Record<string, unknown>;
      void _vid;
      void _params;
      void _opts;
      inputsByModel.value = { ...inputsByModel.value, [modelId]: { ...clean } };
    }
  }

  async function deleteHistoryRun(runId: string): Promise<void> {
    error.value = null;
    try {
      await apiJson(
        "DELETE",
        `/api/app-builder/history/runs/${encodeURIComponent(runId)}`,
      );
      runs.value = runs.value.filter((r) => r.id !== runId);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  /**
   * Export a run as markdown (V1 HistoryPanel.js:112-122).
   * Opens the export endpoint in a new tab (browser download).
   */
  function exportRun(runId: string): void {
    const url = `/api/app-builder/runs/${encodeURIComponent(runId)}/export.md`;
    globalThis.window?.open(url, "_blank");
  }

  /**
   * Submit a quality rating for a run (V1 DynamicOutput.js:454-480 parity).
   *
   * Rating semantics: 1 = 👍 (good), -1 = 👎 (poor).
   * Optimistic update: `run.rating` is set immediately; the POST is best-effort.
   * Backend: POST /api/app-builder/feedback { run_id, rating, text }.
   * Note: backend `rating` field is int 1-5; we map 1→5 (good) and -1→1 (poor)
   * to fit the 1-5 scale while preserving the V1 -1/1 semantic in the store.
   */
  async function submitRating(runId: string, value: 1 | -1): Promise<void> {
    // Find the run and apply optimistic update.
    const run = runs.value.find((r) => r.id === runId);
    if (run === undefined) return;
    run.rating = value;
    // Map V1 -1/1 to backend 1-5 scale: 1 (good) → 5, -1 (poor) → 1.
    const backendRating = value === 1 ? 5 : 1;
    try {
      await apiJson<{ accepted: boolean; feedback_id: string }>(
        "POST",
        "/api/app-builder/feedback",
        { run_id: runId, rating: backendRating },
      );
    } catch (e) {
      // Best-effort: keep the optimistic UI state even on failure.
      console.warn("[appBuilder] submitRating failed:", e instanceof Error ? e.message : e);
    }
  }

  /**
   * Share a run (V1 HistoryPanel.js:124-149).
   * POSTs to the share endpoint, gets a token, copies the share URL.
   */
  async function shareRun(runId: string): Promise<string | null> {
    try {
      const res = await apiJson<{ token: string }>(
        "POST",
        "/api/app-builder/share",
        { run_id: runId },
      );
      const shareUrl = `${globalThis.location?.origin ?? ""}/api/app-builder/share/${res.token}`;
      await globalThis.navigator?.clipboard?.writeText(shareUrl);
      return shareUrl;
    } catch {
      return null;
    }
  }

  /**
   * Poll `GET /api/app-builder/deps-status/packs` for per-pack dependency
   * install progress and merge it into `depsProgress` so the gallery
   * ModelCard badge flips "installing → ready / missing + error hint".
   *
   * V1 parity: `useAppBuilderRegistry.js:269-342 pollDepsStatus` — same 5s
   * interval, same `installing|satisfied→ready|missing` mapping, same
   * auto-stop ("not checking AND all satisfied", or after `maxAttempts`),
   * same idempotent guard (won't start twice / after done). Started by the
   * AppBuilder overlay on open (V1 `useAppBuilder.js:852-853` fired it after
   * `refresh()`), stopped on close to avoid a resident timer.
   *
   * State-Truth-First (AGENTS.md §🔴): the merged rows come straight from the
   * backend checker's real probe/install outcome — never a fabricated "ready".
   */
  async function pollDepsStatus(
    opts: { intervalMs?: number; maxAttempts?: number } = {},
  ): Promise<void> {
    const intervalMs = opts.intervalMs ?? 5000;
    const maxAttempts = opts.maxAttempts ?? 60;
    if (_depsPollingTimer !== null || _depsPollingDone) return;

    const tick = async (): Promise<void> => {
      _depsPollingAttempts += 1;
      try {
        const data = await apiJson<{
          checking?: boolean;
          packs?: Record<
            string,
            {
              satisfied?: boolean;
              missing?: string[];
              installing?: boolean;
              errorKind?: string | null;
              errorHint?: string | null;
              errorRaw?: string | null;
            }
          >;
        }>("GET", "/api/app-builder/deps-status/packs");

        const packs = data.packs ?? {};
        const checking = data.checking ?? false;

        // Merge into the reactive map (replace wholesale so Vue tracks the
        // change; only touch entries the backend reported).
        const next: Record<string, DepsProgressEntry> = { ...depsProgress.value };
        for (const [modelId, ds] of Object.entries(packs)) {
          next[modelId] = {
            depsStatus: ds.installing
              ? "installing"
              : ds.satisfied
                ? "ready"
                : "missing",
            depsErrorKind: ds.errorKind ?? null,
            depsErrorHint: ds.errorHint ?? null,
            depsErrorRaw: ds.errorRaw ?? null,
            depsMissing: Array.isArray(ds.missing) ? ds.missing : [],
          };
        }
        depsProgress.value = next;

        // Stop when no background check is in flight AND every reported pack
        // is satisfied (V1 stop condition). An empty `packs` with
        // `checking=false` also stops (nothing left to install).
        const values = Object.values(packs);
        const allSatisfied = values.every((p) => p.satisfied === true);
        if (!checking && allSatisfied) {
          stopDepsPolling();
        }
      } catch {
        // Ignore transient network errors during polling (V1 swallows them).
      }
      if (_depsPollingAttempts >= maxAttempts) {
        stopDepsPolling();
      }
    };

    // First tick immediately, then start the interval only if not already done.
    await tick();
    if (!_depsPollingDone && _depsPollingTimer === null) {
      _depsPollingTimer = setInterval(() => {
        void tick();
      }, intervalMs);
    }
  }

  /** Stop deps-status polling + mark done so it won't restart this session. */
  function stopDepsPolling(): void {
    _depsPollingDone = true;
    if (_depsPollingTimer !== null) {
      clearInterval(_depsPollingTimer);
      _depsPollingTimer = null;
    }
  }

  /** Reset deps polling so a re-open of AppBuilder can poll again. */
  function resetDepsPolling(): void {
    stopDepsPolling();
    _depsPollingDone = false;
    _depsPollingAttempts = 0;
  }

  /**
   * Delete a model pack (V1 ModelInfoDrawer.js:199-208).
   * Removes the model from the registry + optional file cleanup.
   *
   * 缺陷 P4: the DELETE endpoint now returns 200 + a body
   * ``{model_id, mode, warnings: [...]}`` so we can surface non-fatal
   * file-cleanup warnings (e.g. AV-locked ``.bin`` files) to the user.
   * Returns the warnings tuple so the caller can toast them.
   */
  async function deleteModel(modelId: string): Promise<{
    ok: boolean;
    warnings: string[];
  }> {
    error.value = null;
    try {
      const resp = await apiJson<{
        model_id: string;
        mode: string;
        warnings: string[];
      }>(
        "DELETE",
        `/api/app-builder/models/${encodeURIComponent(modelId)}`,
        undefined,
        // V1 default deleteFiles=true: remove the on-disk pack + weights, not
        // just the DB row (ModelInfoDrawer.js:200-203).
        { query: { deleteFiles: "true" } },
      );
      // Drop the deleted model from the selection + every per-model bucket so
      // nothing keeps referencing it (otherwise the workbench could re-hydrate
      // stale inputs/manifest/schema for an id that no longer exists).
      if (selectedModelId.value === modelId) {
        selectedModelId.value = null;
      }
      _forgetModelState(modelId);
      // State-Truth-First: re-read the authoritative list from the backend
      // rather than trusting only the optimistic local filter, so the gallery
      // + taxonomy counts reflect the real post-delete state (and a deleted
      // model can never linger if the local filter missed an alias).
      await fetchModels();
      return {
        ok: true,
        warnings: Array.isArray(resp?.warnings) ? resp.warnings : [],
      };
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
      return { ok: false, warnings: [] };
    }
  }

  /** Drop every per-model cache/bucket for a removed model id. */
  function _forgetModelState(modelId: string): void {
    models.value = models.value.filter((m) => m.id !== modelId);
    const drop = <T,>(bag: Record<string, T>): Record<string, T> => {
      if (!(modelId in bag)) return bag;
      const next = { ...bag };
      delete next[modelId];
      return next;
    };
    inputsByModel.value = drop(inputsByModel.value);
    paramsByModel.value = drop(paramsByModel.value);
    schemaCache.value = drop(schemaCache.value);
    manifestCache.value = drop(manifestCache.value);
  }

  /**
   * Delete specific variants of a model (V1 ModelInfoDrawer.js:228-276).
   */
  async function deleteVariants(modelId: string, variantIds: string[]): Promise<void> {
    error.value = null;
    try {
      await apiJson(
        "DELETE",
        `/api/app-builder/models/${encodeURIComponent(modelId)}`,
        undefined,
        // V1 sends deleteFiles=true + variantIds for partial delete
        // (ModelInfoDrawer.js:273-274).
        { query: { deleteFiles: "true", variantIds: variantIds.join(",") } },
      );
      // Refresh manifest cache to reflect removed variants.
      void fetchManifest(modelId);
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    }
  }

  // ─── Actions: snapshot view ─────────────────────────────────────────────────
  function viewHistorySnapshot(run: AppRun): void {
    snapshotRun.value = run;
  }

  function exitSnapshot(): void {
    snapshotRun.value = null;
  }

  // ─── Actions: compare (front-end only) ──────────────────────────────────────
  function addToCompare(run: AppRun): void {
    if (run.id === null) return;
    if (compareItems.value.some((c) => c.id === run.id)) {
      compareOpen.value = true;
      return;
    }
    // Enrich with model title + runtime (V1 CompareItem carries modelName /
    // runtime / status / rating so the Table & Radar views have real columns).
    const model = models.value.find((m) => m.id === run.modelId);
    const manifest = manifestCache.value[run.modelId] as
      | Record<string, unknown>
      | undefined;
    const item: CompareItem = buildCompareItem(run, model, manifest);
    const next = [...compareItems.value, item];
    // FIFO cap at COMPARE_MAX.
    compareItems.value = next.slice(Math.max(0, next.length - COMPARE_MAX));
    compareOpen.value = true;
  }

  function removeFromCompare(id: string): void {
    compareItems.value = compareItems.value.filter((c) => c.id !== id);
  }

  function clearCompare(): void {
    compareItems.value = [];
  }

  function toggleCompare(): void {
    compareOpen.value = !compareOpen.value;
  }

  // ─── Actions: send-to-chat prompt ───────────────────────────────────────────
  function resetSendToChatPrompt(defaultPrompt: string): void {
    sendToChatPrompt.value = defaultPrompt;
  }

  // ─── Helpers exposed for the chat bridge ────────────────────────────────────
  /**
   * Compose the user message text injected into chat by Send to Chat.
   * Prepends the (editable) prompt then a compact summary of the result.
   */
  function composeSendToChatMessage(prependPrompt: string): string {
    const run = currentRun.value;
    const model = selectedModel.value;
    const parts: string[] = [];
    if (prependPrompt.trim() !== "") parts.push(prependPrompt.trim());
    if (run !== null && run.output !== null) {
      parts.push(
        summariseOutput(run.output, {
          modelName: model?.title ?? null,
          category: deriveCategoryCode(model?.taxonomy),
        }),
      );
    }
    return parts.join("\n\n");
  }

  // ─── Actions: weight download (model-strip DOWNLOAD UI) ─────────────────────
  /** Read the download slot for a model id (never fabricated; idle if absent). */
  function downloadStateOf(modelId: string): DownloadState {
    return downloads.value[modelId] ?? makeIdleDownloadState();
  }

  /** Replace one model's download slot (reactive re-assign so Vue tracks it). */
  function _setDownloadState(modelId: string, next: DownloadState): void {
    downloads.value = { ...downloads.value, [modelId]: next };
  }

  /** Patch fields onto a model's download slot (merges over the current one). */
  function _patchDownloadState(
    modelId: string,
    patch: Partial<DownloadState>,
  ): void {
    const cur = downloads.value[modelId] ?? makeIdleDownloadState();
    _setDownloadState(modelId, { ...cur, ...patch });
  }

  /**
   * Start downloading a model's weights and stream live progress.
   *
   * 1. No-op if a download is already in flight for this id.
   * 2. Flip to `downloading` (percent 0) so the row swaps button → progress bar.
   * 3. POST to obtain the `job_id`, then open the SSE progress stream.
   * 4. Each `progress` frame updates percent/speed/eta/bytes/total; a frame with
   *    `is_complete === true` marks `extracting` (server unpacking before done).
   * 5. On resolve (`event: done`): mark `done` + percent 100, refresh the model
   *    registry so the row's `status` flips to Ready (✓ icon), then clear the
   *    slot back to idle (Ready now expresses "installed").
   * 6. On reject (error frame / abort): record `error` + message; NEVER throws.
   *
   * State-Truth-First: completion is only claimed after the SSE stream resolves
   * on the server's `event: done`, and the Ready flip comes from re-fetching the
   * authoritative model list — never optimistically fabricated.
   */
  async function startWeightDownload(modelId: string): Promise<void> {
    const existing = downloads.value[modelId];
    if (
      existing !== undefined &&
      (existing.status === "downloading" || existing.status === "extracting")
    ) {
      return; // already in flight — no-op
    }

    const controller = new AbortController();
    _setDownloadState(modelId, {
      ...makeIdleDownloadState(),
      status: "downloading",
      percent: 0,
      controller,
    });

    let jobId: string;
    try {
      const res = await apiJson<WeightsDownloadStartResponse>(
        "POST",
        "/api/app-builder/weights/download",
        { model_id: modelId },
      );
      jobId = res.job_id;
    } catch (e) {
      _patchDownloadState(modelId, {
        status: "error",
        error: e instanceof Error ? e.message : "Download failed",
        controller: null,
      });
      return;
    }
    _patchDownloadState(modelId, { jobId });

    const wsUrl =
      "/api/app-builder/weights/download/" +
      encodeURIComponent(jobId) +
      "/ws";
    const sseFallbackPath =
      "/api/app-builder/weights/download/" +
      encodeURIComponent(jobId) +
      "/progress";

    try {
      await apiWsStream(
        wsUrl,
        {
          onProgress: (data: unknown) => {
            const f = (data ?? {}) as WeightsProgressFrame;
            const bytes = asFiniteNumber(f.bytes_downloaded, 0);
            const total = asNullableNumber(f.total_bytes);
            const speed = asFiniteNumber(f.speed_bps, 0);
            const eta = asNullableNumber(f.eta_seconds);
            const percent = asNullableNumber(f.percent);
            const isComplete = f.is_complete === true;
            _patchDownloadState(modelId, {
              status: isComplete ? "extracting" : "downloading",
              bytesDownloaded: bytes,
              totalBytes: total,
              speedBps: speed,
              etaSeconds: eta,
              percent,
            });
          },
        },
        {
          signal: controller.signal,
          sseFallbackPath,
          sseOptions: { signal: controller.signal },
        },
      );
    } catch (e) {
      // apiSSE REJECTS on `event: error` or abort. Abort (user cancel) is
      // handled/reset in cancelWeightDownload, so only record an error here
      // when we were not deliberately aborted.
      if (!controller.signal.aborted) {
        _patchDownloadState(modelId, {
          status: "error",
          error: e instanceof Error ? e.message : "Download failed",
          controller: null,
        });
      }
      return;
    }

    // Resolved on `event: done` → weights installed + extracted server-side.
    _patchDownloadState(modelId, {
      status: "done",
      percent: 100,
      speedBps: 0,
      etaSeconds: null,
      controller: null,
    });
    // Re-read the authoritative model list so the row's `status` flips to
    // Ready and the ✓ icon shows (State-Truth-First — never fabricated).
    await fetchModels();
    // Clear the slot back to idle: "Ready" now carries the installed state, so
    // the row renders the ✓ icon rather than a lingering "done" progress bar.
    if (downloads.value[modelId] !== undefined) {
      const next = { ...downloads.value };
      delete next[modelId];
      downloads.value = next;
    }
  }

  /**
   * Cancel an in-flight weight download: abort the SSE stream, best-effort
   * DELETE the server job, and reset the slot to idle so the row shows the
   * Download button again.
   */
  async function cancelWeightDownload(modelId: string): Promise<void> {
    const cur = downloads.value[modelId];
    if (cur === undefined) return;
    const { controller, jobId } = cur;
    try {
      controller?.abort();
    } catch {
      // ignore — already settled
    }
    if (jobId !== null) {
      try {
        await apiJson(
          "DELETE",
          "/api/app-builder/weights/download/" + encodeURIComponent(jobId),
        );
      } catch {
        // best-effort — the abort already stopped the front-end stream
      }
    }
    // Reset to idle (drop the slot so the row falls back to Download button).
    const next = { ...downloads.value };
    delete next[modelId];
    downloads.value = next;
  }

  // ─── Actions: standalone-app hosting (Phase 4) ──────────────────────────────
  /**
   * Extract the stable, localizable error code from a thrown apiJson error.
   *
   * apiJson rejects with an `ApiError` that carries the backend envelope's
   * `.code` (e.g. `app_builder.port_in_use`, plan §5.7). We surface that code
   * so the UI can map it to translated text — never the raw English `.message`.
   * Defensive: falls back to a generic `.code` on the object, else null.
   */
  function _errorCodeOf(e: unknown): string | null {
    if (e instanceof ApiError) return e.code;
    if (e !== null && typeof e === "object" && "code" in e) {
      const c = (e as { code?: unknown }).code;
      if (typeof c === "string") return c;
    }
    return null;
  }

  /** Project a run/stop `RunAppResponse` into the per-app view-model slot. */
  function _setAppRunState(appId: string, res: RunAppResponse): void {
    appRunStates.value = {
      ...appRunStates.value,
      [appId]: {
        status: res.status,
        port: res.port,
        url: res.url,
        pid: res.pid,
        processId: res.process_id,
        manualCommand: res.manual_command,
        error: null,
      },
    };
  }

  /**
   * List generated app projects (GET /apps). Best-effort like the other
   * registry fetches: a failure leaves `apps` empty (never throws) but is
   * logged so the menu shows the "no apps yet" empty state rather than an
   * error dialog.
   */
  async function fetchApps(): Promise<void> {
    try {
      const res = await apiJson<AppListResponse>(
        "GET",
        "/api/app-builder/apps",
      );
      apps.value = res.apps ?? [];
    } catch (e) {
      apps.value = [];
      console.warn(
        "[appBuilder] fetchApps failed:",
        e instanceof Error ? e.message : e,
      );
    }
  }

  /**
   * Delete a generated app project (DELETE /apps/{id}). The backend stops the
   * managed process first (if running) then removes the dev project dir under
   * data/app_builder/<id>/ (packaged zips are NOT touched). On success we drop
   * the app's local run/log/package state and refresh the list. Re-throws on
   * failure so the caller can toast the localized error code.
   */
  async function deleteApp(appId: string): Promise<void> {
    // Stop any local status poll first so it can't resurrect a deleted app.
    stopAppStatusPoll(appId);
    await apiJson<void>(
      "DELETE",
      `/api/app-builder/apps/${encodeURIComponent(appId)}`,
    );
    // Drop local per-app state (run / logs / package slots).
    const dropKey = <T,>(rec: Record<string, T>): Record<string, T> => {
      if (!(appId in rec)) return rec;
      const { [appId]: _removed, ...rest } = rec;
      return rest;
    };
    appRunStates.value = dropKey(appRunStates.value);
    appLogs.value = dropKey(appLogs.value);
    appPackageStates.value = dropKey(appPackageStates.value);
    if (selectedAppId.value === appId) selectedAppId.value = null;
    await fetchApps();
  }

  /**
   * Fetch one app's full detail (GET /apps/{id}). Returns the response or null
   * on failure (best-effort). Not currently surfaced by the menu but exposed
   * for the preview button / future detail drawer.
   */
  async function fetchAppDetail(
    appId: string,
  ): Promise<AppDetailResponse | null> {
    try {
      return await apiJson<AppDetailResponse>(
        "GET",
        `/api/app-builder/apps/${encodeURIComponent(appId)}`,
      );
    } catch (e) {
      console.warn(
        "[appBuilder] fetchAppDetail failed:",
        e instanceof Error ? e.message : e,
      );
      return null;
    }
  }

  /**
   * Start an app's managed process (POST /apps/{id}/run).
   *
   * `open_browser` is always false: the front-end owns opening the returned
   * loopback URL (a backend-authored `http://127.0.0.1:<port>/`, never an
   * LLM-authored URL — plan §5.8) so it can honour the same-origin/trusted-URL
   * policy. On success the per-app run-state slot is updated and the response
   * is returned. On failure the *error code* (plan §5.7) is recorded on the
   * slot and the error is re-thrown so the component can toast localized text.
   */
  async function runApp(
    appId: string,
    options?: { port?: number | null },
  ): Promise<RunAppResponse> {
    // Optimistic pre-set + immediate polling: the POST /run response only
    // resolves once backend readiness settles (up to ~30s), so without this
    // the badge would sit on its old value for the whole window. We show
    // "starting" right away and poll the live status so the badge advances to
    // "running"/"ready" as soon as the backend reports it — even before the
    // POST returns.
    const prevSlot = appRunStates.value[appId];
    appRunStates.value = {
      ...appRunStates.value,
      [appId]: {
        status: "starting",
        port: prevSlot?.port ?? null,
        url: prevSlot?.url ?? null,
        pid: prevSlot?.pid ?? null,
        processId: prevSlot?.processId ?? null,
        manualCommand: prevSlot?.manualCommand ?? null,
        error: null,
      },
    };
    startAppStatusPoll(appId);
    try {
      const res = await apiJson<RunAppResponse, RunAppRequest>(
        "POST",
        `/api/app-builder/apps/${encodeURIComponent(appId)}/run`,
        { port: options?.port ?? null, open_browser: false },
      );
      _setAppRunState(appId, res);
      // Backend returned: if already settled, stop polling; if still
      // "starting", keep polling until it reaches a steady/terminal state.
      if (res.status === "starting") {
        startAppStatusPoll(appId);
      } else {
        stopAppStatusPoll(appId);
      }
      return res;
    } catch (e) {
      stopAppStatusPoll(appId);
      const code = _errorCodeOf(e);
      const prev = appRunStates.value[appId];
      appRunStates.value = {
        ...appRunStates.value,
        [appId]: {
          status: prev?.status ?? "failed",
          port: prev?.port ?? null,
          url: prev?.url ?? null,
          pid: prev?.pid ?? null,
          processId: prev?.processId ?? null,
          manualCommand: prev?.manualCommand ?? null,
          error: code,
        },
      };
      throw e;
    }
  }

  /**
   * Stop an app's managed process (DELETE /apps/{id}/run). Updates the per-app
   * run-state slot from the response. Records the error code + re-throws on
   * failure so the component can toast localized text (plan §5.7).
   */
  async function stopApp(appId: string): Promise<RunAppResponse> {
    stopAppStatusPoll(appId);
    // Optimistic feedback: stopping kills the whole process tree
    // (taskkill /F /T) which takes a moment, and the DELETE only resolves
    // after that. Without an immediate state change the button looks
    // unresponsive and users click it repeatedly. Flip to a transient
    // client-side "stopping" status right away so the badge shows
    // "停止中…" and the controls disable until the response lands.
    const prevSlot = appRunStates.value[appId];
    if (prevSlot !== undefined) {
      appRunStates.value = {
        ...appRunStates.value,
        [appId]: { ...prevSlot, status: "stopping", error: null },
      };
    }
    try {
      const res = await apiJson<RunAppResponse>(
        "DELETE",
        `/api/app-builder/apps/${encodeURIComponent(appId)}/run`,
      );
      _setAppRunState(appId, res);
      return res;
    } catch (e) {
      const code = _errorCodeOf(e);
      const prev = appRunStates.value[appId];
      if (code === "app_builder.app_not_running") {
        // State-Truth-First: a 409 "app not running" means the backend has NO
        // managed process for this app (it exited on its own, or was stopped
        // elsewhere) while the UI still showed a stale Ready/running badge
        // (possibly straight from the list snapshot, with no run-state slot
        // yet). Rolling back to / leaving the stale Ready would keep Stop
        // enabled and the user stuck re-clicking → repeated 409s. Force a
        // `stopped` run-state slot (even when none existed) so it overrides
        // the list snapshot's `status`: Stop disables and Run re-enables.
        appRunStates.value = {
          ...appRunStates.value,
          [appId]: {
            status: "stopped",
            port: null,
            url: null,
            pid: null,
            processId: null,
            manualCommand: null,
            // Keep the code so the caller can still toast "not running" — the
            // status is authoritative (stopped); the error is just feedback.
            error: code,
          },
        };
      } else if (prev !== undefined) {
        // Other failures: roll back the optimistic "stopping" to the prior
        // real status so a failed stop doesn't leave the badge stuck on
        // "stopping", and record the error code for the toast.
        appRunStates.value = {
          ...appRunStates.value,
          [appId]: {
            ...prev,
            status: prevSlot?.status ?? prev.status,
            error: code,
          },
        };
      }
      throw e;
    }
  }

  /**
   * Fetch an app's retained stdout/stderr tail (GET /apps/{id}/logs). Stores
   * the output under `appLogs[appId]` and reconciles the run-state status from
   * the authoritative response (State-Truth-First). Best-effort: a failure
   * leaves the previous logs/status intact but is logged.
   */
  async function fetchAppLogs(appId: string): Promise<void> {
    try {
      const res = await apiJson<AppLogsResponse>(
        "GET",
        `/api/app-builder/apps/${encodeURIComponent(appId)}/logs`,
      );
      appLogs.value = { ...appLogs.value, [appId]: res.output };
      const prev = appRunStates.value[appId];
      appRunStates.value = {
        ...appRunStates.value,
        [appId]: {
          status: res.status,
          port: prev?.port ?? null,
          url: prev?.url ?? null,
          pid: prev?.pid ?? null,
          processId: prev?.processId ?? null,
          manualCommand: prev?.manualCommand ?? null,
          error: prev?.error ?? null,
        },
      };
    } catch (e) {
      console.warn(
        "[appBuilder] fetchAppLogs failed:",
        e instanceof Error ? e.message : e,
      );
    }
  }

  /** Read the run-state slot for an app id (never fabricated; null if absent). */
  function appRunStateOf(appId: string): AppRunState | null {
    return appRunStates.value[appId] ?? null;
  }

  // Per-app status-poll timers. A managed run's `/health` readiness is
  // resolved backend-side, but the POST /run response may return "starting"
  // (readiness still in progress) and the app can also change state later
  // (e.g. crash). We poll the lightweight logs+status endpoint until the app
  // reaches a terminal/steady state so the row badge advances
  // "starting" → "ready"/"running" (or "failed"/"stopped") on its own,
  // without the user clicking anything. Kept in a module-private map so the
  // reactive state stays a pure view-model.
  const _appStatusPollers = new Map<string, ReturnType<typeof setTimeout>>();
  const _APP_STATUS_POLL_MS = 1500;

  /** Stop polling an app's status (idempotent). */
  function stopAppStatusPoll(appId: string): void {
    const t = _appStatusPollers.get(appId);
    if (t !== undefined) {
      clearTimeout(t);
      _appStatusPollers.delete(appId);
    }
  }

  /**
   * Refresh one app's live run status from the backend (logs endpoint, which
   * returns the authoritative status — State-Truth-First), preserving the
   * url/port/pid/manualCommand captured from the run response. Returns the
   * fresh status string, or null on failure.
   */
  async function refreshAppStatus(appId: string): Promise<string | null> {
    try {
      const res = await apiJson<AppLogsResponse>(
        "GET",
        `/api/app-builder/apps/${encodeURIComponent(appId)}/logs`,
      );
      appLogs.value = { ...appLogs.value, [appId]: res.output };
      const prev = appRunStates.value[appId];
      appRunStates.value = {
        ...appRunStates.value,
        [appId]: {
          status: res.status,
          port: prev?.port ?? null,
          url: prev?.url ?? null,
          pid: prev?.pid ?? null,
          processId: prev?.processId ?? null,
          manualCommand: prev?.manualCommand ?? null,
          error: prev?.error ?? null,
        },
      };
      return res.status;
    } catch (e) {
      console.warn(
        "[appBuilder] refreshAppStatus failed:",
        e instanceof Error ? e.message : e,
      );
      return null;
    }
  }

  /**
   * Start polling an app's status until it settles. "starting" keeps polling;
   * "ready"/"running"/"failed"/"stopped" stop it. A transient failure (e.g.
   * the logs/status endpoint 409s because the process is not registered YET,
   * during the brief pre-spawn window) is tolerated up to a bounded number of
   * consecutive misses so the poll survives startup. Safe to call repeatedly
   * (a running poller for the same app is replaced).
   */
  function startAppStatusPoll(appId: string): void {
    stopAppStatusPoll(appId);
    let misses = 0;
    const MAX_MISSES = 8; // ~12s of tolerance at the poll interval
    const tick = async (): Promise<void> => {
      const status = await refreshAppStatus(appId);
      if (status === null) {
        // Endpoint not answering with a status yet (pre-spawn 409 / transient).
        misses += 1;
        if (misses >= MAX_MISSES) {
          _appStatusPollers.delete(appId);
          return;
        }
        _appStatusPollers.set(
          appId,
          setTimeout(() => void tick(), _APP_STATUS_POLL_MS),
        );
        return;
      }
      misses = 0;
      // Keep polling only while transiently starting; stop on any terminal
      // or steady state.
      if (status === "starting") {
        _appStatusPollers.set(
          appId,
          setTimeout(() => void tick(), _APP_STATUS_POLL_MS),
        );
      } else {
        _appStatusPollers.delete(appId);
      }
    };
    _appStatusPollers.set(
      appId,
      setTimeout(() => void tick(), _APP_STATUS_POLL_MS),
    );
  }

  // ─── Actions: standalone-app packaging (Phase 5) ────────────────────────────
  // Live SSE AbortControllers per app id, kept in a module-private side map so
  // the reactive `PackageState` slot stays a pure serializable view-model (the
  // controller is a lifecycle handle, not UI state). `cancelPackage` aborts via
  // this map and best-effort DELETEs the job — mirrors the weight-download
  // controller-on-state pattern, minus the controller field on the slot.
  const _packageControllers = new Map<string, AbortController>();

  /** Write a full package-state slot for an app id (reactive replace). */
  function _setPackageState(appId: string, next: PackageState): void {
    appPackageStates.value = { ...appPackageStates.value, [appId]: next };
  }

  /** Shallow-patch the package-state slot for an app id (no-op if absent). */
  function _patchPackageState(
    appId: string,
    patch: Partial<PackageState>,
  ): void {
    const cur = appPackageStates.value[appId];
    if (cur === undefined) return;
    _setPackageState(appId, { ...cur, ...patch });
  }

  /**
   * Package a generated app into a distributable ZIP (POST /apps/{id}/package),
   * then follow the SSE progress stream to completion.
   *
   * State-Truth-First (plan §5.6/§9.3): "packaged" is only claimed after the
   * server's `event: done` resolves the stream — the zip path + size come from
   * the authoritative `done` frame, never optimistically fabricated. Structured
   * exactly like `startWeightDownload`: start → apiJson<{job_id}> → apiSSE loop
   * (onProgress patch) → resolve-on-done, with error codes surfaced on the slot
   * for the menu to map to localized toast text (plan §5.7).
   */
  async function packageApp(appId: string): Promise<void> {
    const existing = appPackageStates.value[appId];
    if (existing !== undefined && existing.running) {
      return; // already in flight — no-op
    }

    let jobId: string;
    try {
      const res = await apiJson<PackageJobResponse>(
        "POST",
        `/api/app-builder/apps/${encodeURIComponent(appId)}/package`,
      );
      jobId = res.job_id;
    } catch (e) {
      _setPackageState(appId, {
        jobId: null,
        phase: "collecting",
        percent: 0,
        message: "",
        zipPath: null,
        sizeBytes: null,
        isComplete: false,
        error: _errorCodeOf(e) ?? "package_failed",
        running: false,
      });
      throw e;
    }

    _setPackageState(appId, {
      jobId,
      phase: "collecting",
      percent: 0,
      message: "",
      zipPath: null,
      sizeBytes: null,
      isComplete: false,
      error: null,
      running: true,
    });

    const controller = new AbortController();
    _packageControllers.set(appId, controller);

    const wsUrl =
      `/api/app-builder/apps/${encodeURIComponent(appId)}/package/` +
      encodeURIComponent(jobId) +
      "/ws";
    const sseFallbackPath =
      `/api/app-builder/apps/${encodeURIComponent(appId)}/package/` +
      encodeURIComponent(jobId) +
      "/progress";

    const asNullableString = (v: unknown): string | null =>
      typeof v === "string" ? v : null;

    try {
      await apiWsStream(
        wsUrl,
        {
          onProgress: (data: unknown) => {
            const f = (data ?? {}) as PackageProgressFrame;
            const prev = appPackageStates.value[appId];
            const nextSize = asNullableNumber(f.size_bytes);
            const nextZip = asNullableString(f.zip_path);
            _patchPackageState(appId, {
              phase: asNullableString(f.phase) ?? prev?.phase ?? "collecting",
              percent: asFiniteNumber(f.percent, prev?.percent ?? 0),
              message: asNullableString(f.message) ?? prev?.message ?? "",
              // Preserve the last-known size/path when a frame omits them so a
              // later frame can't null out values the terminal frame carried.
              sizeBytes: nextSize ?? prev?.sizeBytes ?? null,
              zipPath: nextZip ?? prev?.zipPath ?? null,
              isComplete: f.is_complete === true,
            });
          },
          onError: (err: ApiError) => {
            // Capture the server's error `code` (plan §5.7) directly off the
            // `event: error` frame — apiWsStream then rejects, handled below.
            _patchPackageState(appId, {
              error: err.code ?? "package_failed",
            });
          },
        },
        {
          signal: controller.signal,
          sseFallbackPath,
          sseOptions: { signal: controller.signal },
        },
      );
    } catch (e) {
      // apiSSE REJECTS on `event: error` or abort. Abort (user cancel) is
      // handled/reset in cancelPackage, so only record an error here when we
      // were not deliberately aborted.
      _packageControllers.delete(appId);
      if (!controller.signal.aborted) {
        _patchPackageState(appId, {
          error:
            appPackageStates.value[appId]?.error ??
            _errorCodeOf(e) ??
            "package_failed",
          running: false,
        });
        throw e;
      }
      return;
    }

    // Resolved on `event: done` → ZIP written server-side. The backend
    // guarantees the terminal `progress` frame (is_complete=true) already
    // carried `zip_path`/`size_bytes`, and `apiSSE`'s `onDone` is `() => void`
    // (no payload), so those values are captured in `onProgress` above; here
    // we only finalize the percent/complete/running flags (State-Truth-First:
    // the last progress frame is the source of the zip path + size).
    _packageControllers.delete(appId);
    _patchPackageState(appId, {
      percent: 100,
      isComplete: true,
      running: false,
    });
  }

  /**
   * Cancel an in-flight packaging job: abort the SSE stream and best-effort
   * DELETE the server job, then mark the slot not-running (drops the progress
   * bar back to the Package button).
   */
  async function cancelPackage(appId: string): Promise<void> {
    const cur = appPackageStates.value[appId];
    if (cur === undefined || !cur.running) return;
    const controller = _packageControllers.get(appId);
    try {
      controller?.abort();
    } catch {
      // ignore — already settled
    }
    _packageControllers.delete(appId);
    const jobId = cur.jobId;
    if (jobId !== null) {
      try {
        await apiJson(
          "DELETE",
          `/api/app-builder/apps/${encodeURIComponent(appId)}/package/` +
            encodeURIComponent(jobId),
        );
      } catch {
        // best-effort — the abort already stopped the front-end stream
      }
    }
    _patchPackageState(appId, { running: false });
  }

  /** Read the package-state slot for an app id (undefined if never started). */
  function packageStateOf(appId: string): PackageState | undefined {
    return appPackageStates.value[appId];
  }

  /**
   * Dismiss the completed-package result panel for an app. Clears the slot so
   * the row returns to its normal state (Package button re-enabled). Does NOT
   * cancel an in-flight job — use ``cancelPackage`` for that.
   */
  function clearPackageResult(appId: string): void {
    const st = appPackageStates.value[appId];
    if (st === undefined || st.running) return; // no-op if in flight
    const { [appId]: _removed, ...rest } = appPackageStates.value;
    appPackageStates.value = rest;
  }

  /** Set the currently-focused app id (menu highlight / preview target). */
  function selectApp(appId: string | null): void {
    selectedAppId.value = appId;
  }

  return {
    // state
    models,
    workerStatus,
    loading,
    error,
    taxonomy,
    taxonomyTree,
    schemaCache,
    manifestCache,
    depsProgress,
    downloads,
    selectedModelId,
    selectedVariantId,
    selectedModelIds,
    selectedTaskId,
    selectedGroupId,
    setTaxonomyFilter,
    runs,
    currentRunIndex,
    snapshotRun,
    live,
    compareItems,
    compareOpen,
    sendToChatPrompt,
    // standalone-app hosting (Phase 4)
    apps,
    selectedAppId,
    appRunStates,
    appLogs,
    appPackageStates,
    // derived
    selectedModel,
    selectedModelInfos,
    selectedSchema,
    selectedManifest,
    weightsMissing,
    currentRun,
    displayedRun,
    inputs,
    params,
    canRun,
    toolParamsForChat,
    // registry / taxonomy / schema
    fetchModels,
    fetchTaxonomy,
    fetchTaxonomyTree,
    fetchSchema,
    fetchManifest,
    fetchWorkerStatus,
    // selection
    selectModel,
    selectVariant,
    selectTask,
    toggleSelectedModelId,
    setSelectedModelIds,
    clearSelectedModelIds,
    setInput,
    setInputs,
    setParam,
    setParams,
    applyExample,
    // run
    startRun,
    cancelRun,
    retryRun,
    // artifacts / metrics
    fetchArtifacts,
    fetchMetrics,
    artifactBlobUrl,
    // history
    fetchHistory,
    loadModelHistoryAndRestore,
    deleteHistoryRun,
    exportRun,
    shareRun,
    submitRating,
    deleteModel,
    pollDepsStatus,
    stopDepsPolling,
    resetDepsPolling,
    deleteVariants,
    viewHistorySnapshot,
    exitSnapshot,
    // compare
    addToCompare,
    removeFromCompare,
    clearCompare,
    toggleCompare,
    // send-to-chat
    resetSendToChatPrompt,
    composeSendToChatMessage,
    // weight download (model-strip DOWNLOAD UI)
    startWeightDownload,
    cancelWeightDownload,
    downloadStateOf,
    // standalone-app hosting (Phase 4)
    fetchApps,
    deleteApp,
    fetchAppDetail,
    runApp,
    stopApp,
    fetchAppLogs,
    refreshAppStatus,
    startAppStatusPoll,
    stopAppStatusPoll,
    appRunStateOf,
    selectApp,
    // standalone-app packaging (Phase 5)
    packageApp,
    cancelPackage,
    packageStateOf,
    clearPackageResult,
  };
});
