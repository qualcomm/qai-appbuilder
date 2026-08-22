// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Per-tab scroll-position memory for the chat message list.
 *
 * Problem this solves: switching chat tabs (and, under <KeepAlive>, navigating
 * away from /chat and back) previously always forced the message list to jump
 * to the newest message (`scrollToBottomInstant`), discarding the user's
 * reading position. This composable records each tab's `scrollTop` when the
 * user leaves it and restores it on return.
 *
 * Why the key is `conversationId` (with `tabId` fallback)
 * -------------------------------------------------------
 * Every `openTab(...)` mints a fresh `tabId` (see `idGenerators.ts`
 * `nextLocalTabId`), so closing a tab in `keep` mode and re-opening the SAME
 * conversation from the sidebar yields a NEW `tabId` — keyed by `tabId` the
 * saved entry would never be hit. `conversationId` is the back-end-stable
 * identity of the conversation, matching the philosophy of
 * `saveSessionToolOverride` (`chatTabs.ts:646`). New / blank tabs that do not
 * yet have a `conversationId` fall back to their `tabId` so the in-session
 * tab↔tab switch still remembers position.
 *
 * Why we also save `wasAtBottom`
 * ------------------------------
 * The naked `scrollTop` cannot distinguish "user was reading at the bottom
 * and wants to keep following new content" from "user was reading in the
 * middle and wants to stay there". When a tab is left at the bottom and new
 * messages arrive in the background (e.g. streaming continues while the user
 * is viewing another tab), restoring the raw `scrollTop` lands the view in
 * the middle of the newly-grown content. We record `wasAtBottom` so the
 * restore path can `scrollToBottomInstant()` in that case and let the
 * follow-streaming watcher continue tracking new arrivals.
 *
 * Two storage tiers:
 *   - In-memory `Map<Key, Entry>`: the fast path for tab↔tab switches within
 *     a session (no serialization cost).
 *   - `sessionStorage`: persists across component remounts and page reload
 *     within the same browser tab. `sessionStorage` (not `localStorage`)
 *     because a scroll position is transient per-session UI state.
 *
 * The module-scoped store survives the component's own unmount/remount, which
 * is exactly what we want for both KeepAlive and (when KeepAlive max evicts)
 * full remount paths.
 *
 * Storage key is bumped to `v2` because the on-disk entry shape changed from
 * a bare number to `{ scrollTop, wasAtBottom }`. A v1 payload (numeric values)
 * is silently ignored by the typed hydrate validator below.
 */

import type { ConversationId, TabId } from "@/stores/_chatTabsTypes";

const STORAGE_KEY = "qai.chat.scrollTop.v2";
/** Legacy storage key from before the schema change (bare-number entries
 *  keyed by raw tabId). We purge it on first hydrate so it does not linger
 *  in sessionStorage as orphan data. The legacy entries can NOT be migrated
 *  safely: v1 entries were keyed by `tabId` which changes on every reopen,
 *  so even a "rescue" import would never be hit again. */
const LEGACY_STORAGE_KEY_V1 = "qai.chat.scrollTop.v1";

/** Key namespace marker — `sub:<subagentId>`, `c:<conversationId>` or `t:<tabId>` */
type MemoryKey = string;

interface ScrollEntry {
  scrollTop: number;
  wasAtBottom: boolean;
}

/** In-memory fast path: key → last known entry. */
const memory = new Map<MemoryKey, ScrollEntry>();

let hydrated = false;

/**
 * Build the storage key for a tab. Prefers `conversationId` so the saved
 * position survives close+reopen of the SAME conversation (which mints a new
 * `tabId`). For tabs without a conversation yet (new / blank), use `tabId` so
 * in-session tab switches still remember their position.
 *
 * Sub-agent tabs (SCROLL LEAK FIX 2026-07-11): a sub-agent tab's
 * `conversationId` is set to the PARENT/ROOT conversation id (see
 * `chatTabs.ts` `openSubAgentTab`), so keying purely on `conversationId` made
 * the main tab and ALL its sub-agent tabs share ONE `c:<conversationId>` entry
 * — switching between them clobbered each other's scroll position (the reported
 * "sub-agent jumps near the top" bug). When a `subagentId` is present we key on
 * it FIRST (`sub:<subagentId>`), mirroring the store's existing
 * `_subOverrideKey(subagentId)` convention ("NEVER key sub-agents by the parent
 * conversationId — that would collide"). Each agent view then remembers and
 * restores its OWN position independently.
 */
export function chatScrollKey(
  tabId: TabId | null,
  conversationId: ConversationId | null | undefined,
  subagentId?: string | null,
): MemoryKey | null {
  if (subagentId !== null && subagentId !== undefined && subagentId !== "") {
    return `sub:${subagentId}`;
  }
  if (conversationId !== null && conversationId !== undefined && conversationId !== "") {
    return `c:${conversationId}`;
  }
  if (tabId !== null && tabId !== "") {
    return `t:${tabId}`;
  }
  return null;
}

function safeSessionStorage(): Storage | null {
  try {
    // Accessing `sessionStorage` can throw in some sandboxed/privacy contexts.
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

/** Lazily load the persisted map from sessionStorage into the in-memory Map. */
function hydrate(): void {
  if (hydrated) return;
  hydrated = true;
  const ss = safeSessionStorage();
  if (ss === null) return;
  // Best-effort purge of the legacy v1 key so it does not linger forever as
  // orphan data in the user's sessionStorage after the schema upgrade.
  try {
    ss.removeItem(LEGACY_STORAGE_KEY_V1);
  } catch {
    /* ignore */
  }
  try {
    const raw = ss.getItem(STORAGE_KEY);
    if (raw === null) return;
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return;
    }
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof k !== "string" || k === "") continue;
      if (v === null || typeof v !== "object" || Array.isArray(v)) continue;
      const obj = v as Record<string, unknown>;
      const st = obj.scrollTop;
      const ab = obj.wasAtBottom;
      if (typeof st !== "number" || !Number.isFinite(st) || st < 0) continue;
      if (typeof ab !== "boolean") continue;
      memory.set(k, { scrollTop: st, wasAtBottom: ab });
    }
  } catch {
    /* corrupt payload — ignore, start fresh */
  }
}

/** Mirror the in-memory Map back to sessionStorage (best-effort). */
function persist(): void {
  const ss = safeSessionStorage();
  if (ss === null) return;
  try {
    ss.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(memory)));
  } catch {
    /* quota / disabled storage — non-fatal */
  }
}

export interface ChatScrollMemory {
  /** Record the latest scroll entry for a key (in memory + persisted). */
  save(key: MemoryKey, entry: ScrollEntry): void;
  /** Recorded entry for a key, or `null` if none has been saved. */
  get(key: MemoryKey): ScrollEntry | null;
  /** Drop a key's saved entry (call when a conversation is destroyed). */
  forget(key: MemoryKey): void;
}

export function useChatScrollMemory(): ChatScrollMemory {
  hydrate();
  return {
    save(key: MemoryKey, entry: ScrollEntry): void {
      if (key === "") return;
      if (!Number.isFinite(entry.scrollTop) || entry.scrollTop < 0) return;
      if (typeof entry.wasAtBottom !== "boolean") return;
      memory.set(key, { scrollTop: entry.scrollTop, wasAtBottom: entry.wasAtBottom });
      persist();
    },
    get(key: MemoryKey): ScrollEntry | null {
      const v = memory.get(key);
      return v === undefined ? null : { ...v };
    },
    forget(key: MemoryKey): void {
      if (memory.delete(key)) persist();
    },
  };
}

/**
 * Drop scroll memory for every possible key of a tab (sub-agent-scoped,
 * conversation-scoped and tab-scoped). Standalone helper (not the composable)
 * so non-component call sites (e.g. the chat-tabs store's `closeTab` in its
 * `destroy` mode) can purge a dead conversation's entry without instantiating
 * the composable inside a Pinia action.
 */
export function forgetChatScroll(
  tabId: TabId | null,
  conversationId: ConversationId | null | undefined,
  subagentId?: string | null,
): void {
  hydrate();
  let mutated = false;
  if (subagentId !== null && subagentId !== undefined && subagentId !== "") {
    mutated = memory.delete(`sub:${subagentId}`) || mutated;
  }
  if (conversationId !== null && conversationId !== undefined && conversationId !== "") {
    mutated = memory.delete(`c:${conversationId}`) || mutated;
  }
  if (tabId !== null && tabId !== "") {
    mutated = memory.delete(`t:${tabId}`) || mutated;
  }
  if (mutated) persist();
}

/** Test-only: reset the module singleton between specs. */
export function _resetChatScrollMemory(): void {
  memory.clear();
  hydrated = false;
  const ss = safeSessionStorage();
  try {
    ss?.removeItem(STORAGE_KEY);
    ss?.removeItem(LEGACY_STORAGE_KEY_V1);
  } catch {
    /* ignore */
  }
}
