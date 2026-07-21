// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useMbProAccessGate — shared access-gate state machine for the MB Pro
 * (Model Builder Pro) LDAP membership check tooltip.
 *
 * Used by both `ModeFramePro.vue` (toolbar mode chip) and
 * `ProEmptyState.vue` (welcome-screen chip). Both surfaces need to:
 *   - Show a tooltip when the user hovers a disabled "Connect" button.
 *   - Offer "Apply to join" + "Refresh access" actions.
 *   - Manage a small mouseleave debounce so the pointer can travel
 *     into the tooltip body without the tooltip closing mid-transit.
 *
 * This composable owns the tooltip visibility state + the shared
 * subscribe URL + the refresh action. `isMbProAuthorized` /
 * `mbProAccessCheckFailed` are read directly from `useAuthStore` at
 * the call site (they're not local UI state — they're server-driven
 * derived state).
 *
 * Design note (2026-07-20): previously each surface reimplemented
 * this state machine inline with a 120ms vs 150ms mouseleave delay
 * inconsistency. Consolidated to 150ms — the ModeFramePro toolbar
 * chip was the primary entry point and used 150ms; conservative to
 * match it. 30ms user-facing difference is imperceptible.
 */
import { ref } from "vue";

import { useAuthStore } from "@/stores/auth";

/** Subscribe URL for the ModelBuilderProUsers mailing list. Kept as a
 *  named export so any surface (in-app UI, docs, tests) references
 *  exactly one canonical URL string. */
export const MB_PRO_APPLY_URL =
  "https://lists.qualcomm.com/ListManager?query=ModelBuilderProUsers&field=default&match=sw";

/** Mouseleave debounce so the pointer can travel from the trigger into
 *  the tooltip body without the tooltip closing mid-transit. Matches the
 *  prior ModeFramePro value. */
const HIDE_TOOLTIP_DELAY_MS = 150;

export function useMbProAccessGate() {
  const authStore = useAuthStore();

  const isRefreshingAccess = ref(false);
  const accessTooltipVisible = ref(false);
  let hideTimer: ReturnType<typeof setTimeout> | null = null;

  function showAccessTooltip(): void {
    if (hideTimer !== null) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
    accessTooltipVisible.value = true;
  }

  function hideAccessTooltip(): void {
    hideTimer = setTimeout(() => {
      accessTooltipVisible.value = false;
      hideTimer = null;
    }, HIDE_TOOLTIP_DELAY_MS);
  }

  async function onRefreshAccess(): Promise<void> {
    isRefreshingAccess.value = true;
    try {
      await authStore.refreshMbProAccess();
    } finally {
      isRefreshingAccess.value = false;
    }
  }

  function onApplyToJoin(): void {
    window.open(MB_PRO_APPLY_URL, "_blank", "noopener,noreferrer");
  }

  return {
    isRefreshingAccess,
    accessTooltipVisible,
    showAccessTooltip,
    hideAccessTooltip,
    onRefreshAccess,
    onApplyToJoin,
    MB_PRO_APPLY_URL,
  };
}
