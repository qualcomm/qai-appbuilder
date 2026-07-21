// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useConversationBudget` — reactive reader + mutator for the per-conversation
 * TOKEN budget (`max_budget_tokens`).
 *
 * The budget cap + running counter are PERSISTED in `Conversation.meta.budget`
 * ({ max_tokens, used_tokens }), unlike the in-memory per-session tool override.
 * This composable:
 *
 *  - reads the current snapshot READ-ONLY on demand (`refresh`) from
 *    `GET /api/chat/conversations/{id}` (via `fetchConversationBudget`);
 *  - watches a conversation-id source and refetches when it changes to a
 *    non-null value (session switch);
 *  - saves a new cap / reset via `PATCH .../budget` (`save` / `reset`), updating
 *    the local snapshot from the authoritative PATCH response
 *    (State-Truth-First: the UI reflects the backend result, not an optimistic
 *    guess);
 *  - exposes derived `enabled` / `pct` / `severity` for the badge + progress
 *    bar (three-tier tint reused from `useContextUsage.severityFor`).
 *
 * It is silent on transient read errors — the badge simply hides (snapshot
 * stays `null`) when the endpoint is unreachable or the conversation is not
 * persisted yet (fresh tab).
 */
import { ref, computed, watch, type Ref, type ComputedRef } from "vue";
import {
  fetchConversationBudget,
  setConversationBudget,
  type ConversationBudgetSnapshot,
} from "@/api/conversationBudget";
import {
  severityFor,
  type ContextSeverity,
} from "@/composables/chat/useContextUsage";

export type BudgetSeverity = ContextSeverity;

export interface UseConversationBudget {
  /** Current snapshot, or `null` when unavailable / not yet loaded. */
  snapshot: Ref<ConversationBudgetSnapshot | null>;
  /** `true` while a read / write is in flight. */
  loading: Ref<boolean>;
  /** `true` when a positive cap is configured for the active conversation. */
  enabled: ComputedRef<boolean>;
  /** Fractional usage (`used / max`), un-clamped (may exceed 1.0). 0 when disabled. */
  pct: ComputedRef<number>;
  /** Three-tier severity for the progress-bar tint (ok / warn / danger). */
  severity: ComputedRef<BudgetSeverity>;
  /** Re-read the snapshot from the backend (read-only). */
  refresh: () => Promise<void>;
  /**
   * Persist a new cap (`null` disables). Optionally reset the running counter.
   * Updates the local snapshot from the PATCH response. Returns the snapshot
   * (or `null` when there is no conversation to write to).
   */
  save: (
    maxTokens: number | null,
    resetUsed?: boolean,
  ) => Promise<ConversationBudgetSnapshot | null>;
  /** Reset the running `used_tokens` counter (keeps the current cap). */
  reset: () => Promise<ConversationBudgetSnapshot | null>;
}

export function useConversationBudget(
  conversationIdSource: Ref<string | null> | (() => string | null),
): UseConversationBudget {
  const snapshot = ref<ConversationBudgetSnapshot | null>(null);
  const loading = ref(false);

  function readId(): string | null {
    return typeof conversationIdSource === "function"
      ? conversationIdSource()
      : conversationIdSource.value;
  }

  async function refresh(): Promise<void> {
    const convId = readId();
    if (convId === null || convId === "") {
      snapshot.value = null;
      return;
    }
    loading.value = true;
    try {
      snapshot.value = await fetchConversationBudget(convId);
    } finally {
      loading.value = false;
    }
  }

  async function save(
    maxTokens: number | null,
    resetUsed = false,
  ): Promise<ConversationBudgetSnapshot | null> {
    const convId = readId();
    if (convId === null || convId === "") {
      return null;
    }
    loading.value = true;
    try {
      const next = await setConversationBudget(convId, {
        max_tokens: maxTokens,
        reset_used: resetUsed,
      });
      snapshot.value = next;
      return next;
    } finally {
      loading.value = false;
    }
  }

  async function reset(): Promise<ConversationBudgetSnapshot | null> {
    // Preserve the current cap; only zero the running counter.
    return save(snapshot.value?.max_tokens ?? null, true);
  }

  const enabled = computed<boolean>(
    () => snapshot.value !== null && snapshot.value.enabled,
  );

  const pct = computed<number>(() => {
    const s = snapshot.value;
    if (s === null || s.max_tokens === null || s.max_tokens <= 0) return 0;
    return s.used_tokens / s.max_tokens;
  });

  const severity = computed<BudgetSeverity>(() =>
    enabled.value ? severityFor(pct.value) : "ok",
  );

  // Refetch whenever the conversation id changes to a different value (session
  // switch). Immediate so the first mount reads the persisted budget.
  watch(
    () => readId(),
    (nextId, prevId) => {
      if (nextId !== prevId) {
        void refresh();
      }
    },
    { immediate: true },
  );

  return {
    snapshot,
    loading,
    enabled,
    pct,
    severity,
    refresh,
    save,
    reset,
  };
}
