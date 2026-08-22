// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatCommands — main-chat slash command system (V1 parity).
 *
 * Ported 1:1 from V1's verified logic in
 * `QAIModelBuilder_v1_pure/frontend/js/composables/useChat.js:1566-1929`
 * (alias table `_TOP_LEVEL_ALIASES` @1569, dispatch @1590-1926). The
 * command UX (echo bubble + textual reply bubble, alias normalization,
 * argument parsing, list/use indexing, status/compact text) follows V1
 * exactly. Only the backend wire layer is adapted to the V2 Clean-Cutover
 * schema (TestClient-verified), per AGENTS.md "业务逻辑照搬 V1，后端字段
 * 对接以实测为准".
 *
 * V2 backend schema (verified):
 *   GET    /api/chat/conversations
 *            -> { items: [{ id, title, status, created_at, updated_at,
 *                           message_count }] }
 *   PATCH  /api/chat/conversations/{id}            body { title }
 *   DELETE /api/chat/conversations/{id}            -> 204
 *   POST   /api/chat/conversations/{id}/compact    body { model_id?, provider? }
 *            -> ContextSizeResponse (force-compact result; Task T)
 *   GET    /api/chat/conversations/{id}/context
 *            -> { used_tokens, budget_tokens, ratio, needs_compaction }
 *   /models / /model use the unified `useChatModelList` composable, which
 *     fans out to /api/service/models (local) +
 *     /api/model-catalog/cloud-models (cloud) + /api/service/status
 *     (running flag) — see useChatModelList.ts for V1 parity rationale.
 *   POST   /api/service/load-model body { model_name } — auto-load a
 *     local model on `/model <id>` (V1 `useChat.js:1697-1716`).
 *   POST   /api/system/reboot
 *
 * Command messages are display-only: `store.appendCommandMessage` pushes
 * them with `isCommandMsg` / `isCommandReply` markers so they are never
 * sent to the model nor persisted (V1 `is_command_msg` / `is_command_reply`,
 * useChat.js:1457-1476).
 */
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { loadServiceModel } from "@/api/serviceControl";
import { saveChatModelPreference } from "@/composables/chat/useChatModelPreference";
import { resumeConversationIfRunning } from "@/composables/chat/useActiveChatRunAttach";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { useReboot } from "@/composables/useReboot";
import { IS_INTERNAL } from "@/edition";
import { fmtTokenCount } from "@/utils/contextBadge";
import { useChatTabsStore, type TabId, type ChatTab } from "@/stores/chatTabs";
import {
  useConversationsStore,
  type ConversationSummary,
} from "@/stores/conversations";
import { useToastStore } from "@/stores/toast";
import {
  useChatModelList,
  type ChatModelItem,
} from "@/composables/chat/useChatModelList";

// ── Alias table (V1 useChat.js:1569-1573, verbatim) ──────────────────────────
const TOP_LEVEL_ALIASES: Record<string, string> = {
  h: "help",
  n: "new",
  cl: "clear",
  ms: "models",
  m: "model",
  l: "list",
  r: "reboot",
  st: "stop",
  c: "compact",
  rn: "rename",
  del: "delete",
};

interface ContextInfo {
  used_tokens: number;
  budget_tokens: number;
  ratio: number;
  needs_compaction: boolean;
}

/** POST /api/chat/conversations/{id}/compact response (Task T force-compact).
 *  The endpoint reuses ``ContextSizeResponse``; the fields below are the
 *  subset the frontend consumes to render `/compact` reply text. */
interface ForceCompactResponse {
  used_tokens: number;
  budget_tokens: number;
  ratio: number;
  needs_compaction: boolean;
  compacted: boolean;
  compacted_tokens: number | null;
  raw_used_tokens: number;
  raw_ratio: number;
}


export function useChatCommands() {
  const { t } = useI18n();
  const store = useChatTabsStore();
  const conversations = useConversationsStore();
  const toast = useToastStore();
  const modelList = useChatModelList();
  // V1 parity (useChat.js:1698): the `/model` auto-load is LOCAL-host-mode
  // only — in REMOTE mode the daemon switches models server-side, so the
  // client must not issue a local load-model. Read host_mode from forge.config.
  const { config: forgeConfig } = useForgeConfig();
  const { requestRebootDirect } = useReboot();

  // ── helpers ────────────────────────────────────────────────────────────────

  /** True when the trimmed text is a slash command (V1: text.startsWith('/')). */
  function isCommand(text: string): boolean {
    return text.trim().startsWith("/");
  }

  function pushToast(message: string, kind: "success" | "info" | "error" = "success"): void {
    toast.push({
      id: `cmd-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
      kind,
      message,
      timeoutMs: 3000,
    });
  }

  function echoCommand(tabId: TabId, text: string): void {
    store.appendCommandMessage(tabId, {
      role: "user",
      content: text,
      isCommandMsg: true,
    });
  }

  function reply(tabId: TabId, text: string): void {
    store.appendCommandMessage(tabId, {
      role: "assistant",
      content: text,
      isCommandReply: true,
    });
  }

  /**
   * Normalize a raw command line to its canonical `/command args` form,
   * expanding short aliases (V1 useChat.js:1574-1586).
   */
  function normalize(text: string): string {
    let normalized = text.trim();
    const parts = normalized.split(/\s+/);
    const cmdToken = (parts[0] ?? "").slice(1).toLowerCase(); // drop "/"
    const expanded = TOP_LEVEL_ALIASES[cmdToken];
    if (expanded !== undefined) {
      normalized =
        "/" + expanded + (parts.length > 1 ? " " + parts.slice(1).join(" ") : "");
    }
    // /u <n> → /use <n> (V1 useChat.js:1583-1586)
    const lower = normalized.toLowerCase();
    if (lower.startsWith("/u ") || lower === "/u") {
      const uParts = normalized.trim().split(/\s+/);
      normalized = "/use" + (uParts.length > 1 ? " " + uParts.slice(1).join(" ") : "");
    }
    return normalized;
  }

  /** Parse a normalized command line into `{ name, arg }`. */
  function parseCommand(text: string): { name: string; arg: string } {
    const normalized = normalize(text);
    const parts = normalized.trim().split(/\s+/);
    const name = (parts[0] ?? "").toLowerCase();
    const arg = parts.slice(1).join(" ").trim();
    return { name, arg };
  }

  /** Round count for a conversation summary (V2 `message_count` ≈ 2/round). */
  function roundsOf(c: ConversationSummary): number {
    // V2 exposes `message_count` (all roles). A "round" in V1 = one user
    // turn; with user+assistant pairing this is ~ message_count / 2.
    return Math.max(0, Math.floor((c.message_count ?? 0) / 2));
  }

  /**
   * Resolve a `/model` argument against the unified local + cloud list,
   * keeping V1's resolution order (V1 `_resolveModelArg`,
   * useChat.js:1526-1550): by 1-based index → exact `model_id` →
   * exact `name` (case-insensitive) → unique `model_id` prefix
   * (case-insensitive). The list is the V1-shaped continuous list
   * produced by `useChatModelList.loadAll`, so indexing follows the
   * same numbering shown to the user by `/models`.
   */
  function resolveModelArg(
    arg: string,
    models: ChatModelItem[],
  ): ChatModelItem | null {
    if (models.length === 0) return null;
    if (/^\d+$/.test(arg)) {
      const n = parseInt(arg, 10);
      if (n >= 1 && n <= models.length) return models[n - 1] ?? null;
      return null;
    }
    const exactId = models.find((m) => m.model_id === arg);
    if (exactId !== undefined) return exactId;
    const argLower = arg.toLowerCase();
    const byName = models.find((m) => m.name.toLowerCase() === argLower);
    if (byName !== undefined) return byName;
    const prefix = models.filter((m) =>
      m.model_id.toLowerCase().startsWith(argLower),
    );
    if (prefix.length === 1) return prefix[0] ?? null;
    return null;
  }

  // ── per-command handlers ─────────────────────────────────────────────────────

  async function cmdNew(): Promise<void> {
    // V1 `/new` (useChat.js:1590-1597): start a fresh session.
    store.openTab({ title: t("chat.tab.untitled") });
    pushToast(t("chat.newSessionStarted"), "success");
  }

  async function cmdClear(tab: ChatTab): Promise<void> {
    // V1 `/clear` (useChat.js:1598-1612): delete the persisted session if
    // any, else just clear the in-memory view.
    if (tab.conversationId !== null && tab.conversationId !== "") {
      try {
        await apiJson<void>(
          "DELETE",
          `/api/chat/conversations/${encodeURIComponent(tab.conversationId)}`,
        );
      } catch {
        // service may be down; still clear locally (V1 tolerant).
      }
      conversations.remove(tab.conversationId);
      store.clearMessages(tab.id);
      store.clearConversationId(tab.id);
    } else {
      store.clearMessages(tab.id);
    }
    pushToast(t("chat.sessionCleared"), "success");
  }

  async function cmdRename(tab: ChatTab, arg: string): Promise<void> {
    // V1 `/rename <title>` (useChat.js:1901-1913).
    if (arg === "") {
      reply(tab.id, t("chat.renameUsage"));
      return;
    }
    if (tab.conversationId === null || tab.conversationId === "") {
      reply(tab.id, t("chat.renameNoActive"));
      return;
    }
    try {
      await apiJson<unknown, { title: string }>(
        "PATCH",
        `/api/chat/conversations/${encodeURIComponent(tab.conversationId)}`,
        { title: arg },
      );
      conversations.rename(tab.conversationId, arg);
      store.renameTabsByConversation(tab.conversationId, arg);
      reply(tab.id, t("chat.renamed", { name: arg }));
    } catch {
      reply(tab.id, t("chat.renameFailed"));
    }
  }

  async function cmdDelete(tab: ChatTab): Promise<void> {
    // V1 `/delete` (useChat.js:1914-1922).
    if (tab.conversationId === null || tab.conversationId === "") {
      reply(tab.id, t("chat.deleteNoActive"));
      return;
    }
    const conv = conversations.conversations.find((c) => c.id === tab.conversationId);
    const title = conv?.title ?? t("chat.untitled");
    try {
      await apiJson<void>(
        "DELETE",
        `/api/chat/conversations/${encodeURIComponent(tab.conversationId)}`,
      );
    } catch {
      // tolerant — still drop locally.
    }
    conversations.remove(tab.conversationId);
    store.clearMessages(tab.id);
    store.setConversationId(tab.id, "");
    reply(tab.id, t("chat.sessionDeleted", { title }));
  }

  async function fetchContext(convId: string): Promise<ContextInfo | null> {
    try {
      return await apiJson<ContextInfo>(
        "GET",
        `/api/chat/conversations/${encodeURIComponent(convId)}/context`,
      );
    } catch {
      return null;
    }
  }

  async function cmdCompact(tab: ChatTab, arg: string): Promise<void> {
    // Task T + Task V: `/compact` is now the umbrella command for the new
    // compaction stack.
    //
    //   /compact                → force a compaction pass
    //   /compact status         → show occupancy + digest / ledger badges
    //   /compact clear          → drop the persisted compaction checkpoint
    //   /compact migrate        → P5 handoff: fork into a fresh session
    //                             seeded with the current session digest
    //
    // Legacy `/compact <n>` (rounds arg) is no longer supported — the new
    // engine is token-budget driven with a model-aware context window.
    if (tab.conversationId === null || tab.conversationId === "") {
      // No persisted session — nothing to compact / status / clear / migrate.
      reply(tab.id, t("chat.compactNoConversation"));
      return;
    }
    const sub = arg.trim().toLowerCase();
    if (sub === "status") {
      await cmdCompactStatus(tab);
      return;
    }
    if (sub === "clear") {
      await cmdCompactClear(tab);
      return;
    }
    if (sub === "migrate") {
      await cmdCompactMigrate(tab);
      return;
    }
    if (sub !== "") {
      // Unknown / legacy rounds argument.  Explicit rejection so users are
      // not silently ignored.
      reply(tab.id, t("chat.compactArgDeprecated"));
      return;
    }
    // /compact  → force compaction pass.
    try {
      const res = await apiJson<
        ForceCompactResponse,
        { model_id?: string; provider?: string }
      >(
        "POST",
        `/api/chat/conversations/${encodeURIComponent(tab.conversationId)}/compact`,
        { model_id: tab.modelId !== "" ? tab.modelId : undefined },
      );
      // The compaction checkpoint just changed, but no streaming turn ran —
      // clear the tab's stale turn-internal live reading and signal the
      // composer badge to re-read `GET /context`, so the badge stops showing
      // the PRE-compaction figures the moment this reply lands.
      store._patchTab(tab.id, {
        liveContextUsedTokens: undefined,
        liveContextLimit: undefined,
      });
      store.bumpCtxRefresh();
      const before = res.used_tokens;
      const after =
        res.compacted && res.compacted_tokens !== null
          ? res.compacted_tokens
          : before;
      const savings = Math.max(0, before - after);
      if (!res.compacted || savings === 0) {
        reply(
          tab.id,
          t("chat.compactNoSavings", { tokens: fmtTokenCount(after) }),
        );
        return;
      }
      const pct =
        before > 0 ? Math.round((savings / before) * 100) : 0;
      reply(
        tab.id,
        t("chat.compactDone", {
          before: fmtTokenCount(before),
          after: fmtTokenCount(after),
          savings: fmtTokenCount(savings),
          pct,
        }),
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      reply(tab.id, t("chat.backendCompactFailed", { msg }));
    }
  }

  async function cmdCompactStatus(tab: ChatTab): Promise<void> {
    // Task V F6: reuse GET /context which now returns compaction state
    // (occupancy + escalation) so users on the web see the same badge the
    // IM `/compact status` verb produces.
    if (tab.conversationId === null || tab.conversationId === "") return;
    try {
      const params = new URLSearchParams();
      if (tab.modelId !== "" && tab.modelId !== "qai-default") {
        params.set("model_id", tab.modelId);
      }
      const query = params.toString() === "" ? "" : `?${params.toString()}`;
      const res = await apiJson<{
        used_tokens: number;
        budget_tokens: number;
        ratio: number;
        needs_compaction: boolean;
        compacted_tokens: number | null;
        compacted: boolean;
        raw_used_tokens?: number;
        raw_ratio?: number;
        escalation?: "ok" | "suggest_handoff";
      }>(
        "GET",
        `/api/chat/conversations/${encodeURIComponent(tab.conversationId)}/context${query}`,
      );
      // Prefer the REAL (un-clamped) occupancy — identical口径 to the composer
      // badge (`fetchContextUsage`).  `used_tokens` / `ratio` are clamped to the
      // window to satisfy the ContextSize domain invariant (budget >= used), so
      // they pin at 100% once the history reaches the window and would report a
      // misleading "200000 / 200000 (100%)".
      const used =
        typeof res.raw_used_tokens === "number"
          ? res.raw_used_tokens
          : res.used_tokens;
      const ratio =
        typeof res.raw_ratio === "number" ? res.raw_ratio : res.ratio;
      const pct = Math.round((ratio ?? 0) * 100);
      reply(
        tab.id,
        t("chat.compactStatus", {
          used: fmtTokenCount(used),
          budget: fmtTokenCount(res.budget_tokens),
          pct,
          compacted: res.compacted ? t("common.yes") : t("common.no"),
          suggest_handoff:
            res.escalation === "suggest_handoff"
              ? t("common.yes")
              : t("common.no"),
        }),
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      reply(tab.id, t("chat.backendCompactFailed", { msg }));
    }
  }

  async function cmdCompactClear(tab: ChatTab): Promise<void> {
    // Task V F7: drop the persisted compaction checkpoint via
    // POST /compact/clear so the next turn re-compacts from scratch.
    if (tab.conversationId === null || tab.conversationId === "") return;
    try {
      await apiJson<{ cleared: boolean }, Record<string, never>>(
        "POST",
        `/api/chat/conversations/${encodeURIComponent(tab.conversationId)}/compact/clear`,
        {},
      );
      // Checkpoint dropped over REST — same badge-staleness problem as
      // `/compact` above (no streaming turn to trigger a re-read).
      store._patchTab(tab.id, {
        liveContextUsedTokens: undefined,
        liveContextLimit: undefined,
      });
      store.bumpCtxRefresh();
      reply(tab.id, t("chat.compactCleared"));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      reply(tab.id, t("chat.backendCompactFailed", { msg }));
    }
  }

  async function cmdCompactMigrate(tab: ChatTab): Promise<void> {
    // Task V F8: P5 handoff — fork the current conversation into a fresh
    // one seeded with the session digest, then open the new conversation
    // in a new tab so the user can keep working.
    if (tab.conversationId === null || tab.conversationId === "") return;
    try {
      const res = await apiJson<
        { new_conversation_id: string; source_conversation_id: string },
        Record<string, never>
      >(
        "POST",
        `/api/chat/conversations/${encodeURIComponent(tab.conversationId)}/compact/migrate`,
        {},
      );
      reply(tab.id, t("chat.compactMigrated", { id: res.new_conversation_id }));
      // Best-effort: open the new conversation in a new tab so the user
      // does not have to navigate manually.  Failure here does not
      // regress the migration itself (the row was already created).
      try {
        const opened = store.openTab({
          title: t("chat.compactMigratedTitle"),
          conversationId: res.new_conversation_id,
        });
        await store.loadHistoryMessages(opened.id);
      } catch {
        // silent — the reply already tells the user the new id.
      }
    } catch (e) {
      // The backend now degrades instead of refusing (it waits for an
      // in-flight summary, then falls back to the conversation tail), so a
      // failure here is a genuine fault — say "migration failed", not
      // "history trim failed".
      const msg = e instanceof Error ? e.message : String(e);
      reply(tab.id, t("chat.compactMigrateFailed", { msg }));
    }
  }

  function cmdList(tab: ChatTab, arg: string): void {
    // V1 `/list [n]` (useChat.js:1845-1866). Default 5, cap 50.
    const count = arg !== "" && /^\d+$/.test(arg) ? Math.min(parseInt(arg, 10), 50) : 5;
    const convs = conversations.conversations.slice(0, count);
    if (convs.length === 0) {
      reply(tab.id, t("chat.noHistory"));
      return;
    }
    const currentId = tab.conversationId;
    const lines: string[] = [t("chat.historyHeader", { n: convs.length })];
    convs.forEach((c, i) => {
      const marker = c.id === currentId ? `${i + 1}. ▶` : `${i + 1}.`;
      const rounds = roundsOf(c);
      const roundInfo = rounds > 0 ? t("chat.roundsSuffix", { n: rounds }) : "";
      lines.push(`${marker} ${c.title || t("chat.untitled")}${roundInfo}`);
    });
    lines.push("");
    lines.push(t("chat.activeMarker"));
    lines.push(t("chat.useHint"));
    reply(tab.id, lines.join("\n"));
  }

  async function cmdUse(tab: ChatTab, arg: string): Promise<void> {
    // V1 `/use <n>` (useChat.js:1867-1880).
    if (arg === "" || !/^\d+$/.test(arg)) {
      reply(tab.id, t("chat.useUsage"));
      return;
    }
    const idx = parseInt(arg, 10) - 1;
    const list = conversations.conversations;
    if (idx < 0 || idx >= list.length) {
      reply(tab.id, t("chat.indexOutOfRange", { arg, total: list.length }));
      return;
    }
    const target = list[idx];
    if (target === undefined) {
      reply(tab.id, t("chat.indexOutOfRange", { arg, total: list.length }));
      return;
    }
    // V2 navigation: reuse an existing tab for that conversation, else
    // open one and load its history (SidebarPanel.selectConversation parity).
    const existing = store.tabs.find((tt) => tt.conversationId === target.id);
    if (existing !== undefined) {
      store.switchTab(existing.id);
    } else {
      const opened = store.openTab({
        title: target.title,
        conversationId: target.id,
      });
      await store.loadHistoryMessages(opened.id);
      // Re-attach to a still-running background turn (with any pending
      // question), same as the sidebar re-open path. No-op when none matches.
      await resumeConversationIfRunning(opened.id, opened.conversationId);
    }
    const rounds = roundsOf(target);
    // Reply goes into the now-active tab.
    const activeId = store.activeTabId;
    if (activeId !== null) {
      reply(activeId, t("chat.switchedToSession", {
        title: target.title || t("chat.untitled"),
        rounds,
      }));
    }
  }

  async function cmdStatus(tab: ChatTab): Promise<void> {
    // V1 `/status` (useChat.js:1881-1900).
    if (tab.conversationId === null || tab.conversationId === "") {
      reply(tab.id, t("chat.noActiveSession"));
      return;
    }
    const conv = conversations.conversations.find((c) => c.id === tab.conversationId);
    const title = conv?.title ?? tab.title ?? t("chat.untitled");
    const rounds = conv !== undefined ? roundsOf(conv) : 0;
    const modelDisplay =
      tab.modelId !== "" && tab.modelId !== "qai-default"
        ? tab.modelId
        : t("chat.followGlobal");
    const lines: string[] = [
      t("chat.statusHeader"),
      t("chat.statusName", { title }),
      t("chat.statusRounds", { n: rounds }),
      t("chat.statusModel", { model: modelDisplay }),
    ];
    const ctx = await fetchContext(tab.conversationId);
    if (ctx !== null) {
      const pct = Math.round((ctx.ratio ?? 0) * 100);
      lines.push(
        t("chat.statusContextBudget", {
          used: ctx.used_tokens,
          budget: ctx.budget_tokens,
          pct,
        }),
      );
    }
    lines.push(t("chat.statusSessionId", { id: tab.conversationId.slice(0, 12) }));
    lines.push(t("chat.statusFooterHint"));
    reply(tab.id, lines.join("\n"));
  }

  async function cmdModels(tab: ChatTab): Promise<void> {
    // V1 `/models` (useChat.js:1638-1651 + _formatModelsReply 1479-1519).
    // V2 sources: `useChatModelList.loadAll()` aggregates
    //   GET /api/service/models             (local)
    //   GET /api/model-catalog/cloud-models (cloud)
    //   GET /api/service/status             (running flag)
    // and yields a V1-shaped `ChatModelItem[]` so we can reproduce V1's
    // continuously-numbered "[Local] then [Cloud]" listing verbatim.
    try {
      const all = await modelList.loadAll();
      const real = all.filter((m) => !m.is_placeholder);
      if (real.length === 0) {
        reply(tab.id, t("chat.noModelsAvailable"));
        return;
      }
      const local = real.filter((m) => m.is_local);
      const cloud = real.filter((m) => !m.is_local);

      const lines: string[] = [t("chat.modelListHeader", { n: real.length })];
      let idx = 1;

      if (local.length > 0) {
        lines.push(t("chat.localModels"));
        for (const m of local) {
          const status = m.is_running
            ? t("chat.modelRunning")
            : t("chat.modelNotLoaded");
          lines.push(`  [${idx}] ${m.name}  ${status}`);
          idx += 1;
        }
      }

      if (local.length > 0 && cloud.length > 0) {
        lines.push("");
      }

      if (cloud.length > 0) {
        lines.push(t("chat.cloudModels"));
        for (const m of cloud) {
          const providerStr = m.provider !== "" ? `  (${m.provider})` : "";
          lines.push(`  [${idx}] ${m.name}${providerStr}`);
          idx += 1;
        }
      }

      const current = tab.modelId;
      if (current !== "" && current !== "qai-default") {
        lines.push(t("chat.currentInUse", { name: current }));
      } else {
        lines.push(t("chat.currentFollowGlobal"));
      }
      lines.push(t("chat.modelSwitchHint"));
      reply(tab.id, lines.join("\n"));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      reply(tab.id, t("chat.fetchModelsFailed", { msg }));
    }
  }

  async function cmdModel(tab: ChatTab, arg: string): Promise<void> {
    // V1 `/model [arg]` (useChat.js:1652-1722). V2: resolve against the
    // unified local+cloud list, persist the pick on the active tab
    // (modelId + modelProvider, V1 parity), and auto-load local models
    // that aren't currently running (V1 `/api/service/load-model` call,
    // useChat.js:1697-1716).
    if (arg === "") {
      const cur =
        tab.modelId !== "" && tab.modelId !== "qai-default"
          ? tab.modelId
          : t("chat.followGlobal");
      reply(tab.id, t("chat.modelCurrent", { cur }));
      return;
    }
    if (arg === "0" || arg === "default" || arg === "默认") {
      // Restore "follow global" (V1 `selectedModelId.value = ''`,
      // V2 default sentinel "qai-default" + empty provider).
      store._patchTab(tab.id, { modelId: "qai-default", modelProvider: "" });
      // V1 parity (useChat.js:1666-1675): clear the persisted global pref.
      saveChatModelPreference("", "");
      reply(tab.id, t("chat.followGlobalRestored"));
      return;
    }
    let all: ChatModelItem[];
    try {
      all = await modelList.loadAll();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      reply(tab.id, t("chat.modelSwitchFailed", { msg }));
      return;
    }
    const real = all.filter((m) => !m.is_placeholder);
    const found = resolveModelArg(arg, real);
    if (found === null) {
      reply(tab.id, t("chat.modelNotFound", { arg }));
      return;
    }
    // Persist the selection on the active tab (V1 parity:
    // selectedModelId + selectedModelProvider).
    store._patchTab(tab.id, {
      modelId: found.model_id,
      modelProvider: found.provider,
    });
    // V1 parity (useChat.js:1687-1694): persist the global selection so a
    // refresh / new session restores it.
    saveChatModelPreference(found.model_id, found.provider);
    let replyText = t("chat.modelSwitched", { modelId: found.model_id });
    // V1 auto-load (useChat.js:1697-1716): if a local model is selected
    // and isn't currently running, kick off `load-model` on the daemon
    // and append the resulting message to the reply. Cloud picks never
    // touch the local daemon. V1 parity (#7): only in LOCAL host mode — in
    // REMOTE mode the daemon lives on another machine and switches models
    // server-side on the next request, so the client must not load-model.
    const hostMode =
      ((forgeConfig.value?.service_launch as
        | Record<string, unknown>
        | undefined)?.host_mode as string | undefined) ?? "local";
    if (found.is_local && !found.is_running && hostMode === "remote") {
      replyText += "\n\n" + t("models.remoteModelSelected", { name: found.name });
    } else if (found.is_local && !found.is_running) {
      try {
        // V1 stripped a "<format>::" prefix from `model_id` before
        // sending; V2 locals don't carry such a prefix (model_id ===
        // name), but we keep the V1 split-on-"::" defensive logic so
        // callers passing a fully-qualified id still work.
        const modelName = found.model_id.includes("::")
          ? found.model_id.split("::").slice(1).join("::")
          : found.name;
        await loadServiceModel({ model_name: modelName });
        replyText += "\n\n" + t("chat.modelRunning");
      } catch (le) {
        const lmsg = le instanceof Error ? le.message : String(le);
        replyText += t("chat.autoStartFailed", { msg: lmsg });
      }
    }
    reply(tab.id, replyText);
  }

  function cmdHelp(tab: ChatTab): void {
    // V1 `/help` (useChat.js:1630-1636) — uses the help namespace text.
    // Internal builds also append the Qualcomm support address so users
    // who prefer the /help command (rather than the sidebar user menu)
    // can still find it. IS_INTERNAL is a build-time constant, so the
    // append branch is DCE'd on external/Release builds — the internal
    // support address never ships in the open-source bundle.
    let text = t("help.mainText");
    if (IS_INTERNAL) {
      text += "\n\n📮 Support: qai-appbuilder.support@qti.qualcomm.com";
    }
    reply(tab.id, text);
  }

  async function cmdReboot(tab: ChatTab, text: string): Promise<void> {
    // V1 `/reboot` (useChat.js:1613-1628) — no confirm dialog; the typed
    // command is the confirmation. POST the reboot (via the shared controller,
    // which also shows the full-screen overlay + polls health + auto-refreshes
    // once the service is back). Connection errors are expected as the daemon
    // exits (REBOOT_EXIT_CODE=75) and are swallowed inside the controller.
    echoCommand(tab.id, text);
    reply(tab.id, t("chat.rebootRequested"));
    await requestRebootDirect();
  }

  function cmdStop(tab: ChatTab): void {
    // V1 `/stop` (help text: stop the current task). V2: cancel the
    // in-flight stream via the store state machine (the transport observes
    // `aborting` and tears down). No-op when not streaming.
    if (tab.status === "streaming") {
      store.requestCancel(tab.id);
      reply(tab.id, t("chat.stopped"));
    } else {
      reply(tab.id, t("chat.stopNotStreaming"));
    }
  }

  // ── dispatch (V1 useChat.js:1588-1926) ──────────────────────────────────────

  /**
   * Execute a slash command. Returns `true` when the input was handled
   * as a command (caller must then NOT send it to the model), `false`
   * when it was not a command at all.
   */
  async function executeCommand(text: string): Promise<boolean> {
    if (!isCommand(text)) {
      return false;
    }
    const tabId = store.activeTabId;
    if (tabId === null) {
      return false;
    }
    const tab = store.tabById(tabId);
    if (tab === null) {
      return false;
    }

    const raw = text.trim();
    const { name, arg } = parseCommand(raw);

    switch (name) {
      case "/new":
        await cmdNew();
        return true;
      case "/clear":
        await cmdClear(tab);
        return true;
      case "/reboot":
        await cmdReboot(tab, raw);
        return true;
      case "/help":
        echoCommand(tabId, raw);
        cmdHelp(tab);
        return true;
      case "/stop":
        echoCommand(tabId, raw);
        cmdStop(tab);
        return true;
      case "/models":
        echoCommand(tabId, raw);
        await cmdModels(tab);
        return true;
      case "/model":
        echoCommand(tabId, raw);
        await cmdModel(tab, arg);
        return true;
      case "/compact":
        echoCommand(tabId, raw);
        await cmdCompact(tab, arg);
        return true;
      case "/list":
        echoCommand(tabId, raw);
        cmdList(tab, arg);
        return true;
      case "/use":
        echoCommand(tabId, raw);
        await cmdUse(tab, arg);
        return true;
      // `/s` (bare) → status (V1 special-cases this @1836; our normalize
      // does not expand a bare `/s`, so handle it here).
      case "/status":
      case "/s":
        echoCommand(tabId, raw);
        await cmdStatus(tab);
        return true;
      case "/rename":
        echoCommand(tabId, raw);
        await cmdRename(tab, arg);
        return true;
      case "/delete":
        echoCommand(tabId, raw);
        await cmdDelete(tab);
        return true;
      // `/cc` / `/oc` are channel-only slash commands (WeChat / Feishu).
      // In the web UI, CC/OC session management lives in the dedicated
      // Claude Code / Open Code mode (the composer's CC/OC pills open a
      // full session panel — new/switch/rename/delete/model — see
      // ChatViewClaudeCode.vue + CodingSessionPanel.vue, the V2 GUI
      // replacement for V1's `/cc`/`/oc` web-chat shortcuts in app.js).
      // 回退-6: V1's web `/help` text lists `/cc`/`/oc`, and previously the
      // default branch silently forwarded `/cc list` etc. to the model as a
      // prompt — misleading. We intercept here and point the user to the
      // CC/OC mode pills instead of re-implementing the slash commands
      // (which would duplicate the panel UI). The commands remain fully
      // functional in the WeChat / Feishu channels.
      case "/cc":
        echoCommand(tabId, raw);
        reply(tab.id, t("help.webuiCcHint"));
        return true;
      case "/oc":
        echoCommand(tabId, raw);
        reply(tab.id, t("help.webuiOcHint"));
        return true;
      default:
        // Unknown slash token: not a recognized command. V1 would let it
        // fall through to the model; we mirror that by returning false so
        // the caller sends it as a normal prompt.
        return false;
    }
  }

  return { isCommand, parseCommand, executeCommand };
}
