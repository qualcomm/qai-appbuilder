// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Conversations store — shared "Recent Chats" list source.
 *
 * M-align (V1 → V2, C-2): V1 keeps a single in-memory `conversations`
 * ref (`useChat.js:529-557`) that the sidebar renders directly, so a
 * newly created / renamed / deleted conversation reflects in the
 * sidebar *immediately* (no full reload). V2 previously had each
 * sidebar component fetch `GET /api/chat/conversations` once on mount
 * with no refresh path, so sending a message that auto-creates a
 * conversation never showed up until reload.
 *
 * This store is that single source: the sidebar reads `conversations`,
 * and the chat transport calls `upsert(...)` right after it creates a
 * conversation (mirroring V1's in-memory upsert), plus `remove(...)` /
 * rename happen here so every consumer stays consistent.
 *
 * Backend wire schema (TestClient-verified):
 *   GET  /api/chat/conversations
 *     -> { items: [{ id, title, status, created_at, updated_at,
 *                     message_count }] }
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { apiJson } from "@/api";

export interface ConversationSummary {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  // V1-parity sidebar badges (backend-appended, default 0): user-turn
  // count and tool-call count.
  round_count?: number;
  tool_call_count?: number;
  // Sub-agent count (backend-appended, default 0): number of sub-agent
  // sessions spawned under this conversation. The sidebar shows the
  // "expand sub-agents" arrow ONLY when this is > 0.
  subagent_count?: number;
  // V1-parity channel source metadata (SidebarPanel.js:116-132).
  // Stores e.g. {"source":"wechat"} for channel conversations; absent/null
  // for web-UI conversations.  Appended field (AGENTS.md §3.1).
  meta?: Record<string, unknown> | null;
  // Pin / favorite flags (backend-appended, default false): persisted in
  // meta.pinned / meta.favorite, surfaced as top-level booleans so the sidebar
  // can pin conversations on top and the favorites dialog can filter them.
  pinned?: boolean;
  favorite?: boolean;
  // Promote-ready detection result (backend migration 057). The backend
  // detects at turn end and persists it onto Conversation.detected_model; the
  // single-GET (`GET /api/chat/conversations/{id}`) carries it so the
  // "Promote to App Builder" CTA can be surfaced with ZERO on-open disk scans.
  // `workdir` empty + `variants` empty ⇒ "checked, nothing to promote";
  // absent / null ⇒ never detected (legacy / forward-compatible). The list
  // projection does not populate it (sidebar does not need it).
  detected_model?: {
    workdir: string;
    variants: { precision: string; label: string }[];
    checked_at?: string;
  } | null;
}

interface ConversationListResponse {
  items: ConversationSummary[];
}

export const useConversationsStore = defineStore("conversations", () => {
  const conversations = ref<ConversationSummary[]>([]);
  const loading = ref(false);
  const loadFailed = ref(false);

  // ── Read-after-write protection (bug2 + concurrency) ────────────────────
  // `fetch()` replaces the whole list. Several races could make local state
  // "revert a moment later", AND — critically — could make a turn's
  // authoritative refetch (the only carrier of the backend-computed
  // `subagent_count` / a brand-new row) get *silently dropped* when another
  // concurrent local write happened mid-flight (multiple chat tabs / parallel
  // inference). The races:
  //
  //   1. Stale fetch overwrite — two `fetch()`es in flight; the older
  //      (slower) one must not clobber the newer one's result.
  //   2. Fetch-clobbers-rename — a `fetch()` returning a row before the
  //      backend write was observed must not re-set the old title.
  //   3. **Local-write-drops-fetch (the sub-agent bug)** — a local optimistic
  //      mutation (upsert/rename/remove from ANY tab) used to bump the same
  //      shared `fetchSeq`, which made an in-flight `fetch()` discard its WHOLE
  //      response on return. Harmless for plain chat (it reuses the optimistic
  //      row), but fatal for sub-agent turns whose new `subagent_count` / new
  //      row can ONLY come from that fetch → the sidebar never refreshed until
  //      a manual reload.
  //
  // Fix: `fetchSeq` is bumped ONLY by `fetch()` itself (so a newer fetch still
  // supersedes an older one — race 1). Local writes NO LONGER invalidate an
  // in-flight fetch; instead, mutations that happen while a fetch is in flight
  // are recorded in `pendingMutations` and *replayed* onto the server list when
  // the fetch returns (so the authoritative list lands AND the optimistic
  // changes are preserved). Titles additionally use a long-lived
  // `localTitleOverrides` map (race 2): a rename stays authoritative until the
  // backend echoes the same title back (then the override self-clears).
  let fetchSeq = 0;
  // Number of `fetch()`es currently in flight. When > 0, a local mutation
  // records itself in `pendingMutations` so the returning fetch can replay it.
  let fetchesInFlight = 0;
  // Optimistic local mutations recorded while a fetch is in flight, replayed in
  // order onto the freshly-fetched server list so a mid-flight write is never
  // lost. Cleared once no fetch is in flight.
  //
  //   - "upsert": a full summary (new-row insert / full merge) — used by
  //     `upsert()` when the chat transport creates a conversation.
  //   - "patch":  a partial field set keyed by id — used by `setWorkspace()`,
  //     which must NOT replay a whole stale row over fresher server fields;
  //     it only re-applies the specific field it changed.
  //   - "remove": drop a row by id — used by `remove()`.
  type PendingMutation =
    | { kind: "upsert"; summary: ConversationSummary }
    | { kind: "patch"; id: string; fields: Partial<ConversationSummary> }
    | { kind: "remove"; id: string };
  let pendingMutations: PendingMutation[] = [];
  // id -> title the client last set via rename(); authoritative until the
  // backend echoes the same title back (or the row disappears).
  const localTitleOverrides = new Map<string, string>();

  function recordPendingMutation(mut: PendingMutation): void {
    if (fetchesInFlight > 0) pendingMutations.push(mut);
  }

  /** Replay pending optimistic mutations onto a freshly-fetched server list so
   *  a mid-flight local write (new row, removed row, patched field) survives
   *  the authoritative replacement. Pure: returns the reconciled list. */
  function replayPendingMutations(
    items: ConversationSummary[],
  ): ConversationSummary[] {
    if (pendingMutations.length === 0) return items;
    let result = items;
    for (const mut of pendingMutations) {
      if (mut.kind === "remove") {
        result = result.filter((c) => c.id !== mut.id);
      } else if (mut.kind === "patch") {
        // Patch ONLY the changed fields onto the server row; if the row isn't
        // present (e.g. not yet returned) we skip — a partial patch can't
        // synthesise a full row, and the field will land on the next fetch.
        const idx = result.findIndex((c) => c.id === mut.id);
        if (idx >= 0) {
          const next = result.slice();
          // Spreading a full row then a partial patch is a complete row at
          // runtime; the cast tells TS the optional-field merge is total.
          next[idx] = { ...next[idx], ...mut.fields } as ConversationSummary;
          result = next;
        }
      } else {
        const idx = result.findIndex((c) => c.id === mut.summary.id);
        if (idx >= 0) {
          const next = result.slice();
          next[idx] = { ...next[idx], ...mut.summary };
          result = next;
        } else {
          result = [mut.summary, ...result];
        }
      }
    }
    return result;
  }

  /** Apply any locally-authoritative titles onto a fresh server list,
   *  clearing overrides the backend has now caught up with. */
  function reconcileLocalTitles(
    items: ConversationSummary[],
  ): ConversationSummary[] {
    if (localTitleOverrides.size === 0) return items;
    const result = items.map((item) => {
      const override = localTitleOverrides.get(item.id);
      if (override === undefined) return item;
      if (item.title === override) {
        // Backend agrees — the override has served its purpose; drop it.
        localTitleOverrides.delete(item.id);
        return item;
      }
      // Backend still reports the stale title — keep showing the local one.
      return { ...item, title: override };
    });
    // Drop overrides whose conversation no longer exists server-side
    // (e.g. deleted), so the map can't grow unbounded.
    const presentIds = new Set(items.map((i) => i.id));
    for (const id of [...localTitleOverrides.keys()]) {
      if (!presentIds.has(id)) localTitleOverrides.delete(id);
    }
    return result;
  }

  async function fetch(): Promise<void> {
    const seq = ++fetchSeq;
    fetchesInFlight++;
    loading.value = true;
    loadFailed.value = false;
    try {
      const res = await apiJson<ConversationListResponse>(
        "GET",
        "/api/chat/conversations",
      );
      // Stale-guard (race 1): a NEWER fetch() started after us → drop this late
      // response so it can't clobber the fresher one. NOTE: only a real newer
      // fetch bumps `fetchSeq` now — local upsert/rename/remove no longer
      // invalidate an in-flight fetch (that was the sub-agent refresh bug);
      // instead they record into `pendingMutations`, replayed below.
      if (seq !== fetchSeq) return;
      const server = reconcileLocalTitles(res.items ?? []);
      // Replay any optimistic mutations that landed while we were in flight, so
      // the authoritative list (new row / `subagent_count`) is adopted AND the
      // mid-flight local change survives (race 3).
      conversations.value = replayPendingMutations(server);
    } catch (err) {
      // Endpoint missing / 4xx / network — degrade gracefully.
      void err;
      if (seq !== fetchSeq) return;
      conversations.value = [];
      loadFailed.value = true;
    } finally {
      if (seq === fetchSeq) loading.value = false;
      fetchesInFlight--;
      // Once no fetch is in flight, the replay window is closed: any recorded
      // mutations have already been applied to `conversations.value` directly
      // by their own setters, so clear the buffer to avoid unbounded growth /
      // double-replay on the next fetch.
      if (fetchesInFlight === 0) pendingMutations = [];
    }
  }

  /**
   * Insert-or-update a conversation summary, mirroring V1's in-memory
   * upsert (`useChat.js:536-557`). New entries go to the front so the
   * sidebar shows the just-created chat at the top of "Today".
   */
  function upsert(summary: ConversationSummary): void {
    // Record so an in-flight fetch replays this onto the server list instead
    // of dropping it (race 3); does NOT invalidate the fetch.
    recordPendingMutation({ kind: "upsert", summary });
    const idx = conversations.value.findIndex((c) => c.id === summary.id);
    if (idx >= 0) {
      const next = conversations.value.slice();
      next[idx] = { ...next[idx], ...summary };
      conversations.value = next;
    } else {
      conversations.value = [summary, ...conversations.value];
    }
  }

  /** Patch only the title of an existing conversation (rename). */
  function rename(id: string, title: string): void {
    // The title is protected across any in-flight / future fetch by the
    // long-lived `localTitleOverrides` map (reconcileLocalTitles re-applies it
    // until the backend echoes the new title back). We deliberately do NOT
    // record a `pendingMutations` upsert here: that would replay the WHOLE row
    // as it looked at rename time, clobbering any fresher fields the in-flight
    // fetch brings back (e.g. an updated `subagent_count` / `message_count`).
    // Title-only protection is exactly what reconcileLocalTitles provides.
    localTitleOverrides.set(id, title);
    conversations.value = conversations.value.map((c) =>
      c.id === id ? { ...c, title } : c,
    );
  }

  function remove(id: string): void {
    recordPendingMutation({ kind: "remove", id });
    localTitleOverrides.delete(id);
    conversations.value = conversations.value.filter((c) => c.id !== id);
  }

  /**
   * Patch only the session-level workspace (write directory) of a
   * conversation, optimistically updating `meta.workspace`.
   *
   * Like the other optimistic writers, a mid-flight fetch replays this change
   * (via `pendingMutations`) instead of dropping it, and the backend echoes
   * `meta.workspace` back on the next fetch. Unlike titles we don't keep a
   * long-lived override map — workspace is set explicitly by the user (not
   * auto-mutated mid streaming the way titles are), so the replay window is
   * sufficient.
   *
   * `workspace === null` (or empty) clears the session-level override so the
   * row falls back to the global default; we drop the `meta.workspace` key
   * rather than store an empty string, matching the backend wire shape (the
   * key is simply absent when unset).
   */
  function setWorkspace(id: string, workspace: string | null): void {
    const applyWorkspace = (c: ConversationSummary): ConversationSummary => {
      const nextMeta: Record<string, unknown> = { ...(c.meta ?? {}) };
      if (workspace === null || workspace === "") {
        delete nextMeta.workspace;
      } else {
        nextMeta.workspace = workspace;
      }
      return {
        ...c,
        meta: Object.keys(nextMeta).length > 0 ? nextMeta : null,
      };
    };
    const existing = conversations.value.find((c) => c.id === id);
    if (existing !== undefined) {
      // Replay ONLY the patched `meta` (not the whole stale row) so an
      // in-flight fetch's fresher fields on this row are preserved.
      recordPendingMutation({
        kind: "patch",
        id,
        fields: { meta: applyWorkspace(existing).meta },
      });
    }
    conversations.value = conversations.value.map((c) =>
      c.id === id ? applyWorkspace(c) : c,
    );
  }

  /**
   * Patch only the pin flag of a conversation, optimistically updating both
   * the top-level `pinned` boolean and `meta.pinned` (the backend echoes both
   * back on the next fetch). Mirrors {@link setWorkspace}'s replay-window
   * protection: a mid-flight fetch replays this patch instead of dropping it.
   * Clearing drops the `meta.pinned` key (matching the backend wire shape,
   * which omits the key when false).
   */
  function setPinned(id: string, pinned: boolean): void {
    const applyPinned = (c: ConversationSummary): ConversationSummary => {
      const nextMeta: Record<string, unknown> = { ...(c.meta ?? {}) };
      if (pinned) nextMeta.pinned = true;
      else delete nextMeta.pinned;
      return {
        ...c,
        pinned,
        meta: Object.keys(nextMeta).length > 0 ? nextMeta : null,
      };
    };
    const existing = conversations.value.find((c) => c.id === id);
    if (existing !== undefined) {
      const patched = applyPinned(existing);
      recordPendingMutation({
        kind: "patch",
        id,
        fields: { pinned: patched.pinned, meta: patched.meta },
      });
    }
    conversations.value = conversations.value.map((c) =>
      c.id === id ? applyPinned(c) : c,
    );
  }

  /**
   * Patch only the favorite flag of a conversation, optimistically updating
   * both the top-level `favorite` boolean and `meta.favorite`. Same
   * replay-window protection as {@link setPinned}.
   */
  function setFavorite(id: string, favorite: boolean): void {
    const applyFavorite = (c: ConversationSummary): ConversationSummary => {
      const nextMeta: Record<string, unknown> = { ...(c.meta ?? {}) };
      if (favorite) nextMeta.favorite = true;
      else delete nextMeta.favorite;
      return {
        ...c,
        favorite,
        meta: Object.keys(nextMeta).length > 0 ? nextMeta : null,
      };
    };
    const existing = conversations.value.find((c) => c.id === id);
    if (existing !== undefined) {
      const patched = applyFavorite(existing);
      recordPendingMutation({
        kind: "patch",
        id,
        fields: { favorite: patched.favorite, meta: patched.meta },
      });
    }
    conversations.value = conversations.value.map((c) =>
      c.id === id ? applyFavorite(c) : c,
    );
  }

  return {
    conversations,
    loading,
    loadFailed,
    fetch,
    upsert,
    rename,
    remove,
    setWorkspace,
    setPinned,
    setFavorite,
  };
});
