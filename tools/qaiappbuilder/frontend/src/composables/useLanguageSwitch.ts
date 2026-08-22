// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useLanguageSwitch — V1-parity sidebar language dropdown.
 *
 * Extracted from `AppSidebar.vue` (cohesion split). Owns the dropdown's
 * open flag + container ref, the locale tables (3 supported locales:
 * `en` / `zh-CN` / `zh-TW`), the active label, the toggle/select
 * handlers, and the `useClickOutside` / `useEscClose` dismiss
 * listeners.
 *
 * Locale persistence is delegated to `useUiStore().setLocale()` —
 * the store handles localStorage + i18n updates. The composable just
 * issues the command.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useUiStore } from "@/stores/ui";
import { useClickOutside, useEscClose } from "@/composables/useClickOutside";

export interface UseLanguageSwitchReturn {
  SUPPORTED_LANGS: readonly string[];
  LANG_SHORT: Record<string, string>;
  LANG_FULL: Record<string, string>;
  langDropdownOpen: Ref<boolean>;
  langWrapEl: Ref<HTMLElement | null>;
  currentLangLabel: ComputedRef<string>;
  toggleLangDropdown: () => void;
  selectLang: (locale: string) => void;
}

export function useLanguageSwitch(): UseLanguageSwitchReturn {
  const ui = useUiStore();

  const LANG_SHORT: Record<string, string> = {
    en: "EN",
    "zh-CN": "简中",
    "zh-TW": "繁中",
  };
  const LANG_FULL: Record<string, string> = {
    en: "English",
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
  };
  const SUPPORTED_LANGS: readonly string[] = ["en", "zh-CN", "zh-TW"];

  const langDropdownOpen = ref(false);
  const langWrapEl = ref<HTMLElement | null>(null);

  const currentLangLabel = computed(() => LANG_SHORT[ui.locale] ?? "EN");

  function toggleLangDropdown(): void {
    langDropdownOpen.value = !langDropdownOpen.value;
  }

  function selectLang(locale: string): void {
    ui.setLocale(locale);
    langDropdownOpen.value = false;
  }

  // V1 parity: outside-click + ESC both dismiss the dropdown. Uses
  // capture-phase click (default) so an inner button's @click.stop
  // never swallows the dismiss.
  useClickOutside(
    langWrapEl,
    () => {
      langDropdownOpen.value = false;
    },
    { when: () => langDropdownOpen.value },
  );
  useEscClose(
    () => {
      langDropdownOpen.value = false;
    },
    () => langDropdownOpen.value,
  );

  return {
    SUPPORTED_LANGS,
    LANG_SHORT,
    LANG_FULL,
    langDropdownOpen,
    langWrapEl,
    currentLangLabel,
    toggleLangDropdown,
    selectLang,
  };
}
