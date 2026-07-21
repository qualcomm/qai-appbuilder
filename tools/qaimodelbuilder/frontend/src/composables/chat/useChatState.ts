// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatState — single-tab message list + role/sender state.
 *
 * S5 PR-053: skeleton + types only. This composable owns the
 * reactive message buffer for one chat tab: append / clear / replace
 * for streaming chunk assembly. Real persistence is delegated to
 * `useChatPersistence` and the WS-driven mutation flow lands in
 * PR-054.
 */
import { ref, computed, type Ref, type ComputedRef } from "vue";

export type ChatMessageRole =
  | "user"
  | "assistant"
  | "system"
  | "tool"
  | "tool_indicator";

export interface ChatMessage {
  readonly id: string;
  readonly role: ChatMessageRole;
  readonly content: string;
  readonly createdAt: number;
  readonly conversationId?: string;
  readonly toolCallId?: string;
  readonly meta?: Record<string, unknown>;
}

export interface UseChatState {
  readonly messages: ComputedRef<readonly ChatMessage[]>;
  readonly streamingContent: Ref<string>;
  readonly isStreaming: Ref<boolean>;
  readonly inputText: Ref<string>;
  appendMessage(message: ChatMessage): void;
  replaceMessage(id: string, message: ChatMessage): void;
  removeMessage(id: string): void;
  clear(): void;
  beginStreaming(): void;
  appendStreamingChunk(chunk: string): void;
  finishStreaming(finalMessage: ChatMessage | null): void;
  abortStreaming(): void;
}

export function useChatState(): UseChatState {
  const internalMessages = ref<ChatMessage[]>([]);
  const streamingContent = ref<string>("");
  const isStreaming = ref<boolean>(false);
  const inputText = ref<string>("");

  return {
    messages: computed(() => internalMessages.value),
    streamingContent,
    isStreaming,
    inputText,
    appendMessage(message) {
      internalMessages.value = [...internalMessages.value, message];
    },
    replaceMessage(id, message) {
      internalMessages.value = internalMessages.value.map((m) =>
        m.id === id ? message : m,
      );
    },
    removeMessage(id) {
      internalMessages.value = internalMessages.value.filter(
        (m) => m.id !== id,
      );
    },
    clear() {
      internalMessages.value = [];
      streamingContent.value = "";
      isStreaming.value = false;
    },
    beginStreaming() {
      streamingContent.value = "";
      isStreaming.value = true;
    },
    appendStreamingChunk(chunk) {
      streamingContent.value = streamingContent.value + chunk;
    },
    finishStreaming(finalMessage) {
      if (finalMessage !== null) {
        internalMessages.value = [...internalMessages.value, finalMessage];
      }
      streamingContent.value = "";
      isStreaming.value = false;
    },
    abortStreaming() {
      streamingContent.value = "";
      isStreaming.value = false;
    },
  };
}
