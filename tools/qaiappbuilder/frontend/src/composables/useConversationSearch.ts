// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useConversationSearch — V1-parity full-text conversation search for
 * the sidebar (extracted from `AppSidebar.vue` cohesion split).
 *
 * V1 (`useChat.js:3064-3080` + `SidebarPanel.js:69-106`) does NOT filter
 * the in-memory list locally. Typing in the search box (debounced 300ms
 * — V1 `SidebarPanel.onSearchInput`) calls
 *   GET /api/conversations/search?q=...&limit=20
 * which searches across conversation titles AND message bodies on the
 * backend, returning `{ results: [{ id, title, snippet, ... }], total,
 * query }`. While a query is active the sidebar swaps the grouped list
 * for an independent results list (with snippet highlight / 🔍 / spinner
 * / no-results hint); clearing the box restores the grouped list. We
 * mirror that exactly here.
 *
 * The composable owns its own debounce timer and stale-response guard
 * (`searchSeq`), and registers an `onBeforeUnmount` cleanup so the
 * timer never outlives the host component. The `selectSearchResult`
 * handler delegates the actual tab-open / history-load to a callback
 * the host passes in, because that path still mutates the chat-tabs
 * store and lives inside the host component.
 */
import { computed, onBeforeUnmount, ref, watch, type ComputedRef, type Ref } from "vue";
import { apiJson } from "@/api";
import type { ConversationSummary } from "@/stores/conversations";

export interface ConversationSearchResult {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  snippet: string;
}

interface ConversationSearchResponse {
  results: ConversationSearchResult[];
  total: number;
  query: string;
}

export interface UseConversationSearchReturn {
  chatSearchQuery: Ref<string>;
  searchResults: Ref<ConversationSearchResult[]>;
  isSearching: Ref<boolean>;
  searchActive: ComputedRef<boolean>;
  selectSearchResult: (result: ConversationSearchResult) => Promise<void>;
}

export function useConversationSearch(opts: {
  /**
   * Open / bind the conversation in the chat-tabs store. Mirrors the
   * grouped-list click path so a search-result click reaches the same
   * end state. The host owns this because it touches the chat-tabs
   * store + lazy history loader.
   */
  onSelect: (conv: ConversationSummary) => Promise<void>;
}): UseConversationSearchReturn {
  const chatSearchQuery = ref("");
  const searchResults = ref<ConversationSearchResult[]>([]);
  const isSearching = ref(false);

  // True whenever the user has a non-empty query → render the search
  // results list instead of the grouped list (V1 SidebarPanel.js:84-106).
  const searchActive = computed(() => chatSearchQuery.value.trim() !== "");

  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  // Guards against a slow earlier request overwriting a newer one's results.
  let searchSeq = 0;

  async function runConversationSearch(query: string): Promise<void> {
    const q = query.trim();
    if (q === "") {
      searchResults.value = [];
      isSearching.value = false;
      return;
    }
    const seq = ++searchSeq;
    isSearching.value = true;
    try {
      const data = await apiJson<ConversationSearchResponse>(
        "GET",
        `/api/conversations/search?q=${encodeURIComponent(q)}&limit=20`,
      );
      // Ignore stale responses (V1's single ref is naturally last-write; we
      // replicate by dropping out-of-order completions).
      if (seq !== searchSeq) return;
      searchResults.value = data.results ?? [];
    } catch (err) {
      // V1 parity (useChat.js:3074-3076): swallow + empty results on failure.
      void err;
      if (seq === searchSeq) searchResults.value = [];
    } finally {
      if (seq === searchSeq) isSearching.value = false;
    }
  }

  // V1 SidebarPanel onSearchInput — 300ms debounce around the backend call.
  watch(chatSearchQuery, (next) => {
    if (searchTimer !== null) clearTimeout(searchTimer);
    const q = next.trim();
    if (q === "") {
      searchResults.value = [];
      isSearching.value = false;
      return;
    }
    searchTimer = setTimeout(() => {
      void runConversationSearch(q);
    }, 300);
  });

  async function selectSearchResult(
    result: ConversationSearchResult,
  ): Promise<void> {
    // Reuse the same open/bind path as a grouped-list click, then clear
    // the search box so the grouped list returns (V1 SidebarPanel.js:90).
    await opts.onSelect({
      id: result.id,
      title: result.title,
      status: result.status,
      created_at: result.created_at,
      updated_at: result.updated_at,
      message_count: result.message_count,
    });
    chatSearchQuery.value = "";
    searchResults.value = [];
  }

  onBeforeUnmount(() => {
    if (searchTimer !== null) {
      clearTimeout(searchTimer);
      searchTimer = null;
    }
  });

  return {
    chatSearchQuery,
    searchResults,
    isSearching,
    searchActive,
    selectSearchResult,
  };
}
