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
 * CALIBER CONTRACT: both arguments MUST share the same baseline, and they
 * already do — each is a WHOLE-wire figure carrying exactly ONE runtime
 * overhead (system prompt + tool schemas + persona + skill blocks, which the
 * compressor can never touch). `compacted` is the provider-measured wire sent
 * now. `used` is `conv.full_history_tokens`, which is NOT a pure history
 * counter: it is SEEDED from a provider-measured whole wire at
 * `streaming.py:5155` (`full_history_tokens = eff_prompt + completion`, where
 * `eff_prompt` INCLUDES the overhead) and afterwards grows only by pure
 * history deltas (`streaming.py:5178-5180`, where the two `eff_prompt`
 * readings cancel their overhead) — so it is `pure_history + overhead`.
 *
 * That seed branch is the single most easily missed line in this whole
 * accounting chain: a prior audit read only the delta branch, concluded "the
 * denominator excludes overhead", and added a SECOND overhead to `used`. That
 * made the baseline `pure_history + 2×overhead` — a wire that never existed —
 * overstating the saving by 6-7 points and breaking the badge's on-screen
 * arithmetic. NEVER add overhead to `used`. See
 * `docs/90-refactor/CONTEXT-COMPACTION.md` §六.
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

/**
 * Human-readable token count with an automatic K / M unit.
 *
 * The single formatter for every user-facing token figure (composer badge,
 * `/compact` replies, `/compact status`) so the same number never renders
 * two different ways. Raw counts in the 10^5–10^6 range are unreadable
 * ("185599"), so:
 *
 * - `< 1000`        → the integer verbatim (`"742"`)
 * - `< 1_000_000`   → thousands with one decimal (`"185.6K"`), trailing
 *                     `.0` trimmed (`"200K"`, not `"200.0K"`)
 * - `>= 1_000_000`  → millions with two decimals (`"1.05M"`), trailing
 *                     zeros trimmed (`"1M"`, not `"1.00M"`)
 *
 * Negative / non-finite inputs collapse to `"0"` — a token count is never
 * meaningfully negative and a NaN must not leak into the UI.
 *
 * @example fmtTokenCount(742)      === "742"
 * @example fmtTokenCount(59_575)   === "59.6K"
 * @example fmtTokenCount(200_000)  === "200K"
 * @example fmtTokenCount(1_048_576) === "1.05M"
 */
export function fmtTokenCount(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 1000) return Math.round(n).toString();
  if (n < 1_000_000) {
    const k = (n / 1000).toFixed(1);
    return `${k.endsWith(".0") ? k.slice(0, -2) : k}K`;
  }
  const m = (n / 1_000_000).toFixed(2);
  // Trim "1.00" → "1", "1.50" → "1.5" (keep a single significant decimal).
  const trimmed = m.replace(/\.?0+$/, "");
  return `${trimmed}M`;
}
