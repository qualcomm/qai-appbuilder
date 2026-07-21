// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Streaming contract types — hand-written.
 *
 * The chat WebSocket route `/api/chat/ws` and the QAI-native SSE
 * envelope are NOT included in `api-snapshot.json` (FastAPI does not
 * emit WebSocket schemas; SSE bodies are `text/event-stream` blobs).
 * The shapes below are locked in `docs/90-refactor/api-contract.md`
 * §3 (SSE) and §4 (WebSocket).
 *
 * S5 PR-050: type definitions only — wire-level parsing lives in
 * PR-051 (`apiSSE` + `apiStream`) and PR-054 (chat WS client).
 */

// ---------------------------------------------------------------------------
// SSE — QAI-native envelope (api-contract.md §3.1)
// ---------------------------------------------------------------------------

/** StreamFrame `frame_type` discriminator (chat).
 *
 *  Mirrors the backend `qai.chat.domain.stream_frame.StreamFrameType`
 *  enum 1:1 (chunk / tool_call / tool_result / tool_mode_changed /
 *  error / end). `final` was a frontend-only legacy alias that never
 *  matched any wire value — removed so the discriminant is honest about
 *  the frames the server actually emits (api-contract §3 / §4).
 *
 *  `turn_warning` is V1 parity (useChat.js:1422-1432) — emitted by the
 *  backend agentic loop when the conversation reaches the configured
 *  per-turn limit, so the UI can append a system-styled notice in the
 *  message list. The V2 backend does not emit this frame yet (the
 *  per-turn cap is not surfaced in the stream); the type entry keeps
 *  the frontend ready for the additive backend wiring without breaking
 *  the discriminated union when no such frame is present.
 *
 *  `subagent_*` + `agent_summary` are V1 parity (chat_handler.py:2204-
 *  2343 + 597-694; useChat.js:1345-1408) — emitted when the LLM
 *  dispatches the `agent` tool; let the UI render an in-progress
 *  sub-agent block per parallel sub-agent and a summary separator
 *  before the parent agent's follow-up text.  The chat store
 *  (`stores/chatTabs.ts`) accumulates these into a per-message
 *  `subAgentBlocks` array consumed by ChatMessageList +
 *  SubAgentBlock.vue.
 *

 *  `speaker_changed` is the Multi-Agent discussion (block-5) anchor frame
 *  (design §7 — NEW frame type, §3.1 allows new frame kinds; older frontends
 *  ignore it). Emitted by `OrchestrateDiscussionUseCase` whenever the next
 *  named participant takes the floor, so the UI can commit the prior speaker's
 *  bubble and open a fresh one attributed to the new speaker (soft reset,
 *  parallel to `agent_summary`). Carries `{ sender_id, display_name, model_id? }`.
 *
 *  `context_usage` is the main-agent turn-internal live context refresh (V2
 *  enhancement; backend appended frame type, §3.1). Emitted at each agentic
 *  ROUND boundary inside ONE turn with the round-just-completed's
 *  provider-MEASURED wire size (`{ used_tokens, context_limit }` —
 *  State-Truth-First, not an estimate) so the main-conversation context badge
 *  tracks the real wire growth (e.g. 33K → 70K) WHILE a long multi-round turn
 *  runs, instead of staying frozen until the turn-boundary `GET /context`
 *  re-fetch. The mirror of the sub-agent per-round `used_tokens` refresh; the
 *  turn-boundary `/context` remains the authoritative override.
 */
export type StreamFrameType =
  | "chunk"
  | "tool_call"
  | "tool_result"
  | "tool_mode_changed"
  | "turn_warning"
  | "error"
  | "end"
  | "subagent_start"
  | "subagent_output"
  | "subagent_tool"
  | "subagent_tool_result"
  | "subagent_done"
  | "subagent_error"
  | "agent_summary"
  | "speaker_changed"
  | "plan_ready"
  | "implementation_item_started"
  | "implementation_item_finished"
  | "implementation_phase_changed"
  | "reasoning"
  | "injected_message"
  | "compaction_progress"
  | "context_usage"
  | "network_retry";

/** Canonical chat StreamFrame projection — wire body of `event: message`. */
export interface ChatStreamFrame<P = unknown> {
  readonly frame_id: string;
  readonly frame_type: StreamFrameType;
  readonly sequence: number;
  readonly payload: P;
}

/** Payload of a `chunk` frame — incremental assistant text. */
export interface ChunkFramePayload {
  readonly text: string;
  /** 0-based agentic-loop round (= the LLM call that produced this text).
   *  Backend StreamFrame appended field (AGENTS.md 3.1); lets the
   *  frontend group each round's text with its tool cards with ZERO
   *  inference. Optional: absent on older frames / non-agentic turns. */
  readonly round_index?: number;
  /** Multi-Agent discussion (block-5) — id of the named participant that
   *  produced this text. Backend appended field (§3.1); present ONLY in a
   *  discussion turn. Absent ⇒ ordinary single-agent text (current behaviour). */
  readonly sender_id?: string;
}

/** Payload of a `reasoning` frame — incremental "thinking" text.
 *  Mirrors `ChunkFramePayload` exactly; the only difference is the frame
 *  type, so the UI routes thinking into a collapsible block separate from
 *  the answer. Two producers: cloud reasoning models' `delta.reasoning_content`
 *  (previously discarded by the adapter) and the internal query-service
 *  adapter's noise-filtered thinking. */
export interface ReasoningFramePayload {
  readonly text: string;
  /** 0-based agentic-loop round (same semantics as ChunkFramePayload). */
  readonly round_index?: number;
  /** Multi-Agent discussion — id of the participant speaking this thinking. */
  readonly sender_id?: string;
}

/** Payload of an `injected_message` frame (backend `StreamFrame.injected_message`).
 *  Emitted when the user's "inject" button content is folded into the SAME
 *  in-flight run at the inter-round seam: the backend has already appended the
 *  text to the wire as a `role:user` message + persisted it. The frontend uses
 *  this to commit its pending grey injection bubble into a real user message
 *  (pairing by `message_id`) and drop the local pending/queue fallback. */
export interface InjectedMessageFramePayload {
  readonly text: string;
  /** Persisted MessageId of the injected user message — backend appended field
   *  (§3.1). Lets the client pair its optimistic local bubble to the real one.
   *  Optional: absent if persistence was skipped. */
  readonly message_id?: string;
  /** 0-based agentic-loop round the injection landed before (same semantics as
   *  ChunkFramePayload). Backend appended field (§3.1). Optional. */
  readonly round_index?: number;
}

/** Payload of a `context_usage` frame (backend `StreamFrame.context_usage`).
 *  Emitted at each agentic round boundary inside ONE turn so the
 *  main-conversation context badge can refresh per round WHILE a long
 *  multi-round tool turn runs. Both fields are provider-MEASURED truth
 *  (State-Truth-First): `used_tokens` is the wire prompt size the model saw the
 *  round just completed, `context_limit` is the model's window. The
 *  turn-boundary `GET /context` re-fetch remains the authoritative override. */
export interface ContextUsageFramePayload {
  /** Provider-measured wire prompt size of the round just completed (>= 0). */
  readonly used_tokens: number;
  /** The model's context window (> 0). */
  readonly context_limit: number;
}

/** Payload of a `tool_call` frame (backend `StreamFrame.tool_call`). */
export interface ToolCallFramePayload {
  readonly tool_name: string;
  readonly arguments: Record<string, unknown>;
  /** 0-based agentic-loop round whose LLM call issued this tool call.
   *  Backend appended field (3.1); the authoritative round-grouping key
   *  (replaces the old "lead_in non-empty => new round" heuristic).
   *  Optional: absent on older frames. */
  readonly round_index?: number;
  /** Multi-Agent discussion (block-5) — id of the named participant that
   *  issued this tool call. Backend appended field (§3.1). Absent ⇒ single-agent. */
  readonly sender_id?: string;
}

/** Payload of a `tool_result` frame (backend `StreamFrame.tool_result`). */
export interface ToolResultFramePayload {
  readonly tool_name: string;
  readonly result: unknown;
  /** Original (pre-truncation) output length in characters — appended
   *  field (backend §3.1, `ToolResultTruncationResult.original_length`).
   *  Optional: omitted on older frames / when no truncator is wired. */
  readonly size?: number;
  /** Whether the adaptive truncator shortened the output — appended
   *  field (backend §3.1). Optional for the same reason. */
  readonly truncated?: boolean;
  /** 0-based agentic-loop round of the tool call this result answers
   *  (result shares its call's round). Backend appended field (3.1).
   *  Optional: absent on older frames. */
  readonly round_index?: number;
  /** Tool wall-clock execution time in ms (backend appended field §3.1,
   *  on the final frame). Drives the run-time badge in live + history. */
  readonly duration_ms?: number;
  /** Multi-Agent discussion (block-5) — id of the named participant whose tool
   *  call this result answers. Backend appended field (§3.1). Absent ⇒ single. */
  readonly sender_id?: string;
}

/** OpenAI-style token usage carried on the terminal `end` frame
 *  (backend `_extract_usage` — llm_stream.py:911). All optional since
 *  local / mock providers may omit some keys. */
export interface ChatUsage {
  readonly prompt_tokens?: number;
  readonly completion_tokens?: number;
  readonly total_tokens?: number;
  readonly elapsed_seconds?: number;
  readonly is_mock?: boolean;
}

/** Payload of the terminal `end` frame (backend `StreamFrame.end`). */
export interface EndFramePayload {
  readonly reason: string;
  readonly usage?: ChatUsage;
  /** Multi-Agent discussion (block-5) — id of the participant whose speaking
   *  turn just ended. Backend appended field (§3.1). Absent ⇒ single-agent. */
  readonly sender_id?: string;
}

/** Payload of a `tool_mode_changed` notification frame. */
export interface ToolModeChangedFramePayload {
  readonly mode: string;
  readonly previous_mode?: string;
}

/** Payload of a `turn_warning` notification frame (V1 useChat.js:1422-1432).
 *  Emitted when the server-side agentic loop reaches the configured
 *  per-turn cap; the UI surfaces it as an inline system-styled notice
 *  in the message list. Either `message` (pre-rendered server text) or
 *  `turn_count` (the cap value, used by the client to compose a
 *  localized notice via `chat.turnLimitWarn`) may be present. */
export interface TurnWarningFramePayload {
  readonly message?: string;
  readonly turn_count?: number;
}

// ---------------------------------------------------------------------------
// Sub-agent event family (V1 chat_handler.py:2204-2343 + 597-694 parity)
// ---------------------------------------------------------------------------
// Field names mirror the V1 wire shape verbatim
// (``content`` / ``tool_args`` / ``result`` / ``message``) so the
// store consumer reads the same keys the V1 frontend already used in
// useChat.js:1345-1408 — no per-field shims required when comparing
// V1 / V2 behavior.

/** Payload of a `subagent_start` frame (V1 chat_handler.py:598-605).
 *  Emitted once per sub-agent before any output frames so the UI can
 *  pre-allocate a block entry with the truncated prompt preview. */
export interface SubAgentStartFramePayload {
  /** 0-based index discriminating parallel sub-agents in this turn. */
  readonly index: number;
  /** Total sub-agents this parent turn dispatched (≥ 1). */
  readonly total: number;
  /** Truncated prompt the sub-agent received (≤ 500 chars in V1). */
  readonly prompt_preview: string;
  /** V2 §3.1 tail-appended: resumable / openable session id. Absent when
   *  persistence is unwired. */
  readonly subagent_id?: string;
  /** V2 §3.1 tail-appended: resolved profile name (`general` / `explore`),
   *  driving the i18n type-badge next to the card title. Absent on legacy
   *  frames / when the caller did not resolve a profile. */
  readonly subagent_type?: string;
  /** V2 §3.1 tail-appended: human-readable task label the LLM supplied when
   *  spawning (persisted as `SubAgentSession.title`). Drives the card
   *  title — falls back to the generic `SubAgent N` label when absent. */
  readonly name?: string;
  /** V2 §3.1 tail-appended (UX FIX): parent agent's round number at which
   *  this sub-agent was dispatched. Drives per-round SUBAGENT_START routing
   *  in `handleSubagentStart` — without it two sub-agents spawned in
   *  different rounds of the same parent turn collapse onto the same
   *  message and the second's `index=0` de-dup drops the first. Absent on
   *  legacy frames (older backends / test paths) — the handler then falls
   *  back to `activeSubAgentMessageId` reuse (the historical behaviour). */
  readonly round_index?: number;
}

/** Payload of a `subagent_output` frame — incremental text fragment.
 *  Field is `content` (V1 wire), NOT `text`. */
export interface SubAgentOutputFramePayload {
  readonly index: number;
  readonly content: string;
}

/** Payload of a `subagent_tool` frame — sub-agent dispatched a tool.
 *  Field is `tool_args` (V1 wire), NOT `arguments`. */
export interface SubAgentToolFramePayload {
  readonly index: number;
  readonly tool_name: string;
  readonly tool_args: Record<string, unknown>;
  /** Call id (V2 appended, §3.1) pairing this row to its
   *  `subagent_tool_result` frame. Optional: absent for legacy callers. */
  readonly tool_call_id?: string;
}

/** Payload of a `subagent_tool_result` frame — a sub-agent tool's output
 *  (V2 enhancement). The sub-agent counterpart of `tool_result`: lets the UI
 *  render a structured, collapsible result panel under the matching
 *  `subagent_tool` row (parity with a main-agent tool card) instead of the
 *  model re-narrating the raw output as plain text. */
export interface SubAgentToolResultFramePayload {
  readonly index: number;
  readonly tool_name: string;
  readonly result: unknown;
  /** `false` when the tool failed (`[tool_error]` / `[guardrail_blocked]`
   *  sentinel) so the card renders in its error state. */
  readonly ok: boolean;
  /** Pairs the result to its `subagent_tool` row by id when present; absent ⇒
   *  pair by tool name + order. Appended (§3.1). */
  readonly tool_call_id?: string;
  /** Original (pre-truncation) output length in characters — drives the size
   *  badge. Appended (§3.1); optional. */
  readonly size?: number;
  /** Whether the adaptive truncator shortened the output — drives the
   *  "已截断" badge. Appended (§3.1); optional. */
  readonly truncated?: boolean;
  /** Per-tool execution wall-clock in ms — drives the execution-time badge
   *  (parity with a main-agent tool card). Appended (§3.1); optional. */
  readonly duration_ms?: number;
}

/** Payload of a `subagent_done` frame — sub-agent finished.
 *  Field is `result` (V1 wire), NOT `text`. */
export interface SubAgentDoneFramePayload {
  readonly index: number;
  readonly result: string;
  /** Number of agentic rounds the sub-agent used (≥ 0). */
  readonly rounds: number;
}

/** Payload of a `subagent_error` frame — sub-agent failed.
 *  Field is `message` (V1 wire), NOT `error`. */
export interface SubAgentErrorFramePayload {
  readonly index: number;
  readonly message: string;
}

/** Payload of an `agent_summary` frame — emitted once per parent turn
 *  after all sub-agents finish, before the parent's follow-up text
 *  (V1 chat_handler.py:694).  Lets the UI insert a "main agent
 *  summary" separator and reset its in-flight content buffer. */
export interface AgentSummaryFramePayload {
  readonly total_agents: number;
}

/** Payload of a `speaker_changed` frame — Multi-Agent discussion (block-5,
 *  design §7). Emitted when the next named participant takes the floor so the
 *  UI can commit the prior speaker's bubble and open a new one attributed to
 *  this participant (soft reset). */
export interface SpeakerChangedFramePayload {
  /** Id of the participant now speaking (matches a `chat_participant` id). */
  readonly sender_id: string;
  /** Human-facing display name to show on the new speaker's bubble. */
  readonly display_name: string;
  /** Model id the participant speaks with (optional — informational). */
  readonly model_id?: string;
}

// ---------------------------------------------------------------------------
// DISC-1 implementation orchestration (§22.9 additive control-plane frames)
// ---------------------------------------------------------------------------
// Emitted ONLY by the backend planned-implementation serial runner
// (`OrchestrateDiscussionUseCase._run_planned_implementation` /
// `_run_implementation_control`), reached ONLY behind the OFF-by-default
// `implementation_enabled` flag + a persisted `planned`/`paused` plan. They give
// the UI a STRUCTURED progress feed for the run (item list / which item is
// working / item done-failed-skipped / run-phase transitions) WITHOUT inferring
// it from the chunk/tool stream. Payloads carry ONLY a SHORT control-plane
// summary (never the full tool output / diff — that stays in the message
// system). All fields tail-appended (§3.1); absent in ordinary chat / discussion.

/** One item's SHORT control-plane summary carried in a `plan_ready` frame.
 *  NOT the full backend `FeatureItem` (no `description` / `acceptance_criteria`
 *  / large fields) — just what a progress row needs. */
export interface ImplementationItemSummary {
  readonly id: string;
  readonly title: string;
  readonly status: string;
  readonly assigned_role?: string | null;
  readonly suggested_role?: string | null;
}

/** Payload of a `plan_ready` frame — the run is starting; the UI can render the
 *  full item list up-front. */
export interface PlanReadyFramePayload {
  readonly run_id: string;
  readonly items: readonly ImplementationItemSummary[];
  /** Participant id implementing the run (optional appended, §3.1). */
  readonly sender_id?: string;
}

/** Payload of an `implementation_item_started` frame — item N began. */
export interface ImplementationItemStartedFramePayload {
  readonly run_id: string;
  readonly item_id: string;
  readonly title: string;
  /** Role assigned to implement this item (optional appended, §3.1). */
  readonly assigned_role?: string;
  readonly sender_id?: string;
}

/** Payload of an `implementation_item_finished` frame — item N settled
 *  (`done` / `failed` / `skipped`). */
export interface ImplementationItemFinishedFramePayload {
  readonly run_id: string;
  readonly item_id: string;
  readonly status: string;
  /** SHORT control-plane outcome summary (optional appended, §3.1). */
  readonly result_summary?: string;
  /** SHORT control-plane error summary (optional appended, §3.1). */
  readonly last_error?: string;
  readonly sender_id?: string;
}

/** Payload of an `implementation_phase_changed` frame — run-phase transition
 *  (`implementing` / `completed` / `failed` / `paused`). */
export interface ImplementationPhaseChangedFramePayload {
  readonly run_id: string;
  readonly phase: string;
  /** Id of the item in flight (optional appended, §3.1; `null`/absent ⇒ idle). */
  readonly current_item?: string | null;
  readonly sender_id?: string;
}

/** QAI-native SSE event names. */
export type QaiSseEventName = "message" | "progress" | "error" | "done";

/** Parsed SSE event after envelope decoding. */
export interface QaiSseEvent<T = unknown> {
  readonly event: QaiSseEventName;
  readonly data: T;
}

// ---------------------------------------------------------------------------
// WebSocket — chat (api-contract.md §4)
// ---------------------------------------------------------------------------

/** Server → client message types. */
export type ChatWsServerType = "ready" | "frame" | "error" | "done";

/** Client → server message types. */
export type ChatWsClientType = "send" | "stop";

/** Server → client `ready` handshake (first frame after upgrade). */
export interface ChatWsReady {
  readonly type: "ready";
  readonly session_id: string;
}

/** Server → client `frame` envelope wrapping a StreamFrame. */
export interface ChatWsFrame {
  readonly type: "frame";
  readonly frame: ChatStreamFrame;
}

/** Server → client terminal `error` envelope (closes WS after). */
export interface ChatWsError {
  readonly type: "error";
  readonly error: ApiErrorPayload;
}

/** Server → client terminal `done` envelope (closes WS after). */
export interface ChatWsDone {
  readonly type: "done";
}

/**
 * Server → client keep-alive `ping` envelope (NON-terminal). Emitted by the
 * chat WS route every ~15 s of idle silence (mirrors the SSE `: ping`) so a
 * long SILENT tool does not let an intermediary idle-timeout drop the socket.
 * Carries no turn data — the client treats it purely as a liveness signal and
 * ignores it (no state change, no `onFrame`/`onDone`/`onError`).
 */
export interface ChatWsPing {
  readonly type: "ping";
}

export type ChatWsServerMessage =
  | ChatWsReady
  | ChatWsFrame
  | ChatWsError
  | ChatWsDone
  | ChatWsPing;

/** Client → server `send` envelope — start a turn over this tab. */
export interface ChatWsSend {
  readonly type: "send";
  readonly prompt: string;
  readonly conversation_id?: string;
  /** Optional toolbar mode (code / translate / ppt / …) — additive;
   *  the server forwards it to the system-prompt builder. */
  readonly tool_mode?: string;
  /** Optional feature-mode params (code: {speed,persona?} /
   *  translate: {target_lang} / ppt: {length}). Additive. */
  readonly tool_params?: Record<string, unknown>;
  /** Optional selected model id (V1 `selectedModelId` parity) — additive;
   *  the server forwards it to the provider-routing LLM stream as the
   *  `model_hint` so the turn routes to the owning provider. */
  readonly model_id?: string;
  /** SSE-parity advanced fields (additive — `_ws.py` / `_sse.py` accept the
   *  same names). Omitted for plain turns so the envelope shape is
   *  unchanged. */
  /** Sub-agent take-over: continue this sub-agent's persisted context. */
  readonly subagent_id?: string;
  /** Take-over only: advertise the blocking `question` tool. */
  readonly allow_question?: boolean;
  /** Main-agent turn only: grant the `agent` (spawn) tool to the first-level
   *  sub-agents this turn spawns, so they may create their own (grand)
   *  sub-agents. Default off keeps the hard recursion guard. */
  readonly allow_child_spawn?: boolean;
  /** Sub-agent take-over only: advertise the `agent` (spawn) tool to the
   *  taken-over sub-agent so it may create its own sub-agents. Independent of
   *  `allow_child_spawn`. Default off keeps the autonomous parity. */
  readonly self_allow_spawn?: boolean;
  /** Sampling overrides (merge into tool_params server-side). */
  readonly temperature?: number;
  readonly top_p?: number;
  readonly max_tokens?: number;
  /** Per-session ("this conversation only") tool / SKILL override (additive —
   *  `_ws.py` / `_sse.py` accept the same names). Arrays of the tool names /
   *  skill ids the user switched OFF for this session; omitted when empty so
   *  the envelope shape is unchanged for sessions without an override. The
   *  backend applies them per-turn (never mutates global config). */
  readonly disabled_tools?: string[];
  readonly disabled_skills?: string[];
  /** UI language (en / zh-CN / zh-TW) so the backend localizes its
   *  feature-mode system-prompt framing to the user's selected language
   *  (additive — `_ws.py` / `_sse.py` read the same `locale` field). */
  readonly locale?: string;
}

/** Client → server `stop` envelope — cancel the in-flight turn. */
export interface ChatWsStop {
  readonly type: "stop";
}

export type ChatWsClientMessage = ChatWsSend | ChatWsStop;

// ---------------------------------------------------------------------------
// Unified error envelope (api-contract.md §2.1)
// ---------------------------------------------------------------------------

/**
 * The single JSON shape every error response uses, regardless of
 * source exception. Mapped from `QaiError.to_dict()` server-side; see
 * api-contract.md §2.1 / §2.2 for status code mapping.
 */
export interface ApiErrorPayload {
  readonly type: string;
  readonly code: string;
  readonly message: string;
  readonly details?: Record<string, unknown> | undefined;
}
