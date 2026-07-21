// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Public types for `useCodingSession` and its sub-composables (cohesion
 * split). Extracted verbatim from the original `useCodingSession.ts` so
 * the orchestration layer and every slice factory share one definition
 * without import cycles.
 */

export type CodingKind = "cc" | "oc";

export interface CodingSession {
  session_id: string;
  workspace: string;
  title: string | null;
  status: string;
  provider: string;
  created_at: string;
  terminated_at?: string | null;
  termination_reason?: string | null;
  last_stream_sequence?: number;
  // Optional context badge data (filled by `refreshContextUsage`).
  context_total_tokens?: number;
  context_max_tokens?: number;
  context_percentage?: number;
  // Optional session-level effort (CC only).
  effort?: string | null;
  // Upstream provider conversation id (CC); gates the "new branch" (fork)
  // action — only set after a real SDK turn populates message_start.
  claude_session_id?: string | null;
  // Message count (V1-parity approximation of completed turns) for the
  // session-list turn badge.
  turn_count?: number;
  // ── V1 session-list badge fields (AiCodingPanel.js:1027-1064) ──────────────
  /** Origin channel: "webui" | "wechat" | "feishu" (panel source badge). */
  source?: string | null;
  /** Channel owner id (used for the source-badge tooltip). */
  owner?: string | null;
  /** Bound WeChat notify user id (dual-sync 🔔 badge). */
  wechat_notify_user_id?: string | null;
  /** Bound Feishu notify open id (dual-sync 🔔 badge). */
  feishu_notify_user_id?: string | null;
  /** Cumulative tool-call count (panel 🔧 badge). */
  total_tool_calls?: number;
  /** Last-turn input tokens (panel ctx badge live value). */
  last_input_tokens?: number;
  /** Model context window (panel ctx badge percentage denominator). */
  context_window?: number;
  /** Cumulative input tokens across turns (history-tab ctx badge). */
  total_input_tokens?: number;
}

export type ToolCallStatus = "running" | "done" | "error";

export interface CodingToolCall {
  /** Pairing key — `tool_use_id` (CC) / `call_id` (OC) / `id` fallback. */
  id: string;
  tool: string;
  args: Record<string, unknown>;
  status: ToolCallStatus;
  /** Result text/content when the matching tool_result frame arrives. */
  output?: string;
  isError?: boolean;
  /** Human-readable one-liner (V1 formatToolUse). */
  description?: string;
}

/** Usage rollup carried by Task sub-task frames (V1 parity). */
export interface CodingSubTaskUsage {
  total_tokens?: number;
  tool_uses?: number;
  duration_ms?: number;
}

/**
 * A Task/Agent sub-task (V1 parity: useClaudeCode.js sub-task accumulation).
 * Built from `task_started` (status="running") then refined by
 * `task_progress` (usage / last_tool_name) and finalised by
 * `task_notification` (status / summary / usage).
 */
export interface CodingSubTask {
  task_id: string;
  description: string;
  status: "running" | "completed" | "failed" | "stopped";
  summary: string;
  usage: CodingSubTaskUsage | null;
  last_tool_name?: string | null;
}

export interface CodingMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  isStreaming?: boolean;
  toolCalls?: CodingToolCall[];
  /**
   * Task/Agent sub-tasks (V1 parity: useClaudeCode.js:862-911 +
   * index.html:588-628). Built from `task_started` / `task_progress` /
   * `task_notification` stream frames. The native Anthropic HTTP SSE wire
   * does not emit these today; they appear when an upstream / harness
   * injects Task Agent events. Rendered as the "🤖 sub-tasks" cards.
   */
  subTasks?: CodingSubTask[];
  isWarning?: boolean;
  timestamp?: number;
  /**
   * Stable backend message id (V2 `POST .../messages` returns
   * `user_msg_id`). Stored on the user-row so the rewind button can
   * reference it when locating the corresponding checkpoint.
   */
  userMsgId?: string;
  /**
   * Checkpoint id created server-side when the user message was sent
   * (V2 `POST .../checkpoint` → `{checkpoint.checkpoint_id}`). Required
   * by `POST .../rewind {checkpoint_id}` (V2 `RewindCheckpointRequest`).
   * V1 used `sdkUuid` from the SDK message envelope; V2 uses an
   * explicit checkpoint primitive (PR-105). Absent until the
   * checkpoint create succeeds.
   */
  checkpointId?: string;
  /**
   * Per-turn token-badge data (V1 index.html:728-732 — assistant `📊
   * input↑ output↓ duration` row). Populated on `onDone` from the
   * V2 context_size REST (CC currently exposes only the
   * session-level totals; per-turn deltas are best-effort).
   */
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
    totalTokens?: number;
  };
  /** Wall-clock seconds elapsed for the assistant turn (V1 `duration_s`). */
  durationS?: number;
}

export interface PermissionRequest {
  request_id: string;
  tool: string;
  args: Record<string, unknown>;
  sessionId: string;
}

/** A queued message awaiting its turn while a session is streaming (V1 ccQueue). */
export interface QueuedMessage {
  id: string;
  sessionId: string;
  text: string;
}

/** Per-session progress indicator state (V1 sessionProgress). */
export interface SessionProgress {
  stage: string;
  detail?: string;
  toolName?: string | null;
  startTime: number;
}

/** A history session entry (V1 historySessions, GET /sessions/history/all). */
export type HistorySession = CodingSession & {
  in_memory?: boolean;
};

export interface SessionListResponse {
  sessions: CodingSession[];
}

export interface SendMessageResponse {
  message_id: string;
  user_msg_id: string;
  stream_url: string;
}

export interface ConfigResponse {
  config: { model?: string; [key: string]: unknown };
}

export interface ContextUsageResponse {
  ok: boolean;
  totalTokens: number;
  maxTokens: number;
  percentage: number;
}

/**
 * Body of `GET /api/oc/sessions/{id}/context_size` (V2 OC route only).
 * Carries the richer per-turn counters the V1 token badge expects
 * (last_input_tokens / total_input_tokens / total_output_tokens /
 * context_limit). The CC route (`/context_usage`) does not surface
 * these; CC sessions therefore only get the session-level
 * percentage / max from `ContextUsageResponse`. Token-badge values
 * carry the aggregate's REAL cumulative usage (U-010 / 2-H2); a value
 * of 0 is the initial state (no round streamed yet), not a stub.
 */
export interface ContextSizeResponse {
  last_input_tokens: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tool_calls: number;
  turn_count: number;
  context_limit: number;
  usage_pct: number;
  model: string;
}

export interface CheckpointInfoEnvelope {
  checkpoint_id: string;
  created_at: string;
  label?: string | null;
  message_count: number;
}

export interface CreateCheckpointResponse {
  ok: boolean;
  session_id: string;
  checkpoint: CheckpointInfoEnvelope;
}
