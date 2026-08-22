// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * vue-i18n factory + locale registry.
 *
 * S5 PR-050: bare-bones — 5 keys per locale, just enough to prove the
 * wiring. The legacy `frontend/locales/*.js` (~3000 lines) is migrated
 * to TypeScript modules in PR-053 alongside the SFC rewrite, not here,
 * to keep this PR's diff tractable.
 *
 * Compatibility note (refactor-plan §11.1): vue-i18n 10 with Composition
 * API (`legacy: false`) is mandated; do not toggle legacy mode.
 */
import { createI18n } from "vue-i18n";
import en from "./en";
import zhCN from "./zh-CN";
import zhTW from "./zh-TW";
import type { MessageSchema as MessageSchemaType } from "./schema";

export type SupportedLocale = "en" | "zh-CN" | "zh-TW";

export const SUPPORTED_LOCALES: readonly SupportedLocale[] = [
  "en",
  "zh-CN",
  "zh-TW",
] as const;

export type { MessageSchema } from "./schema";

function resolveInitialLocale(): SupportedLocale {
  // Explicit user choice persisted to localStorage takes precedence so a
  // full page reload / direct URL open restores the selected language.
  try {
    const stored = localStorage.getItem("qai_locale");
    if (stored === "en" || stored === "zh-CN" || stored === "zh-TW") {
      return stored;
    }
  } catch {
    // localStorage unavailable — fall through to navigator language.
  }
  if (typeof navigator === "undefined") {
    return "en";
  }
  const candidate = navigator.language;
  if (candidate?.startsWith("zh-TW") === true) {
    return "zh-TW";
  }
  if (candidate?.startsWith("zh") === true) {
    return "zh-CN";
  }
  return "en";
}

export function createAppI18n() {
  return createI18n<MessageSchemaType, SupportedLocale, false>({
    legacy: false,
    locale: resolveInitialLocale(),
    fallbackLocale: "en",
    missingWarn: false,
    fallbackWarn: false,
    // Several messages legitimately contain angle-bracket placeholders
    // (e.g. the `/grant <path>` command help) that are rendered via text
    // interpolation (auto-escaped by Vue), never `v-html`. Silence the
    // pre-emptive HTML-in-message warning so it doesn't flood the console.
    warnHtmlMessage: false,
    messages: {
      en,
      "zh-CN": zhCN,
      "zh-TW": zhTW,
    },
  });
}
