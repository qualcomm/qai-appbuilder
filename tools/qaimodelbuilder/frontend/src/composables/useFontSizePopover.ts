// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useFontSizePopover — V1-parity sidebar font-size popover dismiss state.
 *
 * Extracted from `AppSidebar.vue` (cohesion split). Owns the popover's
 * open flag + container ref + the `useClickOutside` / `useEscClose`
 * dismiss listeners. The actual font-size controls (the slider /
 * preset buttons / `useFontSize` composable) stay where they are —
 * this composable scope is intentionally narrow to "open / close +
 * outside-click + ESC", mirroring the V1 sidebar widget.
 */
import { ref, type Ref } from "vue";
import { useClickOutside, useEscClose } from "@/composables/useClickOutside";

export interface UseFontSizePopoverReturn {
  fontSizePopoverOpen: Ref<boolean>;
  fontSizeWrapEl: Ref<HTMLElement | null>;
  toggleFontSizePopover: () => void;
}

export function useFontSizePopover(): UseFontSizePopoverReturn {
  const fontSizePopoverOpen = ref(false);
  const fontSizeWrapEl = ref<HTMLElement | null>(null);

  function toggleFontSizePopover(): void {
    fontSizePopoverOpen.value = !fontSizePopoverOpen.value;
  }

  // V1 parity: outside-click + ESC both dismiss the popover. Uses
  // capture-phase click (default) so an inner button's @click.stop
  // never swallows the dismiss.
  useClickOutside(
    fontSizeWrapEl,
    () => {
      fontSizePopoverOpen.value = false;
    },
    { when: () => fontSizePopoverOpen.value },
  );
  useEscClose(
    () => {
      fontSizePopoverOpen.value = false;
    },
    () => fontSizePopoverOpen.value,
  );

  return {
    fontSizePopoverOpen,
    fontSizeWrapEl,
    toggleFontSizePopover,
  };
}
