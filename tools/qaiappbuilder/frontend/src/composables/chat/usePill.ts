// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `usePill` — CC / OC assistant-pill label, tooltip and interaction logic
 * (V1 `index.html:1191-1227` + `app.js:728-747`).
 *
 * Extracted from `ChatComposer.vue` (F1③ cohesion split). Computes the two
 * pills' labels (`🤖 Claude Code ✓ ▾` style) and tooltips, and exposes the
 * click / right-click handlers that drive mode entry/exit + panel toggle
 * with CC↔OC mutual exclusivity (V1 parity).
 *
 * The CC / OC composables and the onboarding transient-hide callback are
 * injected so this composable owns no global state of its own — it is a
 * thin reactive façade over the existing `useClaudeCode` / `useOpenCode`
 * singletons, keeping their behaviour identical while lifting ~80 lines of
 * label/handler boilerplate out of the composer.
 */
import { computed, type ComputedRef } from "vue";
import { useI18n } from "vue-i18n";
import type { useClaudeCode } from "@/composables/useClaudeCode";
import type { useOpenCode } from "@/composables/useOpenCode";

type ClaudeCode = ReturnType<typeof useClaudeCode>;
type OpenCode = ReturnType<typeof useOpenCode>;

export interface UsePill {
  ccPillLabel: ComputedRef<string>;
  ccPillTitle: ComputedRef<string>;
  onClickCCPill: () => void;
  onContextMenuCCPill: (ev: MouseEvent) => void;
  ocPillLabel: ComputedRef<string>;
  ocPillTitle: ComputedRef<string>;
  onClickOCPill: () => void;
  onContextMenuOCPill: (ev: MouseEvent) => void;
}

/**
 * @param claudeCode  The `useClaudeCode()` instance shared with the composer.
 * @param openCode    The `useOpenCode()` instance shared with the composer.
 * @param onPillInteractedWhileOnboarding  Called at the start of a pill
 *        click so the onboarding bubble can transiently hide itself (V1
 *        app.js:728-737 — the bubble shouldn't obstruct the action it
 *        advertises). No-op when the bubble is not showing.
 */
export function usePill(
  claudeCode: ClaudeCode,
  openCode: OpenCode,
  onPillInteractedWhileOnboarding: () => void,
): UsePill {
  const { t } = useI18n();

  const ccPillLabel = computed<string>(() => {
    return claudeCode.isCCMode.value
      ? `${t("index.claudeCodeMode")} ✓ ${claudeCode.panelOpen.value ? "▾" : "▸"}`
      : t("index.claudeCodeMode");
  });

  const ccPillTitle = computed<string>(() => {
    return claudeCode.isCCMode.value
      ? t("index.ccPanelCollapseHint")
      : t("index.ccPillTooltip");
  });

  function onClickCCPill(): void {
    // V1 app.js:728-737 — clicking the pill while the onboarding bubble
    // is showing transiently hides it (so the bubble doesn't obstruct
    // the very action it advertises). Permanent dismissal still requires
    // explicit "Got it" click.
    onPillInteractedWhileOnboarding();
    if (claudeCode.isCCMode.value) {
      // Already in CC mode — toggle the floating panel (V1 onClickCCPill).
      claudeCode.togglePanel();
      return;
    }
    // Mutually exclusive with OC.
    if (openCode.isOCMode.value) {
      openCode.exitOCMode();
    }
    void claudeCode.enterCCMode();
  }

  function onContextMenuCCPill(ev: MouseEvent): void {
    ev.preventDefault();
    if (claudeCode.isCCMode.value) {
      claudeCode.exitCCMode();
    }
  }

  const ocPillLabel = computed<string>(() => {
    return openCode.isOCMode.value
      ? `${t("index.openCodeMode")} ✓ ${openCode.panelOpen.value ? "▾" : "▸"}`
      : t("index.openCodeMode");
  });

  const ocPillTitle = computed<string>(() => {
    return openCode.isOCMode.value
      ? t("index.ocPanelCollapseHint")
      : t("index.ocPillTooltip");
  });

  function onClickOCPill(): void {
    // V1 app.js:738-747 — see onClickCCPill comment.
    onPillInteractedWhileOnboarding();
    if (openCode.isOCMode.value) {
      // Already in OC mode — toggle the floating panel (V1 onClickOCPill).
      openCode.togglePanel();
      return;
    }
    // Mutually exclusive with CC.
    if (claudeCode.isCCMode.value) {
      claudeCode.exitCCMode();
    }
    void openCode.enterOCMode();
  }

  function onContextMenuOCPill(ev: MouseEvent): void {
    ev.preventDefault();
    if (openCode.isOCMode.value) {
      openCode.exitOCMode();
    }
  }

  return {
    ccPillLabel,
    ccPillTitle,
    onClickCCPill,
    onContextMenuCCPill,
    ocPillLabel,
    ocPillTitle,
    onClickOCPill,
    onContextMenuOCPill,
  };
}
