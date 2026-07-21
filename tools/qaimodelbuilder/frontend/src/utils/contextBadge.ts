// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Context-usage badge formatters (V1 index.html:2231-2249 parity).
 *
 * Pure presentation helpers extracted from `ChatComposer.vue` (F1④
 * cohesion split). These format the per-conversation context-token
 * estimate into the V1 "~12K / 200K 5%" badge segments. Keeping them as
 * standalone pure functions (rather than inline component methods) lets
 * both the toolbar badge and the footer badge share one source of truth
 * and makes them trivially unit-testable without mounting the composer.
 *
 * No reactivity, no I/O — given the same numbers they always return the
 * same string. The component still owns the i18n title composition
 * (which needs `t`); these cover only the numeric formatting V1 did
 * inline (`(n/1000).toFixed(1)` / `Math.round(n/1000)` / `Math.round(r*100)`).
 */

/** V1 `(estimated_tokens / 1000).toFixed(1)` — e.g. 12345 → "12.3". */
export function fmtKTokens(n: number): string {
  return (n / 1000).toFixed(1);
}

/** V1 `Math.round(context_limit / 1000)` — e.g. 200000 → "200". */
export function fmtKLimit(n: number): string {
  return Math.round(n / 1000).toString();
}

/** V1 `Math.round(usage_pct * 100)` — fractional ratio → integer percent.
 *  NOT clamped: an over-window ratio (e.g. 1.11) renders as "111", which is
 *  the intended "history exceeds the model window" signal. */
export function fmtPct(ratio: number): string {
  return Math.round(ratio * 100).toString();
}

/** True when the real occupancy ratio is at/over the context window (≥ 1.0),
 *  i.e. the prompt no longer fits and compaction is imminent. A tiny epsilon
 *  guards against float dust so an exact-fit 1.0 still reads as "full". */
export function isOverLimit(ratio: number): boolean {
  return ratio >= 1.0 - 1e-9;
}

/**
 * Saved-percentage of a compaction — "省 N%".
 *
 * `1 - compacted / used`, rounded to an integer percent. Guards against
 * `used <= 0` (returns 0) and clamps to [0, 100] so spurious figures (e.g.
 * `compacted > used`) never render a negative or >100 chip.
 *
 * @example fmtSavedPct(200_000, 45_000) === 78   // saved 78%
 */
export function fmtSavedPct(used: number, compacted: number): number {
  if (!(used > 0)) return 0;
  const saved = Math.round((1 - compacted / used) * 100);
  if (saved < 0) return 0;
  if (saved > 100) return 100;
  return saved;
}

/**
 * Compacted-to ratio — "压 N%" — the inverse of {@link fmtSavedPct}.
 * `compacted / used` as an integer percent (how much of the original remains).
 * Same `used <= 0` guard and [0, 100] clamp.
 *
 * @example fmtCompactRatio(200_000, 45_000) === 23  // kept ~23%
 */
export function fmtCompactRatio(used: number, compacted: number): number {
  if (!(used > 0)) return 0;
  const kept = Math.round((compacted / used) * 100);
  if (kept < 0) return 0;
  if (kept > 100) return 100;
  return kept;
}
