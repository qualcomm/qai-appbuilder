// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useOnboardingBubble` — first-time onboarding bubble for the AI-coding
 * assistant pills (V1 `app.js:706-904` 1:1 port).
 *
 * Extracted from `ChatComposer.vue` (F1② cohesion split). Owns:
 *   - the persisted "dismissed" flag (localStorage, defensively read)
 *   - the transient "hidden this session" flag
 *   - aggregate `showAssistantOnboarding` visibility
 *   - the bubble + arrow position computation (anchored to the CC pill,
 *     or the OC pill when only OC is rendered) and its re-anchoring via
 *     ResizeObserver + window resize + reactive width-changing inputs
 *
 * The element refs (`ccPillEl` / `ocPillEl` / `assistantOnboardingBubbleEl`)
 * are owned here and returned so the composer template can bind them
 * (`ref="…"`). Gating inputs (whether CC / OC are enabled in Settings and
 * whether either mode is currently active) are passed in as reactive
 * getters so this composable stays decoupled from the CC/OC composables.
 *
 * Per-call (not a singleton): each composer owns its own bubble state,
 * exactly as V1's onboarding state lived on the single chat-input
 * instance.
 */
import {
  ref,
  computed,
  nextTick,
  watch,
  onMounted,
  onBeforeUnmount,
  type Ref,
  type ComputedRef,
} from "vue";
import { useSkillsStore } from "@/stores/skills";
import { useChatTabsStore } from "@/stores/chatTabs";

const ASSISTANT_ONBOARDING_KEY = "qai_assistant_onboarding_dismissed";

// Read the persisted "dismissed" flag defensively: `localStorage` may be
// a non-conformant stub in some environments (e.g. happy-dom without a
// localstorage file in unit tests, or browser private mode) where the
// global exists but `getItem` is not a function. Guard on the method
// itself + try/catch so component setup never throws (mirrors the
// setItem guard in `dismiss`).
function readOnboardingDismissed(): boolean {
  try {
    if (
      typeof localStorage !== "undefined" &&
      typeof localStorage.getItem === "function"
    ) {
      return localStorage.getItem(ASSISTANT_ONBOARDING_KEY) === "1";
    }
  } catch {
    // localStorage may be disabled / non-conformant; treat as not dismissed.
  }
  return false;
}

export interface OnboardingBubblePos {
  bubbleStyle: { left: string };
  arrowStyle: { left: string };
}

export interface UseOnboardingBubble {
  /** Bind to the CC pill button (`ref="ccPillEl"`). */
  ccPillEl: Ref<HTMLElement | null>;
  /** Bind to the OC pill button (`ref="ocPillEl"`). */
  ocPillEl: Ref<HTMLElement | null>;
  /** Bind to the bubble root (`ref="assistantOnboardingBubbleEl"`). */
  assistantOnboardingBubbleEl: Ref<HTMLElement | null>;
  /** Aggregate visibility. */
  showAssistantOnboarding: ComputedRef<boolean>;
  /** Computed bubble + arrow inline-style positions. */
  assistantOnboardingPos: Ref<OnboardingBubblePos>;
  /** Permanent dismissal ("Got it"). */
  dismissAssistantOnboarding: () => void;
  /** Transient hide for this session (pill clicked). */
  hideAssistantOnboardingTransient: () => void;
}

/**
 * @param ccEnabled  Reactive getter — `ai_coding.cc.enabled` (Settings).
 * @param ocEnabled  Reactive getter — `ai_coding.oc.enabled` (Settings).
 * @param isCCMode   Reactive getter — user has entered CC mode.
 * @param isOCMode   Reactive getter — user has entered OC mode.
 */
export function useOnboardingBubble(
  ccEnabled: ComputedRef<boolean> | (() => boolean),
  ocEnabled: ComputedRef<boolean> | (() => boolean),
  isCCMode: ComputedRef<boolean> | (() => boolean),
  isOCMode: ComputedRef<boolean> | (() => boolean),
): UseOnboardingBubble {
  const skillsStore = useSkillsStore();
  const store = useChatTabsStore();

  const read = (
    src: ComputedRef<boolean> | (() => boolean),
  ): boolean => (typeof src === "function" ? src() : src.value);

  const _onboardingDismissed = ref<boolean>(readOnboardingDismissed());
  const _onboardingTransientHidden = ref<boolean>(false);

  const ccPillEl = ref<HTMLElement | null>(null);
  const ocPillEl = ref<HTMLElement | null>(null);
  const assistantOnboardingBubbleEl = ref<HTMLElement | null>(null);
  const assistantOnboardingPos = ref<OnboardingBubblePos>({
    bubbleStyle: { left: "0px" },
    arrowStyle: { left: "36px" },
  });

  // Aggregate visibility: only show when (a) at least one mode is enabled
  // in Settings, (b) user hasn't permanently dismissed, (c) not transiently
  // hidden this session, (d) user hasn't already entered CC/OC mode.
  const showAssistantOnboarding = computed<boolean>(() => {
    if (_onboardingDismissed.value) return false;
    if (_onboardingTransientHidden.value) return false;
    if (!read(ccEnabled) && !read(ocEnabled)) return false;
    if (read(isCCMode) || read(isOCMode)) return false;
    return true;
  });

  function dismissAssistantOnboarding(): void {
    _onboardingDismissed.value = true;
    try {
      localStorage.setItem(ASSISTANT_ONBOARDING_KEY, "1");
    } catch {
      // localStorage may be disabled (private mode); fall through.
    }
  }

  function hideAssistantOnboardingTransient(): void {
    _onboardingTransientHidden.value = true;
  }

  /**
   * Recalculate bubble + arrow position so the arrow points at the CC pill
   * (or OC pill if only OC is rendered) center, and the bubble itself is
   * horizontally centered on the same anchor and clamped inside the
   * `.input-toolbar` bounds. Mirrors V1 app.js:782-838 logic. The bubble
   * is `position:absolute` inside `.input-toolbar` (which is
   * `position:relative` per V1 inline style).
   */
  function recalcAssistantOnboardingPos(retry = 0): void {
    const cc = ccPillEl.value;
    const oc = ocPillEl.value;
    const bubble = assistantOnboardingBubbleEl.value;

    if (!cc && !oc) {
      if (retry < 5 && showAssistantOnboarding.value) {
        requestAnimationFrame(() => recalcAssistantOnboardingPos(retry + 1));
      }
      return;
    }
    if (!bubble) {
      if (retry < 5 && showAssistantOnboarding.value) {
        requestAnimationFrame(() => recalcAssistantOnboardingPos(retry + 1));
      }
      return;
    }

    const anchorEl = cc ?? oc;
    if (anchorEl === null) return;
    const anchorRect = anchorEl.getBoundingClientRect();

    // V1 explicitly uses .closest('.input-toolbar') (NOT offsetParent),
    // because nested popover wrappers may become offsetParent and yield
    // a too-narrow reference width (612 vs 936 in V1's bug report).
    const toolbar = bubble.closest<HTMLElement>(".input-toolbar");
    if (toolbar === null) return;
    const toolbarRect = toolbar.getBoundingClientRect();

    const anchorX =
      anchorRect.left + anchorRect.width / 2 - toolbarRect.left;
    const bubbleWidth = bubble.offsetWidth || 320;
    const toolbarWidth = toolbarRect.width || 0;

    let bubbleLeft = anchorX - bubbleWidth / 2;
    const minLeft = 8;
    const maxLeft = Math.max(minLeft, toolbarWidth - bubbleWidth - 8);
    if (bubbleLeft < minLeft) bubbleLeft = minLeft;
    if (bubbleLeft > maxLeft) bubbleLeft = maxLeft;

    let arrowLeft = anchorX - bubbleLeft;
    if (arrowLeft < 12) arrowLeft = 12;
    if (arrowLeft > bubbleWidth - 12) arrowLeft = bubbleWidth - 12;

    assistantOnboardingPos.value = {
      bubbleStyle: { left: `${bubbleLeft}px` },
      arrowStyle: { left: `${arrowLeft}px` },
    };
  }

  function _scheduleRecalc(): void {
    void nextTick(() => requestAnimationFrame(() => recalcAssistantOnboardingPos()));
  }

  // Re-anchor whenever the bubble appears, the toolbar's contents change
  // width (skills count flip, model rename), or the window resizes.
  watch(showAssistantOnboarding, (visible) => {
    if (!visible) return;
    _scheduleRecalc();
  });
  watch(
    () => skillsStore.enabledSkillsCount,
    () => {
      if (showAssistantOnboarding.value) _scheduleRecalc();
    },
  );
  watch(
    () => store.activeTab?.modelId,
    () => {
      if (showAssistantOnboarding.value) _scheduleRecalc();
    },
  );

  let _onboardingResizeObs: ResizeObserver | null = null;
  let _onboardingWinResizeHandler: (() => void) | null = null;

  onMounted(() => {
    if (typeof ResizeObserver !== "undefined") {
      _onboardingResizeObs = new ResizeObserver(() => {
        if (showAssistantOnboarding.value) recalcAssistantOnboardingPos();
      });
      let tries = 0;
      const tryObserve = (): void => {
        const bubble = assistantOnboardingBubbleEl.value;
        const cc = ccPillEl.value;
        const anchor = bubble ?? cc;
        if (anchor !== null) {
          const toolbar = anchor.closest<HTMLElement>(".input-toolbar");
          if (toolbar !== null && _onboardingResizeObs !== null) {
            _onboardingResizeObs.observe(toolbar);
            return;
          }
        }
        if (++tries < 20) requestAnimationFrame(tryObserve);
      };
      tryObserve();
    }
    if (typeof window !== "undefined") {
      _onboardingWinResizeHandler = (): void => {
        if (showAssistantOnboarding.value) recalcAssistantOnboardingPos();
      };
      window.addEventListener("resize", _onboardingWinResizeHandler);
    }
  });

  onBeforeUnmount(() => {
    if (_onboardingResizeObs !== null) {
      _onboardingResizeObs.disconnect();
      _onboardingResizeObs = null;
    }
    if (_onboardingWinResizeHandler !== null && typeof window !== "undefined") {
      window.removeEventListener("resize", _onboardingWinResizeHandler);
      _onboardingWinResizeHandler = null;
    }
  });

  return {
    ccPillEl,
    ocPillEl,
    assistantOnboardingBubbleEl,
    showAssistantOnboarding,
    assistantOnboardingPos,
    dismissAssistantOnboarding,
    hideAssistantOnboardingTransient,
  };
}
