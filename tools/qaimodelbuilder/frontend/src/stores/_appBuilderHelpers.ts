// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Pure module-level helpers extracted from `appBuilder.ts` to keep that
 * store within the cohesion budget. No reactive state, no Pinia/Vue
 * imports — these are unit-testable pure functions/constants reused by
 * the store. The frame demuxer (`applyFrameToRun`) stays in the store
 * because it depends on the local `AppRun` type and is the only piece
 * that mutates run state.
 */

/** FIFO cap for the compare tray (V1 parity). */
export const COMPARE_MAX = 4;

/** Terminal `AppRun.status` values — drives polling stop / counter logic. */
export const TERMINAL_STATES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

/** True when a run's status is terminal (completed / failed / cancelled). */
export function isTerminal(status: string): boolean {
  return TERMINAL_STATES.has(status);
}

/** Local-storage key for the last-used App Builder model id (V1 parity). */
export const LAST_MODEL_KEY = "qai-ab-last-model";

/**
 * Reverse of the backend `LEGACY_CATEGORY_MAP`: map a `group/task` taxonomy
 * pair to the short category code V1 used on cards + in send-to-chat summaries
 * (ASR / OCR / TTS / SR / LLM). Keyed by "group/task". Mirrors
 * `useAppBuilderWorkbench.ts` `CATEGORY_CODE_BY_TAXONOMY` (kept here as a pure
 * helper so the store can derive the code without importing the composable).
 */
const CATEGORY_CODE_BY_TAXONOMY: Readonly<Record<string, string>> = Object.freeze({
  "audio/speech-recognition": "ASR",
  "audio/audio-generation": "TTS",
  "computer-vision/ocr": "OCR",
  "computer-vision/super-resolution": "SR",
  "generative-ai/text-generation": "LLM",
});

/**
 * Derive the V1-parity short category code (ASR/OCR/TTS/SR/…) from a taxonomy
 * segment list. Prefers the known `group/task` legacy code; falls back to the
 * most-specific (last) segment so the value is never empty.
 */
export function deriveCategoryCode(
  taxonomy: readonly string[] | null | undefined,
): string | null {
  if (!Array.isArray(taxonomy) || taxonomy.length === 0) return null;
  if (taxonomy.length >= 2) {
    const code = CATEGORY_CODE_BY_TAXONOMY[`${taxonomy[0]}/${taxonomy[1]}`];
    if (code !== undefined) return code;
  }
  return taxonomy[taxonomy.length - 1] ?? null;
}

/**
 * Pull the list of required field names from an input schema, supporting
 * both JSON-Schema (`{ required: string[] }`) and V1's multi-field shape
 * (`{ kind: "multi", fields: [{ name, required }] }`).
 */
export function extractRequiredFields(
  inputSchema: Record<string, unknown> | null,
): string[] {
  if (inputSchema === null) return [];
  // Shape A (JSON-Schema): { required: string[] }
  const req = inputSchema.required;
  if (Array.isArray(req)) {
    return req.filter((x): x is string => typeof x === "string");
  }
  // Shape B (V1 multi): { kind:'multi', fields:[{name,required}] }
  const fields = inputSchema.fields;
  if (Array.isArray(fields)) {
    return fields
      .filter(
        (f): f is { name: string; required?: boolean } =>
          typeof f === "object" &&
          f !== null &&
          typeof (f as { name?: unknown }).name === "string" &&
          (f as { required?: unknown }).required === true,
      )
      .map((f) => f.name);
  }
  return [];
}

/** Context for {@link summariseOutput} — model name + category for V1-parity
 * readable summaries. Both optional so legacy callers degrade gracefully. */
export interface SummariseContext {
  /** Display name for the `[Model]` prefix (V1 `run.modelName`). */
  modelName?: string | null;
  /** Short category code (ASR / OCR / TTS / SR / …) — V1 `run.category`. */
  category?: string | null;
}

function asFiniteNumber(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function tag(ctx: SummariseContext | undefined, fallback: string): string {
  const name =
    ctx !== undefined &&
    typeof ctx.modelName === "string" &&
    ctx.modelName.trim() !== ""
      ? ctx.modelName.trim()
      : fallback;
  return `[${name}]`;
}

/**
 * Compact, human-readable summary of a run output payload (V1
 * `DynamicOutput.buildSendPayload` parity, DynamicOutput.js:38-147).
 *
 * V1 produced a per-category readable headline (NOT a raw file path):
 *   - SR    `[Model] 512×512 → 2048×2048 ×4 · 120 ms`
 *   - OCR   `[Model] 12 lines — "preview…"`
 *   - ASR   `[Model] 5 segments · zh — "preview…"`
 *   - TTS   `[Model] 3.45s · 24000Hz`
 *   - text  the full text (capped)
 * Audio / image assets are forwarded as real attachments by the bridge
 * (`_buildAttachmentsFromOutput`), so a bare path is the LAST-RESORT fallback
 * only — Bug 2-C was caused by the old generic version returning
 * `result: data/outputs/x.wav` for TTS instead of the readable headline.
 *
 * `ctx` carries the model name + category so the summary can match V1's
 * branching (the store resolves these from `selectedModel`); when omitted the
 * function still degrades to a sensible text/JSON summary.
 */
export function summariseOutput(
  output: Record<string, unknown>,
  ctx?: SummariseContext,
): string {
  const cat = ctx?.category ?? null;

  // TTS (V1 DynamicOutput.js:122-138): duration + sample rate headline.
  // The audio file itself rides along as an attachment, so never emit a path.
  const durRaw = asFiniteNumber(output.duration_s);
  if (cat === "TTS" || (durRaw !== null && typeof output.audio_path === "string")) {
    const dur = durRaw ?? 0;
    const sr = asFiniteNumber(output.sample_rate);
    const head = `${tag(ctx, "TTS")} ${dur.toFixed(2)}s`;
    return sr !== null ? `${head} · ${sr}Hz` : head;
  }

  // SR / image super-resolution (V1 DynamicOutput.js:64-82): sizes + scale + latency.
  const inSz = Array.isArray(output.in_size) ? output.in_size : null;
  const outSz = Array.isArray(output.out_size) ? output.out_size : null;
  if (cat === "SR" || (inSz !== null && outSz !== null)) {
    const parts = [tag(ctx, "SR")];
    if (inSz !== null && outSz !== null) {
      parts.push(`${inSz[0]}×${inSz[1]} → ${outSz[0]}×${outSz[1]}`);
    }
    const scale = asFiniteNumber(output.scale);
    if (scale !== null) parts.push(`×${scale}`);
    if (parts.length > 1) return parts.join(" ");
  }

  // OCR (V1 DynamicOutput.js:86-101): line count + preview.
  if (cat === "OCR" && Array.isArray(output.lines)) {
    const lineCount = output.lines.length;
    const fullText =
      typeof output.fullText === "string" ? output.fullText : "";
    const head = `${tag(ctx, "OCR")} ${lineCount} lines`;
    const preview =
      fullText.length > 80 ? `${fullText.slice(0, 77)}…` : fullText;
    return preview !== "" ? `${head} — "${preview}"` : head;
  }

  // ASR (V1 DynamicOutput.js:105-119): segment count + language + preview.
  if (cat === "ASR" && Array.isArray(output.segments)) {
    const segCount = output.segments.length;
    const lang = typeof output.language === "string" ? output.language : "";
    const fullText =
      typeof output.fullText === "string" ? output.fullText : "";
    const head = `${tag(ctx, "ASR")} ${segCount} segments${lang !== "" ? ` · ${lang}` : ""}`;
    const preview =
      fullText.length > 80 ? `${fullText.slice(0, 77)}…` : fullText;
    return preview !== "" ? `${head} — "${preview}"` : head;
  }

  // Generic text outputs (OCR/ASR fullText, LLM text) — return the text itself.
  if (typeof output.fullText === "string" && output.fullText !== "") {
    return output.fullText.slice(0, 2000);
  }
  if (Array.isArray(output.lines)) {
    return output.lines.map((l) => String(l)).join("\n").slice(0, 2000);
  }
  if (typeof output.text === "string" && output.text !== "") {
    return output.text.slice(0, 2000);
  }

  // Last resort: a labelled headline (no bare path in the readable text — the
  // asset is attached separately). Only fall back to a path string when we
  // have NOTHING else descriptive, and keep it labelled so it isn't mistaken
  // for the result content (Bug 2-C: this used to be the primary TTS branch).
  for (const key of ["image_path", "audio_path", "output_path", "depth_map_path"]) {
    if (typeof output[key] === "string") {
      return `${tag(ctx, "Model")} result`;
    }
  }
  try {
    return JSON.stringify(output).slice(0, 2000);
  } catch {
    return "result";
  }
}
