// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useConversationPin — toggle a conversation's pin / favorite flags.
 *
 * Mirrors `useConversationRename`'s shape (optimistic store write + backend
 * PATCH + toast on failure), but for the two boolean meta flags:
 *   • pin     → PATCH /api/chat/conversations/{id}/pin       { pinned }
 *   • favorite→ PATCH /api/chat/conversations/{id}/favorite  { favorite }
 *
 * Both flags persist in `conversation.meta` server-side (no schema change),
 * so the toggle is a tiny, reversible action — no confirm dialog. We optim
 * istically flip the store immediately (so the sidebar re-buckets / the star
 * fills instantly) and roll back + toast if the backend rejects.
 *
 * No `watch`, no lifecycle hooks: pure functions, safe in any setup function.
 */
import { useI18n } from "vue-i18n";
import { apiJson, ApiError } from "@/api";
import {
  useConversationsStore,
  type ConversationSummary,
} from "@/stores/conversations";
import { useToast } from "@/composables/useToast";

export interface UseConversationPinReturn {
  togglePin: (conv: ConversationSummary) => Promise<void>;
  toggleFavorite: (conv: ConversationSummary) => Promise<void>;
}

export function useConversationPin(): UseConversationPinReturn {
  const { t } = useI18n();
  const conversationsStore = useConversationsStore();
  const toast = useToast();

  async function togglePin(conv: ConversationSummary): Promise<void> {
    // Read the CURRENT pin state from the store (not the possibly-stale `conv`
    // reference the caller passed): the store replaces row objects on every
    // mutation, so a rapid double-click would otherwise compute `next` from an
    // outdated snapshot and desync. Fall back to the passed `conv` if the row
    // is no longer in the store.
    const current =
      conversationsStore.conversations.find((c) => c.id === conv.id) ?? conv;
    const next = current.pinned !== true;
    // Optimistic flip first so the sidebar re-buckets immediately.
    conversationsStore.setPinned(conv.id, next);
    try {
      await apiJson<ConversationSummary>(
        "PATCH",
        `/api/chat/conversations/${encodeURIComponent(conv.id)}/pin`,
        { pinned: next },
      );
    } catch (err) {
      // Roll back the optimistic flip and tell the user it didn't take.
      conversationsStore.setPinned(conv.id, !next);
      void (err instanceof ApiError ? err.code : err);
      toast.error(t("chat.pinFailed"));
    }
  }

  async function toggleFavorite(conv: ConversationSummary): Promise<void> {
    const current =
      conversationsStore.conversations.find((c) => c.id === conv.id) ?? conv;
    const next = current.favorite !== true;
    conversationsStore.setFavorite(conv.id, next);
    try {
      await apiJson<ConversationSummary>(
        "PATCH",
        `/api/chat/conversations/${encodeURIComponent(conv.id)}/favorite`,
        { favorite: next },
      );
    } catch (err) {
      conversationsStore.setFavorite(conv.id, !next);
      void (err instanceof ApiError ? err.code : err);
      toast.error(t("chat.favoriteFailed"));
    }
  }

  return { togglePin, toggleFavorite };
}
