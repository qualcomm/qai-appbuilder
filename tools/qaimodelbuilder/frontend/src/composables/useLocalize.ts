// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Localized-text helpers.
 *
 * Resolves a `LocalizedText` (`string | { lang: string }`) — the wire shape
 * used by the remote model catalog / release manifest — into a single string
 * for rendering, picking the entry that matches the current WebUI language.
 *
 * Why a composable: switching the WebUI language must update displayed catalog
 * text instantly (no re-fetch of the manifest). `useI18n().locale` is reactive,
 * so a `computed(() => localize(text, locale.value))` re-evaluates on switch.
 *
 * Fallback chain (most → least specific):
 *   1. exact UI locale (e.g. `zh-CN`)
 *   2. base locale (e.g. `zh` matches `zh-CN` / `zh-TW`)
 *   3. English (`en`)
 *   4. Simplified Chinese (`zh-CN`)
 *   5. first non-empty entry
 *
 * A plain string is returned as-is (legacy / un-translated catalogs).
 */
import { computed, type ComputedRef } from "vue";
import { useI18n } from "vue-i18n";

import type { LocalizedText } from "@/types/downloads";

/** Pick the best string for `lang` from a `LocalizedText`. */
export function localize(value: LocalizedText | undefined | null, lang: string): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  // Object form: pick by lang with a deterministic fallback chain.
  if (typeof value === "object") {
    const map = value as Record<string, string>;
    // 1. exact match.
    const exact = map[lang];
    if (typeof exact === "string" && exact.length > 0) return exact;
    // 2. base-language match (e.g. "zh" → "zh-CN" / "zh-TW").
    const base = lang.split("-")[0];
    if (base && base !== lang) {
      for (const [k, v] of Object.entries(map)) {
        if (typeof v === "string" && v.length > 0 && k.split("-")[0] === base) {
          return v;
        }
      }
    }
    // 3-4. canonical fallbacks.
    for (const fallback of ["en", "zh-CN"]) {
      const v = map[fallback];
      if (typeof v === "string" && v.length > 0) return v;
    }
    // 5. first non-empty entry.
    for (const v of Object.values(map)) {
      if (typeof v === "string" && v.length > 0) return v;
    }
  }
  return "";
}

/** Localize a list of `LocalizedText` (e.g. `features`). */
export function localizeAll(
  values: readonly LocalizedText[] | undefined | null,
  lang: string,
): string[] {
  if (!values) return [];
  return values.map((v) => localize(v, lang)).filter((s) => s.length > 0);
}

/**
 * Vue composable: returns reactive `localize` / `localizeAll` bound to the
 * current `useI18n().locale`. Use in component scripts where you want a
 * computed that auto-updates on language switch:
 *
 *     const { localize } = useLocalize();
 *     const desc = computed(() => localize(props.model.description));
 */
export function useLocalize(): {
  localize: (value: LocalizedText | undefined | null) => string;
  localizeAll: (values: readonly LocalizedText[] | undefined | null) => string[];
  localized: (value: LocalizedText | undefined | null) => ComputedRef<string>;
} {
  const { locale } = useI18n();
  return {
    localize: (value) => localize(value, locale.value),
    localizeAll: (values) => localizeAll(values, locale.value),
    localized: (value) => computed(() => localize(value, locale.value)),
  };
}
