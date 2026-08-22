// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * usePromoteReadyDetection — surface a "Promote to App Builder" CTA once the
 * active chat's model workspace contains a promote-eligible artefact.
 *
 * Design (Sprint 2 rewrite — backend-detected, pull-from-DB)
 * ==========================================================
 *
 * Problem: after Model Builder / Model Hub finishes converting or downloading a
 * model the resulting `.bin`/`.dlc` variants live on disk in `<workdir>/output/`,
 * but the chat UI has no idea. Users had to know to open the Promote popover
 * from the mode-frame toolbar, which is deep and easy to miss.
 *
 * The FIRST implementation detected readiness purely in the frontend by
 * re-scanning EVERY `<root>\<model>` path mentioned in the conversation on EVERY
 * message commit (`messageAdded`) — a long conversation did N serial `scanBins`
 * HTTP round-trips + disk scans on each turn (the 10s+ first check), and three
 * concurrent composable instances (ChatView + the active mode-frame) each fired
 * their own scans.
 *
 * This version moves detection to the BACKEND (State-Truth-First, AGENTS.md §5):
 *
 *   1. At turn end (`StreamChatUseCase._finalize_assistant_message`) the backend
 *      extracts the model workspace path from the turn's FINAL summary text (the
 *      SKILL contract guarantees every round's summary prints the top-level
 *      `C:\WoS_AI\<model>` path, user-visible), scans it ONCE via the app_pack
 *      `scanBins` contract, and persists the result onto
 *      `Conversation.detected_model` (migration 057).
 *   2. The frontend then reads that persisted result with ZERO on-open disk
 *      scans: `loadHistoryMessages` seeds `tab.detectedModel` from the
 *      conversation summary, and `confirmDone` (turn end, `streaming → idle`)
 *      re-reads it so the CTA refreshes this turn (the model may have only just
 *      produced a promotable variant, or switched to a different model dir).
 *
 * This composable is therefore a THIN reactive projection of
 * `store.activeTab.detectedModel` — no `scanBins`, no scan cache, no candidate
 * sweep, no out-of-order token (the backend owns detection; the store's
 * `_refreshDetectedModel` guards conversation identity). All three instances
 * (ChatView / ModeFrameModelBuilder / ModeFrameModelHub) read the SAME tab
 * field, so notice + card + ready-dot always agree by construction.
 *
 * De-duplication
 * --------------
 * The notice must not spam. We remember, per model workdir, whether the CTA has
 * already been dismissed in the current browser session (`sessionStorage`):
 *
 *   - New workdir detected with ≥1 variant AND not dismissed → show.
 *   - Same workdir, still ready, dismissed → stay hidden.
 *   - Workdir changes (different model) → the new workdir is fresh and can show
 *     even if a PREVIOUS workdir was dismissed.
 *   - No variants → hidden (nothing to promote).
 *
 * We do NOT persist to `localStorage` (no "永久关闭"): the CTA only appears after
 * a real workdir with real variants is detected, so a `sessionStorage` gate is
 * sufficient (Plan §14 UX guardrails).
 */
import { computed, ref, type ComputedRef } from "vue";

import { useChatTabsStore } from "@/stores/chatTabs";

/** One scanned precision variant — same shape the Promote card's picker uses. */
export interface DetectedVariant {
  readonly precision: string;
  readonly label: string;
}

/** Public surface of the composable. */
export interface UsePromoteReadyDetection {
  /** True iff the CTA should currently render. */
  readonly shouldShow: ComputedRef<boolean>;
  /** Workspace dir the detection resolved (empty string when none). */
  readonly detectedWorkdir: ComputedRef<string>;
  /** Detected precision variants (non-empty ⇒ CTA can show). */
  readonly detectedVariants: ComputedRef<DetectedVariant[]>;
  /**
   * Dismiss the CTA for the currently-detected workdir (session-scoped).
   * The CTA stays hidden until either a different workdir with variants is
   * detected, or the browser tab is closed and reopened.
   */
  dismiss: () => void;
}

/** sessionStorage key that carries the last-dismissed workdir marker. */
const DISMISS_KEY_PREFIX = "promoteReady.dismissed:";
/** Safe sessionStorage getter — falls back to `false` when unavailable. */
function readDismissed(workdir: string): boolean {
  if (workdir === "") return false;
  try {
    return window.sessionStorage.getItem(DISMISS_KEY_PREFIX + workdir) === "1";
  } catch {
    return false;
  }
}

/** Safe sessionStorage setter. */
function writeDismissed(workdir: string): void {
  if (workdir === "") return;
  try {
    window.sessionStorage.setItem(DISMISS_KEY_PREFIX + workdir, "1");
  } catch {
    // Storage unavailable (privacy mode / SSR) — silently degrade. The CTA
    // will re-appear next time the same workdir is detected in this tab,
    // which is the closest we can get to "dismiss" without a backing store.
  }
}

/**
 * Create the detection composable. Instantiate once at the container that
 * hosts the notice component (typically `ChatView`) and pipe the returned
 * state into `PromoteReadyNotice` props.
 */
export function usePromoteReadyDetection(): UsePromoteReadyDetection {
  const store = useChatTabsStore();

  // Re-evaluate `shouldShow` whenever `dismiss()` bumps this token — reading
  // `sessionStorage` inside `computed` is not natively reactive, so we drive
  // it explicitly (same pattern used by `useModeIntroCardVisibility`).
  const dismissBump = ref<number>(0);

  // ── Eligibility gate ───────────────────────────────────────────────────────
  // The CTA only makes sense in modes where Promote is a natural next step.
  // Anywhere else (plain chat, translate, gomaster, ppt, code, pro, etc.) it
  // would be an unrelated interruption.
  const activeMode = computed<string>(() => store.activeTab?.activeMode ?? "");
  const modeAllowsCta = computed<boolean>(
    () =>
      activeMode.value === "model-build" ||
      activeMode.value === "app-builder" ||
      activeMode.value === "model-hub",
  );

  // ── Detection projection (backend-owned, read from the active tab) ──────────
  // `tab.detectedModel` is seeded on open (loadHistoryMessages ← conversation
  // summary) and refreshed at each turn end (confirmDone ← summary re-read).
  // Reading it here is fully reactive: switching tabs, opening a conversation,
  // or a turn ending all re-drive these computeds with ZERO disk scans. The
  // per-tab field means there is NO cross-tab bleed (串味) — each tab's CTA
  // reflects ITS OWN conversation's detection.
  const detectedWorkdir = computed<string>(() => {
    const dm = store.activeTab?.detectedModel;
    if (dm == null) return "";
    return typeof dm.workdir === "string" ? dm.workdir : "";
  });
  const detectedVariants = computed<DetectedVariant[]>(() => {
    const dm = store.activeTab?.detectedModel;
    if (dm == null || !Array.isArray(dm.variants)) return [];
    return dm.variants
      .filter(
        (v): v is { precision: string; label: string } =>
          v != null &&
          typeof v.precision === "string" &&
          v.precision !== "" &&
          typeof v.label === "string" &&
          v.label !== "",
      )
      .map((v) => ({ precision: v.precision, label: v.label }));
  });

  // ── Public shouldShow gate ─────────────────────────────────────────────────
  // Renders the CTA when:
  //   1. we are in an eligible mode;
  //   2. a workdir was detected;
  //   3. ≥1 variant was scanned;
  //   4. the user has not dismissed the CTA for THIS workdir this session.
  const shouldShow = computed<boolean>(() => {
    // Depend on `dismissBump` so `dismiss()` bumps re-evaluate.
    void dismissBump.value;
    if (!modeAllowsCta.value) return false;
    if (detectedWorkdir.value === "") return false;
    if (detectedVariants.value.length === 0) return false;
    if (readDismissed(detectedWorkdir.value)) return false;
    return true;
  });

  function dismiss(): void {
    writeDismissed(detectedWorkdir.value);
    dismissBump.value += 1;
  }

  return {
    shouldShow,
    // Exposed as read-only `Ref`-compatible computeds (the interface types them
    // as `Ref<...>`; a `ComputedRef` is assignable — consumers only read
    // `.value`). Keeps the public surface identical to the previous version so
    // the three call sites (ChatView / ModeFrameModelBuilder / ModeFrameModelHub)
    // and `PromoteReadyNotice` need no change.
    detectedWorkdir,
    detectedVariants,
    dismiss,
  };
}
