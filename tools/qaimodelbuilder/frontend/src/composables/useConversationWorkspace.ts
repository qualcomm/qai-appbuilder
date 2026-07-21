// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useConversationWorkspace — session-level workspace (write directory) dialog
 * state + handlers, modelled on `useConversationRename.ts`.
 *
 * V2 enhancement (no V1 equivalent): a conversation can override the global
 * default write directory (`forge.config` → `workspace.model_root`) with its
 * own session-level workspace. This composable owns the four-ref dialog state
 * machine (open / value / loading / target) and the three handlers
 * (open / cancel / confirm), so callers just destructure the refs and bind
 * them to `<ConversationWorkspaceDialog>`.
 *
 * Brand-new tab (no conversation row yet): the dialog must still be usable so
 * the user can pick a workspace BEFORE sending the first message. Callers pass
 * an optional `ensureConversation` resolver to `setConversationWorkspace`; when
 * the dialog is confirmed and the target has no real id yet, we invoke that
 * resolver to lazily materialise a conversation (bind it to the tab + seed the
 * sidebar — mirroring `useDiscussion.ensureConversation`), then PATCH the
 * freshly created conversation. This keeps the 📁 button always actionable
 * instead of being disabled until the first message.
 *
 * The PATCH route accepts `{ workspace: string | null }`; null/empty clears
 * the session-level override and falls back to the global default. We trim the
 * input and send `null` when empty so the backend drops the per-conversation
 * key. On success we optimistically update the shared conversations store via
 * `setWorkspace`; on failure we surface a toast and keep the dialog open so the
 * user can retry (mirrors the rename flow).
 *
 * No `watch`, no lifecycle hooks: pure ref + functions, safe to call in any
 * setup function.
 */
import { ref, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson, ApiError } from "@/api";
import {
  useConversationsStore,
  type ConversationSummary,
} from "@/stores/conversations";
import { useToast } from "@/composables/useToast";

export interface UseConversationWorkspaceReturn {
  workspaceOpen: Ref<boolean>;
  workspaceValue: Ref<string>;
  workspaceLoading: Ref<boolean>;
  workspaceTarget: Ref<ConversationSummary | null>;
  setConversationWorkspace: (
    conv: ConversationSummary,
    ensureConversation?: EnsureConversation,
  ) => void;
  cancelWorkspace: () => void;
  confirmWorkspace: () => Promise<void>;
}

/**
 * Resolver that lazily materialises a real conversation for a brand-new tab
 * that has none yet, returning its id (or null on failure). Provided by the
 * caller (e.g. ChatView) so this composable stays decoupled from tab/transport
 * wiring. When the target conversation already has a real id this is unused.
 */
export type EnsureConversation = () => Promise<string | null>;

/** Read the current session-level workspace from a conversation's meta. */
function readWorkspace(conv: ConversationSummary): string {
  const raw = conv.meta?.workspace;
  return typeof raw === "string" ? raw : "";
}

export function useConversationWorkspace(): UseConversationWorkspaceReturn {
  const { t } = useI18n();
  const conversationsStore = useConversationsStore();
  const toast = useToast();

  const workspaceOpen = ref(false);
  const workspaceValue = ref("");
  const workspaceLoading = ref(false);
  const workspaceTarget = ref<ConversationSummary | null>(null);
  // Resolver to lazily create a conversation when the target has no real id
  // yet (brand-new tab). Captured per-open so it is scoped to the current
  // dialog session; cleared on cancel/close.
  let ensureConversationFn: EnsureConversation | null = null;

  function setConversationWorkspace(
    conv: ConversationSummary,
    ensureConversation?: EnsureConversation,
  ): void {
    workspaceTarget.value = conv;
    workspaceValue.value = readWorkspace(conv);
    ensureConversationFn = ensureConversation ?? null;
    workspaceOpen.value = true;
  }

  function cancelWorkspace(): void {
    workspaceOpen.value = false;
    workspaceTarget.value = null;
    workspaceLoading.value = false;
    ensureConversationFn = null;
  }

  async function confirmWorkspace(): Promise<void> {
    const conv = workspaceTarget.value;
    if (conv === null) return;
    // Tolerate Windows paths (backslashes) — only trim outer whitespace, no
    // over-validation; the backend handles null / empty / normalisation.
    const trimmed = workspaceValue.value.trim();
    const current = readWorkspace(conv);
    // No-op when the value is unchanged AND the conversation already exists.
    // (For a brand-new tab the id is empty, so we never short-circuit here —
    // an empty workspace on a not-yet-created conversation is still a no-op
    // since there is nothing to persist.)
    const hasRealId = conv.id !== "";
    if (trimmed === current && hasRealId) {
      cancelWorkspace();
      return;
    }
    // Empty input clears the session-level override (fall back to global).
    const payload: string | null = trimmed === "" ? null : trimmed;
    workspaceLoading.value = true;
    try {
      // Brand-new tab: materialise a real conversation first so the workspace
      // has a row to attach to (mirrors useDiscussion.ensureConversation).
      let targetId = conv.id;
      if (targetId === "") {
        if (ensureConversationFn === null) {
          // Nothing to create against and no real id — clear (no-op) and close.
          cancelWorkspace();
          return;
        }
        // Empty path on a brand-new tab = nothing to persist; just close.
        if (payload === null) {
          cancelWorkspace();
          return;
        }
        const createdId = await ensureConversationFn();
        if (createdId === null || createdId === "") {
          toast.error(t("sessionWorkspace.saveFailed"));
          workspaceLoading.value = false;
          return;
        }
        targetId = createdId;
      }
      const updated = await apiJson<ConversationSummary>(
        "PATCH",
        `/api/chat/conversations/${encodeURIComponent(targetId)}/workspace`,
        { workspace: payload },
      );
      // Prefer the backend's echoed meta.workspace; fall back to what we sent.
      const echoed = updated.meta?.workspace;
      const finalValue =
        typeof echoed === "string" && echoed !== "" ? echoed : payload;
      conversationsStore.setWorkspace(targetId, finalValue);
      cancelWorkspace();
    } catch (err) {
      // Surface failures via a toast (rename-flow parity); keep the dialog
      // open so the user can retry.
      void (err instanceof ApiError ? err.code : err);
      toast.error(t("sessionWorkspace.saveFailed"));
      workspaceLoading.value = false;
    }
  }

  return {
    workspaceOpen,
    workspaceValue,
    workspaceLoading,
    workspaceTarget,
    setConversationWorkspace,
    cancelWorkspace,
    confirmWorkspace,
  };
}
