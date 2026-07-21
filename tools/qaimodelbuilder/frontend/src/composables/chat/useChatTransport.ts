// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatTransport — chat transport with WS-first / SSE-fallback (PR-054).
 *
 * Encapsulates the decision of whether to drive a chat turn over the
 * WebSocket (preferred) or fall back to the SSE endpoint when the WS
 * client gives up after `maxReconnect` failures (api-contract.md §4
 * lifecycle + §3.1 SSE fallback).
 *
 * Responsibilities:
 *   - Wire one tab's transport to the central `useChatTabsStore` so
 *     state-machine transitions happen at exactly one place.
 *   - Hand off a single `prompt` to whichever transport is currently
 *     viable; do NOT attempt both simultaneously.
 *   - Honour the per-tab `AbortController` for SSE cancellation; for
 *     WS the `stop` envelope is used.
 *
 * The transport is *one-shot per turn*: callers invoke `send()` and
 * receive a promise that resolves when the turn terminates (done /
 * error / abort). They MUST NOT call `send()` again until the
 * previous promise has settled.
 */
import { apiSSE, apiJson, type ApiError } from "@/api";
import {
  useChatTabsStore,
  getOrCreateAbortController,
  resetAbortController,
  attachWebSocket,
  clearWebSocket,
  type TabId,
  type ChatTab,
  type ToolParams,
} from "@/stores/chatTabs";
import type {
  ChatStreamFrame,
  ApiErrorPayload,
} from "@/types/streaming";

import {
  useChatWebSocket,
  type ChatWebSocketClient,
  type UseChatWebSocketOptions,
} from "./useChatWebSocket";
import { useChatControlChannel } from "./useChatControlChannel";
import { useConversationsStore } from "@/stores/conversations";
import { useUiStore } from "@/stores/ui";
import { gomasterMode } from "@/composables/useForgeConfig";

export type TransportKind = "ws" | "sse";

export interface UseChatTransportOptions {
  readonly tabId: TabId;
  /** SSE path template; `{conversationId}` is interpolated. */
  readonly ssePathTemplate?: string;
  /** WS options forwarded to `useChatWebSocket`. */
  readonly ws?: UseChatWebSocketOptions;
  /** Force a transport (test injection). */
  readonly forceTransport?: TransportKind;
}

export interface ChatTransport {
  readonly tabId: TabId;
  readonly currentTransport: () => TransportKind;
  /**
   * Whether THIS transport currently has a turn in flight (between `send()`
   * and its terminal `done` / `error` settlement). Cleared on dispose and
   * after every settled turn. Use this to discriminate "transport exists
   * but is idle (leftover from a finished take-over)" from "transport is
   * actively running a turn" — `ChatView.onCancel` reads this to route ⏹
   * to the right backend abort path: an idle take-over transport must NOT
   * absorb a Stop meant for a parent-spawned sub-agent observed in the same
   * tab (otherwise the user-perceived "Stop did nothing" bug returns when a
   * take-over tab is later re-driven by an external sub-agent wake). The
   * call is a plain getter (returns the current value at the moment of the
   * call) — caller should re-read it; do NOT cache.
   */
  readonly isInFlight: () => boolean;
  /**
   * Send one prompt over the active transport. Resolves on `done` /
   * graceful close, rejects on `error` frames or transport failure.
   *
   * `userMessageId` (optional) is the id of the user message that
   * triggered this turn. When the turn fails, the error is associated
   * with that message so ChatMessageList can render a per-message
   * ↻ Retry banner (V1 index.html:685-689 parity).
   */
  send(prompt: string, userMessageId?: string): Promise<void>;
  /** Cancel the in-flight turn. */
  cancel(): void;
  /** Tear down all transports and listeners. */
  dispose(): void;
}

const DEFAULT_SSE_PATH =
  "/api/chat/conversations/{conversationId}/stream";

function interpolatePath(
  template: string,
  vars: Record<string, string>,
): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => {
    const v = vars[key];
    return v === undefined ? `{${key}}` : encodeURIComponent(v);
  });
}

/**
 * Derive the outgoing `tool_mode` + `tool_params` for a turn from the
 * tab's active mode and aggregated tool params (V1 `_toolParamsComputed`
 * parity). Only the keys relevant to the active mode are emitted so the
 * backend renderer receives a clean, mode-scoped dict.
 *
 *   code:      { speed, file_path, repo_url, persona? }
 *   translate: { target_lang }
 *   ppt:       { length }
 *   model-build: { model_path, model_paths, quant_precision, dataset_path }
 *
 * `app-builder` keeps its mode and carries the workbench selection
 * (model / variant / category + last-run summary) so the backend
 * system-prompt builder inlines the right per-Pack SKILL + pack catalog
 * (V1 `toolParamsForChat` parity). The SKILL files / pack catalog
 * themselves are resolved server-side from `tool_mode === "app-builder"`.
 */
export function deriveToolPayload(tab: ChatTab | undefined): {
  toolMode: string | null;
  toolParams: Record<string, unknown> | null;
} {
  if (tab === undefined) {
    return { toolMode: null, toolParams: null };
  }
  const mode = tab.activeMode;
  if (mode === null) {
    return { toolMode: null, toolParams: null };
  }
  const tp: ToolParams = tab.toolParams ?? {};
  let params: Record<string, unknown> | null = null;
  if (mode === "code") {
    // V1 parity (app.js:1812-1817) — always send speed/file_path/repo_url
    // (empty strings included so the backend renderer sees the keys), and
    // persona only when set (cloud models only).
    params = {
      speed: tp.speed ?? "fast",
      file_path: tp.file_path ?? "",
      repo_url: tp.repo_url ?? "",
    };
    if (typeof tp.persona === "string" && tp.persona !== "") {
      params.persona = tp.persona;
    }
  } else if (mode === "translate") {
    params = { target_lang: tp.target_lang ?? "zh-CN" };
  } else if (mode === "ppt") {
    params = { length: tp.length ?? "smart" };
  } else if (mode === "model-build") {
    // V1 `_toolParamsComputed` (app.js:1787-1799) parity — always send
    // the full dict so the backend system-prompt builder has the model
    // path(s), precision and (optional) dataset path in hand.
    params = {
      model_path: tp.model_path ?? "",
      model_paths:
        Array.isArray(tp.model_paths) && tp.model_paths.length > 0
          ? tp.model_paths
          : tp.model_path
            ? [tp.model_path]
            : [],
      quant_precision: tp.quant_precision ?? "fp16",
      dataset_path: tp.dataset_path ?? "",
    };
  } else if (mode === "app-builder") {
    // V1 `useAppBuilder.toolParamsForChat` parity — surface the active
    // workbench selection so the backend per-model SKILL branch resolves
    // the right Pack. Only emit keys that are set (non-empty).
    const out: Record<string, unknown> = {};
    if (typeof tp.selected_model_id === "string" && tp.selected_model_id !== "") {
      out.selected_model_id = tp.selected_model_id;
    }
    if (
      Array.isArray(tp.selected_model_ids) &&
      tp.selected_model_ids.length > 0
    ) {
      out.selected_model_ids = tp.selected_model_ids;
    }
    if (
      typeof tp.selected_model_name === "string" &&
      tp.selected_model_name !== ""
    ) {
      out.selected_model_name = tp.selected_model_name;
    }
    if (typeof tp.category === "string" && tp.category !== "") {
      out.category = tp.category;
    }
    if (typeof tp.variant_id === "string" && tp.variant_id !== "") {
      out.variant_id = tp.variant_id;
    }
    if (typeof tp.last_run_summary === "string" && tp.last_run_summary !== "") {
      out.last_run_summary = tp.last_run_summary;
    }
    params = Object.keys(out).length > 0 ? out : null;
  } else if (mode === "model-hub") {
    // Model Hub — download pre-compiled models from Qualcomm AI Hub and
    // export them to App Builder. No per-mode tool_params are required at
    // launch (the backend feature prompt drives the AI Hub search/download
    // flow), so we emit `tool_mode='model-hub'` with `tool_params=null`.
    // Extend here (e.g. selected AI Hub model id / target device) once the
    // ModeFrameModelHub sub-toolbar surfaces a concrete selection.
    params = null;
  }
  return { toolMode: mode, toolParams: params };
}

/**
 * Derive the per-session ("this conversation only") tool / SKILL override
 * (OVERRIDE/DIFF semantics — see `ChatTab.sessionToolOverride`). Returns the
 * lists of tool names / skill ids the user switched OFF for this session, or
 * empty arrays when there is no override (the common case — the outgoing
 * payload then omits these fields entirely, leaving the backend default tool /
 * skill set unchanged). The backend applies these PER-TURN; it never mutates
 * the global tool-safety config or per-skill forge.config mode.
 */
function deriveSessionOverride(tab: ChatTab | undefined): {
  disabledTools: string[];
  disabledSkills: string[];
} {
  const o = tab?.sessionToolOverride;
  if (o === undefined) {
    return { disabledTools: [], disabledSkills: [] };
  }
  return {
    disabledTools: Array.isArray(o.disabledTools) ? o.disabledTools : [],
    disabledSkills: Array.isArray(o.disabledSkills) ? o.disabledSkills : [],
  };
}

export function useChatTransport(
  options: UseChatTransportOptions,
): ChatTransport {
  const tabId = options.tabId;
  const ssePathTemplate = options.ssePathTemplate ?? DEFAULT_SSE_PATH;
  const store = useChatTabsStore();
  const conversationsStore = useConversationsStore();
  const uiStore = useUiStore();

  // GoMaster chat routing is only valid when the backend wired the conversational
  // ("agent"/"both") link. In "external" mode GoMaster is a one-click optimize
  // task (opened from the mode's panel), NOT a chat — so a chat send must NOT be
  // routed to ``query::gomaster`` (that hint is unbound in external mode and
  // would lock the turn on a failing route). When external, a send in gomaster
  // mode falls through to the user's normally-selected model.
  //
  // `gomasterMode` is the MODULE-LEVEL, i18n-free reactive read from
  // `useForgeConfig.ts` (NOT the full `useForgeConfig()` composable). Importing
  // the composable here would call `useI18n()` at instantiation, which throws
  // outside a Vue `setup` context (tests / some runtime call sites).
  const gomasterChatEnabled = (): boolean =>
    gomasterMode.value === "agent" || gomasterMode.value === "both";

  // Default to WebSocket (root-cause fix for the "6th concurrent session
  // hangs" bug — see `send()`). `forceTransport` (tests / explicit opt-out)
  // still wins. A WS connect failure auto-falls back to SSE via `onGiveUp`.
  let current: TransportKind = options.forceTransport ?? "ws";
  let ws: ChatWebSocketClient | null = null;
  // Which transport actually carries THIS turn. Usually equals `current`,
  // but a discussion turn always runs over SSE even when `current==="ws"`
  // (the WS data plane has no discussion orchestrator), so `cancel()` must
  // target the transport the turn really used — not the default.
  let activeTurnTransport: TransportKind = current;
  let activeReject: ((err: ApiError | Error) => void) | null = null;
  let activeResolve: (() => void) | null = null;
  let inFlight = false;
  // Id of the user message that triggered the in-flight turn (for the
  // per-message ↻ Retry banner — V1 index.html:685-689). Null when the
  // caller did not supply one.
  let currentUserMessageId: string | null = null;

  /** Associate a turn failure with its originating user message so the
   *  UI can surface a per-message error banner + retry button. No-op
   *  for user-initiated aborts. */
  function flagMessageSendError(message: string, code: string | null = null): void {
    if (currentUserMessageId !== null) {
      store.setMessageSendError(tabId, currentUserMessageId, message, code);
    }
  }

  // ── Per-turn client-side timing (QAIZap method — V1 useChat.js:2377) ──
  // The server does not emit a perf object; we derive ttft / total /
  // tool_rounds from observed frame timing and feed it into the store
  // before `confirmDone` commits the assistant message.
  let turnStartMs = 0;
  let turnFirstTokenMs: number | null = null;
  let turnToolRounds = 0;
  // ── Per-round generation-time tracking (B6 fix: output tok/sec) ───────────
  // Each agentic round's CHUNK frames carry a backend-stamped `round_index`
  // (`_stamp_round`, streaming.py:282). We record, per round, the first and
  // last text-chunk arrival time so we can sum ONLY the model's actual
  // generation spans (first→last token of each round) and EXCLUDE the
  // inter-round tool-execution / approval waits. `total_ms − ttft_ms` (the old
  // output_tps denominator) wrongly counted those tool waits, deflating the
  // rate on multi-round turns. Map: round_index → { firstMs, lastMs }.
  const roundGenSpans = new Map<number, { firstMs: number; lastMs: number }>();
  let turnHadSubAgents = false;

  // Debounce handle for the running-time sidebar refresh so a burst of
  // `subagent_start` frames (parallel sub-agents) triggers a single refetch.
  let subAgentSidebarTimer: ReturnType<typeof setTimeout> | null = null;

  // Guards `maybeRefreshAutoTitle` to exactly one poll loop per turn. The
  // auto-title watch now starts as EARLY as possible (right after the
  // conversation id is known on the first round) instead of waiting for the
  // turn to end — a long multi-tool turn must not keep the tab showing
  // "新对话". `handleDone` also calls it as a fallback; this flag makes the
  // double call idempotent.
  let autoTitleStarted = false;

  function nowMs(): number {
    return typeof performance !== "undefined" &&
      typeof performance.now === "function"
      ? performance.now()
      : Date.now();
  }

  function resetTurnTiming(): void {
    turnStartMs = nowMs();
    turnFirstTokenMs = null;
    turnToolRounds = 0;
    roundGenSpans.clear();
    turnHadSubAgents = false;
    autoTitleStarted = false;
  }

  /** Compute the perf summary and push it into the store for the active
   *  turn (no-op if the turn produced no token timing yet). */
  function flushTurnPerf(): void {
    const total = Math.round(nowMs() - turnStartMs);
    const perf: {
      ttft_ms?: number;
      total_ms?: number;
      tool_rounds?: number;
      input_tokens?: number;
      output_tokens?: number;
      input_tps?: number;
      output_tps?: number;
    } = { total_ms: total };
    if (turnFirstTokenMs !== null) {
      perf.ttft_ms = Math.round(turnFirstTokenMs - turnStartMs);
    }
    if (turnToolRounds > 0) {
      perf.tool_rounds = turnToolRounds;
    }

    // ── Token-rate fields (QAIZap method — V1 useChat.js:2349-2389) ─────────
    // The terminal `end` frame's usage was captured into the tab by
    // `store.applyFrame`; read it back here to derive tok/sec. The V2
    // backend usage is OpenAI-shaped (prompt_tokens / completion_tokens),
    // matching V1's `usage.prompt_tokens` / `usage.completion_tokens`.
    //
    // Multi-round agentic turn correctness (per AGENTS.md 🟡🟡 fix):
    // ``usage.prompt_tokens`` is the cross-round SUM (each round's prompt
    // accumulated by streaming.py::_accumulate_usage). For a multi-round
    // turn that SUM is NOT the wire size — it inflates as N×prompt. The
    // backend tail-appends ``last_round_prompt_tokens`` (the LAST round's
    // _extract_usage-corrected true wire size, streaming.py:3404) which is
    // what the user expects to see ("this message's real input size").
    // Single-round turn: ``last_round_prompt_tokens == prompt_tokens`` →
    // no display change. Multi-round turn: SUM is dropped in favor of the
    // last round's real wire size.
    const tab = store.tabs.find((t) => t.id === tabId);
    const usage = tab?.streamingUsage ?? null;
    if (usage !== null) {
      // V1:2351-2356 — input = prompt_tokens, output = completion_tokens.
      // (V2 has no prompt_tokens_details cache fields, so the V1 cache add
      //  terms are 0; the `total_tokens - completion_tokens` fallback is
      //  preserved for parity when prompt_tokens is absent.)
      // V2 fix: prefer last_round_prompt_tokens when present (multi-round
      // agentic turn correctness).
      const inTok =
        usage.last_round_prompt_tokens ??
        usage.prompt_tokens ??
        (typeof usage.total_tokens === "number"
          ? usage.total_tokens - (usage.completion_tokens ?? 0)
          : 0);
      const outTok = usage.completion_tokens ?? 0;
      perf.input_tokens = inTok;
      perf.output_tokens = outTok;

      // ── input tok/sec (B7 fix — round-coherent numerator & denominator) ────
      // The rate = prompt-tokens / prefill-time only has a physical meaning
      // when the numerator (prompt size) and denominator (TTFT) come from the
      // SAME round. `ttft_ms` is ROUND-0's prefill latency (turnStart → first
      // text chunk), so the numerator must be ROUND-0's prompt too. The backend
      // tail-appends `first_round_prompt_tokens` (round-0's _extract_usage-
      // corrected prompt) for exactly this — it is round-coherent with ttft on
      // BOTH single- AND multi-round turns, so we compute input_tps in all
      // cases (no more "multi-round omitted").
      //
      // IMPORTANT: this RATE numerator (`first_round_prompt_tokens`) is a
      // DIFFERENT figure from the `[I] N tokens` TOTAL above (`inTok` =
      // `last_round_prompt_tokens`, the last round's wire). Two distinct
      // round口径, each correct for its purpose: the TOTAL shows "this
      // message's real input size" (last round), the RATE shows "prompt
      // processing speed" (round-0 prompt ÷ round-0 TTFT).
      //
      // Fallback chain for legacy sessions whose usage predates the field:
      // first_round_prompt_tokens → prompt_tokens → (total − completion).
      const rateInTok =
        usage.first_round_prompt_tokens ??
        usage.prompt_tokens ??
        (typeof usage.total_tokens === "number"
          ? usage.total_tokens - (usage.completion_tokens ?? 0)
          : 0);
      if (perf.ttft_ms !== undefined && perf.ttft_ms > 0) {
        perf.input_tps =
          Math.round((rateInTok / (perf.ttft_ms / 1000)) * 10) / 10;
      }

      // ── output tok/sec (B6 fix — exclude inter-round tool-wait time) ────────
      // Old denominator `total_ms − ttft_ms` counted the ENTIRE post-TTFT turn
      // duration, which on a multi-round agentic turn includes the time spent
      // WAITING for tools to execute between rounds — deflating the generation
      // rate. The true denominator is the sum of each round's actual
      // generation span (its first text chunk → its last text chunk),
      // excluding the tool-execution gaps between rounds.
      let genMs = 0;
      for (const span of roundGenSpans.values()) {
        genMs += Math.max(0, span.lastMs - span.firstMs);
      }
      // For a single-round turn (or any turn where only one chunk arrived so
      // first==last ⇒ genMs 0), fall back to the legacy `total − ttft` phase so
      // we never divide by zero and single-round behaviour is preserved.
      const outPhaseMs =
        genMs > 0
          ? genMs
          : perf.ttft_ms !== undefined
            ? Math.max(0, total - perf.ttft_ms)
            : total;
      if (outPhaseMs > 0) {
        perf.output_tps =
          Math.round((outTok / (outPhaseMs / 1000)) * 10) / 10;
      }
    }

    store.setStreamingPerf(tabId, perf);
  }

  function ensureWs(): ChatWebSocketClient {
    if (ws === null) {
      const baseWsOptions = options.ws ?? {};
      ws = useChatWebSocket({
        ...baseWsOptions,
        // Supply the mandatory `/api/chat/ws` upgrade query params
        // (conversation_id + tab_id). The backend declares them REQUIRED;
        // a bare path is rejected with 403 (root-cause of the WS-always-403
        // bug). Resolved fresh on each connect (initial + reconnect) from the
        // live tab so a reconnect carries the same identifiers. The
        // conversation id is guaranteed non-null here because `sendWs`
        // ensures it BEFORE calling `client.open()`; if it is somehow still
        // null (defensive), returning null makes the client give up → SSE
        // fallback rather than opening a query-less 403 URL.
        getConnectParams: () => {
          const liveTab = store.tabs.find((t) => t.id === tabId);
          const convId = liveTab?.conversationId ?? null;
          if (convId === null) {
            return null;
          }
          return { conversationId: convId, tabId: String(tabId) };
        },
      });
    }
    return ws;
  }

  function settleResolve(): void {
    const r = activeResolve;
    activeResolve = null;
    activeReject = null;
    inFlight = false;
    if (r !== null) {
      r();
    }
  }

  function settleReject(err: ApiError | Error): void {
    const r = activeReject;
    activeReject = null;
    activeResolve = null;
    inFlight = false;
    if (r !== null) {
      r(err);
    }
  }

  function storeNetworkRetryActive(): boolean {
    const liveTab = store.tabs.find((t) => t.id === tabId);
    return liveTab !== undefined && liveTab.networkRetry !== null;
  }

  function handleFrame(frame: ChatStreamFrame): void {
    // `network_retry` (backend transient-network auto-retry progress, V2): the
    // backend keeps retrying a connect/timeout failure indefinitely with an
    // escalating backoff (3s → 5s → 10s → 30s …) until the link recovers. It
    // is otherwise SILENT (no chunks) for up to 30s per attempt, so without
    // this the turn looks frozen. Drive the existing `networkRetry` banner so
    // the user sees "网络中断，正在等待恢复后自动重试 (N)…" on BOTH the WS and
    // SSE paths (the older SSE-only client retry loop set this too). Cleared
    // below on the next non-retry frame (a successful retry resumes chunks).
    if (frame.frame_type === "network_retry") {
      const payload = frame.payload as
        | { attempt?: unknown; delay_seconds?: unknown }
        | null;
      const attempt =
        payload !== null && typeof payload.attempt === "number"
          ? payload.attempt
          : 1;
      const delaySeconds =
        payload !== null &&
        typeof payload.delay_seconds === "number" &&
        Number.isFinite(payload.delay_seconds) &&
        payload.delay_seconds >= 0
          ? payload.delay_seconds
          : 0;
      // The backend retries indefinitely (no fixed ceiling), so DO NOT send a
      // `max` — the banner then shows just the attempt number, not "N/N" (the
      // reported "1/1, 2/2, 3/3" oddity). `deadlineMs` lets the banner run a
      // local countdown to the next automatic attempt using the backend's own
      // `delay_seconds` (no backend per-tick frames needed).
      store.setNetworkRetry(tabId, {
        current: attempt,
        delaySeconds,
        deadlineMs: nowMs() + delaySeconds * 1000,
      });
      return;
    }
    // Any other frame means the stream is producing real output again — clear
    // a lingering network-retry banner so it does not stick after recovery.
    if (storeNetworkRetryActive()) {
      store.setNetworkRetry(tabId, null);
    }
    // In-stream `error` frames travel on the SSE `event: message` channel as
    // a StreamFrame (payload `{code, message}`), NOT on the `event: error`
    // terminal envelope. V1 (useChat.js:1433-1436) throws on such frames so
    // the error surfaces as a toast + retryable send-error. The store's
    // `applyFrame` has no `error` case (no-op), so route these frames through
    // the same error path as the `event: error` envelope here, otherwise the
    // turn would silently end "successfully" with an empty bubble.
    if (frame.frame_type === "error") {
      const payload = frame.payload as
        | {
            code?: unknown;
            message?: unknown;
            retry_disposition?: unknown;
            http_status?: unknown;
            status?: unknown;
            request_id?: unknown;
          }
        | null;
      const code =
        payload !== null && typeof payload.code === "string"
          ? payload.code
          : "stream_error";
      const message =
        payload !== null && typeof payload.message === "string"
          ? payload.message
          : "Stream error";
      // Thread the backend contract's extra fields
      // (`{ retry_disposition, ... }`) onto the envelope so the store's
      // `lastError` carries them for the actionable error bubble + sanitized
      // diagnostics. All optional/defensive — an older frame without them
      // degrades to a plain code+message.
      const retryDisposition =
        payload !== null && typeof payload.retry_disposition === "string"
          ? payload.retry_disposition
          : null;
      const httpStatusRaw =
        payload !== null && typeof payload.http_status === "number"
          ? payload.http_status
          : payload !== null && typeof payload.status === "number"
            ? payload.status
            : null;
      const requestId =
        payload !== null && typeof payload.request_id === "string"
          ? payload.request_id
          : null;
      handleErrorEnvelope({
        type: "error",
        code,
        message,
        retryDisposition,
        httpStatus: httpStatusRaw,
        requestId,
      });
      return;
    }
    // Record client-side timing for the perf badge (V1 parity).
    if (frame.frame_type === "chunk") {
      const payload = frame.payload as
        | { text?: unknown; round_index?: unknown }
        | null;
      const hasText =
        payload !== null &&
        typeof payload === "object" &&
        typeof payload.text === "string" &&
        payload.text.length > 0;
      // Read the clock ONCE per text chunk and reuse it for both the turn TTFT
      // and the per-round generation span (avoids a second nowMs() read drifting
      // the two timestamps apart, and keeps deterministic tests exact).
      if (hasText) {
        const t = nowMs();
        if (turnFirstTokenMs === null) {
          turnFirstTokenMs = t;
        }
        // B6: per-round generation-span tracking. Record the first/last text
        // chunk time for THIS round so output_tps can sum just the generation
        // spans and exclude inter-round tool-execution waits. `round_index` is
        // backend-stamped (0-based); fall back to round 0 when absent (legacy /
        // non-agentic single-round turns) so the single-round path is unchanged.
        const ri =
          payload !== null &&
          typeof payload.round_index === "number" &&
          Number.isFinite(payload.round_index) &&
          payload.round_index >= 0
            ? payload.round_index
            : 0;
        const span = roundGenSpans.get(ri);
        if (span === undefined) {
          roundGenSpans.set(ri, { firstMs: t, lastMs: t });
        } else {
          span.lastMs = t;
        }
      }
    } else if (frame.frame_type === "tool_call") {
      turnToolRounds += 1;
    } else if (frame.frame_type === "subagent_start") {
      // A sub-agent was dispatched this turn. Flag it (so `handleDone` refetches
      // the authoritative `subagent_count`) and kick a debounced running-time
      // refresh so the sidebar "expand sub-agents" arrow appears WHILE the
      // sub-agent runs — the backend persists the RUNNING session early, so a
      // refetch now sees `subagent_count > 0`. Debounced so parallel
      // `subagent_start` frames collapse into one refetch.
      turnHadSubAgents = true;
      scheduleSubAgentSidebarRefresh();
    }
    store.applyFrame(tabId, frame);
  }

  /** Debounced refetch of the conversations list so the sidebar surfaces the
   *  sub-agent expand arrow during a running turn. `fetch()` carries the
   *  backend-authoritative `subagent_count` (SQL COUNT over
   *  `chat_subagent_session`), which the optimistic `refreshConversationCounts`
   *  cannot compute from in-memory messages. */
  function scheduleSubAgentSidebarRefresh(): void {
    if (subAgentSidebarTimer !== null) {
      return;
    }
    subAgentSidebarTimer = setTimeout(() => {
      subAgentSidebarTimer = null;
      void conversationsStore.fetch();
    }, 400);
  }

  /** Cancel a pending running-time sidebar debounce (§铁律5: every turn-exit
   *  path — done / EOF / abort / error — must drop the timer so it can't fire
   *  a stray `fetch()` after the turn has unwound). Idempotent. */
  function cancelSubAgentSidebarTimer(): void {
    if (subAgentSidebarTimer !== null) {
      clearTimeout(subAgentSidebarTimer);
      subAgentSidebarTimer = null;
    }
  }

  /** Flush the sub-agent sidebar refresh at a terminal turn boundary: cancel
   *  any pending debounce (it's superseded) and, when the turn actually
   *  spawned sub-agents, refetch once for the authoritative final
   *  `subagent_count`. */
  function flushSubAgentSidebarRefresh(): void {
    cancelSubAgentSidebarTimer();
    if (turnHadSubAgents) {
      void conversationsStore.fetch();
    }
  }

  function handleErrorEnvelope(
    env: ApiErrorPayload & {
      retryDisposition?: string | null;
      httpStatus?: number | null;
      requestId?: string | null;
    },
  ): void {
    store.recordError(tabId, {
      type: env.type,
      code: env.code ?? "stream_error",
      message: env.message,
      retryDisposition: env.retryDisposition ?? null,
      httpStatus: env.httpStatus ?? null,
      requestId: env.requestId ?? null,
    });
    flagMessageSendError(env.message, env.code ?? null);
    // State-Truth-First rule 5: a terminal error also unwinds the turn — drop
    // any pending sub-agent sidebar debounce so it can't fire after settle.
    cancelSubAgentSidebarTimer();
    // Fabricate a minimal Error for the rejected promise.
    const err = new Error(env.message);
    (err as Error & { type?: string; code?: string }).type = env.type;
    (err as Error & { type?: string; code?: string }).code = env.code;
    settleReject(err);
  }

  /**
   * After a turn settles, refresh this conversation's sidebar badge counts
   * (rounds / tool calls) from the tab's in-memory messages so the "Recent
   * Chats" list updates immediately — no full-page reload / list refetch.
   *
   * V1 parity: `useChat.js:560-568` recomputes `round_count` /
   * `tool_call_count` from the in-memory messages in `saveCurrentConversation`
   * (called in the post-turn finally, `useChat.js:2770`) and writes them onto
   * the in-memory conversations entry the sidebar renders directly. V2 had no
   * such step, so badges stayed at the value `fetch()` loaded on mount until a
   * page reload.
   *
   * Counting口径 is aligned with the backend list SQL via the chatTabs
   * `conversationCounts` getter (round_count = persisted user messages;
   * tool_call_count = SUM of per-message tool-call array lengths), so the
   * optimistic value matches what the next GET would compute (no jump).
   *
   * State-Truth-First: we only PATCH an existing conversation entry (the row
   * the backend already created and we upserted on the first message). If the
   * tab has no conversationId yet, or the entry isn't in the list, we skip —
   * the next `fetch()` will carry the authoritative backend counts.
   */
  function refreshConversationCounts(): void {
    const tab = store.tabs.find((t) => t.id === tabId);
    const conversationId = tab?.conversationId ?? null;
    if (conversationId === null) {
      return;
    }
    const existing = conversationsStore.conversations.find(
      (c) => c.id === conversationId,
    );
    if (existing === undefined) {
      return;
    }
    const { round_count, tool_call_count } =
      store.conversationCounts(tabId);
    // Spread the existing summary so title / updated_at / status / meta are
    // preserved (upsert shallow-merges, but spreading keeps the call's intent
    // explicit and type-complete without touching the conversations store).
    conversationsStore.upsert({
      ...existing,
      round_count,
      tool_call_count,
    });
  }

  function handleDone(): void {
    flushTurnPerf();
    store.confirmDone(tabId);
    refreshConversationCounts();
    // When the turn spawned sub-agents, refetch the list so the sidebar's
    // `subagent_count` (and thus the expand arrow) reflects the final
    // authoritative backend count — `refreshConversationCounts` only patches
    // round/tool counts (derivable in-memory), not the sub-agent SQL
    // projection. Also cancels any pending running-time debounce.
    flushSubAgentSidebarRefresh();
    void maybeRefreshAutoTitle();
    settleResolve();
  }

  /**
   * First-round auto-title refresh (V1 main.py:6817-6851 parity).
   *
   * The backend fire-and-forgets a model-summarised title as soon as the
   * first stream frame arrives (apps/api/_chat_title_push) — it only needs
   * the first user message, so it does NOT wait for the (possibly long,
   * multi-tool) turn to finish. This watch mirrors that: it starts polling
   * `GET /api/chat/conversations/{id}` right after the conversation id is
   * known on the first round, so the tab + sidebar titles update seconds
   * after the user sends — not minutes later when the turn ends.
   *
   * Idempotent per turn via `autoTitleStarted`: called both early (at
   * conversation-bind time) and from `handleDone` (fallback for an
   * already-bound first-round conversation); only the first call runs.
   *
   * Only runs on the first round (round_count === 1). Later rounds never
   * regenerate the title (V1 gate); a user-renamed conversation is protected
   * server-side via `meta.title_manual`.
   */
  async function maybeRefreshAutoTitle(): Promise<void> {
    if (autoTitleStarted) {
      return;
    }
    const tab = store.tabs.find((t) => t.id === tabId);
    const conversationId = tab?.conversationId ?? null;
    if (conversationId === null) {
      return;
    }
    // First-round only: the backend only auto-summarises on round 1.
    const { round_count } = store.conversationCounts(tabId);
    if (round_count !== 1) {
      return;
    }
    autoTitleStarted = true;
    const existing = conversationsStore.conversations.find(
      (c) => c.id === conversationId,
    );
    const placeholder = existing?.title ?? null;
    // Poll for the async server-side title. The window must comfortably
    // exceed the backend LLM title timeout (10 s) so a slow summary is still
    // picked up; cumulative ≈ 0.8/2/4/7/11/16/21 s.
    const delaysMs = [800, 1200, 2000, 3000, 4000, 5000, 5000];
    for (const delay of delaysMs) {
      await new Promise((r) => setTimeout(r, delay));
      let res: { id: string; title?: string } | null = null;
      try {
        res = await apiJson<{ id: string; title?: string }>(
          "GET",
          `/api/chat/conversations/${encodeURIComponent(conversationId)}`,
        );
      } catch {
        continue;
      }
      const newTitle = res?.title;
      if (typeof newTitle !== "string" || newTitle === "") {
        continue;
      }
      if (placeholder !== null && newTitle === placeholder) {
        // Not yet updated server-side — keep polling.
        continue;
      }
      applyTitleEverywhere(conversationId, newTitle);
      return;
    }
  }

  /**
   * Write a resolved title into BOTH title sources so the sidebar list and
   * the top tab stay consistent (the manual-rename path in
   * `useConversationRename` already double-writes; this mirrors it for the
   * auto-title path). Guards against an empty/whitespace title.
   */
  function applyTitleEverywhere(
    conversationId: string,
    title: string,
  ): void {
    const trimmed = title.trim();
    if (trimmed === "") {
      return;
    }
    conversationsStore.rename(conversationId, trimmed);
    // Update EVERY tab bound to this conversation (V2 may open the same
    // conversation in multiple tabs), not just the first one found — so all
    // tabs stay consistent with the sidebar.
    store.renameTabsByConversation(conversationId, trimmed);
  }

  function handleApiError(err: ApiError): void {
    if (err.type === "AbortError" || err.code === "client.aborted") {
      // User-initiated cancel.
      store.confirmAbort(tabId);
      settleResolve();
      return;
    }
    store.recordError(tabId, {
      type: err.type,
      code: err.code,
      message: err.message,
    });
    flagMessageSendError(err.message);
    settleReject(err);
  }

  // -------------------------------------------------------------------
  // WS path
  // -------------------------------------------------------------------

  async function sendWs(prompt: string): Promise<void> {
    // Ensure the tab has a backing conversation BEFORE connecting. The
    // `/api/chat/ws` route declares `conversation_id` as a REQUIRED upgrade
    // query param — a brand-new tab (conversationId === null) had nothing to
    // send and was 403'd at the handshake. Creating it here (shared with the
    // SSE path) means the connect URL always carries a valid id. If creation
    // fails, `ensureConversationId` records the error + throws exactly like
    // the SSE path; we propagate the rejection and never open the WS.
    let conversationId: string | null;
    try {
      conversationId = await ensureConversationId(prompt);
    } catch (e) {
      // ensureConversationId already recorded the error + flagged the message.
      return Promise.reject(e as Error);
    }
    if (conversationId === null) {
      // Defensive (ensureConversationId throws on real failure): never open a
      // query-less WS the backend would 403.
      const err = new Error(
        "Cannot open chat WebSocket without a conversation_id.",
      );
      store.recordError(tabId, {
        type: "ConfigurationError",
        code: "client.no_conversation_id",
        message: err.message,
      });
      flagMessageSendError(err.message);
      return Promise.reject(err);
    }
    const client = ensureWs();
    // Re-fetch the tab AFTER ensureConversationId (it calls setConversationId,
    // which replaces the tab object) so the derived payload below reads the
    // live tab.
    const tab = store.tabs.find((t) => t.id === tabId);
    return new Promise<void>((resolve, reject) => {
      activeResolve = resolve;
      activeReject = reject;
      inFlight = true;

      // Fresh AbortController for this turn (not used by WS proper but
      // kept symmetrical for §10.6 invariant — caller code observes it).
      resetAbortController(tabId);
      resetTurnTiming();

      client.open({
        onReady: () => {
          // Flushed automatically by useChatWebSocket once `send` fires.
        },
        onFrame: (frame) => handleFrame(frame),
        onError: (env) => handleErrorEnvelope(env),
        onDone: () => handleDone(),
        onClose: () => {
          // Close after `done`/`error` is normal; nothing to do.
        },
        onGiveUp: () => {
          // WS exhausted retries — switch transport and replay once.
          current = "sse";
          activeTurnTransport = "sse";
          ws = null;
          if (inFlight) {
            // Replay over SSE.
            sendSseInternal(prompt).then(resolve, reject);
            // Reset the state machine resolves to avoid double-settle.
            activeResolve = null;
            activeReject = null;
          }
        },
      });
      const sock = client.rawSocket();
      if (sock !== null) {
        attachWebSocket(tabId, sock);
      }
      const { toolMode, toolParams } = deriveToolPayload(tab);
      // V1 `selectedModelId` parity — forward the tab's selected model so
      // the backend provider-routing stream routes to the owning provider.
      // `qai-default` is the unselected placeholder; omit it.
      // Pro mode override: when the「Pro / 增强」mode is active, force the
      // ``query::mb_pro`` model hint so the backend ProviderRoutingLLMStream
      // routes the turn to the MB Pro session adapter (decision §2.6 — the
      // mode sets the hint, the user-selected dropdown model is irrelevant).
      const modelId =
        tab?.activeMode === "pro"
          ? "query::mb_pro"
          : tab?.activeMode === "gomaster" && gomasterChatEnabled()
            ? "query::gomaster"
            : typeof tab?.modelId === "string" &&
                tab.modelId !== "" &&
                tab.modelId !== "qai-default"
              ? tab.modelId
              : null;
      // SSE-parity advanced fields (mirrors `sendSseInternal`): sub-agent
      // take-over id + per-tab "allow question" switch + sampling overrides.
      // The WS data plane now accepts these (see `_ws.py`), so a WS turn is
      // functionally equivalent to the SSE turn — no feature regression.
      //
      // Sub-agent take-over: when `tab.kind === "subagent"` forward its id +
      // toggles so the backend continues the turn on that sub-agent's
      // context. Otherwise (`kind !== "subagent"`) forward only the main
      // agent's `allowChildSpawn` toggle.
      const isSubAgent = tab?.kind === "subagent";
      const subagentId = isSubAgent
        ? tab?.subagentMeta?.subagentId ?? null
        : null;
      const allowQuestion =
        subagentId !== null && tab?.allowSubAgentQuestion === true;
      // Sub-agent spawn-permission switches (V2 enhancement; SSE parity below).
      // `allowChildSpawn` is meaningful ONLY on a MAIN-agent turn (no subagent
      // id) and grants the first-level sub-agents this turn spawns the ability
      // to create their own (grand) sub-agents. It still lives on the TAB
      // (`tab.allowChildSpawn`) because it's the PARENT main agent's
      // preference, not a property of any one sub-agent.
      // `selfAllowSpawn` is meaningful ONLY on a sub-agent take-over turn and
      // lets the taken-over sub-agent create its own sub-agents.
      const allowChildSpawn = !isSubAgent && tab?.allowChildSpawn === true;
      const selfAllowSpawn = isSubAgent && tab?.selfAllowSpawn === true;
      const mp = tab?.modelParams;
      const sampling =
        mp !== undefined && !mp.useDefaults
          ? {
              temperature: mp.temperature,
              topP: mp.topP,
              maxTokens: mp.maxTokens > 0 ? mp.maxTokens : null,
            }
          : { temperature: null, topP: null, maxTokens: null };
      const sessionOverride = deriveSessionOverride(tab);
      client.send(prompt, conversationId ?? undefined, {
        toolMode,
        toolParams,
        modelId,
        subagentId,
        allowQuestion,
        allowChildSpawn,
        selfAllowSpawn,
        temperature: sampling.temperature,
        topP: sampling.topP,
        maxTokens: sampling.maxTokens,
        disabledTools: sessionOverride.disabledTools,
        disabledSkills: sessionOverride.disabledSkills,
        // UI language so the backend localizes its feature-mode system-prompt
        // framing to the language the user selected (additive; §3.1).
        locale: uiStore.locale,
      });
      // Start the first-round auto-title watch immediately (see SSE path):
      // the backend summarises from the first user message right away, so we
      // poll now instead of waiting for the turn to end. Idempotent +
      // first-round-gated inside; fire-and-forget.
      void maybeRefreshAutoTitle();
    });
  }

  function cancelWs(): void {
    if (ws !== null) {
      ws.stop();
    }
    store.requestCancel(tabId);
    // Stop over the CONTROL plane, not (only) the data-plane WS. The data-plane
    // receive loop is strictly serial: while a turn streams it is blocked inside
    // `_run_one_turn` and never reads the next frame, so a `{type:"stop"}` sent
    // on the data WS (via `ws.stop()` above) is NOT read until the turn already
    // ended — i.e. it cannot abort an in-flight turn. The control channel is an
    // independent WS (`/api/chat/control`) that hits the SAME
    // `stop_chat_use_case` immediately, so it actually sets the abort Event for
    // the running turn (this is why "Stop" appeared to do nothing for long
    // MB Pro / query-service turns). Fall back to the locked REST stop endpoint
    // when the control WS is not ready. Mirrors `cancelSse`.
    const sentStopOverWs = useChatControlChannel().sendStop(
      tabId,
      "user_requested",
    );
    if (!sentStopOverWs) {
      void apiJson("POST", "/api/chat/stop", {
        tab_id: tabId,
        reason: "user_requested",
      }).catch(() => {
        // ignore — cancel is local-authoritative; the data-plane stop already
        // requested cancellation. A failed stop call must not break UX.
      });
    }
    // The server will close after `done`/error envelope from the stop
    // path; if it doesn't (e.g. WS hung), close locally after a short
    // grace period.
    setTimeout(() => {
      const tab = store.tabs.find((t) => t.id === tabId);
      if (tab !== undefined && tab.status === "aborting") {
        if (ws !== null) {
          ws.close(1000, "client cancel");
        }
        clearWebSocket(tabId);
        store.confirmAbort(tabId);
        settleResolve();
      }
    }, 1500);
  }

  // -------------------------------------------------------------------
  // Network retry (V1 useChat.js:2146-2313 parity)
  // -------------------------------------------------------------------
  // V1 retried the SSE fetch up to MAX_NETWORK_RETRIES times when the
  // upstream connection dropped, surfacing a `chat.networkInterrupted`
  // banner between attempts. We mirror the constants here so the user
  // sees the same recovery behaviour over flaky networks.
  const MAX_NETWORK_RETRIES = 5;
  /** Backoff between attempts (ms). V1 used `1000` (useChat.js:2310);
   *  we keep the same 1s base so the user perceives the same recovery
   *  rhythm. The ceiling guards a degenerate slow-recover case. */
  const NETWORK_RETRY_BASE_MS = 1000;
  const NETWORK_RETRY_MAX_MS = 2000;

  /** Heuristic: is this ApiError a transient network drop (vs a real
   *  server / client error)? V1 `_isNetworkError` (useChat.js:2205-2224)
   *  matched on TypeError + a set of fetch / network message
   *  substrings; in V2 the api layer already collapses those into
   *  `code === "client.network_error"` (errors.ts:309-315) so the
   *  check is straightforward. AbortErrors are excluded — those are
   *  user-initiated and must NOT trigger a retry. */
  function isNetworkError(err: ApiError | Error): boolean {
    const code = (err as ApiError).code;
    if (code === "client.aborted") return false;
    if (code === "client.network_error") return true;
    // Defensive: a TypeError surfacing through a non-`apiSSE` path
    // (e.g. WS exhaustion replaying SSE here) may still arrive as a
    // raw network failure.
    if (err.name === "TypeError") {
      const msg = (err.message ?? "").toLowerCase();
      return (
        msg.includes("failed to fetch") ||
        msg.includes("network error") ||
        msg.includes("networkerror") ||
        msg.includes("load failed")
      );
    }
    return false;
  }

  /** Sleep with abort awareness — resolves either when the timer fires
   *  or when the AbortController for this tab triggers, so a user
   *  pressing Stop mid-backoff doesn't have to wait for the timer. */
  function abortableDelay(
    ms: number,
    signal: AbortSignal,
  ): Promise<void> {
    return new Promise((resolve) => {
      if (signal.aborted) {
        resolve();
        return;
      }
      const timer = window.setTimeout(() => {
        signal.removeEventListener("abort", onAbort);
        resolve();
      }, ms);
      function onAbort(): void {
        window.clearTimeout(timer);
        signal.removeEventListener("abort", onAbort);
        resolve();
      }
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  // -------------------------------------------------------------------
  // Shared: ensure the tab has a backing conversation (V1 parity)
  // -------------------------------------------------------------------

  /**
   * Ensure this tab is bound to a backend conversation, creating one on the
   * fly for a brand-new tab. Returns the conversation id, or `null` only when
   * the tab already had one but it is somehow falsy (never in practice).
   *
   * Extracted verbatim from the SSE path so BOTH transports create the
   * conversation identically:
   *   - SSE needs the conversation row before opening the stream (the stream
   *     route requires it to exist).
   *   - WS needs the conversation id BEFORE connecting, because the
   *     `/api/chat/ws` route declares `conversation_id` as a REQUIRED upgrade
   *     query param (a bare connect is 403'd). A brand-new WS tab had no id to
   *     send → 403; ensuring it here fixes that.
   *
   * Behaviour (unchanged from the former inline SSE block): POST
   * `/api/chat/conversations` with one transient retry; on success
   * `setConversationId` + sidebar `upsert` + seed the top-tab title via
   * `renameTabsByConversation`; on failure record a `client.no_conversation_id`
   * error, flag the user message, and THROW (the caller must not proceed).
   */
  async function ensureConversationId(prompt: string): Promise<string | null> {
    const tab = store.tabs.find((t) => t.id === tabId);
    const existing = tab?.conversationId ?? null;
    if (existing !== null) {
      return existing;
    }
    // V1 parity (useChat.js:1940-1946 + main.py:6625): in V1 the conversation
    // id is client-supplied and sending is NEVER blocked by a persistence
    // hiccup. V2's stream route requires the conversation row to exist first,
    // so we create it here — but a single transient failure must not turn the
    // whole send into a dead-end `client.no_conversation_id`. Retry once on a
    // transient error before giving up (the root backpressure cause is fixed
    // at the bus layer; this guards against any remaining transient blip).
    let res:
      | {
          id: string;
          title?: string;
          status?: string;
          created_at?: string;
          updated_at?: string;
          message_count?: number;
        }
      | null = null;
    let lastErr: unknown = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        res = await apiJson<{
          id: string;
          title?: string;
          status?: string;
          created_at?: string;
          updated_at?: string;
          message_count?: number;
        }>(
          "POST",
          "/api/chat/conversations",
          { title: prompt.slice(0, 80) || "New Chat" },
        );
        break;
      } catch (e) {
        lastErr = e;
        if (attempt === 0) {
          // brief backoff before the single retry
          await new Promise((r) => setTimeout(r, 200));
        }
      }
    }
    if (res === null) {
      const err = new Error(
        "Cannot stream over SSE without a conversation_id.",
      );
      void lastErr;
      store.recordError(tabId, {
        type: "ConfigurationError",
        code: "client.no_conversation_id",
        message: err.message,
      });
      flagMessageSendError(err.message);
      throw err;
    }
    const conversationId = res.id;
    // Link the conversation_id back to the tab. Use the store mutator
    // (NOT a direct `tab.conversationId = …`): `_patchTab` REPLACES the tab
    // object on every patch (chatTabs.ts:1481-1485), so a direct mutation on
    // the captured `tab` reference is lost the next time a streaming frame
    // patches the tab — leaving `store.tabs[*].conversationId === null`.
    // That broke first-round auto-title: `maybeRefreshAutoTitle` updated the
    // sidebar (closure `conversationId` is fine) but `renameTabsByConversation`
    // found no tab whose `conversationId` matched, so the top tab kept showing
    // "新对话". Persisting via `setConversationId` keeps the binding on the
    // live (post-patch) tab object.
    store.setConversationId(tabId, conversationId);
    // C-2 (V1 useChat.js:536-557): immediately reflect the new
    // conversation in the shared sidebar list (no reload needed).
    const nowIso = new Date().toISOString();
    const seedTitle = res.title ?? (prompt.slice(0, 80) || "New Chat");
    conversationsStore.upsert({
      id: res.id,
      title: seedTitle,
      status: res.status ?? "active",
      created_at: res.created_at ?? nowIso,
      updated_at: res.updated_at ?? nowIso,
      message_count: res.message_count ?? 0,
    });
    // 图二 fix — tab title same-source (AGENTS.md 判据2 V1 parity): seed the
    // TOP TAB's title from the conversation at creation time, exactly like
    // the sidebar above, so the tab shows the first message immediately
    // instead of the "新对话" fallback. The async first-round auto-title
    // (`maybeRefreshAutoTitle`) still overrides this with the model-
    // summarised title later if it differs. Previously the tab title was
    // ONLY written by that poll, which skipped the write whenever the
    // server title equalled the placeholder (short message / local-model
    // fallback) — so the tab stayed blank forever. V1 had a single title
    // source (sidebar list); V2's separate tab title must be seeded here.
    store.renameTabsByConversation(conversationId, seedTitle);
    return conversationId;
  }

  // -------------------------------------------------------------------
  // SSE path
  // -------------------------------------------------------------------

  async function sendSseInternal(prompt: string): Promise<void> {
    resetTurnTiming();

    // Auto-create a conversation if the tab doesn't have one yet (shared with
    // the WS path so both transports create it identically).
    const conversationId = await ensureConversationId(prompt);
    if (conversationId === null) {
      // Defensive: ensureConversationId throws on failure, so this is only
      // reachable if a tab somehow has a falsy-but-present id. Treat as the
      // former dead-end.
      const err = new Error(
        "Cannot stream over SSE without a conversation_id.",
      );
      store.recordError(tabId, {
        type: "ConfigurationError",
        code: "client.no_conversation_id",
        message: err.message,
      });
      flagMessageSendError(err.message);
      throw err;
    }
    // Start the first-round auto-title watch NOW (conversation id is known,
    // user message already pushed) — not at `handleDone`. The backend
    // summarises the title from the first user message right away, so polling
    // can begin immediately and the tab/sidebar update seconds after send
    // rather than after a long multi-tool turn. Idempotent + first-round-gated
    // inside `maybeRefreshAutoTitle`; fire-and-forget so it never blocks send.
    void maybeRefreshAutoTitle();
    const path = interpolatePath(ssePathTemplate, {
      conversationId,
    });
    // T2.6-A: surface tool-mode + sampling params via additive query
    // params (refactor-plan §3.1 — new query keys are allowed; the SSE
    // route reads them when present and ignores them otherwise).
    const tabSnapshot = store.tabs.find((t) => t.id === tabId);
    const qs: string[] = [
      `tab_id=${encodeURIComponent(tabId)}`,
      `prompt=${encodeURIComponent(prompt)}`,
    ];
    const { toolMode, toolParams } = deriveToolPayload(tabSnapshot);
    if (toolMode !== null) {
      qs.push(`tool_mode=${encodeURIComponent(toolMode)}`);
    }
    // V1 `selectedModelId` parity (useChat.js / useModels.js): forward the
    // tab's selected model so the backend provider-routing LLM stream routes
    // the turn to the owning cloud / local provider. Without it the backend
    // falls back to its default endpoint (`[no LLM endpoint configured]`).
    // `qai-default` is the unselected placeholder — omit it so the backend
    // keeps its default behaviour.
    const modelId = tabSnapshot?.modelId;
    if (tabSnapshot?.activeMode === "pro") {
      // Pro mode override (decision §2.6): force the ``query::mb_pro`` hint so
      // the backend routes the turn to the MB Pro session adapter regardless
      // of the user-selected dropdown model.
      qs.push(`model_id=${encodeURIComponent("query::mb_pro")}`);
    } else if (tabSnapshot?.activeMode === "gomaster" && gomasterChatEnabled()) {
      // GoMaster CHAT override (agent/both mode only): force ``query::gomaster``
      // so the backend routes the turn to the GomasterSessionAdapter. In
      // "external" mode GoMaster is a one-click optimize task, not a chat, so we
      // do NOT set this hint — a send falls through to the user's normal model.
      qs.push(`model_id=${encodeURIComponent("query::gomaster")}`);
    } else if (
      typeof modelId === "string" &&
      modelId !== "" &&
      modelId !== "qai-default"
    ) {
      qs.push(`model_id=${encodeURIComponent(modelId)}`);
    }
    // UI language so the backend localizes its feature-mode system-prompt
    // framing to the language the user selected (additive; §3.1).
    qs.push(`locale=${encodeURIComponent(uiStore.locale)}`);
    if (toolParams !== null) {
      // Backend SSE route accepts `tool_params` as a JSON-encoded query
      // param (additive — refactor-plan §3.1 allows new query keys).
      qs.push(
        `tool_params=${encodeURIComponent(JSON.stringify(toolParams))}`,
      );
    }
    const mp = tabSnapshot?.modelParams;
    if (mp !== undefined && !mp.useDefaults) {
      qs.push(`temperature=${encodeURIComponent(String(mp.temperature))}`);
      qs.push(`top_p=${encodeURIComponent(String(mp.topP))}`);
      if (mp.maxTokens > 0) {
        qs.push(`max_tokens=${encodeURIComponent(String(mp.maxTokens))}`);
      }
    }
    // Sub-agent takeover (V2 enhancement; additive query param — refactor-plan
    // §3.1 allows new query keys). When the active tab is a sub-agent take-over
    // tab (`kind === "subagent"`) forward its id so the backend continues the
    // turn on that sub-agent's context instead of the parent conversation's.
    // Omitted for ordinary chat tabs (`kind !== "subagent"`).
    const sseIsSubAgent = tabSnapshot?.kind === "subagent";
    const subagentId = sseIsSubAgent
      ? tabSnapshot?.subagentMeta?.subagentId ?? null
      : null;
    if (subagentId !== null) {
      qs.push(`subagent_id=${encodeURIComponent(subagentId)}`);
      // Per-tab "allow sub-agent question" switch (additive query param —
      // refactor-plan §3.1). Default off ⇒ omitted ⇒ the backend excludes the
      // blocking `question` tool from the taken-over sub-agent (autonomous
      // sub-agent parity). Only forwarded (as `allow_question=true`) on a
      // sub-agent take-over path AND when the user enabled the toggle.
      if (tabSnapshot?.allowSubAgentQuestion === true) {
        qs.push("allow_question=true");
      }
      // Per-(sub-agent)-tab "allow THIS sub-agent to create sub-agents" switch
      // (additive query param — refactor-plan §3.1). Take-over path ONLY.
      // Default off ⇒ omitted ⇒ the backend keeps this sub-agent unable to
      // spawn (autonomous sub-agent parity). When on, forwarded as
      // `self_allow_spawn=true` so the backend advertises the `agent` tool to
      // the taken-over sub-agent. Independent of `allow_child_spawn` below.
      if (tabSnapshot?.selfAllowSpawn === true) {
        qs.push("self_allow_spawn=true");
      }
    } else if (tabSnapshot?.allowChildSpawn === true) {
      // Per-(main-agent)-tab "allow first-level sub-agents to spawn their own
      // sub-agents" switch (additive query param — refactor-plan §3.1). MAIN
      // agent path ONLY (no subagent id). Default off ⇒ omitted ⇒ the
      // first-level sub-agents this turn spawns cannot create grand sub-agents
      // (historical hard recursion guard). When on, forwarded as
      // `allow_child_spawn=true` so the backend grants the `agent` tool to the
      // sub-agents this main agent spawns. Independent of `self_allow_spawn`.
      // Still lives on the TAB (it's the parent main agent's preference).
      qs.push("allow_child_spawn=true");
    }
    // Multi-Agent discussion (block-5; additive query params — refactor-plan
    // §3.1 allows new query keys). When this tab's discussion mode is ON the
    // backend routes the turn through `OrchestrateDiscussionUseCase` (multiple
    // named agents speak in turn, each frame stamped with `sender_id`). When the
    // user "called on" a participant, forward its id as `pinned_speaker` so the
    // selector lets that participant speak first. Omitted for ordinary chat tabs
    // (discussion off) → unchanged single-agent behaviour.
    if (tabSnapshot?.discussion?.isDiscussion === true) {
      qs.push("discussion=true");
      const pinned = tabSnapshot.pinnedSpeaker;
      if (typeof pinned === "string" && pinned !== "") {
        qs.push(`pinned_speaker=${encodeURIComponent(pinned)}`);
        // A "call-on" applies to ONE turn only — clear it now that the qs has
        // captured it, so a follow-up turn does not re-pin the same speaker.
        store.setPinnedSpeaker(tabId, null);
      }
    }
    // Per-session ("this conversation only") tool / SKILL override (V2
    // enhancement; additive query params — refactor-plan §3.1). JSON-encoded
    // arrays of the tool names / skill ids the user switched OFF for this
    // session. Omitted entirely when empty so a session without any override
    // sends the byte-identical pre-feature URL (backend default tool/skill set
    // unchanged). The backend applies these per-turn (never mutates global
    // config).
    const sessionOverride = deriveSessionOverride(tabSnapshot);
    if (sessionOverride.disabledTools.length > 0) {
      qs.push(
        `disabled_tools=${encodeURIComponent(JSON.stringify(sessionOverride.disabledTools))}`,
      );
    }
    if (sessionOverride.disabledSkills.length > 0) {
      qs.push(
        `disabled_skills=${encodeURIComponent(JSON.stringify(sessionOverride.disabledSkills))}`,
      );
    }
    const url = `${path}?${qs.join("&")}`;

    // ── Network-retry loop (V1 useChat.js:2227-2313 parity) ───────────
    // Each iteration calls `apiSSE` once with a fresh AbortController.
    // A network drop (`client.network_error`) below MAX_NETWORK_RETRIES
    // surfaces a banner via `store.setNetworkRetry({current,max})` and
    // retries after a short backoff. User-initiated aborts (`client.aborted`)
    // bail out immediately without retrying. Real errors propagate via
    // `recordError` and break the loop.
    let attempt = 0;
    // State-Truth-First (root-cause guard): track whether THIS turn's stream
    // settled via an explicit `done`/`error` event. The EOF fall-through guard
    // below must NOT fire `confirmDone` based solely on the tab's GLOBAL
    // `status === "streaming"`: after a `done` event the queue auto-dequeue
    // (ChatView's streaming→idle watcher) can start the NEXT turn and flip the
    // tab back to `streaming` while this turn's `apiSSE` is still unwinding in
    // its `finally { await reader.cancel() }`. Without this flag the just-
    // finished turn would then wrongly `confirmDone()` the NEW turn (empty
    // bubble, `total ≈ 0.01s`). The flag is per-turn (reset each `send`).
    let turnSettledByEvent = false;
    while (true) {
      const controller = resetAbortController(tabId);
      try {
        await apiSSE(
          url,
          {
            onMessage: (data) => {
              // chat SSE `event: message` carries a StreamFrame projection
              const frame = data as ChatStreamFrame;
              if (
                frame !== null &&
                typeof frame === "object" &&
                typeof frame.frame_type === "string"
              ) {
                handleFrame(frame);
              }
            },
            onError: (err) => {
              turnSettledByEvent = true;
              handleApiError(err);
            },
            onDone: () => {
              turnSettledByEvent = true;
              handleDone();
            },
          },
          { signal: controller.signal },
        );
        // If the loop exited without onDone (e.g. EOF), make sure the
        // promise still settles — confirmDone is idempotent.
        //
        // Guard on `turnSettledByEvent` (NOT just the tab status): a `done`
        // event already settled this turn via `handleDone`, but the queue
        // auto-dequeue may have started the NEXT turn (flipping the tab back
        // to `streaming`) while this `apiSSE` was unwinding. Without this
        // flag the finished turn would re-`confirmDone` the new turn here,
        // committing an empty bubble. Only the genuine EOF case (loop ended
        // with neither `done` nor `error`) reaches the fall-through.
        const finalTab = store.tabs.find((t) => t.id === tabId);
        if (
          !turnSettledByEvent &&
          finalTab !== undefined &&
          finalTab.status === "streaming"
        ) {
          flushTurnPerf();
          store.confirmDone(tabId);
          // Mirror handleDone's sidebar-badge refresh for the EOF path so
          // rounds / tool-call counts update immediately (V1 parity).
          refreshConversationCounts();
          // Mirror handleDone's sub-agent count refresh on the EOF path too:
          // a turn that spawned sub-agents but ended via EOF (no explicit
          // `done`) must still refresh the authoritative `subagent_count`.
          flushSubAgentSidebarRefresh();
          // Mirror handleDone's first-round auto-title refresh too, so a
          // stream that ends via EOF (no explicit `done` event) still picks
          // up the model-summarised title for both the sidebar and the tab.
          void maybeRefreshAutoTitle();
        } else {
          // Not the genuine-EOF branch (already settled / next turn started):
          // still cancel any pending sub-agent sidebar debounce so a stale
          // timer can't fire a redundant fetch after the turn unwound.
          cancelSubAgentSidebarTimer();
        }
        // Successful turn (or graceful EOF) — clear any retry banner
        // that survived a recovered attempt and exit the loop.
        store.setNetworkRetry(tabId, null);
        return;
      } catch (cause) {
        // apiSSE rejects on `event: error` (already handled above) and on
        // network failures.
        const e = cause as ApiError;
        if (e.type === "AbortError" || e.code === "client.aborted") {
          // User-initiated cancel — never retry.
          store.setNetworkRetry(tabId, null);
          store.confirmAbort(tabId);
          // §铁律5 (异常退出路径必须兜底): drop any pending sub-agent sidebar
          // debounce so an aborted turn can't fire a stray fetch afterwards.
          cancelSubAgentSidebarTimer();
          return;
        }

        if (isNetworkError(e) && attempt < MAX_NETWORK_RETRIES) {
          // Surface the banner BEFORE the backoff so the user sees the
          // counter advance immediately (V1 useChat.js:2270-2292).
          attempt += 1;
          store.setNetworkRetry(tabId, {
            current: attempt,
            max: MAX_NETWORK_RETRIES,
          });
          const delay = Math.min(
            NETWORK_RETRY_BASE_MS * attempt,
            NETWORK_RETRY_MAX_MS,
          );
          // Wait either for backoff to elapse or the user to press Stop.
          await abortableDelay(delay, controller.signal);
          // If the user aborted during the backoff window, bail out.
          if (controller.signal.aborted) {
            store.setNetworkRetry(tabId, null);
            store.confirmAbort(tabId);
            return;
          }
          // Loop around for the next attempt.
          continue;
        }

        // Real error OR retries exhausted — clear banner, record, throw.
        store.setNetworkRetry(tabId, null);
        // State-Truth-First self-heal (AGENTS.md §🔴 铁律 1): a stale
        // `conversationId` restored from localStorage can be absent from a
        // fresh/empty backend DB (new machine without a copied `/data`). The
        // stream route then rejects with `chat.conversation_not_found`. Reset
        // the tab to a blank conversation so the user's *next* send (or the
        // Retry button) auto-creates a fresh conversation instead of being
        // permanently dead-ended on the same dead id (V1 parity: sending is
        // never blocked by a stale id). Only on a genuine not-found — never on
        // a transient network/5xx error (handled by the retry branch above).
        if (e.code === "chat.conversation_not_found" || e.status === 404) {
          store.clearConversationId(tabId);
        }
        const finalTab = store.tabs.find((t) => t.id === tabId);
        if (finalTab !== undefined && finalTab.status !== "error") {
          store.recordError(tabId, {
            type: e.type ?? "Error",
            code: e.code ?? "client.unknown",
            message: e.message ?? String(cause),
          });
        }
        flagMessageSendError(e.message ?? String(cause));
        throw cause;
      }
    }
  }

  function cancelSse(): void {
    const ctrl = peekAbortController();
    if (ctrl !== null) {
      try {
        ctrl.abort();
      } catch {
        // ignore
      }
    }
    store.requestCancel(tabId);
    // H-6: unlike V1 (where the agentic loop ran client-side, so a
    // client abort was enough), the V2 backend drives the tool loop
    // server-side. Aborting only the SSE fetch can leave the backend
    // turn running. Best-effort tell the server to stop this tab's
    // in-flight turn.
    //
    // Control-plane fast path (same root-cause as the answer hang): prefer the
    // page-global control WebSocket, which bypasses the HTTP/1.1 6-connection
    // pool that SSE data streams saturate. Fall back to the locked REST
    // endpoint (POST /api/chat/stop body { tab_id } -> { aborted, reason };
    // TestClient-verified) when the WS is not ready. Both hit the SAME
    // server-side `stop_chat_use_case` with identical idempotency.
    // Fire-and-forget; never block or surface errors on cancel.
    const sentStopOverWs = useChatControlChannel().sendStop(
      tabId,
      "user_requested",
    );
    if (!sentStopOverWs) {
      void apiJson("POST", "/api/chat/stop", {
        tab_id: tabId,
        reason: "user_requested",
      }).catch(() => {
        // ignore — cancel is local-authoritative; the SSE abort already
        // detached the client. A failed stop call must not break UX.
      });
    }
    // Fallback (parity with cancelWs): normally the AbortError propagates to
    // the SSE read loop's catch which calls confirmAbort() → idle. But if the
    // stream is half-dead / the abort never surfaces, the tab would stay stuck
    // in `aborting`, leaving the composer's stop button frozen (it reverts to
    // "send" only at `idle`). Force the transition after a short grace period
    // if we're still `aborting`.
    setTimeout(() => {
      const tab = store.tabs.find((t) => t.id === tabId);
      if (tab !== undefined && tab.status === "aborting") {
        store.confirmAbort(tabId);
        settleResolve();
      }
    }, 1500);
  }

  function peekAbortController(): AbortController | null {
    // local helper using exported store helpers — lazy bind.
    return getOrCreateAbortController(tabId);
  }

  // -------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------

  async function send(prompt: string, userMessageId?: string): Promise<void> {
    if (inFlight) {
      throw new Error("Transport already has an in-flight turn.");
    }
    currentUserMessageId = userMessageId ?? null;
    // Pre-open the page-global control-plane WebSocket the moment a turn
    // starts streaming (root-cause fix for the mid-turn "inject" degrading to
    // the send-queue): the control WS is what carries `inject` (and `stop` /
    // `answer`) frames, but it was previously opened only lazily AT the instant
    // of a control action — so the FIRST inject click fired while the WS was
    // still completing its `hello` handshake (`state === "connecting"`),
    // `sendInject` returned false, and the text fell back to the queue. Opening
    // it here means it is `ready` well before the user can click inject, so the
    // injection actually reaches the live run's inter-round seam. Idempotent
    // (the channel is a page-global singleton); `stop` / `answer` still keep
    // their REST fallback, so this only ADDS readiness, never changes behaviour.
    useChatControlChannel().ensureOpen();
    // Transport selection (root-cause fix for the "6th concurrent session
    // hangs" bug): the browser caps a single HTTP/1.1 origin at 6 concurrent
    // connections, and each SSE turn is a long-lived `fetch` holding one for
    // the whole (minutes-long) agentic turn. With `/api/events` taking one,
    // only 5 concurrent chat SSE streams fit — the 6th+ session's `fetch` is
    // queued by the browser and never reaches the backend. WebSockets are NOT
    // subject to that pool, so default to WS for concurrency headroom.
    //
    // Discussion mode still routes over SSE: it is driven by a separate
    // multi-agent orchestrator (`_stream_discussion_sse`) that the WS data
    // plane does not implement. Discussion turns are rare and not the
    // many-parallel-sessions scenario, so keeping them on SSE avoids a
    // feature regression without reintroducing the connection-budget problem.
    const tab = store.tabs.find((t) => t.id === tabId);
    const isDiscussion = tab?.discussion?.isDiscussion === true;
    if (current === "ws" && !isDiscussion) {
      activeTurnTransport = "ws";
      return sendWs(prompt);
    }
    activeTurnTransport = "sse";
    return sendSseInternal(prompt);
  }

  function cancel(): void {
    if (activeTurnTransport === "ws") {
      cancelWs();
    } else {
      cancelSse();
    }
  }

  function dispose(): void {
    if (ws !== null) {
      ws.close();
      ws = null;
    }
    clearWebSocket(tabId);
    activeResolve = null;
    activeReject = null;
    inFlight = false;
  }

  return {
    tabId,
    currentTransport: () => current,
    isInFlight: () => inFlight,
    send,
    cancel,
    dispose,
  };
}
