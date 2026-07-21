// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useAppBuilderWorkbench — view-model glue for the App Builder chat-mode
 * workbench (V1 `AppBuilderWorkbench.js` + `useAppBuilderRegistry.js` parity).
 *
 * Responsibility split (keeps `AppBuilderWorkbenchOverlay.vue` thin, per the
 * §重构质量铁律 cohesion check):
 *
 *   - merge the lean `/models` list with the rich (lazily-fetched, cached)
 *     `/models/{id}/manifest` into a normalized {@link AppModelCardVM} so the
 *     ported `ModelCard` / `ModelInfoDrawer` render every V1 field;
 *   - derive the taxonomy tree (group → task) + per-task model counts for the
 *     left `TaskRail` (V1's `modelCounts` + grouped registry);
 *   - filter the gallery models by the active task/group selection (V1
 *     `modelsForSelection`);
 *   - resolve `weightsMissing` / `outputSubtypeLabel` / `category` derivations.
 *
 * Run / selection / streaming state stays in the Pinia `appBuilder` store; this
 * composable is a pure projection layer over it (no run mutations here).
 */
import { computed, type ComputedRef } from "vue";
import { useI18n } from "vue-i18n";
import type { components } from "@/types/api";
import { useAppBuilderStore } from "@/stores/appBuilder";
import type {
  AppModelCardVM,
  AppModelVariantVM,
  AppModelRuntimeVM,
  AppModelMetricsVM,
  DepsProgressEntry,
} from "@/components/app-builder/types";

type AppModelResponse = components["schemas"]["AppModelResponse"];
type PackManifestResponse = components["schemas"]["PackManifestResponse"];

/** One taxonomy task node (leaf) for the rail. */
export interface TaskRailTask {
  id: string;
  label: string;
  description?: string;
  io?: string[];
  modelCount: number;
}

/** One taxonomy group node (top-level) for the rail. */
export interface TaskRailGroup {
  id: string;
  label: string;
  icon?: string;
  tasks: TaskRailTask[];
}

/** DynamicInput field shape (schema-projected). */
export interface SchemaField {
  key: string;
  type: "text" | "number" | "boolean" | "select" | "textarea";
  label: string;
  options?: string[];
  placeholder?: string;
}

/** DynamicParams param shape (schema-projected, V1 ParamSchema parity). */
export interface ParamDef {
  key: string;
  /** Original manifest field name (== key); kept for i18n key lookup. */
  name: string;
  label: string;
  type: "number" | "string" | "boolean" | "select" | "text";
  default?: unknown;
  min?: number;
  max?: number;
  step?: number;
  options?: Array<{ label: string; value: unknown }>;
  advanced?: boolean;
}

/** VariantSwitcher option shape (schema-projected). */
export interface VariantOption {
  id: string;
  label: string;
  description?: string;
  /** Long label (e.g. "FP16 · NPU optimized") for the dropdown form. */
  longLabel?: string;
  /** Registry status (Ready / NotInstalled) — gates lock/disabled chips. */
  status?: string;
  /** Whether this is the manifest default variant (● mark). */
  isDefault?: boolean;
}

function mapJsonType(kind: string): SchemaField["type"] {
  switch (kind) {
    case "number":
    case "integer":
      return "number";
    case "boolean":
      return "boolean";
    case "text":
    case "string":
      return "textarea";
    default:
      return "text";
  }
}

// ── manifest → VM field extraction helpers ────────────────────────────────

function asString(v: unknown): string | null {
  return typeof v === "string" && v !== "" ? v : null;
}

function asNumber(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function extractRuntime(m: PackManifestResponse | null): AppModelRuntimeVM | null {
  const r = (m?.runtime ?? {}) as Record<string, unknown>;
  const out: AppModelRuntimeVM = {
    backend: asString(r.backend),
    delegate: asString(r.delegate),
    quantization: asString(r.quantization) ?? asString(r.precision),
  };
  if (out.backend === null && out.delegate === null && out.quantization === null) {
    return null;
  }
  return out;
}

function extractMetrics(m: PackManifestResponse | null): AppModelMetricsVM | null {
  const mm = (m?.metrics ?? {}) as Record<string, unknown>;
  const latencyMs = asNumber(mm.latencyMs ?? mm.latency_ms ?? mm.latency);
  const memoryMB = asNumber(mm.memoryMB ?? mm.memory_mb ?? mm.memory);
  if (latencyMs === null && memoryMB === null) return null;
  return { latencyMs, memoryMB };
}

function extractVariants(m: PackManifestResponse | null): AppModelVariantVM[] {
  const raw = m?.variants;
  if (!Array.isArray(raw)) return [];
  return raw.map((v) => {
    const d = (v ?? {}) as Record<string, unknown>;
    const id = asString(d.id) ?? asString(d.precision) ?? "default";
    const rt = (d.runtime ?? {}) as Record<string, unknown>;
    const mt = (d.metrics ?? {}) as Record<string, unknown>;
    const installed = d.installed !== false;
    return {
      id,
      label: asString(d.label) ?? id,
      isDefault: d.is_default === true || d.isDefault === true,
      runtime: {
        backend: asString(rt.backend),
        delegate: asString(rt.delegate),
        quantization: asString(rt.quantization) ?? asString(d.precision),
      },
      sizeMB: asNumber(d.sizeMB ?? d.size_mb ?? rt.modelSizeMB),
      installed,
      // V1 drawer variant rows: status dot + per-variant latency + install path.
      status: asString(d.status) ?? (installed ? "Ready" : "NotInstalled"),
      latencyMs: asNumber(mt.latencyMs ?? mt.latency_ms),
      installPath: asString((d.assets as Record<string, unknown> | undefined)?.installPath),
    } satisfies AppModelVariantVM;
  });
}

function extractKind(
  schema: Record<string, unknown> | null | undefined,
): { kind?: string | null } | null {
  if (schema === null || schema === undefined) return null;
  const kind = asString(schema.kind);
  return kind !== null ? { kind } : null;
}

// Reverse of V1's ``LEGACY_CATEGORY_MAP`` (backend/app_builder/taxonomy.py):
// map a ``group/task`` taxonomy pair to the short category code V1 shows on
// the gallery card badge (ASR / OCR / TTS / SR / …). Keyed by "group/task".
const CATEGORY_CODE_BY_TAXONOMY: Readonly<Record<string, string>> = Object.freeze({
  "audio/speech-recognition": "ASR",
  "audio/audio-generation": "TTS",
  "computer-vision/ocr": "OCR",
  "computer-vision/super-resolution": "SR",
  "generative-ai/text-generation": "LLM",
});

/**
 * Derive the V1-parity category badge from a taxonomy segment list.
 * Prefers the short legacy code for a known ``group/task`` pair; falls back to
 * the last (most specific) segment so the badge is never empty.
 */
function deriveCategory(taxonomy: readonly string[]): string | null {
  if (taxonomy.length === 0) return null;
  if (taxonomy.length >= 2) {
    const key = `${taxonomy[0]}/${taxonomy[1]}`;
    const code = CATEGORY_CODE_BY_TAXONOMY[key];
    if (code !== undefined) return code;
  }
  return taxonomy[taxonomy.length - 1] ?? null;
}

/** Humanize a slug segment ("speech-recognition" → "Speech Recognition"). */
function humanize(slug: string): string {
  return slug
    .split(/[-_]/)
    .filter((w) => w.length > 0)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/**
 * Merge the backend per-variant install status (``AppModelResponse
 * .variant_status[]`` — V1 ``variantStatus`` parity) onto the manifest-derived
 * variant VMs. The backend probes weights on disk, so it is the source of
 * truth for ``status`` / ``installed``; the manifest only contributes the
 * display label / runtime / metrics. When the backend omits the field (legacy
 * single-variant pack / older payload) the manifest-derived rows pass through
 * unchanged.
 */
function mergeVariantStatus(
  variants: AppModelVariantVM[],
  model: AppModelResponse,
): AppModelVariantVM[] {
  const raw = (model as Record<string, unknown>).variant_status;
  if (!Array.isArray(raw) || raw.length === 0) return variants;
  const byId = new Map<string, string>();
  for (const row of raw) {
    const d = (row ?? {}) as Record<string, unknown>;
    const id = asString(d.id);
    const status = asString(d.status);
    if (id !== null && status !== null) byId.set(id, status);
  }
  return variants.map((v) => {
    const status = byId.get(v.id);
    if (status === undefined) return v;
    return { ...v, status, installed: status === "Ready" } satisfies AppModelVariantVM;
  });
}

/**
 * Merge a lean `/models` row with its (optional) rich manifest into the
 * normalized card view-model. The manifest is best-effort: when it is not yet
 * cached the card renders the lean fields only (status dot still shows).
 */
export function buildModelCardVM(
  model: AppModelResponse,
  manifest: PackManifestResponse | null,
  translate?: (key: string) => string,
  deps?: DepsProgressEntry | null,
): AppModelCardVM {
  const runtime = extractRuntime(manifest);
  const metrics = extractMetrics(manifest);
  const variants = mergeVariantStatus(extractVariants(manifest), model);
  // V1 parity: the install-status badge comes from the backend
  // (``GET /api/appbuilder/models`` augmented ``status`` — weights present on
  // disk?). Falls back to the ``enabled`` flag only when the backend omits
  // the field (older payloads / stripped-down test containers).
  const status: string =
    asString((model as Record<string, unknown>).status) ??
    (model.enabled === false ? "NotInstalled" : "Ready");
  // V1 parity: short category code. Prefer the backend-computed badge; fall
  // back to the client-side taxonomy derivation so the badge is never empty.
  const category =
    asString((model as Record<string, unknown>).category) ??
    deriveCategory(model.taxonomy);
  // Info-drawer extras (V1 parity): tags / capabilities / examples / weights /
  // install path, pulled from the rich manifest. Capabilities filter the
  // internal `cancel` flag and localize the remaining truthy keys via
  // `appBuilder.capability.<key>` (V1 ModelInfoDrawer.js:48 uses the same i18n
  // map — e.g. benchmark → "性能测试"); falls back to humanize when no
  // translator is supplied (e.g. unit tests).
  const m = (manifest ?? null) as Record<string, unknown> | null;
  const tags = Array.isArray(m?.tags) ? (m!.tags as unknown[]).map(String) : [];
  const capsRaw = (m?.capabilities ?? {}) as Record<string, unknown>;
  const capabilities = Object.keys(capsRaw)
    .filter((k) => capsRaw[k] === true && k !== "cancel")
    .map((k) =>
      translate ? translate(`appBuilder.capability.${k}`) : humanize(k),
    );
  const examplesRaw = Array.isArray(m?.examples) ? (m!.examples as unknown[]) : [];
  const examples = examplesRaw.map((e) => {
    const d = (e ?? {}) as Record<string, unknown>;
    return {
      name: asString(d.name),
      license: asString(d.license),
      // V1 apply-example parity: carry the preset inputs + paramsOverride so
      // the drawer can emit them and the overlay can call store.applyExample().
      inputs: typeof d.inputs === "object" && d.inputs !== null
        ? (d.inputs as Record<string, unknown>)
        : undefined,
      paramsOverride: typeof d.paramsOverride === "object" && d.paramsOverride !== null
        ? (d.paramsOverride as Record<string, unknown>)
        : undefined,
    };
  });
  const assets = (m?.assets ?? {}) as Record<string, unknown>;
  // V1 `ModelInfoDrawer.js:420` gates the delete panel on whether the model is
  // user-imported (built-ins are protected). The backend `AppModelResponse`
  // DTO exposes this as `user_imported` (`_dto.py` `AppModelResponse.user_imported`
  // / `_model_to_dto`), so read it from the MODEL row (authoritative), not the
  // manifest. Falls back to a manifest-level flag only for older payloads.
  const userImported =
    model.user_imported === true ||
    (model as Record<string, unknown>).userImported === true ||
    m?.user_imported === true ||
    m?.userImported === true;
  return {
    modelId: model.id,
    displayName: asString(manifest?.display_name) ?? model.title,
    description: asString(manifest?.description),
    longDescription: asString(manifest?.long_description),
    category,
    status,
    featured: model.pinned === true,
    runtime,
    metrics,
    variants,
    inputSchema: extractKind(manifest?.input_schema),
    outputSchema: extractKind(manifest?.output_schema),
    license: null,
    vendor: asString(manifest?.vendor),
    version: asString(manifest?.version),
    tags,
    capabilities,
    examples,
    weightsUrl: asString(assets.weightsUrl),
    installPath: asString(assets.installPath),
    userImported,
    // V1 deps-status 逐 pack 进度 parity (useAppBuilderRegistry.js:287-309):
    // merge the live dependency-install progress so the ModelCard can flip
    // "installing → ready / missing + error hint". `deps` is undefined until
    // the store's pollDepsStatus has a row for this pack (treated as unknown).
    depsStatus: deps?.depsStatus ?? null,
    depsErrorKind: deps?.depsErrorKind ?? null,
    depsErrorHint: deps?.depsErrorHint ?? null,
    depsErrorRaw: deps?.depsErrorRaw ?? null,
    depsMissing: deps?.depsMissing ?? [],
  };
}

// ── task → output-subtype + category derivation (V1 parity) ───────────────

const OUTPUT_SUBTYPE_BY_TASK: Readonly<Record<string, string>> = Object.freeze({
  "speech-recognition": "TRANSCRIPT",
  "audio-generation": "AUDIO",
  "audio-classification": "LABEL",
  "audio-enhancement": "AUDIO",
  "image-classification": "LABEL",
  "object-detection": "BOXES",
  "semantic-segmentation": "MASK",
  "depth-estimation": "DEPTH",
  "pose-estimation": "POSE",
  ocr: "TEXT",
  "image-generation": "IMAGE",
  "image-editing": "IMAGE",
  "super-resolution": "IMAGE",
  "text-generation": "TEXT",
  embedding: "VECTOR",
});

export interface UseAppBuilderWorkbench {
  /** All gallery cards (lean+manifest merged), in registry order. */
  allCards: ComputedRef<AppModelCardVM[]>;
  /** Cards filtered by the active task/group selection. */
  cardsForSelection: ComputedRef<AppModelCardVM[]>;
  /** Card VM for the currently-selected model (or null). */
  selectedCard: ComputedRef<AppModelCardVM | null>;
  /** Taxonomy groups (group → tasks) for the TaskRail. */
  taskGroups: ComputedRef<TaskRailGroup[]>;
  /** Flat task list (id+label+count) for the rail. */
  taskNodes: ComputedRef<TaskRailTask[]>;
  /** Output sub-type label for the selected model's task (V1). */
  outputSubtypeLabel: ComputedRef<string>;
  /** DynamicInput fields projected from the selected model schema. */
  inputFields: ComputedRef<SchemaField[]>;
  /** DynamicParams defs projected from the selected model schema. */
  paramDefs: ComputedRef<ParamDef[]>;
  /** VariantSwitcher options projected from the selected model schema. */
  variantOptions: ComputedRef<VariantOption[]>;
  /** Map a model id → its card VM (manifest-aware). */
  cardVMFor: (modelId: string) => AppModelCardVM | null;
  /** Primary input kind of the selected model (audio / image / text / …). */
  selectedInputKind: ComputedRef<string | null>;
  /** Human-readable input constraints hint (V1 "Format: … · Max …"). */
  inputConstraintsHint: ComputedRef<string | null>;
  /** Text input constraints (maxChars/placeholder) for TextEditor. */
  textConstraints: ComputedRef<TextConstraints>;
  /** Classification block rows (Source / Group / Task / Tags) — V1 parity. */
  classificationRows: ComputedRef<ClassificationRow[]>;
}

/** Text input constraints projected from the manifest input_schema. */
export interface TextConstraints {
  maxLength?: number;
  placeholder?: string;
  rows?: number;
}

/** One row of the right-column CLASSIFICATION block (V1 parity). */
export interface ClassificationRow {
  /** i18n key under `appBuilder.classification.*` for the row label. */
  labelKey: string;
  value: string;
}

export function useAppBuilderWorkbench(): UseAppBuilderWorkbench {
  const store = useAppBuilderStore();
  const { t } = useI18n();

  function cardVMFor(modelId: string): AppModelCardVM | null {
    const model = store.models.find((m) => m.id === modelId);
    if (model === undefined) return null;
    return buildModelCardVM(
      model,
      store.manifestCache[modelId] ?? null,
      t,
      store.depsProgress[modelId] ?? null,
    );
  }

  const allCards = computed<AppModelCardVM[]>(() =>
    store.models.map((m) =>
      buildModelCardVM(
        m,
        store.manifestCache[m.id] ?? null,
        t,
        store.depsProgress[m.id] ?? null,
      ),
    ),
  );

  // Build group → tasks. Prefer the full static taxonomy tree from
  // `GET /taxonomy/tree` (V1 parity: every selectable group/task with its
  // human-readable label / icon / description / io, including zero-pack
  // tasks). Fall back to deriving a narrower tree from the registered-model
  // counts when the tree endpoint is unavailable.
  const taskGroups = computed<TaskRailGroup[]>(() => {
    const tree = store.taxonomyTree;
    if (tree !== null && Array.isArray(tree.groups) && tree.groups.length > 0) {
      return tree.groups.map((g) => ({
        id: g.id,
        label: g.label,
        icon: g.icon,
        tasks: g.tasks.map((t) => ({
          id: t.id,
          label: t.label,
          description: t.description,
          io: t.io,
          modelCount: t.model_count,
        })),
      }));
    }
    // Fallback: derive from the flat `/taxonomy` count list (humanized ids).
    const nodes = store.taxonomy;
    if (!Array.isArray(nodes)) return [];
    const groups = new Map<string, TaskRailGroup>();
    for (const node of nodes) {
      const path = node.path;
      if (path.length === 0) continue;
      const groupId = path[0] ?? "";
      if (groupId === "") continue;
      const taskId = path.length > 1 ? (path[1] ?? groupId) : groupId;
      let g = groups.get(groupId);
      if (g === undefined) {
        g = { id: groupId, label: humanize(groupId), tasks: [] };
        groups.set(groupId, g);
      }
      if (!g.tasks.some((task) => task.id === taskId)) {
        g.tasks.push({ id: taskId, label: humanize(taskId), modelCount: node.model_count });
      }
    }
    return [...groups.values()];
  });

  const taskNodes = computed<TaskRailTask[]>(() =>
    taskGroups.value.flatMap((g) => g.tasks),
  );

  const cardsForSelection = computed<AppModelCardVM[]>(() => {
    const sel = store.selectedTaskId;
    if (sel === null) return allCards.value;
    // A model matches the task when any taxonomy segment equals the task id.
    return allCards.value.filter((c) => {
      const model = store.models.find((m) => m.id === c.modelId);
      return model !== undefined && model.taxonomy.includes(sel);
    });
  });

  const selectedCard = computed<AppModelCardVM | null>(() => {
    const id = store.selectedModelId;
    return id !== null ? cardVMFor(id) : null;
  });

  const outputSubtypeLabel = computed<string>(() => {
    const model = store.selectedModel;
    if (model === null) return "RESULT";
    for (const seg of model.taxonomy) {
      const sub = OUTPUT_SUBTYPE_BY_TASK[seg];
      if (sub !== undefined) return sub;
    }
    return "RESULT";
  });

  const inputFields = computed<SchemaField[]>(() => {
    const schema = store.selectedSchema;
    if (schema === null || schema.input_schema === null || schema.input_schema === undefined) {
      return [];
    }
    const raw = schema.input_schema as Record<string, unknown>;
    const propsObj = raw.properties;
    if (propsObj !== undefined && propsObj !== null && typeof propsObj === "object") {
      return Object.entries(propsObj as Record<string, unknown>).map(([key, def]) => {
        const d = (def ?? {}) as Record<string, unknown>;
        return {
          key,
          type: mapJsonType(typeof d.type === "string" ? d.type : "text"),
          label: typeof d.title === "string" ? d.title : key,
          ...(Array.isArray(d.enum) ? { options: d.enum.map((x) => String(x)) } : {}),
        };
      });
    }
    const fields = raw.fields;
    if (Array.isArray(fields)) {
      return fields.map((f) => {
        const d = (f ?? {}) as Record<string, unknown>;
        const name = typeof d.name === "string" ? d.name : "field";
        return {
          key: name,
          type: mapJsonType(typeof d.kind === "string" ? d.kind : "text"),
          label: name,
        };
      });
    }
    if (typeof raw.kind === "string") {
      return [{ key: raw.kind, type: mapJsonType(raw.kind), label: raw.kind }];
    }
    return [];
  });

  const paramDefs = computed<ParamDef[]>(() => {
    // Params live in the rich manifest (not the schema endpoint).
    // Manifest `params` is an array of
    // {name, label?, type?, default?, min?, max?, step?, options?, advanced?}
    // — the full V1 ParamSchema shape. We carry every field through so
    // DynamicParams can render selects / sliders / textareas / toggles and the
    // basic/advanced split, exactly like V1.
    const manifest = store.selectedManifest;
    const params = (manifest as Record<string, unknown> | null)?.params;
    if (!Array.isArray(params)) return [];
    return params.map((p) => {
      const d = (p ?? {}) as Record<string, unknown>;
      const name = typeof d.name === "string" ? d.name : "param";
      const label = typeof d.label === "string" ? d.label : name;
      const rawType = typeof d.type === "string" ? d.type : null;
      let type: ParamDef["type"] = "string";
      if (rawType === "boolean") type = "boolean";
      else if (rawType === "select") type = "select";
      else if (rawType === "text") type = "text";
      else if (rawType === "number" || rawType === "integer" || typeof d.default === "number") type = "number";
      else if (rawType === "string") type = "string";
      const out: ParamDef = { key: name, name, label, type, default: d.default };
      if (typeof d.min === "number") out.min = d.min;
      if (typeof d.max === "number") out.max = d.max;
      if (typeof d.step === "number") out.step = d.step;
      if (d.advanced === true) out.advanced = true;
      if (Array.isArray(d.options)) {
        out.options = d.options.map((opt) => {
          if (opt !== null && typeof opt === "object" && "value" in (opt as Record<string, unknown>)) {
            const o = opt as Record<string, unknown>;
            return { label: String(o.label ?? o.value), value: o.value };
          }
          return { label: String(opt), value: opt };
        });
      }
      return out;
    });
  });

  const variantOptions = computed<VariantOption[]>(() => {
    const schema = store.selectedSchema;
    if (schema === null) return [];
    const variants = (schema as unknown as Record<string, unknown>).variants;
    if (!Array.isArray(variants)) return [];
    return variants.map((v) => {
      const d = (v ?? {}) as Record<string, unknown>;
      const id = typeof d.id === "string" ? d.id : String(d.precision ?? "default");
      const out: VariantOption = {
        id,
        label: typeof d.label === "string" ? d.label : id,
      };
      if (typeof d.description === "string") out.description = d.description;
      if (typeof d.longLabel === "string") out.longLabel = d.longLabel;
      // V1 variant chip: default mark + lock/disable when not installed.
      out.isDefault = d.default === true || d.is_default === true;
      const status = asString(d.status);
      out.status = status ?? (d.installed === false ? "NotInstalled" : "Ready");
      return out;
    });
  });

  // ── input kind + constraints hint + classification (V1 parity) ──────────

  const selectedInputKind = computed<string | null>(() => {
    const manifest = store.selectedManifest;
    const raw = (manifest?.input_schema ?? null) as Record<string, unknown> | null;
    return raw !== null ? asString(raw.kind) : null;
  });

  const inputConstraintsHint = computed<string | null>(() => {
    const manifest = store.selectedManifest;
    const raw = (manifest?.input_schema ?? null) as Record<string, unknown> | null;
    if (raw === null) return null;
    const kind = asString(raw.kind);
    const c = (raw.constraints ?? {}) as Record<string, unknown>;
    const parts: string[] = [];
    // Audio constraints (Format/Max/Hz/mono).
    const formats = c.formats;
    if (Array.isArray(formats) && formats.length > 0) {
      parts.push(
        t("appBuilder.constraintFormat", {
          formats: formats.map((f) => String(f)).join("/"),
        }),
      );
    }
    const maxMB = asNumber(c.maxMB);
    if (maxMB !== null)
      parts.push(t("appBuilder.constraintMaxMB", { n: Math.round(maxMB) }));
    const maxSec = asNumber(c.maxSec);
    if (maxSec !== null)
      parts.push(t("appBuilder.constraintMaxSec", { n: Math.round(maxSec) }));
    const sampleRate = asNumber(c.sampleRate);
    if (sampleRate !== null) parts.push(`${Math.round(sampleRate)}Hz`);
    const channels = asNumber(c.channels);
    if (channels === 1) parts.push(t("appBuilder.constraintMono"));
    else if (channels === 2) parts.push(t("appBuilder.constraintStereo"));
    // Text constraints (V1 parity: "Max 500 chars").
    if (kind === "text") {
      const maxChars = asNumber(c.maxChars);
      if (maxChars !== null)
        parts.push(
          t("appBuilder.constraintMaxChars", { n: Math.round(maxChars) }),
        );
    }
    // Image constraints (max width/height).
    if (kind === "image") {
      const maxW = asNumber(c.maxWidth);
      const maxH = asNumber(c.maxHeight);
      if (maxW !== null && maxH !== null)
        parts.push(
          t("appBuilder.constraintMaxDim", {
            w: Math.round(maxW),
            h: Math.round(maxH),
          }),
        );
    }
    return parts.length > 0 ? parts.join(" · ") : null;
  });

  /**
   * Text input constraints projected from the manifest input_schema. Powers
   * the `<TextEditor>` placeholder + character counter (V1 parity for TTS
   * models like MeloTTS that expose `maxChars` + locale-specific placeholder).
   */
  const textConstraints = computed<TextConstraints>(() => {
    const manifest = store.selectedManifest;
    const raw = (manifest?.input_schema ?? null) as Record<string, unknown> | null;
    if (raw === null) return {};
    if (asString(raw.kind) !== "text") return {};
    const c = (raw.constraints ?? {}) as Record<string, unknown>;
    const out: TextConstraints = {};
    const maxChars = asNumber(c.maxChars);
    if (maxChars !== null) out.maxLength = Math.round(maxChars);
    const placeholder = asString(c.placeholder);
    if (placeholder !== null) out.placeholder = placeholder;
    return out;
  });

  /**
   * Look up a task's authoritative label from the taxonomy tree (V1 parity:
   * keep "OCR"/"ASR" casing rather than humanizing the slug to "Ocr").
   */
  function taskLabelFromTree(taskId: string): string | null {
    const tree = store.taxonomyTree;
    if (tree === null) return null;
    for (const g of tree.groups) {
      const t = g.tasks.find((x) => x.id === taskId);
      if (t !== undefined) return t.label;
    }
    return null;
  }

  const classificationRows = computed<ClassificationRow[]>(() => {
    const model = store.selectedModel;
    if (model === null) return [];
    const rows: ClassificationRow[] = [];
    // Source: MANIFEST when the rich manifest is loaded, else REGISTRY (the
    // lean /models row). Mirrors V1's "Source" line.
    rows.push({
      labelKey: "appBuilder.classification.source",
      value: store.selectedManifest !== null ? "MANIFEST" : "REGISTRY",
    });
    const seg = model.taxonomy;
    if (seg.length >= 1 && seg[0]) {
      rows.push({ labelKey: "appBuilder.classification.group", value: humanize(seg[0]) });
    }
    if (seg.length >= 2 && seg[1]) {
      // Task label: prefer the authoritative human-readable label from the
      // taxonomy tree (e.g. "OCR" stays "OCR", not humanized to "Ocr"), then
      // the i18n task label, else humanize as a last resort.
      const taskLabel = taskLabelFromTree(seg[1]) ?? humanize(seg[1]);
      rows.push({ labelKey: "appBuilder.classification.task", value: taskLabel });
    }
    // Tags: V1 shows the manifest's `taxonomy.tags` (curated display tags),
    // not the top-level `tags`. The manifest endpoint surfaces these as
    // `taxonomy_tags`.
    const manifest = store.selectedManifest as Record<string, unknown> | null;
    const taxTags = manifest?.taxonomy_tags;
    const tags = Array.isArray(taxTags) ? taxTags : [];
    if (tags.length > 0) {
      rows.push({
        labelKey: "appBuilder.classification.tags",
        value: tags.map((x) => String(x)).join(", "),
      });
    }
    return rows;
  });

  return {
    allCards,
    cardsForSelection,
    selectedCard,
    taskGroups,
    taskNodes,
    outputSubtypeLabel,
    inputFields,
    paramDefs,
    variantOptions,
    cardVMFor,
    selectedInputKind,
    inputConstraintsHint,
    textConstraints,
    classificationRows,
  };
}
