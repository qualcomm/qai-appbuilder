// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Pure helpers for the context-compression ratio sliders in
 * `AppConfigPanel.vue` (Settings → App Config → Agent Loop).
 *
 * Two sliders let the user tune context compression, persisted under the
 * forge.config `chat.*` document as 0.0..1.0 floats:
 *
 * - `compaction_target_ratio`  — "post-compression keep size": what fraction of
 *   the model window the context is compressed down to (default 0.35).
 * - `compaction_protect_ratio` — "recent history protection": what fraction of
 *   the window is kept verbatim and never compressed (default 0.35).
 *
 * Extracted as pure functions so the clamp/label/invariant logic is unit-tested
 * in isolation (the component itself only wires these to the sliders + payload).
 */

/** The slider's UI working range for the keep/protect ratios. */
export const COMPACTION_RATIO_MIN = 0.2;
export const COMPACTION_RATIO_MAX = 0.6;
export const COMPACTION_RATIO_DEFAULT = 0.35;

/**
 * Clamp a compaction ratio into the slider's working band. A non-finite value
 * falls back to the default 0.35 (so a corrupt persisted value never poisons
 * the slider).
 */
export function clampCompactionRatio(v: number): number {
  if (!Number.isFinite(v)) return COMPACTION_RATIO_DEFAULT;
  return Math.min(COMPACTION_RATIO_MAX, Math.max(COMPACTION_RATIO_MIN, v));
}

/** Format a 0.0..1.0 ratio as a whole-percent label (0.35 → "35%"). */
export function compactionRatioPercent(v: number): string {
  return `${Math.round(v * 100)}%`;
}

/**
 * Resolve the persisted `{target, protect}` pair from the raw slider values,
 * applying the backend's invariant: the protected recent region can never
 * exceed the post-compression target (otherwise a compression pass would be a
 * no-op). Both are clamped first; `protect` is then capped at `target`.
 */
export function resolveCompactionRatios(
  rawTarget: number,
  rawProtect: number,
): { target: number; protect: number } {
  const target = clampCompactionRatio(rawTarget);
  const protect = Math.min(clampCompactionRatio(rawProtect), target);
  return { target, protect };
}
