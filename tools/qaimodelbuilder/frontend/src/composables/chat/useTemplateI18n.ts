// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useTemplateI18n — resolve built-in discussion-template text for the current
 * UI locale.
 *
 * Built-in agent / roster / mode presets ship their business text
 * (name / description / persona / framing / member display_name+persona) in
 * every supported language. The backend seeds those translations into the DB
 * (single source of truth) and the template list endpoints return them as
 * ``*_i18n`` maps ``{ "en": "...", "zh-CN": "...", "zh-TW": "..." }`` alongside
 * the canonical single-language fields.
 *
 * This composable picks the string for the *current* ``useI18n().locale`` and
 * falls back to the canonical value when:
 *   - the template is user-authored (no i18n map — custom rows are never
 *     translated, the user's own text is shown verbatim), or
 *   - the map is missing the current locale.
 *
 * The resolver reads ``locale`` reactively, so switching the app language
 * re-localises every built-in template in the library without a refetch — and
 * because it is a pure display-layer helper, it never touches what the frontend
 * submits to the backend (imports/apply still send only ids; the backend
 * re-localises the persona/framing injected into the LLM by itself).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import type { SupportedLocale } from "@/locales";

/** A per-locale translation map as returned by the backend template DTOs. */
export type I18nMap = Partial<Record<SupportedLocale, string>> | null | undefined;

export function useTemplateI18n() {
  const { locale } = useI18n();

  const currentLocale = computed<SupportedLocale>(
    () => locale.value as SupportedLocale,
  );

  /**
   * Return the localised string for the current locale, or ``fallback`` when
   * the map is absent / lacks this locale. An empty-string translation IS a
   * valid value (e.g. the "讨论/Discussion" mode has an intentionally empty
   * framing in every language) and is returned as-is.
   */
  function resolve(map: I18nMap, fallback: string): string {
    if (map != null) {
      const hit = map[currentLocale.value];
      if (hit !== undefined && hit !== null) {
        return hit;
      }
    }
    return fallback;
  }

  return { currentLocale, resolve };
}
