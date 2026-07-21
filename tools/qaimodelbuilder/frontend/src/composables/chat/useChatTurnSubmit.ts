// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useChatTurnSubmit` — the single "send a turn to tab X" entry point.
 *
 * Extracted verbatim from `ChatView.vue`'s in-file `submitToTab(id, prompt)`
 * (ARCH cohesion split + reuse) so that EVERY route that needs to start a
 * normal-chat turn on a SPECIFIC tab — the interactive composer submit, the
 * streaming→idle auto-dequeue watcher, AND the scheduled-continuation timer —
 * goes through the exact same path: slash-command interception → error reset →
 * `pushUserMessage` (atomic idle→streaming) → `transport.send`.
 *
 * Why an explicit `id` matters (preserved from the original ChatView note): a
 * queued / scheduled message belongs to the tab it was created on, NOT the
 * active tab. Binding the send to `id` (not the active tab) is what keeps
 * background-tab sends from being dropped by the active tab's idle-gate.
 *
 * This composable owns NO state — it just wires the existing store + transport
 * + commands singletons. Safe to call from multiple component setups.
 */
import { useChatTabsStore } from "@/stores/chatTabs";
import { useChatTransports } from "@/composables/chat/useChatTransports";
import { useChatCommands } from "@/composables/chat/useChatCommands";
import { decideExternalSubmitAction } from "@/composables/chat/usePendingChatSubmit";
import { useCloudModelStatus } from "@/composables/useCloudModelStatus";

export function useChatTurnSubmit(): {
  submitToTab: (id: string, prompt: string) => Promise<void>;
} {
  const store = useChatTabsStore();
  const { getTransport } = useChatTransports();
  const chatCommands = useChatCommands();
  const cloudModelStatus = useCloudModelStatus();

  /**
   * Submit a prompt to a SPECIFIC tab (by id), independent of which tab is
   * currently active. Mirrors the former `ChatView.submitToTab`.
   */
  async function submitToTab(id: string, prompt: string): Promise<void> {
    // Slash-command interception (V1 useChat.js:1566-1928). A recognized
    // command is handled entirely on the front end and NEVER sent to the
    // model. Unknown slash tokens fall through to a normal turn.
    if (chatCommands.isCommand(prompt)) {
      const handled = await chatCommands.executeCommand(prompt);
      if (handled) {
        return;
      }
    }
    // Missing-cloud-API-key interception (guided flow). Before sending a turn
    // with a cloud model that has no API key, DON'T send — instead trigger the
    // edition-aware action (internal → in-place key dialog; external → route
    // to Settings → Cloud Models). This is the single choke point every send
    // path funnels through, so both the interactive composer and background /
    // scheduled sends are covered.
    const targetTab = store.tabs.find((tab) => tab.id === id);
    const modelId = targetTab?.modelId ?? "";
    const modelProvider = targetTab?.modelProvider ?? "";
    // Skip the check for models that never need a cloud key: the placeholder
    // `qai-default`, any `local::` model (on-device, no key), or a model with
    // no provider at all.
    const skipKeyCheck =
      modelId === "" ||
      modelId === "qai-default" ||
      modelId.startsWith("local::") ||
      modelProvider === "";
    if (!skipKeyCheck) {
      // Make sure provider data is loaded so `providerMissingKey` is
      // meaningful. If a fetch has not completed yet, `ensureChecked()`
      // resolves it; on any failure we simply DON'T block — the backend
      // guard still returns `provider_api_key_missing` and the error bubble
      // takes over.
      try {
        await cloudModelStatus.ensureChecked();
      } catch {
        // Non-fatal: fall through to NOT blocking the send.
      }
      if (cloudModelStatus.providerMissingKey(modelProvider)) {
        // Point the shared dialog at THIS provider (so an internal-edition
        // save targets the right one), then run the edition-aware flow and
        // return WITHOUT sending (no user message is pushed).
        cloudModelStatus.openApiKeyFlowForProvider(modelProvider);
        return;
      }
    }
    // A turn that ended in `error` must NOT block the next send: reuse the
    // SAME decision source as the programmatic App-Builder submit path so all
    // routes agree on the error → reset-and-submit transition.
    const status = store.tabs.find((tab) => tab.id === id)?.status ?? "idle";
    if (decideExternalSubmitAction(status) === "reset-and-submit") {
      store.resetError(id);
    }
    const messageId = store.pushUserMessage(id, prompt);
    if (messageId === null) {
      // tab not in idle → no-op (callers guard against this; defensive).
      return;
    }
    const transport = getTransport(id);
    try {
      await transport.send(prompt, messageId);
    } catch {
      // Error already surfaced via store.recordError + per-message sendError
      // marker (transport associates the failure with `messageId`).
    }
  }

  return { submitToTab };
}
