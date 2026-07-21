// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `usePromptEnhance` — composable for LLM-based prompt enhancement.
 *
 * S7.5 L7 PR-702.
 *
 * Enhances a user's prompt via the server-side LLM endpoint.
 * Endpoint: POST `/api/prompt/enhance`.
 *
 * P4-A (T2): no longer pushes a toast on failure — the caller
 * (PromptEnhanceBtn) surfaces failures via a brief shake + tooltip,
 * matching the "no toast for predictable input failures" UX rule.
 * The original text is returned unchanged on any error.
 *
 * Block 1 (P4-A redo): ported V1's undo window + race-guard from
 * `usePromptEnhance.js` (frontend/js/composables, 259 lines):
 *   - `_requestCounter` discards late responses so a slow reply never
 *     overwrites a faster one (V1 `_requestCounter` parity).
 *   - `canUndo` + `undo()` let the button briefly turn into an undo
 *     toggle that restores the pre-enhance text, but only while the
 *     user has not edited the enhanced text themselves.
 */
import { ref } from "vue";

import { apiJson } from "@/api";

// ─── Types ───────────────────────────────────────────────────────────────────

interface EnhanceResponse {
  text: string;
  model_id: string;
  model_provider: string;
}

// ─── Composable ──────────────────────────────────────────────────────────────

export function usePromptEnhance() {
  const enhancing = ref(false);
  const lastFailed = ref(false);
  /** True while an undo of the last enhance is still possible. */
  const canUndo = ref(false);

  // Race-guard: each enhance call grabs an id; only the latest may write.
  let requestCounter = 0;
  // Undo bookkeeping — original text + the enhanced text we produced.
  let lastOriginalText = "";
  let lastEnhancedText = "";

  async function enhance(text: string, modelId?: string): Promise<string> {
    if (!text.trim()) return text;
    if (enhancing.value) return text;

    const myReqId = ++requestCounter;
    enhancing.value = true;
    lastFailed.value = false;
    canUndo.value = false;
    try {
      const body: Record<string, string> = { text };
      if (modelId !== undefined && modelId !== "") {
        body.model_id = modelId;
      }
      const res = await apiJson<EnhanceResponse>(
        "POST",
        "/api/prompt/enhance",
        body,
      );
      // Discard a stale response superseded by a newer call.
      if (myReqId !== requestCounter) return text;
      const enhanced = res.text.trim();
      lastOriginalText = text;
      lastEnhancedText = enhanced;
      canUndo.value = enhanced !== "" && enhanced !== text;
      return enhanced;
    } catch {
      if (myReqId === requestCounter) lastFailed.value = true;
      return text; // Return original on failure (caller signals via lastFailed).
    } finally {
      enhancing.value = false;
    }
  }

  /**
   * Restore the pre-enhance text — returns it to the caller when the
   * `current` text still matches what we produced (i.e. the user has
   * not edited it). Returns `null` when undo is not applicable so the
   * caller can leave the textarea untouched. V1 parity: only undo when
   * `cur === _lastEnhancedText`.
   */
  function undo(current: string): string | null {
    if (!canUndo.value) return null;
    if (current !== lastEnhancedText) {
      // User edited the enhanced text — undo no longer safe.
      canUndo.value = false;
      return null;
    }
    canUndo.value = false;
    return lastOriginalText;
  }

  return { enhancing, lastFailed, canUndo, enhance, undo };
}
