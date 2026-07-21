// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Per-conversation token-budget (`max_budget_tokens`) API client.
 *
 * Covers the persisted per-conversation TOKEN cap the user configures from the
 * composer's "本会话工具 / 技能" popover:
 *
 *   PATCH /api/chat/conversations/{id}/budget
 *     body  { max_tokens: number | null, reset_used?: boolean }
 *     → ConversationBudgetSnapshot (the effective cap + running counter)
 *
 * There is no dedicated GET budget route: the cap + running counter live in
 * `Conversation.meta["budget"]` ({ max_tokens, used_tokens }), which the
 * conversation summary endpoint already returns. `fetchConversationBudget`
 * reads that snapshot READ-ONLY (no mutation) via
 * `GET /api/chat/conversations/{id}` so the badge / popover can show the
 * current state on open / session switch without touching the cap.
 *
 * Wire contract (AGENTS.md §3.1): the frontend ONLY calls these two already
 * existing backend routes — it never adds a new route nor changes the request /
 * response shape. Types are defined locally because the OpenAPI `types/api.ts`
 * does not (yet) surface the budget PATCH schema.
 */

import { apiJson, type ApiRequestOptions } from "./http";
import { ApiError } from "./errors";

/**
 * Effective budget snapshot.
 *
 * `PATCH .../budget` returns the full shape (mirrors the backend
 * `ConversationBudgetResponse` / `BudgetCheckResult`); the read-only
 * `GET /conversations/{id}` snapshot only carries `max_tokens` / `used_tokens`
 * (from `meta.budget`), so the derived fields (`exceeded` / `remaining` /
 * `enabled`) are computed locally when reading. `max_tokens === null` (or `<= 0`)
 * means the budget is DISABLED (no cap) for this conversation.
 */
export interface ConversationBudgetSnapshot {
  /** The per-conversation TOKEN cap; `null` = disabled (no limit). */
  max_tokens: number | null;
  /** Running cumulative tokens consumed by this conversation's turns. */
  used_tokens: number;
  /** `true` when a cap is set and `used_tokens >= max_tokens`. */
  exceeded: boolean;
  /** Tokens left (`max_tokens - used_tokens`, floored at 0); `null` = unbounded. */
  remaining: number | null;
  /** `true` when a positive cap is configured. */
  enabled: boolean;
}

/** `PATCH /api/chat/conversations/{id}/budget` request body. */
export interface SetConversationBudgetInput {
  /** New cap; `null` (or `<= 0`, normalised by the backend) DISABLES the budget. */
  max_tokens: number | null;
  /** When `true`, zero the running `used_tokens` counter (fresh window). */
  reset_used?: boolean;
}

/** Raw `PATCH .../budget` response shape (backend `ConversationBudgetResponse`). */
interface ConversationBudgetResponse {
  max_tokens: number | null;
  used_tokens: number;
  exceeded: boolean;
  remaining: number | null;
  enabled: boolean;
}

/** Minimal shape of the `GET /conversations/{id}` summary we read from. */
interface ConversationSummaryResponse {
  meta?: {
    budget?: {
      max_tokens?: number | null;
      used_tokens?: number | null;
    } | null;
  } | null;
}

/**
 * Derive a full snapshot from a raw `{ max_tokens, used_tokens }` pair, so the
 * read-only path (which lacks the backend-computed fields) exposes the SAME
 * shape as the PATCH response.
 */
function deriveSnapshot(
  rawMax: number | null | undefined,
  rawUsed: number | null | undefined,
): ConversationBudgetSnapshot {
  // Normalise the cap the same way the backend does: non-positive ⇒ disabled.
  const max =
    typeof rawMax === "number" && Number.isFinite(rawMax) && rawMax > 0
      ? Math.floor(rawMax)
      : null;
  const used =
    typeof rawUsed === "number" && Number.isFinite(rawUsed) && rawUsed > 0
      ? Math.floor(rawUsed)
      : 0;
  const enabled = max !== null;
  const exceeded = enabled && used >= max;
  const remaining = enabled ? Math.max(0, max - used) : null;
  return { max_tokens: max, used_tokens: used, exceeded, remaining, enabled };
}

/**
 * Set (or clear) the per-conversation token budget.
 *
 * `max_tokens = null` disables the cap; a positive integer enables it. Pass
 * `reset_used: true` to zero the running counter (independent of the cap change
 * — e.g. "reset used amount" button). Returns the effective snapshot.
 */
export async function setConversationBudget(
  conversationId: string,
  input: SetConversationBudgetInput,
  opts?: ApiRequestOptions,
): Promise<ConversationBudgetSnapshot> {
  const res = await apiJson<
    ConversationBudgetResponse,
    SetConversationBudgetInput
  >(
    "PATCH",
    `/api/chat/conversations/${encodeURIComponent(conversationId)}/budget`,
    {
      max_tokens: input.max_tokens,
      ...(input.reset_used === true ? { reset_used: true } : {}),
    },
    opts,
  );
  return {
    max_tokens: res.max_tokens ?? null,
    used_tokens:
      typeof res.used_tokens === "number" && res.used_tokens > 0
        ? res.used_tokens
        : 0,
    exceeded: res.exceeded === true,
    remaining: typeof res.remaining === "number" ? res.remaining : null,
    enabled: res.enabled === true,
  };
}

/**
 * Read the current budget snapshot READ-ONLY from `Conversation.meta.budget`.
 *
 * Uses `GET /api/chat/conversations/{id}` (no mutation). Returns `null` when
 * there is no conversation id yet or the conversation is not persisted (fresh
 * tab → 404) or the endpoint is unreachable — mirroring the silent-degrade
 * contract of `fetchContextUsage`. A persisted conversation with no budget set
 * returns a DISABLED snapshot (`max_tokens: null`).
 */
export async function fetchConversationBudget(
  conversationId: string | null,
  opts?: ApiRequestOptions,
): Promise<ConversationBudgetSnapshot | null> {
  if (conversationId === null || conversationId === "") {
    return null;
  }
  try {
    const res = await apiJson<ConversationSummaryResponse>(
      "GET",
      `/api/chat/conversations/${encodeURIComponent(conversationId)}`,
      undefined,
      opts,
    );
    const budget = res.meta?.budget ?? null;
    return deriveSnapshot(budget?.max_tokens, budget?.used_tokens);
  } catch (err) {
    // 404 = conversation not yet persisted; other errors are also silent —
    // the caller treats null as "no reading available".
    void (err instanceof ApiError ? err.code : err);
    return null;
  }
}
