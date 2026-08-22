// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatPersistence — localStorage / IndexedDB cache composable.
 *
 * S5 PR-053: localStorage-only baseline. The legacy useChat.js wrote
 * to `window.localStorage` with ad-hoc keys; here we centralise the
 * read/write/clear and namespace each tab.
 *
 * IndexedDB-backed long-term storage is deferred to PR-054 (where it
 * coexists with the multi-tab WS state machine).
 */
import type { ChatMessage } from "./useChatState";

const KEY_PREFIX = "qai.chat.tab.";

export interface PersistedTabState {
  readonly version: 1;
  readonly tabId: string;
  readonly conversationId: string | null;
  readonly messages: readonly ChatMessage[];
  readonly updatedAt: number;
}

function safeStorage(): Storage | null {
  if (typeof globalThis === "undefined") {
    return null;
  }
  const candidate = (globalThis as { localStorage?: Storage }).localStorage;
  if (candidate === undefined) {
    return null;
  }
  try {
    const probe = "__qai_chat_persistence_probe__";
    candidate.setItem(probe, "1");
    candidate.removeItem(probe);
    return candidate;
  } catch {
    return null;
  }
}

export interface UseChatPersistence {
  load(tabId: string): PersistedTabState | null;
  save(state: PersistedTabState): void;
  remove(tabId: string): void;
  list(): readonly string[];
  clearAll(): void;
}

export function useChatPersistence(): UseChatPersistence {
  return {
    load(tabId) {
      const storage = safeStorage();
      if (storage === null) {
        return null;
      }
      const raw = storage.getItem(KEY_PREFIX + tabId);
      if (raw === null) {
        return null;
      }
      try {
        const parsed = JSON.parse(raw) as PersistedTabState;
        if (parsed.version !== 1 || parsed.tabId !== tabId) {
          return null;
        }
        return parsed;
      } catch {
        return null;
      }
    },
    save(state) {
      const storage = safeStorage();
      if (storage === null) {
        return;
      }
      storage.setItem(KEY_PREFIX + state.tabId, JSON.stringify(state));
    },
    remove(tabId) {
      const storage = safeStorage();
      if (storage === null) {
        return;
      }
      storage.removeItem(KEY_PREFIX + tabId);
    },
    list() {
      const storage = safeStorage();
      if (storage === null) {
        return [];
      }
      const ids: string[] = [];
      for (let i = 0; i < storage.length; i = i + 1) {
        const key = storage.key(i);
        if (key !== null && key.startsWith(KEY_PREFIX)) {
          ids.push(key.slice(KEY_PREFIX.length));
        }
      }
      return ids;
    },
    clearAll() {
      const storage = safeStorage();
      if (storage === null) {
        return;
      }
      const toRemove: string[] = [];
      for (let i = 0; i < storage.length; i = i + 1) {
        const key = storage.key(i);
        if (key !== null && key.startsWith(KEY_PREFIX)) {
          toRemove.push(key);
        }
      }
      for (const key of toRemove) {
        storage.removeItem(key);
      }
    },
  };
}
