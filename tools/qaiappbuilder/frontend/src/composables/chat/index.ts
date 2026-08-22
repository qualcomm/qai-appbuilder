// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Chat composables barrel (PR-053 + PR-054).
 *
 * The legacy frontend/js/composables/useChat.js (~3170 LOC) is split
 * into eight focused composables here. PR-053 shipped the skeletons
 * for six; PR-054 upgrades them and adds two transport composables
 * (`useChatWebSocket`, `useChatTransport`) implementing the multi-tab
 * 4-state machine per refactor-plan §10.5/§10.6.
 */
export {
  useChatTabs,
  type UseChatTabs,
} from "./useChatTabs";
export {
  useChatState,
  type UseChatState,
  type ChatMessage,
  type ChatMessageRole,
} from "./useChatState";
export {
  useChatStream,
  type UseChatStream,
  type UseChatStreamOptions,
  type ChatStreamHandlers,
} from "./useChatStream";
export {
  useChatTools,
  type UseChatTools,
  type ToolCall,
  type ToolCallStatus,
} from "./useChatTools";
export {
  useChatSubagent,
  type UseChatSubagent,
  type SubAgentBlock,
  type SubAgentTreeNode,
} from "./useChatSubagent";
export {
  useChatPersistence,
  type UseChatPersistence,
  type PersistedTabState,
} from "./useChatPersistence";
export {
  useChatWebSocket,
  type ChatWebSocketClient,
  type ChatWsHandlers,
  type ChatWsState,
  type UseChatWebSocketOptions,
} from "./useChatWebSocket";
export {
  useChatTransport,
  type ChatTransport,
  type UseChatTransportOptions,
  type TransportKind,
} from "./useChatTransport";
