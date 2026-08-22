// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useConversationRename — V1-parity Recent Chats rename dialog state +
 * handlers, extracted from `AppSidebar.vue` (cohesion split).
 *
 * Owns the small four-ref dialog state machine (open/value/loading/target)
 * and the three handlers (open / cancel / confirm), exactly as V1
 * `RenameDialog.js` + `useChat.js` rename flow does. The composable
 * internally pulls the conversations store, the multi-tab chat store
 * (so a rename also retitles the tab strip), the toast bus, and i18n —
 * so callers just destructure the refs and bind them to `<RenameDialog>`.
 *
 * No `watch`, no lifecycle hooks: pure ref + functions, safe to call in
 * any setup function.
 */
import { ref, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson, ApiError } from "@/api";
import {
  useConversationsStore,
  type ConversationSummary,
} from "@/stores/conversations";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useToast } from "@/composables/useToast";

export interface UseConversationRenameReturn {
  renameOpen: Ref<boolean>;
  renameValue: Ref<string>;
  renameLoading: Ref<boolean>;
  renameTarget: Ref<ConversationSummary | null>;
  renameConversation: (conv: ConversationSummary) => void;
  cancelRename: () => void;
  confirmRename: () => Promise<void>;
}

export function useConversationRename(): UseConversationRenameReturn {
  const { t } = useI18n();
  const conversationsStore = useConversationsStore();
  const chatTabs = useChatTabsStore();
  const toast = useToast();

  const renameOpen = ref(false);
  const renameValue = ref("");
  const renameLoading = ref(false);
  const renameTarget = ref<ConversationSummary | null>(null);

  function renameConversation(conv: ConversationSummary): void {
    renameTarget.value = conv;
    renameValue.value = conv.title;
    renameOpen.value = true;
  }

  function cancelRename(): void {
    renameOpen.value = false;
    renameTarget.value = null;
    renameLoading.value = false;
  }

  async function confirmRename(): Promise<void> {
    const conv = renameTarget.value;
    if (conv === null) return;
    const trimmed = renameValue.value.trim();
    if (trimmed === "" || trimmed === conv.title) {
      cancelRename();
      return;
    }
    renameLoading.value = true;
    try {
      const updated = await apiJson<ConversationSummary>(
        "PATCH",
        `/api/chat/conversations/${encodeURIComponent(conv.id)}`,
        { title: trimmed },
      );
      const finalTitle = updated.title ?? trimmed;
      conversationsStore.rename(conv.id, finalTitle);
      // Also update EVERY open tab bound to this conversation (V2 may open
      // the same conversation in multiple tabs).
      chatTabs.renameTabsByConversation(conv.id, finalTitle);
      cancelRename();
    } catch (err) {
      // V1 useChat.js parity: surface rename failures via a toast instead of
      // silently swallowing the error. 4xx → keep dialog open so the user can
      // retry; the toast tells them it didn't take effect.
      void (err instanceof ApiError ? err.code : err);
      toast.error(t("chat.renameFailed"));
      renameLoading.value = false;
    }
  }

  return {
    renameOpen,
    renameValue,
    renameLoading,
    renameTarget,
    renameConversation,
    cancelRename,
    confirmRename,
  };
}
