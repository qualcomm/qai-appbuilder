// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useConversationFork — POST /api/chat/conversations/{id}/fork wrapper.
 *
 * Shared by two call sites (AppSidebar's 🔀 button and ChatMessageList's
 * per-message 🔀 button, hosted by ChatView) so the branching flow —
 *
 *   1. POST /fork with the sliced payload
 *   2. Upsert the new conversation into the sidebar store
 *   3. Open a new tab bound to the fork
 *   4. Load its history + switch to it
 *   5. Toast success (`chat.fork.success`) or failure
 *      (`chat.fork.failed`)
 *
 * — is captured in one place and cannot drift between sidebar and message
 * entry points. The composable is intentionally passive: it does not own
 * the confirm-dialog state (each host component keeps its own visible /
 * target refs), only the async work that runs after the user confirms.
 *
 * Non-goals: routing (the host component owns "switch to /chat" via its
 * own router usage), and dialog UI (`ForkConversationDialog.vue` is the
 * shared modal).
 */
import { useI18n } from "vue-i18n";
import { apiJson, ApiError } from "@/api";
import { useChatTabsStore } from "@/stores/chatTabs";
import {
  useConversationsStore,
  type ConversationSummary,
} from "@/stores/conversations";
import { useToast } from "@/composables/useToast";

/**
 * The dialog's emit payload, mirrored 1:1 for portability between
 * `ForkConversationDialog` and this composable.
 */
export interface ForkConfirmPayload {
  mode:
    | "all"
    | "rounds-first"
    | "rounds-last"
    | "messages-first"
    | "messages-last"
    | "up-to-message";
  count: number | null;
  upToMessageId: string | null;
  title: string;
  includeToolCalls: boolean;
  inheritSettings: boolean;
}

/** Backend body shape — mirrors ``ForkConversationRequest`` (see _rest.py). */
interface ForkRequestBody {
  keep_first_n: number | null;
  keep_last_n: number | null;
  keep_first_rounds: number | null;
  keep_last_rounds: number | null;
  up_to_message_id: string | null;
  include_tool_calls: boolean;
  title: string | null;
  inherit_settings: boolean;
}

export interface UseConversationForkReturn {
  /**
   * Execute a fork. Returns the newly-created ``ConversationSummary`` on
   * success, ``null`` on failure (a toast is emitted either way, so
   * callers rarely need to inspect the value; the return exists so the
   * host can e.g. dismiss its own dialog only on success).
   */
  forkConversation: (
    sourceId: string,
    payload: ForkConfirmPayload,
  ) => Promise<ConversationSummary | null>;
}

/**
 * Translate the dialog's ``mode`` + ``count`` into the mutually-exclusive
 * backend slicer. Kept pure so the mapping is trivially testable and never
 * silently sends two slicers (which the backend rejects with 400).
 */
function _buildBody(payload: ForkConfirmPayload): ForkRequestBody {
  const body: ForkRequestBody = {
    keep_first_n: null,
    keep_last_n: null,
    keep_first_rounds: null,
    keep_last_rounds: null,
    up_to_message_id: null,
    include_tool_calls: payload.includeToolCalls,
    title: payload.title !== "" ? payload.title : null,
    inherit_settings: payload.inheritSettings,
  };
  // The anchor (`upToMessageId`) is orthogonal to the count slicer: the
  // backend cuts to the anchor FIRST, then applies any trailing slicer on
  // that prefix. So in message-fork mode a "last N rounds" selection sends
  // BOTH up_to_message_id AND keep_last_rounds — "the last N rounds up to
  // this message". Carry the anchor whenever the dialog provided one.
  if (payload.upToMessageId !== null && payload.upToMessageId !== "") {
    body.up_to_message_id = payload.upToMessageId;
  }
  switch (payload.mode) {
    case "rounds-first":
      body.keep_first_rounds = payload.count;
      break;
    case "rounds-last":
      body.keep_last_rounds = payload.count;
      break;
    case "messages-first":
      body.keep_first_n = payload.count;
      break;
    case "messages-last":
      body.keep_last_n = payload.count;
      break;
    case "up-to-message":
      // Anchor already set above; no count slicer → keep everything up to
      // and including the anchor.
      break;
    case "all":
    default:
      // No count slicer. With no anchor → full clone; with an anchor (can't
      // happen for "all" in message mode, which emits "up-to-message") the
      // anchor above still applies.
      break;
  }
  return body;
}

export function useConversationFork(): UseConversationForkReturn {
  const { t } = useI18n();
  const toast = useToast();
  const chatTabs = useChatTabsStore();
  const conversationsStore = useConversationsStore();

  async function forkConversation(
    sourceId: string,
    payload: ForkConfirmPayload,
  ): Promise<ConversationSummary | null> {
    try {
      const body = _buildBody(payload);
      const result = await apiJson<ConversationSummary>(
        "POST",
        `/api/chat/conversations/${encodeURIComponent(sourceId)}/fork`,
        body,
      );
      // 1. Sidebar: sidebar store owns the recent-chats list. Upsert so the
      //    row appears immediately (no wait for the next `fetch()` cycle).
      conversationsStore.upsert(result);
      // 2. Open a new main-agent tab bound to the fork. `openTab` activates
      //    the new tab (stores/chatTabs.ts: this.activeTabId = tab.id), so
      //    the user immediately lands on the fork.
      const tab = chatTabs.openTab({
        title: result.title,
        conversationId: result.id,
      });
      // 3. Lazy-load the sliced history so the message list renders.
      await chatTabs.loadHistoryMessages(tab.id);
      toast.success(t("chat.fork.success"));
      return result;
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      toast.error(t("chat.fork.failed", { msg }));
      return null;
    }
  }

  return { forkConversation };
}
