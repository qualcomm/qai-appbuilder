// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useComposerCtxBadge` — chat-composer context-usage badge state
 * (ARCH-1 cohesion split, extracted verbatim from `ChatComposer.vue`).
 *
 * Wraps `useContextUsage` for the composer's toolbar + footer ctx badges,
 * owning the derived conversation/model ids, the clickable-refresh tooltip,
 * the severity class, and the click handler.
 *
 * Data source is branched by the active tab's `kind`:
 *  - ordinary chat tabs (`kind !== "subagent"`) read
 *    `GET /api/chat/conversations/{id}/context` via `useContextUsage`;
 *  - sub-agent tabs (`kind === "subagent"`) read the sub-agent's OWN
 *    usage off `tab.subagentMeta` (populated by
 *    `GET /api/chat/subagents/{id}` and refreshed LIVE from the
 *    sub-agent's stream).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore } from "@/stores/chatTabs";
import {
  useContextUsage,
  severityForUsage,
  type ContextUsage,
  type ContextSeverity,
} from "@/composables/chat/useContextUsage";
import { fmtKLimit, fmtSavedPct, isOverLimit } from "@/utils/contextBadge";

export function useComposerCtxBadge() {
  const { t } = useI18n();
  const store = useChatTabsStore();

  // ─── Context usage badge (T2.7-C) ──────────────────────────────────────────
  // Reads `GET /api/chat/conversations/{id}/context` and reuses the V1
  // `.ctx-badge-toolbar` styling (already migrated to V2 components.css).
  // Auto-refreshes when the active tab's conversationId changes; also
  // refreshes on streaming → idle transitions so the count updates after
  // each turn completes.
  const ctxConversationId = computed<string | null>(
    () => store.activeTab?.conversationId ?? null,
  );
  // V1 parity (`useChat.js:3015-3025`): pass the active tab's selected
  // model id so the badge budget reflects the model's real context window
  // (200K / 128K / 32K ...). Treat the "qai-default" placeholder as "no
  // model selected" so the backend keeps its default budget.
  const ctxModelId = computed<string | null>(() => {
    const mid = store.activeTab?.modelId ?? null;
    return mid && mid !== "qai-default" ? mid : null;
  });
  // Provider slug for the selected model — disambiguates identical model_ids
  // living under different providers (e.g. claude-4-6-sonnet 128K under one
  // provider vs 200K under another) so the budget tracks the SELECTED entry.
  const ctxProvider = computed<string | null>(
    () => store.activeTab?.modelProvider ?? null,
  );
  const {
    info: convCtxInfo,
    severity: convCtxSeverity,
    loading: ctxLoading,
    refresh: refreshConvCtx,
  } = useContextUsage(ctxConversationId, ctxModelId, ctxProvider);

  // Sub-agent's own context usage, derived from `tab.subagentMeta`
  // (server-estimated from the sub-agent's wire history). Returns null
  // when the active tab is not a sub-agent tab OR the usage fields aren't
  // populated yet, so the badge falls back to / hides like the conversation
  // path.
  const subagentCtxInfo = computed<ContextUsage | null>(() => {
    const tab = store.activeTab;
    if (tab === null || tab.kind !== "subagent") return null;
    const meta = tab.subagentMeta;
    if (
      meta === undefined ||
      typeof meta.usedTokens !== "number" ||
      typeof meta.budgetTokens !== "number"
    ) {
      return null;
    }
    const budget = meta.budgetTokens > 0 ? meta.budgetTokens : 1;
    // Prefer the REAL (un-clamped) occupancy + ratio when present, identical
    // 口径 to the main agent's `fetchContextUsage` (which prefers
    // `raw_used_tokens` / `raw_ratio`). This lets a sub-agent whose history
    // exceeds its window display >100% ("compaction imminent") instead of
    // being pinned at the 100% floor. Fall back to the clamped figures (and a
    // local ratio) for older payloads that carry no raw fields.
    const estimated =
      typeof meta.rawUsedTokens === "number" ? meta.rawUsedTokens : meta.usedTokens;
    const pct =
      typeof meta.rawRatio === "number"
        ? meta.rawRatio
        : typeof meta.ratio === "number"
          ? meta.ratio
          : Math.min(meta.usedTokens / budget, 1);
    return {
      estimated_tokens: estimated,
      context_limit: meta.budgetTokens,
      usage_pct: pct,
      // Sub-agent meta carries no compaction info — keep the single-value
      // (uncompacted) display so the badge never claims a phantom compaction.
      compactedTokens: null,
      compacted: false,
    };
  });

  // Whether the active tab is a sub-agent tab (drives the data-source branch).
  const isSubAgentTab = computed<boolean>(
    () => store.activeTab?.kind === "subagent",
  );

  // Turn-internal LIVE context usage for an ordinary chat tab (V2 enhancement;
  // mirror of the sub-agent per-round refresh). The `context_usage` frame sets
  // `tab.liveContextUsedTokens` / `liveContextLimit` at each agentic round
  // boundary with the round-just-completed's PROVIDER-MEASURED wire size
  // (State-Truth-First, NOT an estimate). When present we surface it as a
  // `ContextUsage` so the badge tracks the real wire growth (e.g. 33K → 70K)
  // WHILE a long multi-round turn runs, instead of staying frozen at the prior
  // turn's `/context` value. Returns null when the active tab is a sub-agent
  // (sub-agent has its own live path) and before any live frame arrives, so the
  // badge falls back to the `/context` estimate. The live value is CLEARED on
  // the next `refreshCtx` (turn-boundary streaming→idle), letting the
  // authoritative `/context` probe override it (State-Truth-First 铁律 3).
  const liveCtxInfo = computed<ContextUsage | null>(() => {
    const tab = store.activeTab;
    if (tab === null || tab.kind === "subagent") return null;
    const used = tab.liveContextUsedTokens;
    const limit = tab.liveContextLimit;
    if (
      typeof used !== "number" ||
      !Number.isFinite(used) ||
      used < 0 ||
      typeof limit !== "number" ||
      !Number.isFinite(limit) ||
      limit <= 0
    ) {
      return null;
    }
    // Same un-clamped口径 as `fetchContextUsage` (raw_used_tokens / raw_ratio):
    // the ratio may exceed 1.0 so the badge can show an over-window state while
    // the turn keeps growing the wire. Live frames carry no compaction info →
    // single-value (uncompacted) display.
    return {
      estimated_tokens: used,
      context_limit: limit,
      usage_pct: used / limit,
      compactedTokens: null,
      compacted: false,
    };
  });

  // Unified badge info: sub-agent tab reads its own usage; an ordinary chat
  // tab PREFERS the turn-internal live reading while it is set (so the badge
  // refreshes per round during a long turn), then falls back to the
  // per-conversation `/context` estimate (the turn-boundary authoritative
  // value, which also overrides the live value once `refreshCtx` clears it).
  const ctxInfo = computed<ContextUsage | null>(() => {
    if (isSubAgentTab.value) return subagentCtxInfo.value;
    return liveCtxInfo.value ?? convCtxInfo.value;
  });
  const ctxSeverity = computed<ContextSeverity>(() => {
    if (isSubAgentTab.value) {
      return subagentCtxInfo.value === null
        ? "ok"
        : severityForUsage(subagentCtxInfo.value);
    }
    const info = liveCtxInfo.value ?? convCtxInfo.value;
    return info === null ? convCtxSeverity.value : severityForUsage(info);
  });

  // For a sub-agent tab the usage comes from the tab's own `subagentMeta`
  // (refreshed by the store when the run terminates), so the manual refresh
  // re-fetches the sub-agent detail; otherwise re-query the conversation
  // `/context`.
  async function refreshCtx(): Promise<void> {
    const tab = store.activeTab;
    if (tab !== null && tab.kind === "subagent") {
      const sid = tab.subagentMeta?.subagentId;
      if (sid !== undefined && sid !== "") {
        await store._refreshSubAgentTab(tab.id, sid);
        return;
      }
    }
    // Turn-boundary authoritative override (State-Truth-First 铁律 3): clear the
    // turn-internal live reading BEFORE re-fetching `/context` so the badge
    // immediately stops showing the stale per-round value and falls back to the
    // authoritative probe value the refresh installs. `refreshCtx` is called on
    // every streaming→idle transition (useComposerModelSelection) + on a manual
    // badge click, i.e. exactly at the points where `/context` is authoritative.
    if (
      tab !== null &&
      (tab.liveContextUsedTokens !== undefined ||
        tab.liveContextLimit !== undefined)
    ) {
      store._patchTab(tab.id, {
        liveContextUsedTokens: undefined,
        liveContextLimit: undefined,
      });
    }
    await refreshConvCtx();
  }

  const ctxBadgeClass = computed(() => `ctx-${ctxSeverity.value}`);

  // ─── Compacted-state derived values ────────────────────────────────────────
  // When the prompt has been compacted at least once, the badge switches from
  // "~used / budget · pct%" to "原始窗口 → 压缩后 · 节省比率". These computeds
  // keep the templates declarative (just bind, no inline math).
  const ctxCompacted = computed<boolean>(
    () => ctxInfo.value !== null && ctxInfo.value.compacted === true && ctxInfo.value.compactedTokens !== null,
  );
  /** Saved percentage (省 N%) — `1 - compacted/used`, guarded + clamped. */
  const ctxSavedPct = computed<number>(() => {
    const i = ctxInfo.value;
    if (i === null || i.compactedTokens === null) return 0;
    return fmtSavedPct(i.estimated_tokens, i.compactedTokens);
  });

  // Over-window flag for the UNcompacted state: the real (un-clamped) history
  // is at/over the model window, so the prompt no longer fits and compaction is
  // imminent. Drives a small "超出 / over" marker on the badge. Suppressed in
  // the compacted state (which already tells the full before→after story).
  const ctxOverLimit = computed<boolean>(() => {
    const i = ctxInfo.value;
    if (i === null || ctxCompacted.value) return false;
    return isOverLimit(i.usage_pct);
  });

  // V1 parity (index.html:2231-2249) — the ctx badge in the input footer
  // is CLICKABLE: clicking it re-fetches the context usage (V1
  // `@click="fetchContextSize()"`). Tooltip differs from the auto-refresh
  // toolbar version: V1 appends a "click to refresh" hint when info is
  // already loaded, and shows "click to query" when nothing is loaded yet.
  const ctxBadgeFooterTitle = computed(() => {
    // Footer badge is bound to a conversation OR a sub-agent tab; hide it
    // only when neither source can provide a figure.
    if (ctxConversationId.value === null && !isSubAgentTab.value) return "";
    const info = ctxInfo.value;
    if (info === null) {
      return t("index.ctxClickToQuery");
    }
    // Defensive: a partial backend payload (or a test stub returning `{}`)
    // can land in `info` with `estimated_tokens === undefined`. Render the
    // "click to query" hint in that case rather than crash the badge —
    // `toLocaleString` on undefined would throw inside a Vue computed and
    // surface as an unhandled rejection that breaks the whole composer
    // rerender.
    if (typeof info.estimated_tokens !== "number") {
      return t("index.ctxClickToQuery");
    }
    // Compacted: expand the full picture (full → compacted, window, saved %).
    if (info.compacted && info.compactedTokens !== null) {
      return (
        t("chat.composer.ctxCompactedTooltip", {
          full: info.estimated_tokens.toLocaleString(),
          compacted: info.compactedTokens.toLocaleString(),
          window: fmtKLimit(info.context_limit),
          saved: ctxSavedPct.value.toString(),
        }) +
        t("index.ctxClickToRefresh")
      );
    }
    return (
      t("chat.composer.ctxBadgeTitle", {
        tokens: info.estimated_tokens.toLocaleString(),
        limit: fmtKLimit(info.context_limit),
        pct: (info.usage_pct * 100).toFixed(1),
      }) +
      t("index.ctxClickToRefresh")
    );
  });

  function onCtxBadgeClick(): void {
    void refreshCtx();
  }

  return {
    ctxConversationId,
    ctxModelId,
    ctxInfo,
    ctxLoading,
    refreshCtx,
    ctxBadgeClass,
    ctxBadgeFooterTitle,
    ctxCompacted,
    ctxSavedPct,
    ctxOverLimit,
    onCtxBadgeClick,
  };
}
