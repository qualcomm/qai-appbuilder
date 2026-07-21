// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useFontSize` — global font-size preference.
 *
 * S7.5 L7 PR-703 introduced the discrete `sm | md | lg | xl` enum
 * (`ui.fontSize`) + `setFontSize` / `cycle`. Those are retained
 * verbatim for backward compatibility (and the PR-703 shape test).
 *
 * M-align (V1→V2): adds (additive only) the *functional* global
 * pixel-size layer — it scales the global `--text-*` CSS custom
 * properties on `:root` so every page font resizes uniformly, persists
 * the choice to localStorage, and exposes a slider/step + reset API
 * consumed by the sidebar font-size popover and Settings page.
 * The enum layer (`fontSize` / `setFontSize` / `cycle`) is left
 * untouched; the pixel-size layer is what actually moves pixels.
 */
import { computed, ref, watch, type ComputedRef, type Ref } from "vue";

import { useUiStore, type FontSize } from "@/stores/ui";

// ─── Enum layer (PR-703 — unchanged) ─────────────────────────────────────────

const STORAGE_KEY = "qai-font-size";
const SIZE_ORDER: readonly FontSize[] = ["sm", "md", "lg", "xl"];

// ─── Pixel-size layer (global font slider) ───────────────────────────────────

/** Global body/base font-size range requested by product settings. */
const MIN_FONT_SIZE_PX = 12;
const MAX_FONT_SIZE_PX = 24;
const DEFAULT_FONT_SIZE_PX = 16;
/** Preset pixel steps for slider/buttons: every 1px from 12px to 24px. */
const FONT_SIZE_STEPS: readonly number[] = Array.from(
  { length: MAX_FONT_SIZE_PX - MIN_FONT_SIZE_PX + 1 },
  (_, i) => MIN_FONT_SIZE_PX + i,
);
const FONT_SIZE_PX_STORAGE_KEY = "qai-font-size-px";

/** Base sizes (px) — must match styles/variables.css `--text-*` defaults. */
const BASE_SIZES: Readonly<Record<string, number>> = {
  "--text-xs": 11,
  "--text-sm": 12,
  "--text-base": 13,
  "--text-md": 14,
  "--text-lg": 16,
  "--text-xl": 20,
  "--text-2xl": 24,
};

// Module-level singleton px ref so every `useFontSize()` caller shares the same
// reactive value (a single global font-size preference).
let _fontSizePxRef: Ref<number> | null = null;
let _fontSizePxWired = false;

function clampFontSizePx(px: number): number {
  if (!Number.isFinite(px)) return DEFAULT_FONT_SIZE_PX;
  return Math.max(MIN_FONT_SIZE_PX, Math.min(MAX_FONT_SIZE_PX, Math.round(px)));
}

/**
 * Apply a selected base/body font size to the `:root` `--text-*` variables.
 *
 * `--text-base` becomes the exact selected px value. The smaller/larger token
 * sizes keep their original proportions from styles/variables.css, so changing
 * one slider updates the entire UI typography scale globally.
 */
function applyFontSizePx(px: number): void {
  if (typeof document === "undefined") {
    return;
  }
  const root = document.documentElement;
  const safePx = clampFontSizePx(px);
  const baseTextSize = BASE_SIZES["--text-base"] ?? 13;
  const factor = safePx / baseTextSize;
  for (const [varName, baseVal] of Object.entries(BASE_SIZES)) {
    root.style.setProperty(varName, `${Math.round(baseVal * factor)}px`);
  }
}

function readStoredFontSizePx(): number {
  try {
    // Try the current px key first.
    const saved = localStorage.getItem(FONT_SIZE_PX_STORAGE_KEY);
    if (saved !== null) {
      const parsed = parseInt(saved, 10);
      return clampFontSizePx(parsed);
    }
    // Migration: the old key stored a percentage scale (50–200).
    // Convert to px so users don't lose their preference on upgrade.
    const oldScale = localStorage.getItem("qai-font-size-scale");
    if (oldScale !== null) {
      const pct = parseInt(oldScale, 10);
      if (Number.isFinite(pct) && pct > 0) {
        // Old 100% ≈ 13px base; convert proportionally and clamp.
        const asPx = Math.round((pct / 100) * 13);
        return clampFontSizePx(asPx);
      }
    }
    return DEFAULT_FONT_SIZE_PX;
  } catch {
    return DEFAULT_FONT_SIZE_PX;
  }
}

// ─── Composable ──────────────────────────────────────────────────────────────

export function useFontSize() {
  const ui = useUiStore();

  // --- Enum layer (unchanged) ---
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
  } catch {
    stored = null;
  }
  if (stored !== null && SIZE_ORDER.includes(stored as FontSize)) {
    ui.setFontSize(stored as FontSize);
  }

  const fontSize: ComputedRef<FontSize> = computed(() => ui.fontSize);

  watch(
    () => ui.fontSize,
    (val) => {
      try {
        localStorage.setItem(STORAGE_KEY, val);
      } catch {
        /* storage unavailable */
      }
    },
  );

  function setFontSize(size: FontSize): void {
    ui.setFontSize(size);
  }

  function cycle(): void {
    const idx = SIZE_ORDER.indexOf(ui.fontSize);
    const next = SIZE_ORDER[(idx + 1) % SIZE_ORDER.length]!;
    ui.setFontSize(next);
  }

  // --- Pixel-size layer (functional) ---
  if (_fontSizePxRef === null) {
    _fontSizePxRef = ref<number>(readStoredFontSizePx());
  }
  const fontSizeScale = _fontSizePxRef;

  if (!_fontSizePxWired) {
    _fontSizePxWired = true;
    // Apply immediately so the page reflects the stored preference, then keep
    // CSS + localStorage in sync on every change.
    applyFontSizePx(fontSizeScale.value);
    watch(fontSizeScale, (val) => {
      const safeVal = clampFontSizePx(val);
      if (safeVal !== val) {
        fontSizeScale.value = safeVal;
        return;
      }
      try {
        localStorage.setItem(FONT_SIZE_PX_STORAGE_KEY, String(safeVal));
      } catch {
        /* storage unavailable */
      }
      applyFontSizePx(safeVal);
    });
  }

  const currentStepIndex = computed(() =>
    FONT_SIZE_STEPS.indexOf(clampFontSizePx(fontSizeScale.value)),
  );
  const canIncrease = computed(
    () => currentStepIndex.value < FONT_SIZE_STEPS.length - 1,
  );
  const canDecrease = computed(() => currentStepIndex.value > 0);
  const fontSizeLabel = computed(() => `${clampFontSizePx(fontSizeScale.value)}px`);
  /** 0–100 fill percent for the slider track. */
  const fontSizePercent = computed(() => {
    const max = FONT_SIZE_STEPS.length - 1;
    if (max <= 0) return 0;
    const idx = currentStepIndex.value < 0 ? 0 : currentStepIndex.value;
    return Math.round((idx / max) * 100);
  });

  function increaseFontSize(): void {
    if (canIncrease.value) {
      const next = FONT_SIZE_STEPS[currentStepIndex.value + 1];
      if (next !== undefined) fontSizeScale.value = next;
    }
  }

  function decreaseFontSize(): void {
    if (canDecrease.value) {
      const prev = FONT_SIZE_STEPS[currentStepIndex.value - 1];
      if (prev !== undefined) fontSizeScale.value = prev;
    }
  }

  function resetFontSize(): void {
    fontSizeScale.value = DEFAULT_FONT_SIZE_PX;
  }

  return {
    // Enum layer (PR-703 — keep shape stable)
    fontSize,
    setFontSize,
    cycle,
    // Pixel-size layer (global typography scale)
    fontSizeScale,
    fontSizeLabel,
    fontSizePercent,
    canIncrease,
    canDecrease,
    increaseFontSize,
    decreaseFontSize,
    resetFontSize,
    FONT_SIZE_STEPS,
    MIN_FONT_SIZE_PX,
    MAX_FONT_SIZE_PX,
    DEFAULT_FONT_SIZE_PX,
  };
}
