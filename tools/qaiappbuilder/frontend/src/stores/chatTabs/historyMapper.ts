// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * History-message mapping for the chat store (cohesion split, ARCH-1).
 *
 * Maps raw `MessageItem` rows from `GET …/messages` into `ChatMessage`.
 * Shared by `loadHistoryMessages` (newest page) and `loadMoreMessages`
 * (older pages). Moved verbatim from `chatTabs.ts` — this is a pure
 * function with no Pinia / reactive dependency, so it lives outside the
 * store body.
 *
 * The V2 messages wire schema carries
 * `{id, role, text, created_at, parent_id, tool_calls?, usage?, meta?}`.
 * Token `usage` (P1-4) is persisted server-side and re-emitted here so the
 * per-message token line survives a page reload. The V1-parity `meta`
 * envelope (P1: persisted via Message.meta + migration 021) carries the
 * remaining render extras the live stream produced — `request_id` (prompt
 * snapshot button), `perf` (perf line: ttft_ms / total_ms / token counts),
 * `subAgentBlocks` (sub-agent fold blocks) — so they are rehydrated here too,
 * matching V1 (`backend/history_store.py:_row_to_message` promoted the same
 * fields out of `messages.meta`). Image previews need no rehydration: the
 * upload URL lives inside the persisted message `content` markdown
 * (`![name](/api/images/…)`) and `ChatMessageList.extractImages` re-parses
 * it on render. Tool-truncation badges / full-output tabs rehydrate via the
 * `tool_calls[]` entries' `output` / `outputSize` / `truncated` fields.
 */
import type {
  ChatMessage,
  ChatMessageRole,
  ChatMessagePerf,
  ChatMessageUsage,
  ChatToolCall,
  IntegratedNotice,
  SubAgentBlock,
} from "../_chatTabsTypes";
import { COORDINATOR_SENDER_ID } from "../_chatTabsTypes";

/** Wire shape of a single row returned by `GET …/messages`. Shared by
 *  both history-load actions so the inline literal is not duplicated. */
export interface HistoryMessageItem {
  id: string;
  role: string;
  text: string;
  created_at: string;
  parent_id: string | null;
  tool_calls?: ChatToolCall[] | null;
  usage?: ChatMessageUsage | null;
  model_id?: string | null;
  model_provider?: string | null;
  meta?: Record<string, unknown> | null;
  /** Discussion participant id that authored an assistant turn (V2 multi-agent).
   *  Used after participants re-hydration to restore the speaker's role name +
   *  avatar colour on a reloaded history bubble. Absent for single-agent rows. */
  sender_id?: string | null;
}

/** Wire shape of a `GET …/messages` page response. */
export interface HistoryMessagesPage {
  items: HistoryMessageItem[];
  next_cursor: string | null;
}

/** Roles the history endpoint may return; anything else is coerced to
 *  `assistant` for safe rendering (V1 treated unknown roles as model
 *  output). */
const _HISTORY_ALLOWED_ROLES: ReadonlySet<string> = new Set([
  "user",
  "assistant",
  "system",
  "tool",
  "tool_indicator",
  "system_notice",
]);

/**
 * Rehydrate derived perf fields that are computed client-side during live
 * streaming (V1 useChat.js:2377-2389) but not persisted by the backend.
 *
 * The backend persists the raw timing/token data:
 *   `{ttft_ms, total_ms, input_tokens?, output_tokens?, tool_rounds?}`
 *
 * The frontend derives from those:
 *   - `input_tps`  = input_tokens / (ttft_ms / 1000)  [prompt processing rate]
 *   - `output_tps` = output_tokens / ((total_ms - ttft_ms) / 1000) [gen rate]
 *   - `tool_rounds` — persisted when backend passes it; otherwise inferred
 *     from presence of tool_calls on the message (V1 parity fallback).
 */
function _rehydratePerf(
  raw: Record<string, unknown>,
  toolCallCount: number,
  usage: ChatMessageUsage | null | undefined,
): ChatMessagePerf {
  const ttft_ms =
    typeof raw.ttft_ms === "number" ? (raw.ttft_ms as number) : undefined;
  const total_ms =
    typeof raw.total_ms === "number" ? (raw.total_ms as number) : undefined;
  const input_tokens =
    typeof raw.input_tokens === "number"
      ? (raw.input_tokens as number)
      : undefined;
  const output_tokens =
    typeof raw.output_tokens === "number"
      ? (raw.output_tokens as number)
      : undefined;
  const tool_rounds =
    typeof raw.tool_rounds === "number"
      ? (raw.tool_rounds as number)
      : toolCallCount > 0
        ? toolCallCount
        : undefined;

  // input_tps (B7 round-coherence parity with live `flushTurnPerf`): the rate
  // = prompt-tokens / prefill-time is only physically meaningful when the
  // numerator (prompt size) and denominator (ttft) are the SAME round. `ttft_ms`
  // is round-0's prefill latency, so the numerator must be ROUND-0's prompt.
  // The backend tail-appends `usage.first_round_prompt_tokens` (round-0's
  // _extract_usage-corrected prompt) for exactly this — round-coherent with
  // ttft on BOTH single- AND multi-round turns, so we recompute input_tps in
  // ALL cases (matching live, which no longer omits multi-round).
  //
  // NOTE: the numerator comes from `usage.first_round_prompt_tokens`, NOT the
  // persisted `perf.input_tokens` (which `build_assistant_meta` sets to the
  // cross-round SUM `prompt_tokens` — wrong for a rate). Fallback chain for
  // legacy sessions lacking the field: first_round → prompt_tokens →
  // (total − completion). The `[I] N tokens` TOTAL display is unaffected
  // (it uses `usage.last_round_prompt_tokens` elsewhere).
  const rateInTok: number | undefined =
    usage != null
      ? (usage.first_round_prompt_tokens ??
        usage.prompt_tokens ??
        (typeof usage.total_tokens === "number"
          ? usage.total_tokens - (usage.completion_tokens ?? 0)
          : undefined))
      : input_tokens;
  let input_tps: number | undefined;
  if (
    rateInTok !== undefined &&
    ttft_ms !== undefined &&
    ttft_ms > 0
  ) {
    input_tps = Math.round((rateInTok / (ttft_ms / 1000)) * 10) / 10;
  }

  // output_tps (B6 generation-rate): the live path sums each round's actual
  // generation span (first→last text chunk) to EXCLUDE inter-round
  // tool-execution waits. Those per-round spans are transient live state and
  // are NOT persisted, so on reload we can only approximate with the legacy
  // `total_ms − ttft_ms` phase. On a multi-round turn this denominator still
  // includes the tool waits, so the reloaded output_tps is a conservative
  // LOWER bound (never an over-estimate); the precise value is shown live the
  // moment the turn completes. Single-round turns are exact either way (no
  // tool-wait to exclude). NOTE: do NOT "fix" this by inventing a fake span on
  // reload — a conservative real bound beats a fabricated precise-looking one
  // (State-Truth-First).
  let output_tps: number | undefined;
  if (output_tokens !== undefined && total_ms !== undefined) {
    const outPhaseMs =
      ttft_ms !== undefined ? Math.max(0, total_ms - ttft_ms) : total_ms;
    if (outPhaseMs > 0) {
      output_tps =
        Math.round((output_tokens / (outPhaseMs / 1000)) * 10) / 10;
    }
  }

  return {
    ...(ttft_ms !== undefined ? { ttft_ms } : {}),
    ...(total_ms !== undefined ? { total_ms } : {}),
    ...(input_tokens !== undefined ? { input_tokens } : {}),
    ...(output_tokens !== undefined ? { output_tokens } : {}),
    ...(input_tps !== undefined ? { input_tps } : {}),
    ...(output_tps !== undefined ? { output_tps } : {}),
    ...(tool_rounds !== undefined ? { tool_rounds } : {}),
  };
}

/** Map raw `MessageItem` rows into `ChatMessage[]`. */
/**
 * Map raw `MessageItem` rows into `ChatMessage[]`.
 *
 * SYSTEM_NOTICE HANDLING (§L2 defense 2026-08-07)
 * -----------------------------------------------
 * The backend persists a ``role=system_notice`` row for every sub-agent
 * completion — its ONLY purpose is provider wire-fold (folded into a
 * ``role=user`` "[System notification]" wrap so the model integrates the
 * result on the next turn). It has NO business appearing as a bubble on
 * the UI: the sub-agent's outcome is already rendered as a
 * ``<SubAgentBlock>`` card hung off the assistant tool-call round message
 * that dispatched it.
 *
 * Two mapping outcomes, per notice:
 *
 * * **Normal (covered)** — the notice's ``meta.source_id`` matches some
 *   assistant message's ``subAgentBlocks[].subagent_id``. The
 *   ``<SubAgentBlock>`` card carries the outcome. Drop the notice — its
 *   text is redundant and rendering it produces the "wall of italic gray"
 *   artefact the user reported.
 *
 * * **Orphan** — no assistant message carries a matching sub-agent block.
 *   This can only happen when the main agent crashed mid-turn AFTER the
 *   sub-agent finished but BEFORE its integrating assistant message was
 *   persisted (e.g. the 2026-08-07 ``round_msg`` NameError). Promote the
 *   notice to a SYNTHETIC assistant message that renders exactly one
 *   ``<SubAgentBlock>`` card summarising the completion, with a
 *   ``meta.orphanSubAgent = true`` marker so the UI can (optionally)
 *   surface a "main agent didn't finish integrating" hint. The synthetic
 *   message reuses the notice's own id + createdAt so scroll position /
 *   pagination cursors stay stable across the transformation.
 */
export function mapHistoryItems(
  items: ReadonlyArray<HistoryMessageItem>,
  convId: string,
): ChatMessage[] {
  // Pass 1: collect every sub-agent id that already has a rendered
  // ``<SubAgentBlock>`` card somewhere in the history. We look at
  // persisted ``meta.subAgentBlocks`` — that IS the render source.
  //
  // N.53: the SAME walk also indexes SYSTEM_NOTICE text by ``meta.dedup_key``.
  // Folding it into this existing pass keeps the mapper at TWO passes total
  // (not three) — the notice rows are already being visited here.
  //
  // P1 (spinner-forever on reload): the same walk ALSO indexes each
  // completion notice's TERMINAL STATUS by its ``meta.source_id``. Under the
  // async spawn contract the parent conversation's stream never receives a
  // ``subagent_done`` frame (``stream_frame.subagent_done`` docstring: the
  // completion travels as a SYSTEM_NOTICE instead), so the block persisted on
  // the dispatching assistant message keeps the ``background`` status it was
  // created with. Replaying that verbatim renders a card that spins forever
  // on every reload. The notice row IS the persisted truth for "this
  // sub-agent finished, with this outcome", and it is already on this page —
  // so settle the block from it (State-Truth-First 铁律 3: the optimistic
  // live status is corrected by真值, never trusted as terminal).
  const coveredSubAgentIds = new Set<string>();
  const noticeTextByDedupKey = new Map<string, string>();
  const noticeStatusBySubAgentId = new Map<string, string>();
  for (const m of items) {
    const meta =
      m.meta && typeof m.meta === "object"
        ? (m.meta as Record<string, unknown>)
        : null;
    if (m.role === "system_notice" && meta !== null) {
      const dk = meta["dedup_key"];
      // Backend constant `SYSTEM_NOTICE_META_DEDUP_KEY = "dedup_key"`
      // (domain/message.py) — the join key for the integration chip.
      if (typeof dk === "string" && dk !== "") {
        noticeTextByDedupKey.set(dk, m.text);
      }
      // Terminal-status index (P1). Only ``subagent_completion`` notices carry
      // a sub-agent outcome; the status lives in the notice text template
      // (`…finished with status=<status>. Result: …`), parsed by the same
      // helper the orphan-promotion path uses so there is ONE parser.
      if (meta["kind"] === "subagent_completion") {
        const sourceId = meta["source_id"];
        if (typeof sourceId === "string" && sourceId !== "") {
          const status = _parseSubAgentCompletionNotice(m.text).status;
          if (status !== null && status !== "") {
            noticeStatusBySubAgentId.set(sourceId, status);
          }
        }
      }
    }
    const blocks = meta?.["subAgentBlocks"];
    if (!Array.isArray(blocks)) continue;
    for (const b of blocks) {
      if (b && typeof b === "object" && "subagent_id" in b) {
        const sid = b.subagent_id;
        if (typeof sid === "string" && sid !== "") {
          coveredSubAgentIds.add(sid);
        }
      }
    }
  }

  // Pass 2: emit ChatMessage[], filtering / promoting SYSTEM_NOTICE rows.
  const out: ChatMessage[] = [];
  for (const m of items) {
    if (m.role === "system_notice") {
      const promoted = _mapSystemNoticeRow(m, convId, coveredSubAgentIds);
      if (promoted !== null) out.push(promoted);
      continue;
    }
    out.push(
      _mapOrdinaryRow(m, convId, noticeTextByDedupKey, noticeStatusBySubAgentId),
    );
  }
  return out;
}

/** N.53 — resolve an assistant row's ``meta.integrated_notice_ids`` into the
 *  chip's view model, pairing each dedup_key with its notice text.
 *
 *  Returns ``undefined`` (never ``[]``) when the row integrated nothing, so
 *  the consumer's "absent ⇒ no chip" reading holds for both a pre-N.53 message
 *  and a turn that folded nothing.
 *
 *  A key whose notice row is NOT in this page still yields an entry with an
 *  empty ``text``: history is paginated, so the notice can live on an older
 *  page than the answer that digested it. Dropping such keys would understate
 *  the count — the chip must say "3 integrated" when the backend recorded 3,
 *  even if only 2 texts can be shown right now. */
function _resolveIntegratedNotices(
  meta: Readonly<Record<string, unknown>> | null,
  noticeTextByDedupKey: ReadonlyMap<string, string>,
): IntegratedNotice[] | undefined {
  const raw = meta?.["integrated_notice_ids"];
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const seen = new Set<string>();
  const out: IntegratedNotice[] = [];
  for (const key of raw) {
    // Defensive: a malformed / non-string entry is skipped rather than
    // rendered as an empty chip row.
    if (typeof key !== "string" || key === "" || seen.has(key)) continue;
    seen.add(key);
    out.push({ dedupKey: key, text: noticeTextByDedupKey.get(key) ?? "" });
  }
  return out.length > 0 ? out : undefined;
}

/** Map a non-SYSTEM_NOTICE history row to a ChatMessage. Verbatim body of
 *  the pre-L2 ``mapHistoryItems`` per-item lambda.
 *
 *  ``noticeStatusBySubAgentId`` (P1) is the reload-time真值 for sub-agent
 *  terminal state — see {@link _settleSubAgentBlocksFromNotices}. */
function _mapOrdinaryRow(
  m: HistoryMessageItem,
  convId: string,
  noticeTextByDedupKey: ReadonlyMap<string, string>,
  noticeStatusBySubAgentId: ReadonlyMap<string, string>,
): ChatMessage {
  const role: ChatMessageRole = _HISTORY_ALLOWED_ROLES.has(m.role)
    ? (m.role as ChatMessageRole)
    : "assistant";
  const created = Date.parse(m.created_at);
  // V1-parity meta envelope (P1). `request_id` is read by ChatMessageList
  // off `msg.meta.request_id`; `perf` / `subAgentBlocks` are top-level
  // ChatMessage fields the live stream commits, so lift them out of the
  // persisted meta to match the live-stream message shape exactly.
  const meta = (m.meta && typeof m.meta === "object" ? m.meta : null) as
    | Record<string, unknown>
    | null;
  const rawPerf = meta?.["perf"];
  const subAgentBlocks = meta?.["subAgentBlocks"];
  // Keep only `request_id` (and any non-lifted keys) in the rendered
  // `meta`; perf / subAgentBlocks are surfaced as their own fields.
  let renderMeta: Record<string, unknown> | undefined;
  if (meta) {
    // `round_index` is a backend grouping aid stamped on per-round assistant
    // messages (_streaming_helpers.py `build_tool_call_message`, used by
    // `_reinsert_injected_messages` to position a reloaded mid-turn injection
    // at its inter-round seam). It is NOT a render field — strip it so the
    // reloaded `meta` shape matches the live-committed shape (which strips the
    // camelCase `roundIndex` transient in messageCommit.ts). Order is already
    // correct: rows render in backend array order (this mapper is a 1:1
    // ``items.map``, no sort/regroup), and the backend now persists injections
    // in their correct inter-round array position.
    const { perf: _p, subAgentBlocks: _s, round_index: _ri, ...rest } = meta;
    void _p;
    void _s;
    void _ri;
    if (Object.keys(rest).length > 0) renderMeta = rest;
  }
  // Rehydrate perf with derived tok/sec fields (V1 parity — V1 persists
  // the fully-computed perf; V2 backend persists raw values and we
  // recompute the derived rates here on reload).
  const toolCallCount = m.tool_calls?.length ?? 0;
  const perf =
    rawPerf && typeof rawPerf === "object"
      ? _rehydratePerf(rawPerf as Record<string, unknown>, toolCallCount, m.usage)
      : undefined;
  // N.53 — pair the persisted dedup_keys with the notice texts indexed in
  // pass 1 (see `_resolveIntegratedNotices` for the pagination caveat).
  const integratedNotices = _resolveIntegratedNotices(
    meta,
    noticeTextByDedupKey,
  );
  // Normalise the persisted content sentinels back to "" so the rehydrated
  // message shape matches the live-stream shape exactly:
  // * ``"[tool_calls]"`` — legacy tool-call-only assistant message
  //   (_streaming_helpers.py:214). The live stream commits such a message
  //   with an EMPTY content (messageCommit.ts) and only renders the
  //   ToolExecPanel — never a text bubble.
  // * ``"[subagent_summary]"`` — SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG
  //   (2026-07-02) sentinel for the DEDICATED sub-agent-blocks message
  //   emitted by :meth:`_build_subagent_summary_message`. The live stream
  //   opens this message with empty content (only ``subAgentBlocks``
  //   accumulated in place); the sentinel exists only because
  //   ``MessageContent.text`` cannot be empty (domain constraint).
  // V1 parity: V1 has no such sentinels at all — tool-call / sub-agent-
  // blocks messages render only their cards (index.html:452).
  const normalisedText =
    m.text === "[tool_calls]" || m.text === "[subagent_summary]"
      ? ""
      : m.text;
  return {
    id: m.id,
    role,
    content: normalisedText,
    createdAt: Number.isFinite(created) ? created : Date.now(),
    conversationId: convId,
    ...(m.tool_calls && m.tool_calls.length > 0
      ? { toolCalls: m.tool_calls.map((c) => ({ ...c })) }
      : {}),
    ...(m.usage && typeof m.usage === "object"
      ? { usage: { ...m.usage } }
      : {}),
    ...(m.model_id ? { modelId: m.model_id } : {}),
    ...(m.model_provider ? { modelProvider: m.model_provider } : {}),
    // Discussion speaker id (V2 multi-agent). The role name + avatar colour
    // are resolved later in `loadHistoryMessages`, after the participant
    // roster is re-hydrated (this pure mapper has no roster context).
    ...(m.sender_id ? { senderId: m.sender_id } : {}),
    // Coordinator (主持人) identity recovery on reload: the live
    // `speaker_changed.is_coordinator` flag does NOT persist, but the backend
    // stamps the stable `COORDINATOR_SENDER_ID` sentinel on the coordinator
    // message's `sender_id`. Re-derive `isCoordinator` from it so a reloaded
    // coordinator bubble renders the fixed host identity + badge instead of
    // the raw sentinel (coordinator feature — history parity).
    ...(m.sender_id === COORDINATOR_SENDER_ID ? { isCoordinator: true } : {}),
    ...(renderMeta ? { meta: renderMeta } : {}),
    ...(perf !== undefined ? { perf } : {}),
    ...(Array.isArray(subAgentBlocks) && subAgentBlocks.length > 0
      ? {
          subAgentBlocks: _settleSubAgentBlocksFromNotices(
            subAgentBlocks as SubAgentBlock[],
            noticeStatusBySubAgentId,
          ),
        }
      : {}),
    // N.53 — background notices this turn folded into its answer. Resolved
    // here (not in the component) so the chip's data is a plain field on the
    // message, identical whether the tab came from a reload or a snapshot
    // adopt. `meta.integrated_notice_ids` STAYS in `renderMeta` too: it is
    // the raw persisted truth, and stripping it would make the reloaded meta
    // shape diverge from what the backend wrote.
    ...(integratedNotices !== undefined ? { integratedNotices } : {}),
  };
}

/**
 * Correct a reloaded turn's sub-agent blocks against the PERSISTED completion
 * truth (P1 — "子 Agent 已完成但 UI 一直转圈", reload half).
 *
 * A block is persisted with the status it last held on the wire. Under the
 * async spawn contract that is ``background``: the parent stream emits
 * ``subagent_start(status="background")`` and then NEVER a ``subagent_done``
 * for it — the completion is delivered out-of-band as a ``system_notice`` row
 * (see ``stream_frame.subagent_done`` §F3-async-contract). So a verbatim
 * replay of the persisted block renders a spinner + a live "stop" button for a
 * sub-agent that finished minutes ago, on every single reload.
 *
 * State-Truth-First 铁律 3: the live status is an optimistic value and MUST be
 * corrected by真值. Here the真值 is the completion notice persisted alongside
 * the block in the very same history page, keyed by ``meta.source_id``.
 *
 * Only NON-terminal blocks (``running`` / ``background`` / ``aborting``) are
 * touched, and only when a notice for that ``subagent_id`` exists — so a block
 * that already carries a genuine terminal status (an inline dispatch that DID
 * receive its ``subagent_done``, or a typed failure with ``errorKind``) is
 * returned untouched, and a genuinely still-running sub-agent keeps spinning.
 * Returns the SAME array reference when nothing changed, so the common case
 * allocates nothing.
 */
function _settleSubAgentBlocksFromNotices(
  blocks: readonly SubAgentBlock[],
  noticeStatusBySubAgentId: ReadonlyMap<string, string>,
): SubAgentBlock[] {
  if (noticeStatusBySubAgentId.size === 0) return blocks as SubAgentBlock[];
  let mutated = false;
  const next = blocks.map((b) => {
    if (
      b.status !== "running" &&
      b.status !== "background" &&
      b.status !== "aborting"
    ) {
      return b;
    }
    const sid = b.subagent_id;
    if (typeof sid !== "string" || sid === "") return b;
    const noticeStatus = noticeStatusBySubAgentId.get(sid);
    if (noticeStatus === undefined) return b;
    mutated = true;
    // Backend ``SubAgentSessionStatus`` (domain/sub_agent_session.py): only
    // ``done`` is a success; ``error`` / ``interrupted`` are failures. Anything
    // unrecognised is still TERMINAL (the notice only exists because the run
    // ended), so it degrades to ``error`` rather than leaving the card spinning.
    return {
      ...b,
      status: noticeStatus === "done" ? ("done" as const) : ("error" as const),
      ...(noticeStatus !== "done" && b.error === undefined
        ? { error: noticeStatus }
        : {}),
    };
  });
  return mutated ? next : (blocks as SubAgentBlock[]);
}

/** Map a ``role=system_notice`` history row. Returns ``null`` when the
 *  notice is redundant (some assistant message already carries the
 *  matching sub-agent block); returns a SYNTHETIC assistant message
 *  carrying the block otherwise (orphan promotion). */
function _mapSystemNoticeRow(
  m: HistoryMessageItem,
  convId: string,
  coveredSubAgentIds: ReadonlySet<string>,
): ChatMessage | null {
  // Narrow ``m.meta`` with runtime checks — ``in``/``typeof`` guards
  // (no inline object-cast reads).
  const meta: Readonly<Record<string, unknown>> | null =
    m.meta && typeof m.meta === "object" ? m.meta : null;
  const kind: unknown = meta !== null && "kind" in meta ? meta["kind"] : null;
  const sourceId: unknown =
    meta !== null && "source_id" in meta ? meta["source_id"] : null;

  // Only ``subagent_completion`` notices are known to this mapper. Any
  // other kind (future notice variants) drops silently — it never was
  // meant to render as a bubble anyway.
  if (kind !== "subagent_completion") return null;
  if (typeof sourceId !== "string" || sourceId === "") return null;

  // Covered: some assistant message already renders a ``<SubAgentBlock>``
  // for this sub-agent. The notice is pure wire-fold noise — drop it.
  if (coveredSubAgentIds.has(sourceId)) return null;

  // ------------------------------------------------------------------
  // Orphan: the main agent never got to persist an integrating message.
  // Synthesize a minimal assistant message carrying exactly one
  // ``<SubAgentBlock>`` card. Reuse the notice's own id + createdAt so
  // list keys, pagination cursors, and scroll-restoration references
  // stay identical to the pre-transformation shape (there is no other
  // ChatMessage with this id — the notice was 1:1 with this row).
  // ------------------------------------------------------------------
  const created = Date.parse(m.created_at);

  // Parse the notice content to extract a stable prompt preview + final
  // result text. The backend format is
  //   ``Sub-agent "<name>" (<id>) finished with status=<status>. Result: <text>``
  // where ``<name>``, ``<status>``, and ``<text>`` are user-visible. If
  // parsing fails (future format change / partial write) fall back to
  // the raw content so no information is lost.
  const parsed = _parseSubAgentCompletionNotice(m.text);

  // Build the synthetic block. The runtime shape mirrors what the
  // backend persists on a normal ``subagent_summary`` message's
  // ``meta.subAgentBlocks[]`` entry (``round_index`` in turns is
  // snake_case there — the frontend SubAgentTurn interface is
  // aspirationally camelCase but reads survive undefined at runtime).
  // Cast is a boundary conversion, assigned once to a named const per
  // ``ts-no-inline-cast-access`` rule (structurally-identical shape the
  // compiler cannot unify with our runtime snake_case turns entry).
  const rawBlock = {
    index: 0,
    total: 1,
    prompt_preview: parsed.name ?? "sub-agent",
    turns: [
      {
        round_index: 0,
        content: parsed.result ?? m.text,
        tools: [],
      },
    ],
    rounds: 1,
    // ``SubAgentBlock.status`` = "running"|"aborting"|"done"|"error"
    // (see _chatTabsTypes.ts:510). Backend ``status="done"`` maps
    // 1:1; any other terminal value degrades to "error".
    status: parsed.status === "done" ? "done" : "error",
    _collapsed: true,
    subagent_id: sourceId,
  };
  const block = rawBlock as unknown as SubAgentBlock;

  return {
    id: m.id,
    role: "assistant",
    // Empty content — the block carries the payload, same shape as a
    // normal ``[subagent_summary]`` sentinel message.
    content: "",
    createdAt: Number.isFinite(created) ? created : Date.now(),
    conversationId: convId,
    subAgentBlocks: [block],
    // ``orphanSubAgent`` marker so ChatMessageList / future UI can show
    // a "main agent didn't finish integrating" hint next to the card
    // without special-casing content parsing.
    meta: { orphanSubAgent: true, source_id: sourceId },
  };
}

/** Extract ``{name, status, result}`` from the backend's SYSTEM_NOTICE
 *  content template.  Defensive — returns partials rather than throwing
 *  on any deviation so a future format tweak degrades to "raw text as
 *  result" instead of "empty card". */
function _parseSubAgentCompletionNotice(text: string): {
  name: string | null;
  status: string | null;
  result: string | null;
} {
  // Header up to and including the first ``. Result: `` — everything
  // after is the model-produced summary body.
  const headerMatch = text.match(
    /^Sub-agent "([^"]*)" \([^)]*\) finished with status=([a-z_]+)\. Result: /,
  );
  if (!headerMatch) {
    return { name: null, status: null, result: text };
  }
  // Regex captured 2 groups plus the whole match — under `noUncheckedIndexedAccess`
  // TS still types the elements as `string | undefined`. Narrow to `null` on the
  // impossible case so the return type stays `string | null` (defensive, keeps
  // the parser total). `full.length` is safe: `full` is `headerMatch[0]`, always
  // defined by the regex match contract.
  const [full, name, status] = headerMatch;
  return {
    name: name ?? null,
    status: status ?? null,
    result: full !== undefined ? text.slice(full.length) : text,
  };
}
