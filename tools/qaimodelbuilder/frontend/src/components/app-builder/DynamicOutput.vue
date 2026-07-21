<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * DynamicOutput — App Builder result renderer (V1 `DynamicOutput.js` parity).
 *
 * Routes the run output to a task-specific viewer (V1 `DynamicOutput.js`
 * :197-209), renders the empty/queued/running status card and the rich error
 * card (OOM / not-installed / generic with copyable diagnostics segments), and
 * exposes a download menu (srt/vtt/json/md/png/wav). The 6 specialized
 * `outputs/*` viewers are reused as-is and fed normalized props.
 *
 * Dispatch (V1 parity):
 *   image / super-resolution / depth → ImageDiffViewer
 *   audio / text-to-speech           → AudioPlayer
 *   ocr (lines/blocks)               → OcrLayoutViewer
 *   speech-recognition (segments)    → TranscriptPlayer
 *   predictions[]                    → ClassificationTable
 *   detections[]                     → DetectionViewer
 *   otherwise                        → JsonView
 *
 * Uses global `.ab-output-*` classes from styles/app-builder/app-builder.css.
 */
import { computed, nextTick, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useAppBuilderStore } from "@/stores/appBuilder";
import { useToast } from "@/composables/useToast";
import JsonView from "@/components/app-builder/JsonView.vue";
import AudioPlayer from "@/components/app-builder/outputs/AudioPlayer.vue";
import ImageDiffViewer from "@/components/app-builder/outputs/ImageDiffViewer.vue";
import OcrLayoutViewer from "@/components/app-builder/outputs/OcrLayoutViewer.vue";
import TranscriptPlayer from "@/components/app-builder/outputs/TranscriptPlayer.vue";
import ClassificationTable from "@/components/app-builder/outputs/ClassificationTable.vue";
import DetectionViewer from "@/components/app-builder/outputs/DetectionViewer.vue";
import ErrorDiagnosticsPanel from "@/components/app-builder/outputs/ErrorDiagnosticsPanel.vue";
import { resolveAppBuilderAssetUrl } from "@/utils/appBuilderAssetUrl";
import type { AppRun } from "@/stores/appBuilder";

interface Props {
  /** The displayed run (current or snapshot). */
  run: AppRun | null;
  /** Run status: idle/queued/running/streaming/completed/failed/cancelled. */
  status: string;
  /** Output sub-type label shown in the section header (V1). */
  subtype?: string;
  /** Resolve an artifact relative path → blob URL (store.artifactBlobUrl). */
  resolveUrl: (path: string) => string;
}

const props = withDefaults(defineProps<Props>(), { subtype: "RESULT" });

const emit = defineEmits<{
  "send-to-chat": [];
  "re-run": [];
  "add-to-compare": [];
  "rate": [payload: { runId: string; rating: number }];
}>();

const { t } = useI18n();
const store = useAppBuilderStore();
const toast = useToast();

const output = computed<Record<string, unknown> | null>(
  () => props.run?.output ?? null,
);
const hasData = computed<boolean>(
  () => output.value !== null && Object.keys(output.value).length > 0,
);

// ── Quality rating (👍 / 👎) — V1 DynamicOutput.js:448-493 parity ──────────
// Rating semantics: null = unrated; 1 = 👍; -1 = 👎.
// Optimistic update: ratingValue is set immediately; the store action POSTs
// to /api/app-builder/feedback best-effort.
const ratingValue = ref<number | null>(null);
const ratingSubmitting = ref(false);

// Sync ratingValue when the displayed run changes (V1 parity: restore from
// run.rating if the server already has a record for this run).
watch(
  () => props.run?.id,
  (newId, oldId) => {
    if (newId !== oldId) {
      const existing = props.run?.rating;
      ratingValue.value =
        existing === 1 || existing === -1 || existing === 0 ? existing : null;
    }
  },
  { immediate: true },
);

async function submitRating(value: 1 | -1): Promise<void> {
  if (ratingSubmitting.value) return;
  const runId = props.run?.id;
  if (runId === null || runId === undefined) return;
  ratingSubmitting.value = true;
  // Optimistic update.
  ratingValue.value = value;
  try {
    await store.submitRating(runId, value);
    emit("rate", { runId, rating: value });
  } catch {
    // store.submitRating already logs; keep optimistic UI state.
  } finally {
    ratingSubmitting.value = false;
  }
}

// ── Output / Logs sub-view (V1 DynamicOutput.js viewMode parity) ───────────
// Logs are demuxed by the store from `payload.event === "log"` SSE frames
// (V1 NDJSON v3.1 `run.logs`). When the store carries them we render those
// directly; otherwise fall back to scanning `run.frames` for legacy fixtures
// and old hydrated history runs without a logs[] slot.
interface LogLine {
  stream: string;
  line: string;
}
const logs = computed<LogLine[]>(() => {
  const stored = props.run?.logs;
  if (Array.isArray(stored) && stored.length > 0) {
    return stored;
  }
  const frames = props.run?.frames ?? [];
  const out: LogLine[] = [];
  for (const f of frames) {
    const p = (f?.payload ?? {}) as Record<string, unknown>;
    const raw = p.log ?? p.line ?? (p.kind === "log" ? p.message : undefined);
    if (typeof raw === "string" && raw !== "") {
      const stream = typeof p.stream === "string" ? p.stream : "stdout";
      out.push({ stream, line: raw });
    }
  }
  return out;
});

const viewMode = ref<"output" | "logs">("output");
const logsContainerRef = ref<HTMLElement | null>(null);
const logsAutoScroll = ref(true);

function onLogsScroll(ev: Event): void {
  const el = ev.currentTarget as HTMLElement | null;
  if (el === null) return;
  logsAutoScroll.value =
    el.scrollHeight - el.scrollTop - el.clientHeight < 8;
}

watch(
  () => [logs.value.length, viewMode.value] as const,
  ([, mode]) => {
    if (mode !== "logs" || !logsAutoScroll.value) return;
    void nextTick(() => {
      const el = logsContainerRef.value;
      if (el !== null) el.scrollTop = el.scrollHeight;
    });
  },
);

/** A model task hint inferred from output shape (V1 used run.category). */
type Viewer =
  | "image-diff"
  | "audio"
  | "ocr"
  | "transcript"
  | "classification"
  | "detection"
  | "json";

const viewer = computed<Viewer>(() => {
  const d = output.value;
  if (d === null) return "json";
  if (typeof d.image_path === "string" || typeof d.depth_map_path === "string") {
    return "image-diff";
  }
  if (typeof d.audio_path === "string") return "audio";
  if (Array.isArray(d.predictions)) return "classification";
  if (Array.isArray(d.detections)) return "detection";
  if (Array.isArray(d.segments)) return "transcript";
  if (Array.isArray(d.lines) || Array.isArray(d.blocks)) return "ocr";
  return "json";
});

// ── normalized props for each viewer ───────────────────────────────────────

function url(path: unknown): string {
  return typeof path === "string" && path !== "" ? props.resolveUrl(path) : "";
}

/**
 * Resolve a run INPUT path (e.g. `run.inputs.image` / `run.inputs.audio`) to a
 * browser-usable URL (V1 `resolveAssetUrl` parity). V2 inputs may hold a
 * dataURL (ImageDropzone) or a blob: / object URL (AudioInput) passed through
 * as-is; http(s) URLs likewise. A repo-internal / absolute disk path under
 * `data/blobs/chat/` (e.g. a chat-triggered run whose source image was persisted
 * to `data/blobs/chat/[date]/appbuilder-x/img.png`) is rewritten onto the backend
 * static mount `/api/images/files/...` so the browser loads it over HTTP
 * instead of refusing a `file://` local resource (problem 6).
 */
function inputUrl(value: unknown): string {
  return resolveAppBuilderAssetUrl(value);
}

/** Coerce a runner size field (`[w, h]`) to a `[number, number]` or null. */
function sizePair(value: unknown): [number, number] | null {
  if (Array.isArray(value) && value.length === 2) {
    const w = Number(value[0]);
    const h = Number(value[1]);
    if (Number.isFinite(w) && Number.isFinite(h)) return [w, h];
  }
  return null;
}

const imageDiff = computed(() => {
  const d = output.value ?? {};
  const after = url(d.image_path ?? d.depth_map_path ?? d.output_path);
  const before = url(d.source_path ?? d.input_path ?? d.original_path);
  // Size / scale / tiled meta (V1 ImageDiffViewer.js:140-164 parity).
  const scaleRaw = d.scale;
  return {
    beforeUrl: before || after,
    afterUrl: after,
    modelInSize: sizePair(d.model_in_size),
    modelOutSize: sizePair(d.model_out_size),
    sourceInSize: sizePair(d.in_size),
    sourceOutSize: sizePair(d.out_size),
    scale:
      typeof scaleRaw === "number" || typeof scaleRaw === "string"
        ? scaleRaw
        : null,
    tiled: d.tiled === true,
  };
});

const audio = computed(() => {
  const d = output.value ?? {};
  return {
    url: url(d.audio_path),
    duration_seconds:
      typeof d.duration_s === "number"
        ? d.duration_s
        : typeof d.duration_seconds === "number"
          ? d.duration_seconds
          : undefined,
    sample_rate: typeof d.sample_rate === "number" ? d.sample_rate : undefined,
    format: typeof d.format === "string" ? d.format : undefined,
  };
});

interface OcrLine {
  box: number[];
  text: string;
  conf: number;
  line_idx?: number;
}
const ocr = computed<{
  imageUrl: string;
  pageSize: [number, number] | null;
  fullText: string;
  langDetected: string;
  lines: OcrLine[];
}>(() => {
  const d = output.value ?? {};
  // V1/manifest schema: lines[].box is an 8-point pixel quadrilateral +
  // conf in [0,1]; `blocks` is a legacy alias for the same shape.
  const raw = (Array.isArray(d.lines) ? d.lines : d.blocks) as unknown[];
  const lines: OcrLine[] = (Array.isArray(raw) ? raw : []).map((b) => {
    const o = (b ?? {}) as Record<string, unknown>;
    const box = Array.isArray(o.box)
      ? o.box.map((n) => Number(n) || 0)
      : Array.isArray(o.bbox)
        ? o.bbox.map((n) => Number(n) || 0)
        : [];
    const conf =
      typeof o.conf === "number"
        ? o.conf
        : typeof o.confidence === "number"
          ? o.confidence
          : 0;
    return {
      box,
      text: typeof o.text === "string" ? o.text : "",
      conf,
      ...(typeof o.line_idx === "number" ? { line_idx: o.line_idx } : {}),
    };
  });
  const ps = Array.isArray(d.page_size) && d.page_size.length === 2
    ? ([Number(d.page_size[0]) || 0, Number(d.page_size[1]) || 0] as [
        number,
        number,
      ])
    : null;
  // OCR source image is the run INPUT image (output carries no image path).
  const inputs = props.run?.inputs ?? {};
  const imageUrl = inputUrl(
    inputs.image ?? inputs.path ?? d.image_path ?? d.source_path,
  );
  return {
    imageUrl,
    pageSize: ps,
    fullText: typeof d.fullText === "string" ? d.fullText : "",
    langDetected: typeof d.lang_detected === "string" ? d.lang_detected : "",
    lines,
  };
});

interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
  conf?: number;
}
const transcript = computed<{
  audioUrl: string;
  fullText: string;
  language: string;
  segments: TranscriptSegment[];
}>(() => {
  const d = output.value ?? {};
  const raw = Array.isArray(d.segments) ? d.segments : [];
  const segments: TranscriptSegment[] = raw.map((s) => {
    const o = (s ?? {}) as Record<string, unknown>;
    return {
      start: Number(o.start) || 0,
      end: Number(o.end) || 0,
      text: typeof o.text === "string" ? o.text : "",
      // Whisper carries per-segment conf in [0,1]; Zipformer omits it.
      ...(typeof o.conf === "number" ? { conf: o.conf } : {}),
    };
  });
  // Audio source is the run INPUT audio (ASR output has no audio path) so the
  // user can listen while reading (V1 TranscriptPlayer parity).
  const inputs = props.run?.inputs ?? {};
  const audioUrl = inputUrl(
    inputs.audio ?? inputs.path ?? d.audio_path ?? d.source_path,
  );
  return {
    audioUrl,
    fullText: typeof d.fullText === "string" ? d.fullText : "",
    language: typeof d.language === "string" ? d.language : "",
    segments,
  };
});

interface ClassificationResult {
  label: string;
  confidence: number;
  /** Raw class index (V1 `class_idx`); shown as `class_N` when no label. */
  classIdx?: number;
  /** Whether the model carries human-readable labels (V1 `has_labels`). */
  hasLabel: boolean;
}
const classification = computed<{
  results: ClassificationResult[];
  topK: number | null;
  numClasses: number | null;
}>(() => {
  const d = output.value ?? {};
  const raw = Array.isArray(d.predictions) ? d.predictions : [];
  const results = raw.map((p) => {
    const o = (p ?? {}) as Record<string, unknown>;
    const hasLabel = typeof o.label === "string" && o.label !== "";
    const classIdx =
      typeof o.class_idx === "number"
        ? o.class_idx
        : typeof o.class === "number"
          ? o.class
          : undefined;
    return {
      label: hasLabel
        ? (o.label as string)
        : classIdx !== undefined
          ? `class_${classIdx}`
          : String(o.class ?? "—"),
      confidence: Number(o.confidence ?? o.score) || 0,
      ...(classIdx !== undefined ? { classIdx } : {}),
      hasLabel,
    };
  });
  // V1 ClassificationTable.js:30-31 parity: top_k defaults to the row count;
  // num_classes drives the "Top K / N classes" footer (hidden when absent).
  const topK =
    typeof d.top_k === "number" ? d.top_k : results.length || null;
  const numClasses =
    typeof d.num_classes === "number" ? d.num_classes : null;
  return { results, topK, numClasses };
});

interface Detection {
  label: string;
  confidence: number;
  bbox: [number, number, number, number];
}
const detection = computed<{ imageUrl: string; detections: Detection[] }>(() => {
  const d = output.value ?? {};
  const raw = Array.isArray(d.detections) ? d.detections : [];
  const detections: Detection[] = raw.map((det) => {
    const o = (det ?? {}) as Record<string, unknown>;
    const box = Array.isArray(o.bbox) ? o.bbox : [0, 0, 0, 0];
    return {
      label: typeof o.label === "string" ? o.label : "—",
      confidence: Number(o.confidence ?? o.score) || 0,
      bbox: [Number(box[0]) || 0, Number(box[1]) || 0, Number(box[2]) || 0, Number(box[3]) || 0],
    };
  });
  return { imageUrl: url(d.image_path ?? d.source_path), detections };
});

// ── status card (idle/queued/running/cancelled) — V1 parity ────────────────

const showStatusCard = computed<boolean>(
  () => !hasData.value && props.status !== "completed" && props.status !== "failed",
);

const statusCard = computed<{ icon: string; text: string; spinner: boolean; suffix?: string }>(() => {
  // G4 — queue-position card has precedence over the per-status text. A
  // positive `queuePosition` means the run is waiting behind another run for
  // the NPU lock (V1 `DynamicOutput.js:541-556` "Queued · N ahead"). The
  // backend emits this while the run's DB status is already `streaming`
  // (the runner waits for the lock AFTER the use case marks streaming), so
  // we cannot rely on `props.status === "queued"` alone — check the position
  // first. Cleared to null by `frames.ts` once the run leaves the queue.
  const queuePos = props.run?.queuePosition ?? null;
  if (queuePos !== null && queuePos > 0) {
    return {
      icon: "⋯",
      text: t("appBuilder.statusQueued"),
      spinner: true,
      suffix: t("appBuilder.queue.ahead", { n: queuePos }),
    };
  }
  switch (props.status) {
    case "queued":
    case "pending": {
      // V1 DynamicOutput.js:541-556 parity:
      // pos > 0 → handled above; pos === 0 / null → show "Preparing"
      return { icon: "", text: t("appBuilder.statusPreparing"), spinner: true };
    }
    case "preparing": {
      // V1 DynamicOutput.js:558-570 parity: statusHint drives fine-grained text.
      const hint = props.run?.statusHint;
      if (hint === "loading_model") {
        return { icon: "", text: t("appBuilder.statusLoadingModel"), spinner: true };
      }
      if (hint === "model_loaded") {
        return { icon: "", text: t("appBuilder.statusModelLoaded"), spinner: true };
      }
      if (hint === "model_cached") {
        return { icon: "", text: t("appBuilder.statusModelCached"), spinner: true };
      }
      return { icon: "", text: t("appBuilder.statusPreparing"), spinner: true };
    }
    case "running":
    case "streaming":
      return { icon: "", text: t("appBuilder.statusRunning"), spinner: true };
    case "cancelled":
      return { icon: "⊘", text: t("appBuilder.statusCancelled"), spinner: false };
    default:
      return { icon: "↗", text: t("appBuilder.statusIdle"), spinner: false };
  }
});

// ── error card (V1 errorKind: oom / not-installed / generic) ───────────────

const isError = computed<boolean>(
  () => props.status === "failed" && (props.run?.error ?? null) !== null,
);

const errorMessage = computed<string>(() => props.run?.error ?? "");

const errorKind = computed<"oom" | "not-installed" | "generic">(() => {
  const msg = errorMessage.value.toUpperCase();
  if (msg.includes("OUT_OF_MEMORY") || msg.includes("OOM")) return "oom";
  if (
    msg.includes("WEIGHTS_NOT_INSTALLED") ||
    msg.includes("ASSETS_NOT_INSTALLED") ||
    msg.includes("MODEL_NOT_INSTALLED")
  ) {
    return "not-installed";
  }
  return "generic";
});

// ── download menu (V1 parity: copy + srt/vtt/json/md/png/wav) ──────────────

const downloadOpen = ref(false);
function toggleDownload(): void {
  downloadOpen.value = !downloadOpen.value;
}

interface DownloadFormat {
  fmt: string;
  desc: string;
}
const downloadFormats = computed<DownloadFormat[]>(() => {
  const d = output.value;
  if (d === null) return [];
  const formats: DownloadFormat[] = [];
  if (
    typeof d.fullText === "string" ||
    Array.isArray(d.lines) ||
    Array.isArray(d.blocks)
  ) {
    formats.push({ fmt: "txt", desc: t("appBuilder.downloadDesc.plainText") });
    formats.push({ fmt: "md", desc: "Markdown" });
  }
  if (Array.isArray(d.segments)) {
    formats.push({ fmt: "srt", desc: "SubRip" });
    formats.push({ fmt: "vtt", desc: "WebVTT" });
  }
  if (typeof d.image_path === "string" || typeof d.depth_map_path === "string") {
    formats.push({ fmt: "png", desc: t("appBuilder.downloadDesc.image") });
  }
  if (typeof d.audio_path === "string") {
    formats.push({ fmt: "wav", desc: t("appBuilder.downloadDesc.audio") });
  }
  formats.push({ fmt: "json", desc: t("appBuilder.downloadDesc.rawJson") });
  return formats;
});

function ocrLines(): unknown[] | null {
  // OCR runners may emit the rows under `lines` (ppocrv4) or the legacy
  // `blocks` alias — the display path accepts both, so the export/download
  // path must too (otherwise md/txt for a `blocks`-only runner silently
  // degrades to coreText / hides the buttons).
  const d = output.value;
  if (d === null) return null;
  if (Array.isArray(d.lines)) return d.lines;
  if (Array.isArray(d.blocks)) return d.blocks;
  return null;
}

function coreText(): string {
  const d = output.value;
  if (d === null) return "";
  if (typeof d.fullText === "string") return d.fullText;
  const lines = ocrLines();
  if (lines !== null) {
    // V1 `DynamicOutput.js:392`: fallback to each line's `.text` (NOT the raw
    // line object, which would stringify to "[object Object]").
    return lines
      .map((l) => {
        const o = (l ?? {}) as Record<string, unknown>;
        return typeof o.text === "string" ? o.text : String(l);
      })
      .join("\n");
  }
  if (Array.isArray(d.segments)) {
    return d.segments
      .map((s) => {
        const o = (s ?? {}) as Record<string, unknown>;
        return typeof o.text === "string" ? o.text : "";
      })
      .join("\n");
  }
  try {
    return JSON.stringify(d, null, 2);
  } catch {
    return String(d);
  }
}

function buildSegmentsSrt(): string {
  const d = output.value ?? {};
  const segs = Array.isArray(d.segments) ? d.segments : [];
  return segs
    .map((s, i) => {
      const o = (s ?? {}) as Record<string, unknown>;
      const start = fmtTimecode(Number(o.start) || 0, true);
      const end = fmtTimecode(Number(o.end) || 0, true);
      return `${i + 1}\n${start} --> ${end}\n${String(o.text ?? "")}\n`;
    })
    .join("\n");
}

function buildSegmentsVtt(): string {
  const d = output.value ?? {};
  const segs = Array.isArray(d.segments) ? d.segments : [];
  const body = segs
    .map((s) => {
      const o = (s ?? {}) as Record<string, unknown>;
      const start = fmtTimecode(Number(o.start) || 0, false);
      const end = fmtTimecode(Number(o.end) || 0, false);
      return `${start} --> ${end}\n${String(o.text ?? "")}\n`;
    })
    .join("\n");
  return `WEBVTT\n\n${body}`;
}

function fmtTimecode(seconds: number, srt: boolean): string {
  const ms = Math.floor((seconds % 1) * 1000);
  const total = Math.floor(seconds);
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  const sep = srt ? "," : ".";
  return `${h}:${m}:${s}${sep}${String(ms).padStart(3, "0")}`;
}

function downloadBlob(name: string, mime: string, content: string): void {
  try {
    const blob = new Blob([content], { type: mime });
    const href = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = href;
    a.download = name;
    a.click();
    URL.revokeObjectURL(href);
  } catch {
    // ignore — download is best-effort
  }
}

/**
 * Trigger a real file download from a (resolved) URL via a hidden `<a download>`,
 * matching V1 `DynamicOutput.js:394-410` for PNG/WAV artifacts. V1 set
 * `a.download` to the file's basename so the browser saves the file instead of
 * navigating to / inlining it (window.open inlined images/audio — a regression).
 */
function downloadFromUrl(resolvedUrl: string, suggestedName: string): void {
  if (resolvedUrl === "") return;
  try {
    const a = document.createElement("a");
    a.href = resolvedUrl;
    a.download = suggestedName;
    // Some browsers ignore `download` for cross-origin URLs; opening in a new
    // tab is the documented fallback. Same-origin static mounts honor it.
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch {
    // ignore — download is best-effort
  }
}

/** Extract the file basename from a runner artifact path (V1 split on / or \). */
function baseName(path: string, fallback: string): string {
  const parts = path.split(/[\\/]/);
  const last = parts[parts.length - 1];
  return last !== undefined && last !== "" ? last : fallback;
}

/**
 * Build a Markdown export (V1 `DynamicOutput.js:386-390` parity for OCR:
 * `# OCR Result` + a `N. text` numbered list). For ASR (segments) we emit the
 * same numbered structure from segment text; otherwise fall back to plain text.
 */
function buildMarkdown(): string {
  const d = output.value ?? {};
  const lines = ocrLines();
  if (lines !== null) {
    const body = lines
      .map((l, i) => {
        const o = (l ?? {}) as Record<string, unknown>;
        return `${i + 1}. ${String(o.text ?? "")}`;
      })
      .join("\n");
    return `# OCR Result\n\n${body}\n`;
  }
  if (Array.isArray(d.segments)) {
    const body = d.segments
      .map((s, i) => {
        const o = (s ?? {}) as Record<string, unknown>;
        return `${i + 1}. ${String(o.text ?? "")}`;
      })
      .join("\n");
    return `# Transcript\n\n${body}\n`;
  }
  return coreText();
}

function pickDownload(fmt: string): void {
  downloadOpen.value = false;
  const d = output.value;
  if (d === null) return;
  const base = props.run?.modelId ?? "output";
  switch (fmt) {
    case "txt":
      downloadBlob(`${base}.txt`, "text/plain", coreText());
      break;
    case "md":
      downloadBlob(`${base}.md`, "text/markdown", buildMarkdown());
      break;
    case "srt":
      downloadBlob(`${base}.srt`, "text/plain", buildSegmentsSrt());
      break;
    case "vtt":
      downloadBlob(`${base}.vtt`, "text/vtt", buildSegmentsVtt());
      break;
    case "png": {
      const p = (d.image_path ?? d.depth_map_path) as string | undefined;
      if (typeof p === "string") {
        downloadFromUrl(url(p), baseName(p, `${base}.png`));
      }
      break;
    }
    case "wav": {
      if (typeof d.audio_path === "string") {
        downloadFromUrl(url(d.audio_path), baseName(d.audio_path, `${base}.wav`));
      }
      break;
    }
    default:
      downloadBlob(`${base}.json`, "application/json", JSON.stringify(d, null, 2));
  }
}

async function copyOutput(): Promise<void> {
  downloadOpen.value = false;
  try {
    await navigator.clipboard.writeText(coreText());
    toast.success(t("appBuilder.copied"));
  } catch {
    toast.error(t("appBuilder.copyFailed"));
  }
}
</script>

<template>
  <div class="ab-output-root">
    <!-- toolbar -->
    <div class="ab-output-toolbar">
      <div
        class="ab-download-menu"
        @click.stop
      >
        <button
          type="button"
          class="ab-output-tool"
          :disabled="!hasData"
          :aria-expanded="downloadOpen ? 'true' : 'false'"
          :title="t('appBuilder.downloadOutput')"
          @click="toggleDownload"
        >
          <span aria-hidden="true">⬇</span>
          {{ t("appBuilder.download") }}
          <span
            aria-hidden="true"
            style="opacity: 0.6"
          >▾</span>
        </button>
        <ul
          v-if="downloadOpen"
          class="ab-download-menu-list"
          role="menu"
        >
          <li
            class="ab-download-menu-item"
            role="menuitem"
            @click="copyOutput"
          >
            <span class="ab-download-menu-fmt">COPY</span>
            <span class="ab-download-menu-label">{{ t("appBuilder.copyOutput") }}</span>
          </li>
          <li
            class="ab-download-menu-divider"
            role="separator"
          ></li>
          <li
            v-for="opt in downloadFormats"
            :key="opt.fmt"
            class="ab-download-menu-item"
            role="menuitem"
            @click="pickDownload(opt.fmt)"
          >
            <span class="ab-download-menu-fmt">{{ opt.fmt.toUpperCase() }}</span>
            <span class="ab-download-menu-label">{{ opt.desc }}</span>
          </li>
        </ul>
      </div>
      <button
        type="button"
        class="ab-output-tool primary"
        :disabled="!hasData"
        :title="t('appBuilder.sendToChatTip')"
        @click="emit('send-to-chat')"
      >
        <span aria-hidden="true">↗</span>
        {{ t("appBuilder.sendToChat") }}
      </button>
      <button
        type="button"
        class="ab-output-tool"
        :title="t('appBuilder.reRun')"
        @click="emit('re-run')"
      >
        <span aria-hidden="true">↻</span>
        {{ t("appBuilder.reRun") }}
      </button>
      <div class="dy-toolbar-right">
        <div
          v-if="run && run.status === 'completed' && run.id"
          class="dy-actions-compact"
        >
          <!-- 👍/👎 rating buttons (V1 DynamicOutput.js:711-727 parity) -->
          <button
            type="button"
            class="ab-output-tool dy-rate-btn"
            :class="{ 'dy-rated': ratingValue === 1 }"
            :disabled="ratingSubmitting"
            :title="t('appBuilder.rateGood')"
            @click="submitRating(1)"
          >
            <span aria-hidden="true">👍</span>
          </button>
          <button
            type="button"
            class="ab-output-tool dy-rate-btn"
            :class="{ 'dy-rated': ratingValue === -1 }"
            :disabled="ratingSubmitting"
            :title="t('appBuilder.rateBad')"
            @click="submitRating(-1)"
          >
            <span aria-hidden="true">👎</span>
          </button>
          <button
            v-if="hasData"
            type="button"
            class="ab-output-tool dy-compare-btn icon-only"
            :aria-label="t('appBuilder.addToCompare')"
            :title="t('appBuilder.addToCompare')"
            @click="emit('add-to-compare')"
          >
            <span aria-hidden="true">⊞</span>
          </button>
        </div>
        <button
          v-else-if="hasData"
          type="button"
          class="ab-output-tool dy-compare-btn icon-only"
          :aria-label="t('appBuilder.addToCompare')"
          :title="t('appBuilder.addToCompare')"
          @click="emit('add-to-compare')"
        >
          <span aria-hidden="true">⊞</span>
        </button>
        <div
          class="ab-output-view-tabs"
          role="tablist"
        >
          <button
            type="button"
            role="tab"
            :class="{ active: viewMode === 'output' }"
            :aria-selected="viewMode === 'output' ? 'true' : 'false'"
            @click="viewMode = 'output'"
          >
            {{ t("appBuilder.viewMode.output") }}
          </button>
          <button
            type="button"
            role="tab"
            :class="{ active: viewMode === 'logs' }"
            :aria-selected="viewMode === 'logs' ? 'true' : 'false'"
            @click="viewMode = 'logs'"
          >
            {{ t("appBuilder.viewMode.logs") }}
            <span
              v-if="logs.length"
              class="ab-output-mode-count"
            >{{ logs.length }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- body -->
    <div class="ab-output-body">
      <template v-if="viewMode === 'output'">
        <!-- error card (V1 parity: 6 segments + per-segment ⧉ copy + Copy diagnostics) -->
        <ErrorDiagnosticsPanel
          v-if="isError"
          :error="errorMessage"
          :error-detail="run?.errorDetail ?? null"
          :error-kind="errorKind"
          :run-logs="logs"
          @send-error-to-chat="emit('send-to-chat')"
        />

        <!-- status card -->
        <div
          v-else-if="showStatusCard"
          class="ab-output-status-card"
          :data-status="status"
        >
          <div
            v-if="statusCard.spinner"
            class="ab-output-spinner"
            aria-hidden="true"
          ></div>
          <div
            v-else
            class="ab-output-status-icon"
            aria-hidden="true"
          >
            {{ statusCard.icon }}
          </div>
          <div class="ab-output-status-msg">
            {{ statusCard.text }}
            <span
              v-if="statusCard.suffix"
              class="ab-output-status-suffix"
            > · {{ statusCard.suffix }}</span>
          </div>
        </div>

        <!-- viewers -->
        <template v-else>
          <ImageDiffViewer
            v-if="viewer === 'image-diff'"
            :before-url="imageDiff.beforeUrl"
            :after-url="imageDiff.afterUrl"
            :model-in-size="imageDiff.modelInSize"
            :model-out-size="imageDiff.modelOutSize"
            :source-in-size="imageDiff.sourceInSize"
            :source-out-size="imageDiff.sourceOutSize"
            :scale="imageDiff.scale"
            :tiled="imageDiff.tiled"
          />
          <AudioPlayer
            v-else-if="viewer === 'audio'"
            :data="audio"
          />
          <OcrLayoutViewer
            v-else-if="viewer === 'ocr'"
            :lines="ocr.lines"
            :image-url="ocr.imageUrl"
            :page-size="ocr.pageSize"
            :full-text="ocr.fullText"
            :lang-detected="ocr.langDetected"
          />
          <TranscriptPlayer
            v-else-if="viewer === 'transcript'"
            :segments="transcript.segments"
            :audio-url="transcript.audioUrl"
            :full-text="transcript.fullText"
            :language="transcript.language"
          />
          <ClassificationTable
            v-else-if="viewer === 'classification'"
            :results="classification.results"
            :top-k="classification.topK"
            :num-classes="classification.numClasses"
          />
          <DetectionViewer
            v-else-if="viewer === 'detection'"
            :image-url="detection.imageUrl"
            :detections="detection.detections"
          />
          <JsonView
            v-else
            :data="output"
          />
        </template>
      </template>

      <!-- logs sub-view (V1 parity) -->
      <template v-else-if="viewMode === 'logs'">
        <div
          ref="logsContainerRef"
          class="ab-output-logs"
          @scroll="onLogsScroll"
        >
          <div
            v-if="!logs.length"
            class="ab-output-logs-empty"
          >
            {{ t("appBuilder.logs.empty") }}
          </div>
          <div
            v-for="(l, i) in logs"
            :key="i"
            class="ab-output-log-line"
            :class="'log-' + l.stream"
          >
            <span class="ab-output-log-line-num">{{ i + 1 }}</span>
            <span class="ab-output-log-line-text">{{ l.line }}</span>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>
