// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatTabs — multi-tab management composable (PR-054).
 *
 * Thin reactive bridge over the `useChatTabsStore` (Pinia). The store
 * is the single source of truth for tab state; this composable only
 * exposes a narrowed surface for SFC / view consumers.
 *
 * The 4-state machine, message buffers, and per-tab transient handles
 * (AbortController / WebSocket) live in the store; see
 * `src/stores/chatTabs.ts` for the full state machine and
 * refactor-plan §10.5/§10.6 for the contract.
 */
import { computed, type ComputedRef } from "vue";
import { storeToRefs } from "pinia";
import {
  useChatTabsStore,
  type ChatTab,
  type OpenTabInput,
  type TabId,
} from "@/stores/chatTabs";

export interface UseChatTabs {
  readonly tabs: ComputedRef<readonly ChatTab[]>;
  readonly activeTabId: ComputedRef<TabId | null>;
  readonly activeTab: ComputedRef<ChatTab | null>;
  /** True once the soft cap (`MAX_OPEN_TABS`) on open tabs is reached. */
  readonly atTabLimit: ComputedRef<boolean>;
  openTab(input?: OpenTabInput): ChatTab;
  closeTab(tabId: TabId, mode?: "keep" | "destroy"): void;
  /** Close every open tab at once, then land on a fresh blank tab. */
  closeAllTabs(): void;
  switchTab(tabId: TabId): void;
  renameTab(tabId: TabId, title: string): void;
  /** Move the `fromId` tab to the slot of the `toId` tab (drag reorder). */
  reorderTabs(fromId: TabId, toId: TabId): void;
}

export function useChatTabs(): UseChatTabs {
  const store = useChatTabsStore();
  const { tabs, activeTabId } = storeToRefs(store);

  const activeTab = computed<ChatTab | null>(() => {
    const id = activeTabId.value;
    if (id === null) {
      return null;
    }
    return tabs.value.find((tab) => tab.id === id) ?? null;
  });

  return {
    tabs: computed(() => tabs.value),
    activeTabId: computed(() => activeTabId.value),
    activeTab,
    atTabLimit: computed(() => store.atTabLimit),
    openTab(input) {
      return store.openTab(input ?? {});
    },
    closeTab(tabId, mode = "keep") {
      store.closeTab(tabId, mode);
    },
    closeAllTabs() {
      store.closeAllTabs();
    },
    switchTab(tabId) {
      store.switchTab(tabId);
    },
    renameTab(tabId, title) {
      store.renameTab(tabId, title);
    },
    reorderTabs(fromId, toId) {
      store.reorderTabs(fromId, toId);
    },
  };
}
