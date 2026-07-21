// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useContextUsage` — fetches per-conversation context-token estimate
 * from the backend (`GET /api/chat/conversations/{id}/context`) so the
 * composer toolbar can render the V1 "~12K / 200K 5%" badge.
 *
 * Backend shape (already exposed by `interfaces/http/routes/chat/_rest.py`):
 *
 *   {
 *     used_tokens:    int,    // estimated tokens currently in the window
 *     budget_tokens:  int,    // configured budget / context limit
 *     ratio:          float,  // 0.0 .. 1.0  (used / budget)
 *     needs_compaction: bool,
 *   }
 *
 * V1 used `{estimated_tokens, context_limit, usage_pct}`; we expose
 * the same field names locally so the template stays close to V1.
 *
 * The composable watches a conversation-id source (typically the
 * active tab's `conversationId`) and refetches whenever it changes
 * to a non-null value. It is silent on transient errors — the badge
 * simply hides if the endpoint is unreachable or the conversation
 * doesn't exist yet (newly opened tab before first `send`).
 */
import { ref, computed, watch, type Ref, type ComputedRef } from "vue";
import { apiJson, ApiError } from "@/api";

export interface ContextUsage {
  /**
   * Full (pre-compaction) estimated tokens — the REAL occupancy. This is the
   * un-clamped figure, so when a history exceeds the model window it is GREATER
   * than `context_limit` (e.g. 222_000 against a 200_000 window). The badge
   * shows it verbatim so "over the window" is visible instead of pinned at the
   * window floor.
   */
  estimated_tokens: number;
  /** V1 alias for `budget_tokens` — the model's context-window size. */
  context_limit: number;
  /**
   * Real fractional ratio (full tokens / budget). UN-clamped: may exceed 1.0
   * (e.g. 1.11 → 111%) when the history is larger than the window. Drives both
   * the displayed percentage and the severity tint.
   */
  usage_pct: number;
  /**
   * Tokens actually sent to the model after the prompt was compacted, or
   * `null` when this conversation has never been compacted. When non-null,
   * the badge switches to its compacted presentation (原始窗口 → 压缩后 ·
   * 节省比率).
   */
  compactedTokens: number | null;
  /** Whether the prompt has been compacted at least once. */
  compacted: boolean;
}

interface ContextSizeResponse {
  used_tokens: number;
  budget_tokens: number;
  ratio: number;
  needs_compaction: boolean;
  /** Tokens after compaction (null = never compacted). Backend tail-added. */
  compacted_tokens: number | null;
  /** Whether the prompt has been compacted. Backend tail-added. */
  compacted: boolean;
  /**
   * REAL (un-clamped) pre-compaction occupancy + ratio. Backend tail-added.
   * `used_tokens`/`ratio` floor at the window (clamped for the domain
   * invariant); these carry the truth and may exceed `budget_tokens` / 1.0.
   */
  raw_used_tokens?: number;
  raw_ratio?: number;
}

/** Three-tier severity for the V1 `.ctx-ok` / `.ctx-warn` / `.ctx-danger`
 *  CSS class on `.ctx-badge-toolbar`. Thresholds match the T2.7-C
 *  spec (70% warn / 90% danger). */
export type ContextSeverity = "ok" | "warn" | "danger";

export function severityFor(usagePct: number): ContextSeverity {
  if (usagePct >= 0.9) return "danger";
  if (usagePct >= 0.7) return "warn";
  return "ok";
}

/**
 * Severity for a (possibly compacted) usage figure. When the prompt has been
 * compacted, the real pressure on the window is the compacted size relative to
 * the budget — not the pre-compaction full tokens — so colour off
 * `compactedTokens / context_limit`. Falls back to the plain `usage_pct`
 * ratio when not compacted or when the figures are unavailable.
 */
export function severityForUsage(u: ContextUsage): ContextSeverity {
  if (u.compacted && u.compactedTokens !== null && u.context_limit > 0) {
    return severityFor(u.compactedTokens / u.context_limit);
  }
  return severityFor(u.usage_pct);
}

/**
 * Standalone (non-reactive) one-shot context-usage fetch.
 *
 * Extracted from `useContextUsage().refresh()` so callers that are NOT a Vue
 * reactive badge — e.g. the scheduled-continuation timer deciding whether to
 * spill into a new session by context threshold — can read the same backend
 * estimate without mounting a composable / owning refs. Returns the mapped
 * `ContextUsage`, or `null` when there is no conversation id yet or the
 * endpoint is unreachable / the conversation isn't persisted (404). Never
 * throws — mirrors the badge's silent-degrade contract.
 */
export async function fetchContextUsage(
  convId: string | null,
  modelId: string | null,
  provider: string | null,
): Promise<ContextUsage | null> {
  if (convId === null || convId === "") {
    return null;
  }
  try {
    // V1 parity: append `?model_id=` when a model is selected so the budget
    // reflects the model's real context window; also append `&provider=` to
    // disambiguate identical model_ids living under different providers.
    const params = new URLSearchParams();
    if (modelId !== null && modelId !== "") params.set("model_id", modelId);
    if (
      modelId !== null &&
      modelId !== "" &&
      provider !== null &&
      provider !== ""
    ) {
      params.set("provider", provider);
    }
    const query = params.toString() === "" ? "" : `?${params.toString()}`;
    const res = await apiJson<ContextSizeResponse>(
      "GET",
      `/api/chat/conversations/${encodeURIComponent(convId)}/context${query}`,
    );
    return {
      // Prefer the REAL un-clamped figures so the badge can show an
      // over-window state (e.g. 222K / 111%). Fall back to the clamped
      // used_tokens/ratio when an older backend omits the raw fields.
      estimated_tokens:
        typeof res.raw_used_tokens === "number"
          ? res.raw_used_tokens
          : res.used_tokens,
      context_limit: res.budget_tokens,
      usage_pct:
        typeof res.raw_ratio === "number" ? res.raw_ratio : res.ratio,
      compactedTokens:
        typeof res.compacted_tokens === "number" ? res.compacted_tokens : null,
      compacted: res.compacted === true,
    };
  } catch (err) {
    // 404 = conversation not yet persisted (fresh tab); other errors are also
    // silent — the caller treats null as "no reading available".
    void (err instanceof ApiError ? err.code : err);
    return null;
  }
}

/**
 * Reactive context-usage reader.
 *
 * @param conversationIdSource  Reactive ref or getter returning the
 *                              current conversation id (or null).
 *                              When it changes to a non-null value,
 *                              `/context` is fetched once.
 * @param modelIdSource         Optional reactive ref or getter returning
 *                              the currently selected model id (or null).
 *                              V1 parity (`useChat.js:3015-3025`): when
 *                              present, it is sent as the `?model_id=`
 *                              query so the backend resolves the model's
 *                              real context-window (200K / 128K / 32K ...)
 *                              for the budget instead of the fixed 8192
 *                              default.
 * @param providerSource        Optional reactive ref or getter returning the
 *                              selected model's provider slug (or null). Sent
 *                              as `&provider=` to disambiguate identical
 *                              `model_id`s living under different providers
 *                              (e.g. `claude-4-6-sonnet` exposed by both
 *                              `provider_a` at 128K and `cloud_gw` at 200K).
 */
export function useContextUsage(
  conversationIdSource: Ref<string | null> | (() => string | null),
  modelIdSource?: Ref<string | null> | (() => string | null),
  providerSource?: Ref<string | null> | (() => string | null),
): {
  info: Ref<ContextUsage | null>;
  loading: Ref<boolean>;
  severity: ComputedRef<ContextSeverity>;
  refresh: () => Promise<void>;
} {
  const info = ref<ContextUsage | null>(null);
  const loading = ref(false);

  function readId(): string | null {
    return typeof conversationIdSource === "function"
      ? conversationIdSource()
      : conversationIdSource.value;
  }

  function readModelId(): string | null {
    if (modelIdSource === undefined) return null;
    return typeof modelIdSource === "function"
      ? modelIdSource()
      : modelIdSource.value;
  }

  function readProvider(): string | null {
    if (providerSource === undefined) return null;
    return typeof providerSource === "function"
      ? providerSource()
      : providerSource.value;
  }

  async function refresh(): Promise<void> {
    const convId = readId();
    if (convId === null || convId === "") {
      info.value = null;
      return;
    }
    loading.value = true;
    try {
      // Delegate the wire call + mapping to the shared standalone helper so
      // the reactive badge and the non-reactive scheduler read identical data.
      info.value = await fetchContextUsage(
        convId,
        readModelId(),
        readProvider(),
      );
    } finally {
      loading.value = false;
    }
  }

  const severity = computed<ContextSeverity>(() =>
    info.value === null ? "ok" : severityForUsage(info.value),
  );

  // Refetch whenever the conversation id changes to a non-null value, or
  // when the selected model id / provider changes (V1 parity: switching the
  // model — or the same model_id under a different provider — re-resolves
  // the context-window budget).
  watch(
    () => [readId(), readModelId(), readProvider()] as const,
    (next, prev) => {
      const [nextId] = next;
      const prevId = prev?.[0] ?? null;
      if (nextId !== prevId) {
        void refresh();
        return;
      }
      // Same conversation, model / provider changed → refresh only if we
      // already have a conversation to query.
      if (nextId !== null && nextId !== "") {
        void refresh();
      }
    },
    { immediate: true },
  );

  return { info, loading, severity, refresh };
}
