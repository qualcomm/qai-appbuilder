// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * SSE composable barrel.
 *
 * S7.5 L8 PR-804: replaces the legacy single-channel `/api/events`
 * subscription with one composable per bounded context (P0-FR9).
 */
export { useSse } from "./useSse";
export type {
  SseConnectionState,
  UseSseOptions,
  UseSseReturn,
} from "./useSse";

export { useSseChat } from "./useSseChat";
export type { ChatStreamFrame, UseSseChatReturn } from "./useSseChat";

export { useSseDownloads } from "./useSseDownloads";
export type {
  DownloadProgressFrame,
  UseSseDownloadsReturn,
} from "./useSseDownloads";

export { useSseAppBuilder } from "./useSseAppBuilder";
export type {
  AppBuilderStateFrame,
  AppBuilderDataFrame,
  UseSseAppBuilderReturn,
} from "./useSseAppBuilder";

export { useSseAiCoding } from "./useSseAiCoding";
export type {
  AiCodingStreamFrame,
  UseSseAiCodingReturn,
} from "./useSseAiCoding";
