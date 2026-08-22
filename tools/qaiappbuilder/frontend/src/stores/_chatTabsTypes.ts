// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Types and constants for the multi-tab chat store. Extracted from
 * `chatTabs.ts` to keep that store within the cohesion budget. The
 * runtime store re-exports everything here via `export * from
 * "./_chatTabsTypes"` so existing
 * `import { ... } from "@/stores/chatTabs"` paths keep working.
 *
 * No reactive state, no module-level mutables — only `export type`,
 * `export interface`, and pure constants.
 */
// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type TabId = string;
export type ConversationId = string;

export type ChatTabStatus = "idle" | "streaming" | "aborting" | "error";

export type ChatMessageRole =
  | "user"
  | "assistant"
  | "system"
  | "tool"
  | "tool_indicator"
  | "system_notice";

/** Tool mode pill state — drives the toolbar mode buttons + outgoing
 *  `tool_mode` SSE query param so the backend system-prompt builder
 *  picks the right feature prompt (refactor-plan §10.7).
 */
export type ToolModeKey =
  | "model-build"
  | "model-hub"
  | "app-builder"
  | "code"
  | "translate"
  | "ppt"
  | "pro"
  | "gomaster";

/** Per-mode tool parameters aggregated by ChatComposer and forwarded
 *  to the backend as `tool_params` (SSE query / WS envelope). Mirrors
 *  V1's `_toolParamsComputed`. All fields optional; the transport only
 *  emits the keys relevant to the active mode.
 *
 *  Contract shapes (block-5 spec §1.4 / §2.3 / §3.3 + block-3 spec §2.1):
 *    code:        { speed, persona? }
 *    translate:   { target_lang }
 *    ppt:         { length }
 *    model-build: { model_path, model_paths, quant_precision, dataset_path }
 */
export interface ToolParams {
  /** code mode: fast | think | expert */
  speed?: "fast" | "think" | "expert";
  /** code mode: selected persona id (cloud models only — omitted for local) */
  persona?: string;
  /** code mode: server-side absolute path of an uploaded code file
   *  (V1 parity `codeUploadedPath` → tool_params.file_path, app.js:1814).
   *  Set by ModeFrameCoding after POST /api/upload/code succeeds. */
  file_path?: string;
  /** code mode: confirmed open-source repo URL to pull into context
   *  (V1 parity `codeRepoConfirmed` → tool_params.repo_url, app.js:1815). */
  repo_url?: string;
  /** translate mode: en | zh-CN | zh-TW */
  target_lang?: "en" | "zh-CN" | "zh-TW";
  /** ppt mode: smart | short | medium | long */
  length?: "smart" | "short" | "medium" | "long";
  /** model-build mode: server-side absolute path of the active model
   *  (block-3 spec §2.1; mirrors V1 `modelBuildUploadedPath`). */
  model_path?: string;
  /** model-build mode: server-side absolute paths of all uploaded models
   *  (mirrors V1 `allModelPaths`). */
  model_paths?: string[];
  /** model-build mode: selected quantization precision value (fp32 ..
   *  w4a8; default fp16). */
  quant_precision?: QuantPrecision;
  /** model-build mode: server-side absolute path of the dataset dir
   *  (mirrors V1 `modelBuildDatasetPath`). */
  dataset_path?: string;
  /** app-builder mode: id of the model selected in the workbench
   *  (mirrors V1 `toolParamsForChat.selected_model_id`). */
  selected_model_id?: string;
  /** app-builder mode: multi-select of imported model ids chosen in the
   *  chat-input strip (additive). Backend injects these Packs into the
   *  system prompt; `selected_model_id` stays set for backward compat. */
  selected_model_ids?: string[];
  /** app-builder mode: display name of the selected model. */
  selected_model_name?: string;
  /** app-builder mode: taxonomy category of the selected model. */
  category?: string;
  /** app-builder mode: selected precision/variant id. */
  variant_id?: string;
  /** app-builder mode: compact summary of the last run's output. */
  last_run_summary?: string;
  /** pro (Model Builder Pro / 增强) mode: remote GPU Agent base URL the user
   *  connected to. Surfaced on the outgoing tool_params for observability;
   *  the actual session is owned server-side (the turn routes via the
   *  ``query::mb_pro`` model hint). */
  agent_url?: string;
  /** pro mode: the remote session id (optional — auto-created when empty). */
  session?: string;
  /** pro mode: skip TLS certificate verification (self-signed intranet cert). */
  insecure?: boolean;
}

/** Quantization precision values surfaced in the Model Builder mode
 *  picker (block-3 spec §1.4). Ordered high → low precision. Int
 *  quantization (anything other than fp16/fp32) requires a dataset. */
export type QuantPrecision =
  | "fp32"
  | "fp16"
  | "w8a16"
  | "w8a8"
  | "w8a8b8"
  | "w4a16"
  | "w4a8";

/** Precision values that DO require a calibration dataset (Int quant).
 *  `needsDataset = precision ∉ {fp16, fp32}` (block-3 spec §1.4). */
export const QUANT_PRECISIONS_NEEDING_DATASET: ReadonlySet<QuantPrecision> =
  new Set<QuantPrecision>(["w8a16", "w8a8", "w8a8b8", "w4a16", "w4a8"]);

/** Sampling parameters surfaced via the Params popover. `useDefaults`
 *  short-circuits the rest so unset sliders don't override the
 *  server-side defaults configured per-model.
 */
export interface ModelParams {
  useDefaults: boolean;
  temperature: number;
  topP: number;
  maxTokens: number;
}

export const DEFAULT_MODEL_PARAMS: ModelParams = {
  useDefaults: true,
  temperature: 0.7,
  topP: 1.0,
  // V1 default (app.js:1201-1203): max_tokens = 0 means "no limit"; the
  // SSE route only forwards it when > 0 (_sse.py:413).
  maxTokens: 0,
};

/** Factory default tool params — mirrors V1 mode-control defaults
 *  (speed=fast, target_lang=zh-CN, length=smart, quant_precision=fp16). */
export const DEFAULT_TOOL_PARAMS: ToolParams = {
  speed: "fast",
  target_lang: "zh-CN",
  length: "smart",
  quant_precision: "fp16",
};

/** History-page size for `loadHistoryMessages` / `loadMoreMessages`
 *  (V1 useChat.js:796,873 — `const PAGE_SIZE = 40`). */
export const HISTORY_PAGE_SIZE = 40;

/** Maximum number of messages that may be queued while a turn is
 *  streaming (V1 useChat.js:241 — `const MAX_QUEUE_SIZE = 10`). When the
 *  queue is full a further Enter is rejected (a toast surfaces "queue
 *  full" in the composer). */
export const MAX_QUEUE_SIZE = 10;

/** Soft cap on the number of chat tabs (sessions) open at once. A "+" /
 *  "new conversation" affordance is disabled once this many tabs are open,
 *  preventing an unbounded fan-out of live transports / WebSockets. Opening
 *  an EXISTING conversation (restore from history — `conversationId` given)
 *  is never blocked by this cap; only brand-new blank tabs are. */
export const MAX_OPEN_TABS = 30;

/** One pending message waiting in a tab's send queue (V1
 *  useChat.js:2827 — `{ id, text }`). `id` is a stable client-side key
 *  for the v-for / removal; `text` is the trimmed prompt that will be
 *  re-sent verbatim when the queue is processed.
 *
 *  `imagePrefix` carries any attached images as the SAME markdown
 *  `![name](url)` prefix a normal submit prepends to the prompt
 *  (`useComposerSubmit.onSubmit` → `uploadPendingImages`). Images are
 *  uploaded AT enqueue time (so the upload is not lost when the composer is
 *  reused) and the resulting prefix is stored here; the re-send recombines
 *  `imagePrefix + text` so the dequeued turn goes through the exact same
 *  `transport.send` → WS/SSE `_extract_image_refs` → vision-block resolution
 *  as a fresh image submit. Empty string when no image was attached.
 *
 *  Note: the queue holds ONLY Enter-while-streaming messages (sent as a fresh
 *  turn after the current one ends). Mid-turn injections (the "inject" button)
 *  are control-plane-only (user decision 2026-06-24) — they go straight into
 *  the conversation as a pending bubble + the control WS, never into this
 *  queue — so there is no longer an `injected` flag here. */
export interface QueuedMessage {
  readonly id: string;
  readonly text: string;
  /** Uploaded-image markdown prefix (`![name](url)\n`), or "" when none. */
  readonly imagePrefix: string;
}

/** One tool invocation surfaced in the chat stream. Assembled from a
 *  `tool_call` frame (id / tool / args / status="running") and later
 *  completed by the matching `tool_result` frame (output / status /
 *  isError). Mirrors V1's per-message tool cards (index.html:455-494). */
export interface ChatToolCall {
  /** Frame id of the originating `tool_call` frame — pairing key. */
  readonly id: string;
  /** Upstream tool_call id (`payload.tool_call_id`) when the backend
   *  provided one. Used as the PRIMARY pairing key against the final
   *  `tool_result` frame's `tool_call_id` so parallel same-named tool calls
   *  (e.g. two `exec` in one round) bind their results correctly instead of
   *  by "most-recent running of the same name" (which mis-pairs). Falls back
   *  to tool-name matching when absent (local XML protocol). */
  readonly callId?: string;
  /** Tool name (`payload.tool_name`). */
  readonly tool: string;
  /** Invocation arguments (`payload.arguments`). */
  readonly args: Record<string, unknown>;
  /** Stringified tool output (filled by the matching `tool_result`). */
  output?: string;
  /** Lifecycle: running until the result arrives, then one of
   *  done / error / backgrounded / stopped / restarted.
   *
   *  ``"backgrounded"`` — the underlying command was moved to the
   *  background-process manager and is STILL RUNNING.
   *  ``"stopped"`` — a background_process(action=stop) call terminated the
   *  target job. Not the same as ``"done"``: nothing failed, but the outcome
   *  is termination, not completion.
   *  ``"restarted"`` — a background_process(action=restart) call replaced the
   *  target job. Same rationale: distinct terminal state.
   *
   *  Fed by the STRUCTURED ``backgrounded`` / ``terminal_kind`` fields on
   *  the ``tool_result`` frame (never by parsing ``output`` prose). */
  status: "running" | "done" | "error" | "backgrounded" | "stopped" | "restarted";
  /** True when the result looked like an error sentinel
   *  (`[tool_error] …` / `[guardrail_blocked] …`). */
  isError?: boolean;
  /** True when this result was synthesized because the user cancelled THIS one
   *  tool (per-call stop). Set from the structured ``cancelled`` field on
   *  the ``tool_result`` frame — the card settles (stops spinning) and shows
   *  the backend's "[已取消]/[cancelled]" text; distinct from a real error. */
  cancelled?: boolean;
  /** Auto-background terminal marker. `true` means the command was moved to
   *  the background-process manager and is still running; `ok` stays true
   *  because nothing failed. Backend structured field (§3.1); replaces the
   *  earlier `[Auto-parked]` prefix sniff. */
  backgrounded?: boolean;
  /** Job id this call was adopted into (`bgp_01KZ…`). Present iff
   *  `backgrounded === true`. Names the job the model now owns. */
  backgroundProcessId?: string;
  /** Original (pre-truncation) output size in characters, from the
   *  `tool_result` frame's appended `size` field (backend
   *  `ToolResultTruncationResult.original_length`). Drives the size
   *  badge in ToolExecPanel (V1 ToolExecPanel.js:151-155). Undefined
   *  when the backend omitted it (older frame / no truncator). */
  outputSize?: number;
  /** Whether the adaptive truncator shortened the output, from the
   *  appended `truncated` field. Drives the "已截断" badge + head/tail
   *  view tabs (V1 ToolExecPanel.js:156-177). */
  truncated?: boolean;
  /** Per-tool timestamp (ms epoch) used by ToolExecPanel's history-mode
   *  header time (V1 ToolExecPanel.js:99-101 — each tool message has its
   *  own `timestamp`, useChat.js:2583). Persisted by the backend per tool
   *  call so reloaded cards show distinct times instead of all sharing the
   *  parent message's `createdAt`. Undefined for live cards / older data. */
  ts?: number;
  /** Tool wall-clock execution time in ms, from the final `tool_result`
   *  frame's appended `duration_ms`. Drives the run-time badge shown in
   *  BOTH live and history modes (V2 enhancement over V1, which only timed
   *  the live indicator and never persisted it). Undefined when absent. */
  durationMs?: number;
  /** True while the model is still STREAMING this tool call's `arguments`
   *  (V2 enhancement, V1 had no equivalent). Set by `handleToolResult` when a
   *  `tool_result` frame carries `phase === "generating_args"` (a throttled
   *  args-accumulation progress frame, NOT a real tool result): the card
   *  appears early in a "正在生成参数…" sub-state of `status === "running"`.
   *  Cleared by `handleToolCall` when the final `tool_call` frame (with the
   *  consolidated arguments) arrives and the card flips to the normal running
   *  (tool-executing) state. Undefined for ordinary tool cards. */
  argsStreaming?: boolean;
  /** Number of `arguments` characters accumulated so far while
   *  `argsStreaming` is true — driven by the `generating_args` frame's
   *  cumulative `result` length (preferred) or by summing `delta` chunks.
   *  Surfaced in the card ("正在生成参数… (N 字符)") so the user perceives the
   *  model working. Undefined for ordinary tool cards. */
  argsCharCount?: number;
  /** True when this card was seeded EARLY from a `generating_args` progress
   *  frame (V2 enhancement), i.e. the user watched the model GENERATE this
   *  tool call's arguments before it executed. Such cards report the TOTAL
   *  wall-clock of "generation + execution" (`totalMs` below) instead of the
   *  backend `durationMs` (which times only the execution and would show a
   *  misleadingly tiny value like 11ms for a 20s+ generate→write). Preserved
   *  across the `argsStreaming → executing` flip so the final-result handler
   *  knows to compute `totalMs`. Undefined for ordinary tool cards. */
  timedFromGeneration?: boolean;
  /** Epoch ms when this card was first seeded from a `generating_args` frame —
   *  the start of the "generation" phase. Set once at seed time and preserved
   *  across the flip; the final `tool_result` handler subtracts it from `now`
   *  to compute `totalMs`. Persisted so a history reload can still derive the
   *  total (though `totalMs` is the canonical persisted value). Undefined for
   *  ordinary cards. */
  generationStartedAt?: number;
  /** Authoritative generation duration in ms, from the final `tool_call`
   *  frame's appended `generation_ms` (backend measures it from the first
   *  `generating_args` frame to draining the TOOL_CALL). Preferred over the
   *  front-end `Date.now()` approximation: `totalMs = generationMs + durationMs`
   *  is computed from backend-authoritative values so it is identical live and
   *  on reload. Undefined when the backend did not emit it (short args / older
   *  frame) — the front-end `generationStartedAt` diff is the fallback. */
  generationMs?: number;
  /** Total wall-clock in ms from "model started generating this tool call's
   *  arguments" to "tool finished executing" (`generationStartedAt → final
   *  tool_result`). Computed in the store when the final result lands on a
   *  `timedFromGeneration` card, so it is stable in BOTH live and history
   *  (reload) modes (unlike the front-end elapsed timer, which has no
   *  generation data after a reload). Drives ToolExecPanel's header badge in
   *  preference to the execution-only `durationMs`. Undefined for ordinary
   *  cards. */
  totalMs?: number;
  /** Machine-readable, USER-FIXABLE failure code from a tool result envelope
   *  (`{ ok:false, error_code, message }` a tool `return`s instead of raising).
   *  Notably `chat.llm.tls_cert_untrusted` from `web_search` behind an
   *  enterprise TLS gateway. Drives the card's inline fix affordance (the same
   *  disable-TLS-and-retry action the turn-level error bubble offers), keyed on
   *  a stable code rather than a brittle message match. Undefined for ordinary
   *  successes and for raised `ToolError` sentinels. */
  errorCode?: string;
}

/** Tool call observed inside a sub-agent's agentic loop.  Captured
 *  from a `subagent_tool` frame (V1 chat_handler.py:2299-2304); the
 *  sub-agent block renders one of these per row.  Field names mirror
 *  V1 wire shape (`tool_args`, not `arguments`). */
export interface SubAgentToolCall {
  /** Tool name (`payload.tool_name`). */
  readonly name: string;
  /** Invocation arguments (`payload.tool_args` — V1 wire field). */
  readonly args: Record<string, unknown>;
  // ── Result fields (V2 enhancement; appended per AGENTS.md §3.1) ────────────
  // Filled by `handleSubagentToolResult` from a `subagent_tool_result` frame
  // (payload: { index, tool_name, result, ok, tool_call_id?, size?, truncated? })
  // OR rehydrated from a persisted block's `tools[i]` on history replay. They
  // let the sub-agent block render each tool row as a full ToolExecPanel card
  // (collapsible header + args + result + size/truncation badges) identical to
  // the main agent's tool cards, instead of a single `🔧 name(args)` line.
  /** 上游 tool_call id（配对键）— 来源：`subagent_tool` / `subagent_tool_result`
   *  帧尾部追加的 `tool_call_id`。用于把结果帧绑回正确的工具行（并发同名工具时
   *  按它精确配对）。本地 XML 协议可能缺省，则退化为按工具名配对。 */
  tool_call_id?: string;
  /** 工具执行结果文本 — 来源：`subagent_tool_result.result`（或持久化 `tools[i].result`）。
   *  填充后该行渲染 ToolExecPanel 的输出面板；未定义表示结果尚未返回（运行中）。 */
  result?: string;
  /** 执行成功标志 — 来源：`subagent_tool_result.ok`。false ⇒ 错误态（红色卡）。 */
  ok?: boolean;
  /** 结果原始（截断前）字符大小 — 来源：`subagent_tool_result.size`。驱动
   *  ToolExecPanel 的大小徽标（≥50KB 变橙）。未定义则不显示徽标。 */
  outputSize?: number;
  /** 输出是否被后端自适应截断 — 来源：`subagent_tool_result.truncated`。驱动
   *  「已截断」徽标 + 头/尾视图切换。未定义则视为未截断。 */
  truncated?: boolean;
  /** 该工具行生命周期状态 — 传给 ToolExecPanel 的 `status`。
   *  规则：有 `result` 即 `done`、`ok === false` 即 `error`、无 `result` 即
   *  `running`（结果到达前显示运行中）。由 `handleSubagentTool`（设 running）与
   *  `handleSubagentToolResult`（设 done/error）维护；可空时模板按上述规则推导。 */
  status?: "running" | "done" | "error";
  /** 该工具的真实执行毫秒数 — 来源：`subagent_tool_result` 帧尾部新增的
   *  `duration_ms`（历史重放时来源于持久化 block 的 `tools[i].duration_ms`）。
   *  归一化后映射到 ToolExecPanel 的 `durationMs`，使子 Agent 工具卡也能在实时与
   *  历史两种模式下显示真实执行时长（与主 Agent 工具卡一致）。未定义则回退到
   *  实时计时器 / 时间戳。Appended per AGENTS.md §3.1。 */
  duration_ms?: number;
  /** 该子 Agent 工具卡的"上游 wall-clock 时间戳"（ms epoch）— 来源：
   *  `subagent_tool` 帧尾部新增的 `emitted_at_ms`（历史重放时来源于持久化
   *  block 的 `tools[i].ts`，由后端 `_streaming_subagent_frames.accumulate_sub_agent_block`
   *  在 fold 时落盘）。
   *
   *  **unmount-survival 语义（与主 Agent `ChatToolCall.ts` 同）**：
   *  归一化后映射到 `ToolCallView.timestamp` → 传给 `ToolExecPanel.vue` 的
   *  `props.timestamp`，让该组件用 `Date.now() - props.timestamp` 计算 elapsed，
   *  而不是组件本地的 `performance.now()` ref。这样浏览器切标签 / 滚出可视区导致
   *  组件 unmount → remount 时 elapsed 仍按真实经过时间累计，**不会归零到 00:00**
   *  （即子 Agent / 讨论场景的工具卡也具备主 Agent 工具卡一样的 elapsed 抗
   *  remount 能力）。Appended per AGENTS.md §3.1；旧数据 / 后端未打戳时缺省，
   *  UI 回退到 remount-local 锚点（pre-fix 行为，无回归）。 */
  ts?: number;
}

/** 主 / 子 Agent 工具卡的「统一视图模型」（normalized view model）。
 *
 *  主 Agent 的工具调用类型是 `ChatToolCall`（字段 `tool` / `output` / `durationMs`
 *  / `ts` …），子 Agent 的是 `SubAgentToolCall`（字段 `name` / `result` /
 *  `duration_ms` …），两套 wire 字段名不同。为了让两处复用 **同一套** 工具卡渲染
 *  逻辑（`ToolCallList.vue` → `TaskListCard` / `ToolExecPanel`），各调用点先用一个
 *  轻量 mapper 把自己的数据归一化成本接口的数组，渲染组件只认这一个形状。
 *
 *  字段命名刻意对齐 `ToolExecPanel` 的 props（`toolName` / `result` / `durationMs`
 *  …）以便直接透传，只在 `key` 上增加一个 v-for 稳定键。归一化只是「字段改名」，
 *  不丢任何主 Agent 已有能力。 */
export interface ToolCallView {
  /** v-for 稳定键（主 Agent 用 `call.id`，子 Agent 用数组下标）。 */
  readonly key: string | number;
  /** 上游 tool_call id（`payload.tool_call_id`）→ 用于「按单个工具取消」时
   *  告诉后端取消哪一个 call（per-call cancel）。主 Agent 有；子 Agent 当前
   *  可能缺失（后端未回填 subagent tool 的 call id），缺失时前端不显示该卡的
   *  停止按钮或退化为整轮停止。 */
  readonly callId?: string;
  /** 工具名 → ToolExecPanel `toolName`（主 `tool` / 子 `name`）。
   *  `=== "todowrite"` 时渲染 TaskListCard，否则渲染 ToolExecPanel。 */
  readonly toolName: string;
  /** 调用参数 → ToolExecPanel `args`（主/子均为 `args`）。 */
  readonly args: Record<string, unknown>;
  /** 工具输出文本 → ToolExecPanel `result`（主 `output` / 子 `result`）。 */
  readonly result?: string;
  /** Lifecycle status → ToolExecPanel `status`. Structured terminal states —
   *  ``"backgrounded"`` / ``"stopped"`` / ``"restarted"`` — get rendered as
   *  their own distinct labels/icons rather than folded into "done"; see the
   *  same field on ``ChatToolCall``. */
  readonly status:
    | "running"
    | "done"
    | "error"
    | "backgrounded"
    | "stopped"
    | "restarted";
  /** 原始（截断前）输出字符大小 → ToolExecPanel `outputSize`。 */
  readonly outputSize?: number;
  /** 输出是否被截断 → ToolExecPanel `truncated`。 */
  readonly truncated?: boolean;
  /** 历史模式时间戳（ms epoch）→ ToolExecPanel `timestamp`。 */
  readonly timestamp?: number;
  /** 真实执行时长（ms）→ ToolExecPanel `durationMs`（主 `durationMs` /
   *  子 `duration_ms`）。 */
  readonly durationMs?: number;
  /** 是否正在流式生成参数 → ToolExecPanel `argsStreaming`（仅主 Agent）。 */
  readonly argsStreaming?: boolean;
  /** 已累计参数字符数 → ToolExecPanel `argsCharCount`（仅主 Agent）。 */
  readonly argsCharCount?: number;
  /** 是否从 generating_args 早期播种 → ToolExecPanel `timedFromGeneration`
   *  （仅主 Agent）。 */
  readonly timedFromGeneration?: boolean;
  /** 「生成」阶段开始的 wall-clock（ms epoch）→ ToolExecPanel `generationStartedAt`
   *  （仅主 Agent，配合 `timedFromGeneration`）。前端起表时优先用它作为绝对基准，
   *  使切浏览器标签 / 路由切换 / v-if 翻转导致 ToolExecPanel unmount→remount 后，
   *  elapsed 仍然从工具真实开始时间累计、不归零。 */
  readonly generationStartedAt?: number;
  /** 「生成 + 执行」总时长（ms）→ ToolExecPanel `totalMs`（仅主 Agent）。 */
  readonly totalMs?: number;
  /** 用户可修复的失败码（工具 `return` 的 `{ ok:false, error_code, message }`
   *  信封）→ ToolExecPanel `errorCode`。驱动卡片内联修复入口（如 web_search
   *  在企业 TLS 网关下的 `chat.llm.tls_cert_untrusted` → 关闭 TLS 校验并重试）。 */
  readonly errorCode?: string;
  /** Background-process job id when `status === "backgrounded"` (see
   *  `ChatToolCall.backgroundProcessId`). Passed through to
   *  ToolExecPanel so the card can point at the owning job. */
  readonly backgroundProcessId?: string;
}

/** One agentic round inside a sub-agent block: the narration text the
 *  sub-agent streamed for this round, plus the tool calls it issued in the
 *  SAME round. Keeping them together in an ordered list (one turn per round)
 *  is what makes the inline block render "text → tools → text → tools" in the
 *  real order the sub-agent produced them — identical to the main agent's
 *  per-round message rendering — instead of piling all narration after all
 *  tools (the old flat `content` + `tools` lost this ordering). */
export interface SubAgentTurn {
  /** 0-based agentic round this turn represents (backend-stamped `round_index`). */
  readonly roundIndex: number;
  /** Narration text the sub-agent streamed during this round. */
  content: string;
  /** Tool calls the sub-agent issued during this round (1 per `subagent_tool`). */
  tools: SubAgentToolCall[];
}

/** Machine-readable classification of a sub-agent failure.
 *
 *  MIRROR of the backend domain enum
 *  ``qai.chat.domain.sub_agent_error.SubAgentErrorKind`` — the two MUST stay
 *  in exact lockstep, because `SubAgentBlock.vue`'s `ERROR_STYLES` map is
 *  keyed by this union and a kind present on the wire but missing here would
 *  render an unstyled card. The backend's own unit test asserts the value set
 *  against the enum, so adding a kind there fails loudly until it lands here.
 *
 *  Semantics per kind (matching the backend docstring):
 *    - `preflight`   — refused BEFORE any work started (depth cap, unknown
 *                      agent type). Nothing ran; nothing to clean up.
 *    - `isolation`   — the run could not be launched (workspace / job
 *                      registration failed). Retryable.
 *    - `execution`   — the sub-agent ran and one of its rounds failed.
 *    - `budget`      — the token budget was exhausted mid-run.
 *    - `interrupted` — stopped (user ⏹ or cancellation) before finishing.
 */
export type SubAgentErrorKind =
  | "preflight"
  | "isolation"
  | "execution"
  | "budget"
  | "interrupted";

/** One sub-agent's reactive block in the assistant turn.  The store
 *  accumulates these into `tab.streamingSubAgentBlocks` while the parent turn
 *  is in flight, then commits them onto the assistant ChatMessage as
 *  `subAgentBlocks` so a reload still shows the rendered blocks. */
export interface SubAgentBlock {
  /** 0-based index discriminating parallel sub-agents in the turn. */
  readonly index: number;
  /** Total sub-agents this parent turn dispatched (≥ 1).  Drives the
   *  "SubAgent N / M" header label in SubAgentBlock.vue. */
  readonly total: number;
  /** Truncated prompt the sub-agent received (≤ 500 chars in V1). */
  readonly prompt_preview: string;
  /** Ordered per-round timeline: narration text + tool cards of each round
   *  live together so the block renders them interleaved in round order (the
   *  main-agent per-round rendering model). Replaces the old flat
   *  `content` + `tools`, which lost the text↔tool ordering and piled all
   *  text at the end. */
  turns: SubAgentTurn[];
  /** Number of agentic rounds the sub-agent ran (set on `subagent_done`).
   *  This is the COUNT used by the "(N rounds)" header annotation — distinct
   *  from `turns.length` (which only counts rounds that produced text/tools). */
  rounds: number;
  /** Lifecycle status (V1 chat_handler.py:603-635 parity).
   *
   *  `"aborting"` is a CLIENT-SIDE optimistic intermediate state set the
   *  instant the user clicks ⏹ on a running sub-agent block (or on a
   *  `kind === "subagent"` tab — both paths route through
   *  `chatTabs.interruptSubAgent`). It tells the UI to immediately show a
   *  "stopping…" affordance instead of leaving the block looking idle while
   *  the backend abort (round boundary + tool subprocess teardown — up to a
   *  few seconds on Windows) takes effect. The terminal `subagent_done` /
   *  `subagent_error` frame (or the snapshot refresh that follows the stream
   *  close) naturally transitions the block to its final `done`/`error`,
   *  so `"aborting"` never needs an explicit "exit" handler. Mirrors the
   *  parent tab's `ChatTabStatus = "streaming" → "aborting" → "idle"`
   *  state machine (see `requestCancel`).
   *
   *  §F3-async-contract (2026-08-07): the earlier ``"parked"`` variant is
   *  REMOVED. Under the new async agent contract a sub-agent's status
   *  transitions are ``running`` (during any actively-streaming rounds)
   *  → ``done`` (bridge posted terminal completion via
   *  ``SubAgentSessionTerminated`` → dispatcher → SYSTEM_NOTICE →
   *  frontend snapshot refresh).  ``hasActiveBackgroundWork`` no longer
   *  looks at block status alone; it also considers
   *  ``AgentSessionCoordinator``-style pending signals threaded through
   *  the tab (see the getter's docstring).
   *
   *  ``"background"`` (N.18) is the SERVER-side counterpart of ``"running"``:
   *  the spawn returned immediately and the sub-agent is executing detached
   *  from the parent turn, so the parent stream is NOT blocked on it. The
   *  ``subagent_start`` frame carries ``status: "background"`` for that path
   *  (``running`` remains the inline / foreground-window shape). It is a LIVE
   *  status in every sense that matters to the UI — the block still shows a
   *  spinner, is still stoppable, and still counts toward
   *  ``hasActiveBackgroundWork`` — only the accent colour differs. Every
   *  ``status === "running"`` liveness test in this store MUST therefore also
   *  accept ``"background"``.
   */
  status: "running" | "background" | "aborting" | "done" | "error";
  /** Error message when status is `error` (V1 `block.error`). */
  error?: string;
  /** Block collapsed/expanded toggle state (V1 default: collapsed). */
  _collapsed: boolean;
  /** Persistent sub-agent id (V2 enhancement; backend `subagent_done` frame
   *  and persisted block carry it). When present the block's header surfaces
   *  an "open in a new tab" affordance that fetches the full sub-agent
   *  transcript (`GET /api/chat/subagents/{id}`) and lets the user take over
   *  the conversation. Undefined for older / not-yet-persisted blocks — the
   *  open affordance is hidden then. Appended per AGENTS.md §3.1. */
  readonly subagent_id?: string;
  /** Resolved sub-agent profile name (V2 UX enhancement; appended per
   *  AGENTS.md §3.1). Carried on the `subagent_start` frame (and rehydrated
   *  from persisted blocks) so the RUNNING card renders its i18n
   *  type-badge next to the title immediately (e.g. `通用` / `Explore`).
   *  Currently one of `general` / `explore`; unknown values fall back to
   *  the raw string in the UI. Undefined on legacy frames / spawns without
   *  a resolved profile — the UI then hides the badge. */
  readonly subagent_type?: string;
  /** Human-readable task label the LLM supplied when spawning this
   *  sub-agent (V2 UX enhancement; appended per AGENTS.md §3.1). Persisted
   *  as `SubAgentSession.title` and echoed on the `subagent_start` frame,
   *  so the card shows a meaningful title like "Fix login bug" instead of
   *  the generic `SubAgent N` fallback. Undefined when the model did not
   *  provide one — the UI falls back to `SubAgent N` (no regression). */
  readonly name?: string;
  /** Machine-readable failure classification (N.54; appended per AGENTS.md
   *  §3.1). Carried on the `subagent_done` frame — a FAILED run terminates
   *  through that same frame under the async contract — and rehydrated from
   *  the persisted block. Present only when `status === "error"`; drives the
   *  typed error card (icon + colour + recovery hint) in SubAgentBlock.vue.
   *  Undefined on legacy blocks / plain `subagent_error` frames, where the
   *  card falls back to rendering `error` as a bare string (no regression). */
  readonly errorKind?: SubAgentErrorKind;
  /** Human-readable cause behind `errorKind` (N.54). Distinct from the
   *  historical `error` field only in that it always accompanies a typed
   *  kind; the accumulator keeps the two in step so neither path goes
   *  blank. */
  readonly errorMessage?: string;
  /** Actionable next step for the user / parent agent (N.54). Genuinely
   *  optional — some failures have no useful hint — in which case the card
   *  falls back to the per-kind default from
   *  `chat.subagent.errDefaultHint.<kind>`. */
  readonly recoveryHint?: string;
}

/** Token usage as carried by the terminal `end` frame (OpenAI shape). */
export interface ChatMessageUsage {
  readonly prompt_tokens?: number;
  readonly completion_tokens?: number;
  readonly total_tokens?: number;
  readonly elapsed_seconds?: number;
  readonly is_mock?: boolean;
  // ── Tail-appended keystone fields (per AGENTS.md §3.1: namespace fields
  // may only be appended). The backend (streaming.py:3404) tail-appends
  // ``last_round_prompt_tokens`` to ``usage`` at finalize so a multi-round
  // agentic turn's badge can show the LAST round's true wire size instead
  // of the cross-round SUM (which inflates ``prompt_tokens``). For a
  // single-round turn these equal ``prompt_tokens`` (no change). See
  // streaming.py:3395-3410 + AGENTS.md "token-display investigation" diag.
  /** Last-round prompt tokens (true wire size for THIS message's last LLM
   *  call). Preferred over ``prompt_tokens`` for input-tokens display. */
  readonly last_round_prompt_tokens?: number;
  /** Last-round cache_read tokens (Anthropic family; OpenAI / Azure /
   *  Gemini fold cache into ``prompt_tokens``, so this is 0 there). */
  readonly last_round_cache_read_tokens?: number;
  /** DISPLAY-ONLY last-round cache_read volume (real cache-hit tokens). On a
   *  cache-hit turn the backend zeroes ``last_round_cache_read_tokens`` so the
   *  eff-prompt counter does not double-add it; this field carries the OBSERVED
   *  cache-read (``cache_read_observed`` → fallback ``cache_read_tokens``) so
   *  the token badge can compute adjustedInput = input − read − write.
   *  Absent on legacy sessions → falls back to ``last_round_cache_read_tokens``. */
  readonly last_round_cache_read_display?: number;
  /** DISPLAY-ONLY last-round cache_WRITE volume (freshly-cached prefix on a
   *  prompt-cache write turn). Sourced from ``cache_write_observed``. Subtracted
   *  from input for the adjusted ↑ (input − read − write). 0 / absent when the
   *  turn wrote no cache. */
  readonly last_round_cache_write_display?: number;
  /** First-round (round-0) prompt tokens (``_extract_usage``-corrected). The
   *  backend tail-appends this so the input tok/sec RATE has a numerator that
   *  is round-COHERENT with ``ttft_ms`` (round-0's prefill latency). Note the
   *  rate numerator (round-0 prompt) differs from the ``[I] N tokens`` TOTAL
   *  numerator (``last_round_prompt_tokens``, the last round's wire) — two
   *  different but each-correct figures. Single-round turn: equals both
   *  ``last_round_prompt_tokens`` and ``prompt_tokens``. Absent on pre-field
   *  (legacy) sessions → rate falls back to ``prompt_tokens``. */
  readonly first_round_prompt_tokens?: number;
  /** DISPLAY-ONLY first-round (round-0) cache_read volume. Mirrors
   *  ``last_round_cache_read_display`` for the FIRST round so the ↑ badge can
   *  reproduce the per-round Σ on a main-agent turn that persists only ONE
   *  assistant message (its ``last_round_*`` bind to the FINAL round, losing
   *  round-0's net-new). Sourced from round-0 ``cache_read_observed`` →
   *  fallback ``cache_read_tokens`` → 0. Falls back to the last round when the
   *  backend had no distinct first_round_usage (first===last → counted once). */
  readonly first_round_cache_read_display?: number;
  /** DISPLAY-ONLY first-round (round-0) cache_WRITE volume. Mirrors
   *  ``last_round_cache_write_display`` for the FIRST round. On the全新会话首轮
   *  write turn this holds the freshly-cached system+tools prefix (e.g. 6778)
   *  so ↑ nets round-0 to the user's sentence (~3). 0 / absent when round-0
   *  wrote no cache or falls back to the last round. */
  readonly first_round_cache_write_display?: number;
}

/** Client-side performance summary (QAIZap method — V1 useChat.js:2377).
 *  Computed by the transport layer from stream timing; the server does
 *  NOT emit a perf object. */
export interface ChatMessagePerf {
  /** Time-to-first-token in ms (user-perceived latency). */
  readonly ttft_ms?: number;
  /** Total turn duration in ms. */
  readonly total_ms?: number;
  /** Number of tool execution rounds in the turn. */
  readonly tool_rounds?: number;
  // ── QAIZap-style token-rate fields (V1 useChat.js:2377-2389) ──────────────
  // Appended (per AGENTS.md §3.1: namespace fields may only be appended).
  // These mirror V1's perf summary so ChatMessageList can render the
  // `[ I ]/[ O ] N tokens @ X tok/sec` line (index.html:738-746). The
  // actual population requires useChatTransport to compute and call
  // setStreamingPerf with these values; until then renders are
  // v-if-guarded and simply omit the rate segment (no NaN).
  /** Prompt (input) token count for the turn. */
  readonly input_tokens?: number;
  /** Completion (output) token count for the turn. */
  readonly output_tokens?: number;
  /** Input tokens/sec (prompt processing rate). */
  readonly input_tps?: number;
  /** Output tokens/sec (generation rate). */
  readonly output_tps?: number;
}

/** N.53 — one background SYSTEM_NOTICE that an assistant turn INTEGRATED into
 *  its own answer (Batch 7's turn-internal fold).
 *
 *  SYSTEM_NOTICE rows exist only to be folded into the provider wire; they are
 *  never rendered as bubbles (see `historyMapper._mapSystemNoticeRow`). That
 *  makes an integrating answer indistinguishable from one that ignored the
 *  notices — which is what this type fixes: the mapper pairs the assistant
 *  message's persisted `meta.integrated_notice_ids` (dedup_keys) back to the
 *  notice rows' text, so the UI can show a "📥 integrated N notices" chip that
 *  expands to the full text of each notice folded in. */
export interface IntegratedNotice {
  /** Stable backend dedup key (`subagent:<id>:<status>` / `bgp:<id>:<status>`)
   *  — the join key between the assistant's `meta.integrated_notice_ids` and
   *  the notice row's `meta.dedup_key`. Also the v-for key. */
  readonly dedupKey: string;
  /** The notice's full text, for the expanded view. Empty string when the
   *  notice row is not in the loaded page (history pagination can put the
   *  notice on an older page than the answer): the chip still COUNTS it, so
   *  the count never lies about what the answer digested. */
  readonly text: string;
}

export interface ChatMessage {
  readonly id: string;
  readonly role: ChatMessageRole;
  readonly content: string;
  readonly createdAt: number;
  readonly conversationId?: string;
  /** Free-form render/state envelope. Recognised keys:
   *   - `request_id` — prompt-snapshot id (📄 button gate). Per-round (V1
   *     parity): EACH agentic round's assistant message carries ITS OWN
   *     round's `request_id` (the backend saves a separate snapshot per LLM
   *     round and stamps each round's frames), so different tool cards open
   *     different prompts. The trailing summary message carries the last
   *     round's id.
   *   - `kind` — `"turn_warning"` inline notice.
   *   - `interrupted` — aborted-turn marker (appends interruptedMark).
   *   - `streaming` — TRANSIENT marker (single-track model): set `true` on a
   *     per-round assistant message while its tool cards / sub-agent blocks
   *     are still being accumulated live in `messages`. Removed when the turn
   *     settles (confirmDone/Abort/recordError), at which point the message's
   *     running cards are finalized. A reload never carries `streaming` (the
   *     backend persists settled messages), so streaming↔reload shapes match. */
  readonly meta?: Record<string, unknown>;
  /** Tool calls executed during this assistant turn (V1 H-gap #1). */
  readonly toolCalls?: ChatToolCall[];
  /** Token usage from the terminal `end` frame (V1 H-gap #2). */
  readonly usage?: ChatMessageUsage;
  /** Client-side perf summary (V1 H-gap #2). */
  readonly perf?: ChatMessagePerf;
  /** Send-failure marker for a user message (V1 M-gap; index.html:685-689).
   *  When set, ChatMessageList shows an inline error banner + ↻ Retry
   *  button under this message. Null / undefined ⇒ no failure. Mutable
   *  (not `readonly`) so a failed turn can be retried and the marker
   *  cleared without rebuilding the whole message. */
  sendError?: string | null;
  /** Error *code* (frame `payload.code`) for the failed turn, when known.
   *  Lets the error banner offer a context-specific affordance — e.g. for
   *  `chat.llm.unsupported_param` a "go to Cloud Model settings" button so
   *  the user can turn the offending sampling parameter off. Null /
   *  undefined ⇒ generic error (retry only). Mutable alongside `sendError`. */
  sendErrorCode?: string | null;
  /** Slash-command echo marker (V1 `is_command_msg`, useChat.js:1463).
   *  Set on the user bubble that echoes the typed command. Such messages
   *  are display-only: they are NOT sent to the model and NOT persisted
   *  to the backend (the transport only ever forwards prompt strings,
   *  and `loadHistoryMessages` would never return them). */
  isCommandMsg?: boolean;
  /** Slash-command reply marker (V1 `is_command_reply`, useChat.js:1474).
   *  Set on the assistant bubble that renders the command's textual
   *  result. Same display-only contract as `isCommandMsg`. */
  isCommandReply?: boolean;
  /** Model id this assistant turn was produced with (V1 `msg.model_id`,
   *  useModels.js:53-70). Lets the header show the real model name even
   *  after the user switches models, instead of tracking the live
   *  selection. Undefined ⇒ fall back to the current selection. */
  readonly modelId?: string;
  /** Model provider slug paired with `modelId` (V1 `msg.model_provider`),
   *  used to disambiguate cloud entries that share a `model_id`. */
  readonly modelProvider?: string;
  /** Sub-agent blocks accumulated during this assistant turn (V1
   *  useChat.js:62 / 2404 parity).  Persisted on the message so a
   *  history reload re-renders the blocks (V1 useChat.js:62
   *  ``extraMeta.subAgentBlocks``).  Each entry pairs 1:1 with one
   *  parallel sub-agent dispatched in the parent ``agent`` tool call;
   *  the array is sorted by ``index``.  Mutable per-block fields
   *  (status / content / tools / `_collapsed`) are kept non-readonly
   *  so SubAgentBlock.vue can toggle the collapsed state on click
   *  without rebuilding the whole message — matches V1's behaviour
   *  (block._collapsed = !block._collapsed). */
  subAgentBlocks?: SubAgentBlock[];
  /** Multi-Agent discussion authorship (V2 enhancement; multi-agent block-5).
   *  Identifies WHICH named participant produced this message. Set by the
   *  frame handlers from the per-frame `sender_id` (chunk / tool_call / … tail
   *  field) + the active `speaker_changed` frame's display name/color, and
   *  persisted onto committed messages so a reload re-renders the right
   *  avatar/name/color per speaker (the backend persists `Message.sender_id`).
   *  ALL THREE are undefined for ordinary single-agent turns (`sender_id`
   *  缺省 = 现状), so non-discussion bubbles render exactly as before. */
  readonly senderId?: string;
  /** Display name of the participant that produced this message (resolved from
   *  the participant registry / `speaker_changed` frame). Drives the bubble's
   *  name label in discussion mode; falls back to the model name when absent. */
  readonly senderName?: string;
  /** Theme-aware palette colour TOKEN for this participant (a CSS custom
   *  property reference like `var(--discussion-speaker-3)`, NEVER a hardcoded
   *  hex — AGENTS.md §3.10 / §5.3). Drives the avatar background + name accent
   *  in discussion mode. Undefined ⇒ default AI bubble styling. */
  readonly senderColor?: string;
  /** Multi-Agent discussion COORDINATOR (主持人) marker (V2 — coordinator
   *  feature). Set true on an assistant message/stream produced by the unified
   *  coordinator (moderator + judge) turn, fed from the `speaker_changed`
   *  frame's `is_coordinator`. When true the bubble renders the fixed
   *  coordinator display name + a distinct host badge INSTEAD of a borrowed
   *  participant role name. Undefined / false ⇒ ordinary bubble (unchanged). */
  readonly isCoordinator?: boolean;
  /** Accumulated reasoning ("thinking") text for this assistant turn, fed by
   *  `reasoning` frames (cloud reasoning models' `delta.reasoning_content`,
   *  previously discarded, + the internal query-service adapter's
   *  noise-filtered thinking). Rendered in a collapsible ReasoningBlock ABOVE
   *  the answer bubble, separate from `content`. Mutable so the frame handler
   *  can append streamed tokens without rebuilding the message. Undefined /
   *  empty ⇒ no thinking block (ordinary non-reasoning turns unchanged). */
  reasoning?: string;
  /** N.53 — background notices this assistant turn folded into its answer,
   *  resolved by `mapHistoryItems` from the persisted
   *  `meta.integrated_notice_ids` (dedup_keys) joined against the
   *  SYSTEM_NOTICE rows in the same page. Undefined when the turn integrated
   *  nothing — the chip is then not rendered at all (absent ⇒ no chip, which
   *  is also every pre-N.53 message, so no migration is needed). */
  readonly integratedNotices?: IntegratedNotice[];
}

/**
 * Reactive per-tab state. `abortController` and `ws` are intentionally
 * NOT stored here (see file header §2): they live in module-private
 * maps so Vue's reactive proxy never wraps native handles.
 */
/** V1 useChat.js:2227-2313 parity — in-flight network-retry banner state.
 *  Surfaced per-tab so each tab's retry counter stays isolated and the
 *  UI (ChatMessageList) can render a localized banner (`chat.networkInterrupted`).
 *
 *  Lifecycle (driven by the transport):
 *    - `null` → idle (no retry in flight).
 *    - `{ current: N, delaySeconds, deadlineMs, ... }` → a network error
 *      occurred and the turn is waiting to re-open; the banner shows the
 *      attempt number + a live countdown to the next automatic attempt, plus a
 *      "立即重试" button.
 *    - back to `null` once the next attempt succeeds OR retries exhaust
 *      (the latter falls through to the regular `error` state machine
 *      via `recordError`).
 *
 *  Backend-driven (WS + SSE) NETWORK_RETRY frames carry `attempt` +
 *  `delay_seconds`; the transport derives `deadlineMs = now + delay_seconds*1000`
 *  so the UI can render a countdown WITHOUT a per-frame timer on the backend.
 *  The infinite backend retry has no fixed ceiling, so `max` is optional —
 *  when absent the banner shows just the attempt count (not "N/N"). */
export interface NetworkRetryState {
  /** 1-based attempt ordinal about to run. */
  readonly current: number;
  /** Legacy SSE client-retry loop's fixed ceiling; ABSENT for the
   *  backend-driven infinite retry (banner then omits the "/max" part). */
  readonly max?: number;
  /** The backoff (seconds) the backend is waiting before the next attempt;
   *  drives the countdown. Absent on the legacy SSE path. */
  readonly delaySeconds?: number;
  /** `Date.now()` epoch-ms when the next automatic attempt is due; the banner
   *  counts down to it. Absent on the legacy SSE path. */
  readonly deadlineMs?: number;
}

/** One entry of the live task list driven by the `todowrite` chat tool
 *  (V2 enhancement; V1 has no equivalent). Built from the `tool_call`
 *  frame's `arguments.todos`; drives the top TaskListBar (latest) and the
 *  in-conversation TaskListCard (per-call snapshot). */
export interface TodoItem {
  readonly content: string;
  readonly status: "pending" | "in_progress" | "completed" | "cancelled";
  readonly priority?: "high" | "medium" | "low";
}

/** A question raised by the `question` chat tool (V2 enhancement).
 *  Captured from the `tool_call` frame's `arguments` so the in-conversation
 *  ChatQuestionCard can prompt the user; the answer is POSTed back to
 *  `/api/chat/answer` keyed by the tab id, which resolves the suspended tool
 *  handler. */
export interface PendingQuestionOption {
  readonly label: string;
  readonly description?: string;
}

/** One question of a (possibly multi-question) `question` tool call.
 *  The backend's new `arguments.questions` array wire shape maps 1:1 onto
 *  this; the legacy single-question wire shape (`arguments.{question,header,
 *  options,multiple}`) is wrapped into a one-element `questions` array so the
 *  card only ever deals with this normalised model. */
export interface PendingQuestionItem {
  readonly question: string;
  readonly header?: string;
  readonly options: PendingQuestionOption[];
  readonly multiple: boolean;
}

export interface PendingQuestion {
  /** Originating `tool_call` frame id — used to de-dupe re-delivered frames. */
  readonly frameId: string;
  /** Normalised question list (always ≥ 1 entry; single-question wire shape
   *  is wrapped into a one-element array). */
  readonly questions: PendingQuestionItem[];
}

// ---------------------------------------------------------------------------
// Multi-Agent discussion (V2 enhancement — multi-agent block-5, design §2-§8)
// ---------------------------------------------------------------------------

/** Speaker-selection strategy for a discussion (design §4.1 / decision 1).
 *  `manager` = an LLM picks the next speaker each round (default); `round_robin`
 *  = deterministic rotation through the participant order. The user may switch
 *  this at any time in the DiscussionPanel (it maps to the backend
 *  `discussion.selector_mode`). */
export type SelectorMode = "manager" | "round_robin";

/** Per-participant runtime config (backend participant `config_json`).
 *  `allowed_tools` is the user-customisable tool set the named agent may use
 *  (design §4.3 / decision D3); `color` is the palette INDEX (0-based) assigned
 *  to the participant — the front-end maps it to a theme-aware CSS token via
 *  `discussionColorToken` (NEVER a hardcoded colour, §5.3). */
export interface DiscussionParticipantConfig {
  /** Tool names this named agent is allowed to call (design §4.3). Empty / absent
   *  ⇒ no tools (the safe default; `agent`/`question` are never injected in
   *  discussion mode regardless — backend-enforced). */
  readonly allowed_tools?: string[];
  /** Skill ids (SKILL.md parent-directory name) this named agent may use in
   *  discussion mode (backend `config.enabled_skills`, design multi-Agent skill
   *  whitelist). Empty / absent ⇒ the role has NO skill (the safe default; the
   *  backend derives skill tools + prompt catalog from this list). Only ids of
   *  globally-enabled skills are offered in the UI. */
  readonly enabled_skills?: string[];
  /** Palette colour INDEX (0-based) → theme token via `discussionColorToken`.
   *  Absent ⇒ derive deterministically from the participant id/order. */
  readonly color?: number;
}

/** One named participant in a multi-Agent discussion (backend
 *  `chat_participant` row, `kind=named_agent`). Mirrors the CRUD route body
 *  `{ display_name, model_id?, persona?, config{allowed_tools, color?} }` plus
 *  the server-assigned `id` (ULID). */
export interface DiscussionParticipant {
  /** Server-assigned participant id (ULID). Empty for an un-persisted draft
   *  being created in the panel (the POST response fills it). */
  readonly id: string;
  /** Human-facing name shown on the bubble / panel (e.g. "架构师"). */
  display_name: string;
  /** Model the participant speaks with. Absent ⇒ the tab's current model. */
  model_id?: string;
  /** System-prompt persona text injected for this participant (design §4.3). */
  persona?: string;
  /** Runtime config: allowed tool set + palette colour index. */
  config: DiscussionParticipantConfig;
}

/** Conversation-level discussion configuration (backend
 *  `Conversation.meta["discussion"]`, design §5.2 方案 A). Mirrors the
 *  `GET/PATCH /api/chat/conversations/{id}/discussion` body. */
export interface DiscussionConfig {
  /** Whether discussion mode is ON for this conversation. When false the tab
   *  behaves as an ordinary single-agent chat (sender_id 缺省). */
  isDiscussion: boolean;
  /** Active speaker-selection strategy (user-switchable). */
  selectorMode: SelectorMode;
  /** Hard cap on discussion rounds (design §4.4 / decision 5). */
  maxRounds: number;
  /** Whether to run a final judge/summary round (design §4.4 / decision 5). */
  enableJudge: boolean;
  /** Optional discussion FRAMING prompt prepended before each speaker's
   *  persona (design §18.1). Empty / undefined ⇒ the backend falls back to its
   *  built-in default (the panel shows that default as a placeholder). */
  discussionPrompt?: string;
  /** Selected collaboration mode id (design §26/§27 V1). Empty / undefined ⇒
   *  no mode selected → the back-end keeps its existing framing + tool
   *  behaviour (deep_task zero-regression). */
  selectedModeId?: string;
  /** How the mode was selected: auto / manual / locked / suggested (§26.4). */
  modeSelectionPolicy?: string;
  /** Discussion convergence-control master switch (DISC-2 §22A.8). When OFF the
   *  three convergence sub-flags below have no effect (the discussion runs to
   *  the hard round cap as before). Backend meta key absent ⇒ OFF (legacy
   *  conversations are untouched); a fresh tab defaults this ON (see
   *  DEFAULT_DISCUSSION_CONFIG). */
  convergenceControlEnabled: boolean;
  /** Allow the manager to end the discussion EARLY once it judges the topic
   *  converged, before the hard round cap (DISC-2 §22A.8). */
  managerEarlyEndEnabled: boolean;
  /** Soft-stop repeated / low-information turns (DISC-2 §22A.8). */
  softStopEnabled: boolean;
  /** Soft-stop strategy id (DISC-2 §22A.8). "conservative" is the only mode in
   *  P1-step1; further modes land in P1-step4. */
  softStopMode: string;
  /** Social/lightweight-path response policy (DISC-2 P4-step1 §22A.7). Shapes a
   *  greeting / thanks reply: "silent" (no reply), "single_brief_reply" (default
   *  = phase-1 behaviour), "single_closing_reply" (closing tone),
   *  "continue_last_topic" (carry the previous topic forward). Backend meta key
   *  absent/illegal ⇒ "single_brief_reply". */
  socialResponsePolicy: string;
  /** Manager (moderator) scheduling-preference append text (DISC-2 P4-step2
   *  §22A.7). Manager-selector mode only: an advisory scheduling preference
   *  appended to the END of the moderator prompt (the immutable protocol
   *  segment always precedes it). Empty ⇒ no append (phase-1 prompt). The UI
   *  exposes only this textarea; a non-empty value implies the backend
   *  "append_instruction" mode (the mode field is inferred at read time). */
  managerPromptAppend: string;
  /** DISC-1 §22.7: master switch for "discussion → implementation" — once the
   *  discussion produces a plan and the user assigns roles, an @mention with an
   *  implementation verb routes the addressed role into IMPLEMENTATION mode
   *  (tools opened up, sandbox policy, run budget). Backend meta key absent ⇒
   *  OFF (legacy conversations untouched); a fresh tab defaults this ON (用户
   *  2026-06-24 拍板). */
  implementationEnabled: boolean;
  /** DISC-2 §22A.5: enable the LLM grey-zone INTENT CLASSIFIER (a conservative
   *  fallback that classifies ambiguous user turns when heuristics are
   *  uncertain). Backend meta key absent ⇒ OFF (legacy conversations
   *  untouched); a fresh tab defaults this ON (用户 2026-06-24 拍板). */
  intentClassifierEnabled: boolean;
  /** DISC-1 TODO-2 user-tunable knobs. Each mirrors a ``meta["discussion"]``
   *  numeric/string key; the value here is the UI default (= the backend
   *  constant default) so the panel always shows a sensible number. A persisted
   *  ABSENT key still resolves to the backend default at read time (legacy
   *  untouched). */
  // Run-level implementation budget caps (§22.5).
  implMaxTotalFileEdits: number;
  implMaxTotalExecCalls: number;
  implMaxTotalRuntimeSeconds: number;
  implMaxTotalChangedFiles: number;
  // Soft-stop tuning thresholds (§22A.4).
  softStopSimilarity: number;
  softStopMinRounds: number;
  softStopConsecutiveTurns: number;
  // Intent classifier model + timeout (§22A.5). Empty model ⇒ "let the ladder
  // decide" (the backend resolver falls back through manager/roster/default).
  intentClassifierModel: string;
  intentClassifierTimeoutMs: number;
  // Feature-item extractor (planner) model + timeout (§22.4).
  implementationPlannerModel: string;
  implementationPlannerTimeoutMs: number;
  /** DISC-1 三期-step5: enable the OPTIONAL independent LLM validator that
   *  reviews each item's acceptance criteria vs the agent's result (pass/fail).
   *  Backend meta key absent ⇒ OFF (legacy untouched); the validator is OFF by
   *  default even for a fresh tab (it costs an extra LLM call per item — opt-in).
   */
  implementationValidatorEnabled: boolean;
  /** DISC-1 三期-step5: validator review LLM timeout (ms). */
  implementationValidatorTimeoutMs: number;
  /** DISC-1 完成判定 B: per-item verify-command exec timeout (ms). */
  implementationVerifyCommandTimeoutMs: number;
  /** Coordinator (主持人) model id — the cloud model the unified coordinator
   *  (moderator + judge) uses. `null` ⇒ unset; the backend resolves the ladder
   *  coordinator_model_id → roster[0].model_id → default_model_id. Wire key
   *  `coordinator_model_id` (tail-appended; absent ⇒ null, legacy untouched). */
  coordinatorModelId: string | null;
  /** Coordinator (主持人) extra prompt — advisory text appended to BOTH the
   *  moderator system prompt and the judge directive. Empty ⇒ built-in default
   *  only. Wire key `coordinator_prompt` (tail-appended; absent ⇒ ""). */
  coordinatorPrompt: string;
  /** Coordinator (主持人) permission override — user-configurable narrowing of
   *  the coordinator's tool + skill sets (task 2 UI: "编辑协调者" dialog). Wire
   *  key `coordinator_config` (tail-appended; absent ⇒ null, backend uses
   *  dynamic defaults = all tools minus `agent` + all globally-enabled skills).
   *  Only two keys are honoured; anything else is ignored server-side. */
  coordinatorConfig: CoordinatorConfig | null;
  /** Participant registry (named agents) for this conversation. */
  participants: DiscussionParticipant[];
}

/** Coordinator permission override written into
 *  `discussion.coordinator_config`. Missing key ⇒ backend dynamic default for
 *  that dimension (not the empty list). Both defaults resolve to "all
 *  available"; the user narrows either dimension by writing an explicit list. */
export interface CoordinatorConfig {
  /** Explicit tool whitelist for the coordinator. Absent ⇒ dynamic default
   *  (all registered tools minus `agent`). Empty list ⇒ NO tools. */
  allowedTools?: string[];
  /** Explicit skill whitelist for the coordinator. Absent ⇒ dynamic default
   *  (all globally-enabled skills). Empty list ⇒ NO skills. */
  enabledSkills?: string[];
}

/** Factory default discussion config for a fresh tab — discussion OFF, so the
 *  tab is an ordinary single-agent chat until the user opens the panel and
 *  enables it. Defaults mirror the design (manager selector, 6-round cap,
 *  judge on).
 *
 *  Convergence-control defaults (DISC-2 §22A.8, 用户 2026-06-23 拍板): a NEW tab
 *  defaults the convergence controls ON with the CONSERVATIVE soft-stop — i.e.
 *  discussions stop repeating / end early by default. (Read side: a backend
 *  meta key that is ABSENT means OFF, so existing conversations are unaffected;
 *  these defaults only seed brand-new tabs.) */
export const DEFAULT_DISCUSSION_CONFIG: DiscussionConfig = {
  isDiscussion: false,
  selectorMode: "manager",
  maxRounds: 6,
  enableJudge: true,
  convergenceControlEnabled: true,
  managerEarlyEndEnabled: true,
  softStopEnabled: true,
  softStopMode: "conservative",
  socialResponsePolicy: "single_brief_reply",
  managerPromptAppend: "",
  implementationEnabled: true,
  intentClassifierEnabled: true,
  implMaxTotalFileEdits: 80,
  implMaxTotalExecCalls: 120,
  implMaxTotalRuntimeSeconds: 1800,
  implMaxTotalChangedFiles: 60,
  softStopSimilarity: 0.72,
  softStopMinRounds: 3,
  softStopConsecutiveTurns: 2,
  intentClassifierModel: "",
  intentClassifierTimeoutMs: 2000,
  implementationPlannerModel: "",
  implementationPlannerTimeoutMs: 8000,
  implementationValidatorEnabled: false,
  implementationValidatorTimeoutMs: 8000,
  implementationVerifyCommandTimeoutMs: 120000,
  participants: [],
  coordinatorModelId: null,
  coordinatorPrompt: "",
  coordinatorConfig: null,
};

/** Number of distinct theme-aware speaker palette tokens (design §5.3). The
 *  tokens `--discussion-speaker-0 .. --discussion-speaker-{N-1}` are defined in
 *  the global chat theme CSS with BOTH dark and `html.light` overrides, so a
 *  participant colour is ALWAYS a theme variable, never a hardcoded literal. */
export const DISCUSSION_PALETTE_SIZE = 8;

/** Stable sentinel `sender_id` the backend stamps on the unified coordinator
 *  (主持人) turn. It rides the live `speaker_changed` frame alongside
 *  `is_coordinator=true`, but a PERSISTED coordinator message reloads carrying
 *  only this sentinel id (no `is_coordinator` flag), so the front end recovers
 *  the coordinator identity from `senderId === COORDINATOR_SENDER_ID` on
 *  history load — see `mapHistoryItems` + `ChatMessageList` isCoordinator
 *  helpers. Never a display value: the bubble still renders the fixed i18n
 *  `chat.discussion.coordinator.displayName`. */
export const COORDINATOR_SENDER_ID = "__coordinator__";

/** Resolve a participant's palette index into the theme-aware CSS custom
 *  property reference used for its avatar background / name accent. The index
 *  wraps modulo the palette size so any number of participants stays within the
 *  defined token set. Returns e.g. `"var(--discussion-speaker-3)"`. This is the
 *  SINGLE place a colour is derived (§5.3 — no hardcoded colours anywhere). */
export function discussionColorToken(index: number): string {
  const i =
    Number.isFinite(index) && index >= 0
      ? Math.floor(index) % DISCUSSION_PALETTE_SIZE
      : 0;
  return `var(--discussion-speaker-${i})`;
}

// ---------------------------------------------------------------------------
// DISC-1 implementation orchestration view-model (§22.9 observability layer)
// ---------------------------------------------------------------------------

/** One feature item in the implementation run, as a camelCase view model
 *  (mapped from the snake_case `plan_ready` item summary + updated in place by
 *  the `implementation_item_*` frames). Holds ONLY the control-plane fields the
 *  UI needs for a progress row — never the full output / diff (§22.9). */
export interface ImplementationItemVM {
  id: string;
  title: string;
  /** `pending` / `in_progress` / `done` / `failed` / `skipped` (backend status). */
  status: string;
  /** Role assigned to implement this item; `null` until assigned. */
  assignedRole: string | null;
  /** Role the extractor proposed; `null` when absent. */
  suggestedRole: string | null;
  /** SHORT outcome summary set on `done` (`null` otherwise). */
  resultSummary: string | null;
  /** SHORT error summary set on `failed` (`null` otherwise). */
  lastError: string | null;
  /** Implementation note (detail panel — populated by the GET, NOT the SSE
   *  frame; the live frames carry only the control-plane subset). */
  description: string;
  /** "Done when…" checks (detail panel). */
  acceptanceCriteria: string[];
  /** DISC-1 完成判定 B — per-item verification command (detail panel; editable). */
  verifyCommand: string;
  /** Item ids this one depends on (detail panel, read-only display). */
  dependsOn: string[];
  /** How many run attempts have been made (detail panel). */
  attemptCount: number;
}

/** Per-tab implementation-run observability state (§22.9). Populated ONLY when
 *  the OFF-by-default backend implementation orchestration emits its control-
 *  plane frames; an idle tab carries `DEFAULT_IMPLEMENTATION_STATE` (phase
 *  `"none"`, empty items) so ordinary chat / discussion is unaffected. The
 *  authoritative reactive copy lives on the tab (parallel to `discussion`), read
 *  by the future ImplementationPanel. */
export interface TabImplementationState {
  /** Run phase from the backend state machine: `none` / `planned` /
   *  `implementing` / `completed` / `failed` / `paused`. */
  phase: string;
  /** ULID of the active/last run; `null` before any run. */
  runId: string | null;
  /** Id of the item currently being implemented; `null` when idle/terminal. */
  currentItem: string | null;
  /** Ordered feature items with their live status. */
  items: ImplementationItemVM[];
}

/** Neutral idle implementation state for a fresh / non-implementation tab. */
export const DEFAULT_IMPLEMENTATION_STATE: TabImplementationState = {
  phase: "none",
  runId: null,
  currentItem: null,
  items: [],
};

// ---------------------------------------------------------------------------
// (β) Flat tab-strip model: every sub-agent tab — at any depth — is a
// first-class top-level tab in `state.tabs`, rendered directly on the top
// strip alongside main-agent tabs. There is no nested `subAgents[]` mirror
// on the parent tab and no separate SubAgentRail component: subagent tabs
// use `kind === "subagent"` + `subagentMeta` and drive their own composer /
// context badge / model dropdown exactly like a main-agent tab. Nested
// drill-in (main → sub → grand → great-grand → ...) works uniformly because
// `subagentMeta.parentSubagentId` / `depth` describe the tree edges honestly.
// ---------------------------------------------------------------------------

export interface ChatTab {
  readonly id: TabId;
  conversationId: ConversationId | null;
  /** Promote-ready detection result for this tab's conversation (backend
   *  migration 057). Loaded from the single-GET conversation summary on open
   *  (`loadHistoryMessages` → historyLoader) and refreshed at turn end when
   *  the backend re-detects + the frontend re-reads. Drives the "Promote to
   *  App Builder" CTA with ZERO on-open disk scans (replaces the old
   *  every-message global scan). `null` / undefined ⇒ never detected;
   *  `workdir` empty + `variants` empty ⇒ "checked, nothing to promote". */
  detectedModel?: {
    workdir: string;
    variants: { precision: string; label: string }[];
    checkedAt?: string;
  } | null;
  title: string;
  modelId: string;
  /**
   * V1 parity (useModels.js:14, index.html:996-1028) — selected model's
   * `provider` slug (e.g. "provider_a" / "cloud_llm"). Required to
   * disambiguate cloud entries that share the same `model_id` but live
   * under different upstream providers (the backend returns multiple
   * such entries on `/api/model-catalog/cloud-models`). Empty string
   * means "no provider filter / local model" — same semantics as V1's
   * `selectedModelProvider = ''`.
   */
  modelProvider: string;
  status: ChatTabStatus;
  messages: ChatMessage[];
  streamingContent: string;
  /** Last error envelope when `status === "error"`, else null.
   *  `retryDisposition` / `httpStatus` / `requestId` are OPTIONAL diagnostic
   *  fields threaded from the stream ERROR frame's extra payload (backend
   *  contract: `{ code, message, retryable, retry_disposition, ... }`). They
   *  drive the actionable error bubble (registry-driven rendering) and the
   *  sanitized "Copy diagnostics" affordance. Absent ⇒ the transport had no
   *  extra fields (older frames / non-LLM errors); the UI degrades to a plain
   *  code+message + generic actions. */
  lastError:
    | {
        type: string;
        code: string;
        message: string;
        retryDisposition?: string | null;
        httpStatus?: number | null;
        requestId?: string | null;
      }
    | null;
  createdAt: number;
  lastActiveAt: number;
  /** Active toolbar mode — null = default chat. */
  activeMode: ToolModeKey | null;
  /** Per-mode tool parameters aggregated for the outgoing turn. */
  toolParams: ToolParams;
  /** Sampling parameters; passed through `extra.tool_params` on send. */
  modelParams: ModelParams;
  /** DEPRECATED single-track buffer — retained as an always-`[]` field for
   *  type/back-compat only. The single-list streaming model (V1 parity,
   *  useChat.js:2455-2520) now pushes per-round assistant messages with
   *  `toolCalls` directly into `messages` while streaming (see
   *  `frameHandlers.handleToolCall` / `handleToolResult`), so live tool cards
   *  live IN `messages` (bound to their round's lead-in), not in a separate
   *  bottom-of-stream buffer. Nothing writes to this anymore; renderers and
   *  commit builders read `messages`. Kept (not removed) so external callers /
   *  `setStreaming` resets don't churn. */
  streamingToolCalls: ChatToolCall[];
  /** Id of the streaming assistant message currently accumulating this
   *  round's tool cards (the "active tool message"). A new `tool_call` with a
   *  fresh lead-in opens a new round → new message → new id here; parallel /
   *  batched tool_calls in the SAME round append onto this message. `null`
   *  between rounds (no active tool message). Reset to `null` on every
   *  `setStreaming` start and every terminal transition. V1 parity:
   *  useChat.js:2460-2505 pushes one `assistant{content,tool_calls}` + one
   *  `tool_indicator` per round into the single `messages` list. */
  activeToolMessageId: string | null;
  /** Map of agentic-loop `round_index` (0-based) → the streaming assistant
   *  message id that holds that round's lead-in text + tool cards. The
   *  authoritative, zero-inference grouping key (backend stamps every
   *  CHUNK / TOOL_CALL / TOOL_RESULT frame with `round_index` —
   *  `StreamFrame.with_round_index` / streaming.py). `tool_call`/`chunk`
   *  for a round look the round up here: present ⇒ append onto that
   *  message; absent ⇒ open a new round message (and record its id). This
   *  replaces the fragile "lead_in non-empty ⇒ new round" heuristic, so
   *  same-round inter-tool narration no longer mis-orders. Reset to `{}`
   *  on every `setStreaming` start and every terminal transition. */
  roundMessageIds: Record<number, string>;
  /** Map of agentic-loop `round_index` (0-based) → the streaming assistant
   *  message id that holds THAT round's SUB-AGENT blocks (a dedicated
   *  message, distinct from the main-agent's `roundMessageIds[ri]`).
   *
   *  Why a SECOND map (and not reuse `roundMessageIds[ri]`)?
   *
   *  The main-agent's per-round message (opened by `handleChunk` /
   *  `handleToolCall` and pinned to `roundMessageIds[ri]`) already carries
   *  that round's `content` (lead-in text) and `toolCalls` (incl. the
   *  `agent` dispatch tool card). If `handleSubagentStart` folded the
   *  sub-agent blocks onto that SAME message, one message would carry all
   *  three fields (`content` + `toolCalls` + `subAgentBlocks`) and the
   *  visual order in a single bubble would be driven by the template's
   *  hard-coded field order — a fragile assumption that breaks the moment
   *  the real event timeline is `content₁ → toolCall₁ → subagent →
   *  content₂ → toolCall₂` (interleaved), or even the far more common
   *  `content → toolCall(agent) → subagent` (where the sub-agent card must
   *  appear AFTER the `agent` tool card, not before). See the 2026-07-01
   *  ordering regression: the round-shared `roundMessageIds` merger put
   *  sub-agent cards ABOVE the `agent` tool card because the template
   *  rendered `subAgentBlocks` before `toolCalls`.
   *
   *  Fix (2026-07-02): give sub-agent blocks THEIR OWN per-round message.
   *  `handleSubagentStart` looks up `roundSubAgentMessageIds[ri]` first —
   *  present ⇒ append onto that message (parallel sub-agents spawned in
   *  the SAME round share ONE message, keeping the dedup-by-index
   *  semantics intact); absent ⇒ open a fresh assistant message AFTER
   *  the main-agent's round message (so `tab.messages` naturally reads
   *  `[main-agent round-0 msg, sub-agents round-0 msg, main-agent round-1
   *  msg, sub-agents round-1 msg, …]` — the array order IS the visual
   *  order, no template heuristics needed). The multi-round
   *  Crit-block-overwrite fix (backend-stamped `round_index` on
   *  SUBAGENT_START) is preserved: sub-agents dispatched in different
   *  rounds land on different messages via THIS map's per-round key.
   *
   *  Reset to `{}` on every `setStreaming` start, every terminal
   *  transition (confirmDone/confirmAbort/recordError), and every
   *  `speaker_changed` frame (discussion mode boundary) — mirroring
   *  `roundMessageIds`'s lifecycle exactly. */
  roundSubAgentMessageIds: Record<number, string>;
  /** Id of the streaming assistant message currently accumulating this turn's
   *  sub-agent blocks. Unified into the single `messages` list the same way
   *  tool cards are (user requirement). `null` until the first `subagent_*`
   *  frame of the turn opens it. Reset on `setStreaming` / terminal. */
  activeSubAgentMessageId: string | null;
  /** N.35 — flat index of the sub-agent ids currently doing LIVE work in this
   *  tab (`running` / `background` / `aborting`).
   *
   *  This is a DERIVED PERFORMANCE INDEX, not a truth source. The truth is
   *  `messages[].subAgentBlocks[].status` (that is what persists and what a
   *  reload rebuilds from); this set exists so `hasActiveBackgroundWork` — read
   *  on every render of the typing indicator and the Stop button — is O(1)
   *  instead of walking every message's every block on each evaluation.
   *
   *  Maintained in exactly two places, and nowhere else:
   *    1. Live frames — `frameHandlers.handleSubagentStart` adds,
   *       `handleSubagentDone` / `handleSubagentError` delete.
   *    2. Reload — `openConversation` / `_refreshSubAgentTab` REBUILD the whole
   *       set from the persisted blocks (`_deriveActiveSubAgentIds`), because
   *       after a reload no frames will ever arrive for work that finished
   *       while the page was closed.
   *
   *  A plain `Set` is correct here: membership is dynamic (inserted/deleted at
   *  runtime) and `.size` is the read. Reactivity is verified — Vue 3.5's
   *  reactive proxy instruments `Set.prototype.add`/`delete`, so a getter that
   *  reads `.size` recomputes on mutation even for a Set nested inside the
   *  `tabs` array (locked by `chatTabs.activeSubAgentIds.spec.ts`). */
  activeSubAgentIds: Set<string>;
  /** Flat index of background-process ids still in flight for this tab.
   *
   *  Same role as `activeSubAgentIds` above, for the other kind of work that
   *  keeps the agent busy: a long `exec` handed to the background-process
   *  manager, or a service the model started. Without it the tab reads IDLE
   *  (no typing indicator, no Stop) while a command is still running — the
   *  user sees "finished" for work that is very much alive.
   *
   *  Maintained from the SAME `background_process.updated` / `.deleted`
   *  global-SSE frames the process cards already consume, so this adds no
   *  new polling: a non-terminal status adds, a terminal status or a
   *  `deleted` frame removes. `hasActiveBackgroundWork` reads `.size`, so
   *  the busy predicate stays O(1) like the sub-agent half. */
  activeBgProcessIds: Set<string>;
  /** Token usage captured from the `end` frame of the in-flight turn. */
  streamingUsage: ChatMessageUsage | null;
  /** Client-side perf summary for the in-flight turn (filled by the
   *  transport via `setStreamingPerf`). */
  streamingPerf: ChatMessagePerf | null;
  // ── History pagination state (V1 useChat.js:227-231 parity) ────────────────
  // Appended per AGENTS.md §3.1 (namespace fields may only be appended).
  // V1 paginates newest-first then prepends older pages on scroll-up
  // (loadMoreMessages, useChat.js:869-902). The V2 backend cursor is a
  // FORWARD, ascending `position:<int>` cursor (TestClient-verified), so we
  // track the absolute position of the oldest message currently loaded and
  // page *backwards* by decreasing it (see `loadMoreMessages`).
  /** True while a "load older" page request is in flight (V1
   *  `isLoadingMoreMessages`). Guards re-entrancy from the sentinel. */
  loadingMore: boolean;
  /** Whether older messages remain to be loaded (V1 `hasMoreMessages`).
   *  Drives the IntersectionObserver sentinel in ChatMessageList. */
  hasMoreMessages: boolean;
  /** Absolute backend `position` of the oldest message currently loaded.
   *  Older page = `position:(messagesOldestPos - PAGE_SIZE)`. -1 ⇒ not yet
   *  initialised (no history load happened). */
  messagesOldestPos: number;
  // ── Message queue (V1 useChat.js:240-242 parity) ───────────────────────────
  // Appended per AGENTS.md §3.1 (namespace fields may only be appended).
  // V1 lets the user keep pressing Enter while a turn is streaming; each
  // press enqueues the input here (capped at MAX_QUEUE_SIZE) instead of
  // being dropped. When the in-flight turn finishes the transport layer
  // dequeues the head item and re-sends it (see ChatView's streaming→idle
  // watcher). Per-tab so each tab's pending sends stay isolated (§10.6).
  /** Pending messages queued while this tab is streaming (FIFO). */
  messageQueue: QueuedMessage[];
  /** Whether the floating queue panel is expanded (list view) vs the
   *  collapsed count badge (V1 `queueExpanded`, useChat.js:242). */
  queueExpanded: boolean;
  /** Text set by the ``inject_as_new_turn`` frame handler when a turn's
   *  normal-completion tail drained still-pending Ctrl+Enter injections.
   *  A ChatView watcher observes this field and re-submits the text via the
   *  ordinary composer-submit path (``useChatTurnSubmit.submitToTab``) as
   *  soon as the tab settles back to ``idle`` — byte-identical to a user
   *  pressing Enter. Cleared to ``null`` immediately after re-submission.
   *  ``null`` = no deferred injection (byte-for-byte unchanged for every
   *  turn that folded its injections on time). */
  pendingDeferredInject: string | null;
  /** V1 useChat.js:2227-2313 parity — surfaces the in-flight network
   *  retry state to the UI so ChatMessageList can render a banner like
   *  "Network interrupted, retrying ({current}/{max})…". `null` means
   *  no retry is currently in progress (the normal idle state). The
   *  transport (`useChatTransport`) sets this via `setNetworkRetry`. */
  networkRetry: NetworkRetryState | null;
  /** DEPRECATED single-track buffer — retained as an always-`[]` field for
   *  type/back-compat only. Sub-agent blocks are now unified into the single
   *  `messages` list (user requirement): `subagent_*` frames build / update a
   *  streaming assistant message's `subAgentBlocks` in place (see
   *  `activeSubAgentMessageId` + frameHandlers). Nothing writes to this. */
  streamingSubAgentBlocks: SubAgentBlock[];
  /** Prompt snapshot request_id captured from the terminal ``end`` frame
   *  (V1 parity: ``backend/main.py:6716-6720`` done frame payload contains
   *  ``request_id``).  Set by ``applyFrame`` when the ``end`` frame carries
   *  ``payload.request_id``; written onto the committed assistant message's
   *  ``meta.request_id`` by ``confirmDone`` so the "Prompt Snapshot" button
   *  can surface it.  Reset to ``null`` on ``setStreaming`` / ``confirmAbort``. */
  streamingRequestId: string | null;
  /** Persisted assistant Message id for the in-flight turn, captured from the
   *  terminal ``end`` frame's ``payload.assistant_message_id`` (backend §3.1
   *  tail-append). Used by ``confirmDone`` as the committed trailing assistant
   *  message's ``id`` so it matches the DB row — enabling per-message fork
   *  (``up_to_message_id``) on a just-finished turn without a reload. ``null``
   *  when the backend did not supply one (legacy / no assistant message);
   *  ``confirmDone`` then falls back to a client-local id (prior behaviour).
   *  Reset to ``null`` on ``setStreaming`` / ``confirmDone`` / ``confirmAbort``. */
  streamingAssistantMessageId: string | null;
  /** True while the newest history page is being fetched for a
   *  just-opened conversation tab (V1 index.html:407-419 skeleton-card
   *  loading UX). Drives the skeleton placeholder in ChatMessageList
   *  so the user sees a loading state instead of an empty flash when
   *  switching to an existing conversation. */
  loadingHistory: boolean;
  /** Live task list from the most recent `todowrite` tool call (V2
   *  enhancement). Replaced wholesale on each `todowrite` (the model sends
   *  the complete list every time). Empty ⇒ the top TaskListBar is hidden. */
  todoList: TodoItem[];
  /** Whether the top sticky task-list bar (TaskListBar) is expanded (full
   *  latest list) vs collapsed (progress line only). Defaults to collapsed —
   *  the bar is an always-available at-a-glance entry; the full per-call
   *  snapshot lives in the in-conversation TaskListCard. */
  todoExpanded: boolean;
  /** Pending question from the `question` tool (V2 enhancement).
   *  Non-null ⇒ an in-conversation ChatQuestionCard is ACTIVE (awaiting an
   *  answer) and the agentic loop is suspended server-side. Acts as the
   *  "which card is currently awaiting answer" pointer; the answered/read-only
   *  state is derived per-call from the message's tool calls (+ answer), not
   *  from this single value. Cleared once the user answers (or the turn ends
   *  / aborts). */
  pendingQuestion: PendingQuestion | null;
  /** Multi-Agent discussion configuration for this tab's conversation (V2
   *  enhancement; multi-agent block-5). Drives the DiscussionPanel UI + the
   *  `discussion` / `pinned_speaker` SSE query params. `isDiscussion=false`
   *  (the default) ⇒ ordinary single-agent chat (zero behaviour change). */
  discussion: DiscussionConfig;
  /** DISC-1 implementation-run observability state (§22.9; V2 enhancement,
   *  appended per AGENTS.md §3.1). Populated by the `plan_ready` /
   *  `implementation_item_*` / `implementation_phase_changed` frames the
   *  OFF-by-default backend implementation orchestration emits; an idle tab
   *  carries `DEFAULT_IMPLEMENTATION_STATE` (phase `"none"`) so ordinary
   *  chat / discussion is unaffected. The future ImplementationPanel reads it. */
  implementation: TabImplementationState;
  /** Id of the participant currently speaking in the live discussion turn,
   *  set by the `speaker_changed` frame (multi-agent block-5). Drives the live
   *  streaming bubble's avatar/name/color and is stamped onto per-round/trailing
   *  messages as `senderId`. `null` between speakers / for non-discussion turns. */
  streamingSenderId: string | null;
  /** Display name of the current live speaker (from `speaker_changed`). */
  streamingSenderName: string | null;
  /** Theme-aware palette colour token for the current live speaker. */
  streamingSenderColor: string | null;
  /** Model id the current live speaker is using (from `speaker_changed.model_id`)
   *  — drives the "· model-name" suffix next to the speaker name in the bubble
   *  meta line (V2 enhancement 2026-06-21: "show which model each role uses"),
   *  and is stamped onto every assistant message persisted for this speaker
   *  via `modelId`. `null` between speakers / for non-discussion turns. */
  streamingSenderModelId: string | null;
  /** Coordinator (主持人) marker for the current live speaker (from
   *  `speaker_changed.is_coordinator`). Drives the live streaming bubble's host
   *  identity + badge and is stamped onto messages persisted for this floor via
   *  `isCoordinator`. `false` between speakers / for non-coordinator turns. */
  streamingIsCoordinator: boolean;
  /** Id of the participant the user "called on" to speak on the NEXT turn
   *  (multi-agent block-5). Forwarded as the `pinned_speaker` SSE query param
   *  so the backend selector lets that participant speak first. Cleared after
   *  the turn starts / when discussion mode is off. `null` ⇒ no pinned speaker
   *  (the selector picks normally). */
  pinnedSpeaker: string | null;
  /** Tab kind (V2 enhancement; appended per AGENTS.md §3.1). Absent /
   *  `"chat"` ⇒ an ordinary chat session. `"subagent"` ⇒ the tab was opened
   *  from a sub-agent block / history list to inspect and take over a
   *  sub-agent's conversation; the transport then forwards `subagent_id` so
   *  the backend continues the turn on that sub-agent's context. */
  readonly kind?: "chat" | "subagent";
  /** Sub-agent binding for a `kind === "subagent"` tab (appended per
   *  AGENTS.md §3.1). Holds the sub-agent id (forwarded as the `subagent_id`
   *  SSE query param when the user takes over the conversation), the ROOT
   *  conversation id (top-of-tree main-agent conversation), the DIRECT parent
   *  sub-agent id (`null` for depth-1), the tree depth (1 = first-level,
   *  2 = grand, ...), and the last-known lifecycle status/owner surfaced in
   *  the tab header. Undefined for ordinary chat tabs. */
  readonly subagentMeta?: {
    readonly subagentId: string;
    /** The ROOT (top-of-tree main-agent) conversation id — identical for
     *  every sub-agent under that root, regardless of depth. Historically
     *  the front-end called this `parentConversationId`; the honest name is
     *  `rootConversationId` (a grand sub-agent's DIRECT parent is another
     *  sub-agent, not a conversation). Retained as the tab's own
     *  `conversationId` too so the transport / interrupt path routes on the
     *  same value. */
    readonly rootConversationId: string;
    /** Direct-parent sub-agent id. `null`/absent = the direct parent is the
     *  main agent (a depth-1 sub-agent under the root conversation);
     *  non-null = the direct parent is another sub-agent row (grand /
     *  great-grand cell). Drives no UI directly today; carried on the meta
     *  so parent-relative operations stay honest at any depth. */
    readonly parentSubagentId?: string | null;
    /** Recursion depth (1 = first-level, 2 = grand, 3 = great-grand, ...).
     *  Defaults to 1 for legacy rows. */
    readonly depth?: number;
    readonly status: string;
    readonly owner: string;
    /** Sub-agent's OWN context usage (appended per AGENTS.md §3.1). Sourced
     *  from `GET /api/chat/subagents/{id}`'s `used_tokens` / `budget_tokens`
     *  / `ratio` fields. A standalone sub-agent tab carries its PARENT's
     *  `conversationId`, so the per-conversation `/context` endpoint would
     *  report the parent's usage — the context badge reads these instead so
     *  it reflects the sub-agent's own window. Undefined until the detail
     *  fetch populates them. */
    readonly usedTokens?: number;
    readonly budgetTokens?: number;
    readonly ratio?: number;
    /** REAL (un-clamped) occupancy + ratio (appended per AGENTS.md §3.1),
     *  same口径 as the main agent's `/context` `raw_used_tokens` / `raw_ratio`.
     *  `usedTokens` / `ratio` above are clamped to the window (floor 100%);
     *  these preserve the over-window truth so the sub-agent badge can show
     *  >100% at parity with the main agent. Sourced from
     *  `GET /api/chat/subagents/{id}` AND refreshed LIVE from the sub-agent
     *  stream's `used_tokens` / `context_limit` frame fields (every tool round
     *  while running). Undefined until the first detail fetch / live frame. */
    readonly rawUsedTokens?: number;
    readonly rawRatio?: number;
    /** Sub-agent's OWN model (appended per AGENTS.md §3.1). Sourced from
     *  `GET /api/chat/subagents/{id}`'s `model_id` / `model_provider`
     *  (session is the authoritative truth source — State-Truth-First). The
     *  sub-agent's default model = its spawning parent's model; the user may
     *  switch it per sub-agent tab via the model dropdown, which PATCHes the
     *  session and recomputes `budgetTokens` (denominator) WITHOUT touching
     *  `usedTokens` (numerator) — and never writes the global pref nor the
     *  parent. Drives the ModelDropdown's selected-item display + the
     *  context-badge budget for this sub-agent only. Undefined until the
     *  detail fetch populates them (provider may be null until first switch). */
    readonly modelId?: string;
    readonly modelProvider?: string;
    // Sub-agent回看 token usage + per-round prompt snapshots are NO LONGER
    // carried on subagentMeta: the sub-agent now reuses the main agent's
    // STANDARD per-message rendering — each round's token usage + prompt-
    // snapshot request_id are stamped onto the assistant turn (backend) and
    // surfaced via the SHARED `mapHistoryItems` (per-message token badge + 📄
    // button), so there is no sub-agent-specific top-of-tab usage badge or
    // per-round snapshot button to drive from here.
  };
  /** Per-(sub-agent)-tab "allow sub-agent question" switch (appended per
   *  AGENTS.md §3.1). Session-scoped + persisted with the tab. When `true`
   *  AND the user is taking over this sub-agent (`kind === "subagent"`), the
   *  transport forwards `allow_question=true` so the backend advertises the
   *  blocking `question` tool to the taken-over sub-agent (its dialog is
   *  reachable because the user has the tab open). Default `false` (omitted) ⇒
   *  `question` stays excluded, matching the autonomous sub-agent tool set.
   *  Only meaningful for `kind === "subagent"` tabs; ignored elsewhere. */
  allowSubAgentQuestion?: boolean;
  /** Per-(main-agent)-tab "allow first-level sub-agents to spawn their own
   *  sub-agents" switch (V2 enhancement; appended per AGENTS.md §3.1).
   *  Per-tab, session-lifetime state. Meaningful ONLY on a depth-0 chat tab
   *  (`kind !== "subagent"`). When `true` the transport forwards
   *  `allow_child_spawn=true`; the backend then grants the `agent` (spawn)
   *  tool to the FIRST-LEVEL sub-agents this main agent spawns — i.e. it
   *  lets a first-level sub-agent create second-level (grand) sub-agents.
   *  It controls ONLY the main agent's direct children, not deeper levels.
   *  Default `false` (omitted) ⇒ first-level sub-agents cannot spawn
   *  (current/historical behaviour — the hard recursion guard). */
  allowChildSpawn?: boolean;
  /** Per-(sub-agent)-tab "allow THIS sub-agent to create sub-agents" switch
   *  (V2 enhancement; appended per AGENTS.md §3.1). Per-tab, session-lifetime
   *  state. Meaningful ONLY when the user is taking over a sub-agent
   *  (`kind === "subagent"`). When `true` the transport forwards
   *  `self_allow_spawn=true`; the backend then advertises the `agent` (spawn)
   *  tool to THIS taken-over sub-agent so it can create its own sub-agents.
   *  Independent of the main agent's `allowChildSpawn` toggle (the two are
   *  separate per-tab switches). Default `false` (omitted) ⇒ this sub-agent
   *  cannot spawn (autonomous sub-agent parity). Ignored on non-subagent
   *  tabs. */
  selfAllowSpawn?: boolean;
   /** Per-session ("this conversation only") temporary tool / SKILL switches
    *  (V2 enhancement; appended per AGENTS.md §3.1). Semantics are
    *  OVERRIDE / DIFF, never a snapshot: only the items the user explicitly
    *  toggled OFF for this session are recorded here. Anything not listed follows
    *  the global default (chat tool set / skill mode) — so a later global change
    *  is auto-picked-up by untouched sessions, and "reset" simply clears the
    *  diff. `undefined` ⇒ no per-session override (the common case; behaviour is
    *  byte-for-byte the pre-feature path).
    *
    *  PERSISTED (keyed store, independent of the open-tabs layout — see
    *  `chatTabsPersistence`): a MAIN-agent tab persists by conversationId; a
    *  SUB-agent tab persists by `sub:<subagentId>` (NOT the parent
    *  conversationId, to avoid colliding with the parent's override). So the
    *  toggles survive closing+reopening the tab AND a full reload. A sub-agent
    *  tab with no saved override of its own INHERITS the parent conversation's
    *  override on first open (a sensible default the user can then change).
    *
    *  Forwarded to the backend as the additive `disabled_tools` /
    *  `disabled_skills` payload fields, which the use case applies per-turn (it
    *  never mutates global forge.config). */
   sessionToolOverride?: SessionToolOverride;
  /** Per-tab "away auto-answer" settings for the blocking `question` tool (V2
   *  enhancement; appended per AGENTS.md §3.1). Lets the user, before stepping
   *  away from the computer, authorise THIS conversation to auto-answer a
   *  pending `question` after a timeout by sending a preset prompt to the model
   *  (so the agentic loop keeps going instead of stalling on the question).
   *
   *  Strictly PER-TAB and DEFAULT-OFF (user requirement): each tab owns its own
   *  switch + timeout + prompt; turning it on in one tab never affects another.
   *  NOT persisted with the tab layout — the "I'm away" state is intentionally
   *  ephemeral (enable before leaving, disable on return), so a reload returns
   *  the tab to OFF and never silently answers on the user's behalf. `undefined`
   *  ⇒ feature OFF (the common case; behaviour is byte-for-byte the pre-feature
   *  path). The countdown / auto-send lives entirely front-end and resolves the
   *  question through the SAME `answerQuestion` path as a manual answer (no
   *  backend protocol change). */
  awayAutoAnswer?: AwayAutoAnswerSettings;
  /** One-shot "don't auto-answer THIS question" suppression pointer (V2
   *  enhancement; appended per AGENTS.md §3.1). Set to the originating
   *  `question` frame id when the user clicks "skip auto-answer for this
   *  question" on the active ChatQuestionCard — the countdown for exactly that
   *  frame is cancelled while leaving `awayAutoAnswer.enabled` untouched (so the
   *  next question still auto-answers). Cleared / overwritten as new questions
   *  arrive. `null` / `undefined` ⇒ nothing suppressed. */
  awayAutoAnswerSuppressedFrameId?: string | null;
  /** True while the backend is compacting (compressing) this conversation's
   *  context and the model call is delayed by it (V2 enhancement; appended per
   *  AGENTS.md §3.1). Driven by the `compaction_progress` frame: set on a
   *  `state:"compressing"` frame and cleared on `state:"done"` (or any terminal
   *  transition). The backend only emits these frames when compaction is slow
   *  enough for the user to notice (≈2s+, typically the Level 2 LLM summary), so
   *  this is `false`/absent for the common fast-compaction case. Drives a
   *  transient "compressing context…" status banner in ChatMessageList.
   *  `undefined` ⇒ not compacting (byte-for-byte the pre-feature path). */
  compacting?: boolean;
  /** Turn-internal LIVE context usage for an ordinary chat tab (V2
   *  enhancement; appended per AGENTS.md §3.1). Set by the `context_usage`
   *  frame at each agentic round boundary WHILE a turn runs, carrying the
   *  round-just-completed's PROVIDER-MEASURED wire prompt size
   *  (State-Truth-First — NOT an estimate). `useComposerCtxBadge` prefers this
   *  over the `GET /context` estimate WHILE it is set, so the main-conversation
   *  context badge tracks the real wire growth (e.g. 33K → 70K) per round
   *  instead of staying frozen until the turn-boundary `/context` re-fetch.
   *  LIFECYCLE: set per round during a turn; CLEARED on the next `/context`
   *  refresh (turn-boundary streaming→idle, owned by
   *  `useComposerCtxBadge.refreshCtx`) so the authoritative probe value
   *  overrides the stale live value (State-Truth-First 铁律 3). The
   *  main-conversation mirror of `subagentMeta.usedTokens`'s live refresh.
   *  `undefined` ⇒ no live reading (byte-for-byte the pre-feature path; the
   *  badge reads `/context` as before). */
  liveContextUsedTokens?: number;
  /** The model's context window paired with `liveContextUsedTokens` (V2
   *  enhancement; appended per §3.1). From the `context_usage` frame's
   *  `context_limit` so the badge renders "~used / window · pct%" without
   *  guessing the window client-side. Set/cleared together with
   *  `liveContextUsedTokens`. `undefined` ⇒ no live reading. */
  liveContextLimit?: number;
  /** Monotonic signal set on a stream END frame carrying
   *  `reason: "budget_exceeded"` (per-conversation `max_budget_tokens` cap hit).
   *  Set to `Date.now()` each time so a watcher fires even for a repeat hit.
   *  The composer (setup context, where i18n + toast + budget refresh live)
   *  watches this to raise a warning toast and re-read the budget snapshot so
   *  the budget badge shows the exhausted state. The store layer has no `t` /
   *  toast instance (frame handlers only patch tab state), so this transient
   *  signal is the seam between the frame reducer and the UI side-effect.
   *  `undefined` ⇒ never hit (byte-for-byte the pre-feature path). */
  budgetExceededSignal?: number;
  /** Budget-decision metadata from the `budget_exceeded` terminal END payload
   *  (`{ used, max, nextMax, raisePct }`). The composer reads this alongside
   *  `budgetExceededSignal` to render the interactive continue/stop dialog: on
   *  "continue" it raises the cap to `nextMax` via `PATCH .../budget` and
   *  resends a continuation turn; on "stop" it just clears. `undefined` when
   *  the cap was never hit (or a legacy END without the metadata). */
  budgetDecision?: {
    used: number;
    max: number;
    nextMax: number;
    raisePct: number;
  };
  /** Monotonic signal set on every terminal stream END frame, marking the one
   *  moment a QGenie model's daily quota counters can have moved. Set to
   *  `Date.now()` so consecutive turns each fire the watcher.
   *
   *  The sidebar gauge (setup context, where i18n + the confirm dialog live)
   *  watches this to re-read the snapshot and, when the allowance in use is at
   *  or past the configured threshold, offer to move subsequent requests to the
   *  other traffic class. Same seam as `budgetExceededSignal`: frame handlers
   *  only patch tab state and must not perform an ~860 ms network call.
   *
   *  Set for every provider (the reducer does not know which is selected); the
   *  watcher is what scopes the work to QGenie models. */
  qgenieQuotaCheckSignal?: number;
}

/** Per-tab away auto-answer settings (see `ChatTab.awayAutoAnswer`). All three
 *  fields are independent: `enabled` gates the countdown, `timeoutSeconds` is
 *  the wait before auto-sending, and `prompt` is the text sent to the model
 *  (empty ⇒ the UI falls back to the current-locale default prompt). */
export interface AwayAutoAnswerSettings {
  /** Master switch for THIS tab. Default `false`. */
  enabled: boolean;
  /** Seconds to wait for a manual answer before auto-sending. Default `180`;
   *  clamped to `[10, 3600]` by the store action. */
  timeoutSeconds: number;
  /** Preset text auto-sent to the model on timeout. Empty string ⇒ the UI uses
   *  the current-locale `chat.awayQuestionAutoAnswer.defaultPrompt`. */
  prompt: string;
}

/** Default away auto-answer settings for a fresh / unset tab: OFF, 180s, empty
 *  prompt (UI substitutes the locale default at send time). */
export const DEFAULT_AWAY_AUTO_ANSWER: AwayAutoAnswerSettings = {
  enabled: false,
  timeoutSeconds: 180,
  prompt: "",
};

/** Inclusive bounds for `AwayAutoAnswerSettings.timeoutSeconds`. */
export const AWAY_AUTO_ANSWER_MIN_SECONDS = 10;
export const AWAY_AUTO_ANSWER_MAX_SECONDS = 3600;

/** Per-session temporary tool / SKILL override (OVERRIDE/DIFF semantics — see
 *  `ChatTab.sessionToolOverride`). Holds ONLY the names the user switched OFF
 *  for this session; everything absent follows the global default. */
export interface SessionToolOverride {
  /** Chat tool names (e.g. "exec" / "agent" / "web_fetch") disabled for THIS
   *  session only. The backend drops these from the advertised tool schemas
   *  for every turn sent from this tab. */
  disabledTools: string[];
  /** Skill ids (directory name = `skill_id`) disabled for THIS session only.
   *  The backend drops these from the per-turn skill catalog (local
   *  `<available_skills>` XML + cloud system-prompt skill list). */
  disabledSkills: string[];
}

/**
 * SubAgentIndexEntry — one row in the per-conversation cache of "all sub-agent
 * sessions that were EVER spawned under this root conversation" (SubAgentRail
 * data source; recovers the β-deleted two-level tab bar).
 *
 * Rationale: Rail must show ALL historical sub-agents for the active main tab
 * — including sub-agents whose tab is NOT currently open in `state.tabs`
 * (closed by the user, or never opened yet in this session). Closing a
 * sub-agent chip (× on rail) removes the sub-agent's TAB from `state.tabs`
 * (memory cleanup) but leaves its INDEX ENTRY here, so the chip stays visible
 * (greyed) and one click re-hydrates via `openSubAgentTab(sid)`.
 *
 * Filled by `_fetchSubAgentIndex(convId)` via
 * `GET /api/chat/conversations/{convId}/subagents`; incrementally kept in sync
 * on live `subagent_start` frames (new spawn) and on any `openSubAgentTab` /
 * `_refreshSubAgentTab` write (fresh status / model / usage). Never deleted on
 * `closeTab` (that's the whole point). Deleted when the main conversation is
 * destroyed.
 *
 * This is NOT the β "nested mirror" model (that was `ChatTab.subAgents[]`,
 * one array PER PARENT TAB, requiring bidirectional sync with `state.tabs`).
 * This is a plain read-only cache of the backend's flat `SubAgentSession`
 * table, keyed by root conversation id. Sub-agent tabs themselves stay in
 * `state.tabs` as first-class top-level entries (β kept that part correct).
 */
export interface SubAgentIndexEntry {
  readonly subagentId: string;
  readonly rootConversationId: string;
  /** Direct-parent sub-agent id (null = direct parent is the main agent). */
  readonly parentSubagentId: string | null;
  /** Recursion depth (1 = first-level, 2 = grand, 3 = great-grand, ...). */
  readonly depth: number;
  readonly title: string;
  /** Last-known backend status ("running" / "done" / "error" / "aborting" /
   *  "idle"). Live "running" status only ever reflects reality via an OPEN
   *  sub-agent tab in `state.tabs`; this field is snapshotted from the backend
   *  at fetch time and updated incrementally, so a closed sub-agent's chip
   *  faithfully shows its final terminal state. */
  readonly status: string;
  readonly owner: string;
  readonly usedTokens?: number;
  readonly budgetTokens?: number;
  readonly modelId?: string;
  readonly modelProvider?: string;
}

export interface ChatTabsState {
  tabs: ChatTab[];
  activeTabId: TabId | null;
  /** Per-root-conversation cache of ALL sub-agent sessions (open OR closed).
   *  See {@link SubAgentIndexEntry}. Absent / [] ⇒ no cache loaded yet (rail
   *  falls back to filtering `state.tabs` — the graceful degradation path). */
  subAgentIndex: Record<string, SubAgentIndexEntry[]>;
  /**
   * Monotonic counter bumped whenever something OUTSIDE a streaming turn
   * invalidates the per-conversation context figures — currently only the
   * `/compact` family of slash commands (`useChatCommands.cmdCompact`).
   *
   * The composer badge re-fetches `GET /context` on `conversationId` /
   * `modelId` / `provider` changes and on every `streaming → idle`
   * transition. A REST-only `/compact` triggers NONE of those, so the badge
   * kept rendering the PRE-compaction figures while the `/compact` reply
   * already reported the new ones (two contradictory numbers on one screen).
   * Watching this counter gives the badge an explicit "re-read now" signal
   * without coupling `useChatCommands` to the composer's local refs.
   */
  ctxRefreshNonce: number;
}
