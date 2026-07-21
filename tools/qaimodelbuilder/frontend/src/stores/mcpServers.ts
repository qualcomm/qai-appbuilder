// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * MCP servers store — caches the configured MCP servers + their live status,
 * and polls status while the settings panel is open.
 *
 * Thin client over `@/api/mcpServers` (the 4 chat-context MCP endpoints). MCP
 * (Model Context Protocol) servers are external tool providers the chat agent
 * can connect to; once connected their tools are advertised to the LLM
 * alongside the built-in tools.
 *
 * State surface consumed by `McpServersPanel.vue`:
 *   servers  — reactive list of server statuses
 *   loading  — list fetch in-flight
 *   enabled  — master gate (chat_mcp_enabled); when false, servers configure
 *              but do not connect
 *   errors   — last error message (empty when none)
 *
 * PURE V2 enhancement (V1 has no MCP). The store owns HTTP + polling only.
 */
import { defineStore } from "pinia";
import { ref } from "vue";

import {
  addMcpServer,
  browseRegistry as apiBrowseRegistry,
  installFromCatalog,
  listCatalog,
  listMcpServers,
  refreshCatalog as apiRefreshCatalog,
  removeMcpServer,
  setGlobalEnabled as apiSetGlobalEnabled,
  setServerEnabled,
  testMcpServer,
  type McpCatalogEntry,
  type McpInstallBody,
  type McpServerConfigInput,
  type McpServerStatus,
} from "@/api/mcpServers";

/** Poll interval (ms) for refreshing connection status while panel is open. */
const POLL_INTERVAL_MS = 8000;

/** Online-catalog page size per browse. Set to the backend's hard ceiling (100)
 *  to maximise entries per request — the registry maps ~half of its raw listings
 *  to installable entries, so a 100-listing page yields ~50 cards, minimising the
 *  number of (auto-triggered) "load more" round-trips while browsing. */
const MARKET_PAGE_LIMIT = 100;

export const useMcpServersStore = defineStore("mcpServers", () => {
  const servers = ref<McpServerStatus[]>([]);
  const enabled = ref(false);
  const loading = ref(false);
  const error = ref("");
  // Per-server test-in-flight flags keyed by name.
  const testing = ref<Record<string, boolean>>({});
  // Curated marketplace catalog cache.
  const catalog = ref<McpCatalogEntry[]>([]);
  const catalogSources = ref<string[]>([]);
  const catalogLoading = ref(false);
  // Non-empty when the dynamic official-registry source is degraded.
  const catalogRegistryError = ref<string>("");

  // ── Registry browse (search + cursor pagination) — user-driven network ──
  // Accumulated online (registry-source) entries loaded via `browseRegistry`.
  const marketOnline = ref<McpCatalogEntry[]>([]);
  // Cursor for the NEXT page; null → no more pages (hide "load more").
  const marketNextCursor = ref<string | null>(null);
  // The active search term (name substring). Empty = browse all.
  const marketSearch = ref<string>("");
  // Browse-in-flight flag (search / load-more).
  const marketLoading = ref(false);
  // Non-empty when the last browse could not reach the registry.
  const marketRegistryError = ref<string>("");

  let pollTimer: ReturnType<typeof setInterval> | null = null;

  async function refresh(): Promise<void> {
    loading.value = true;
    error.value = "";
    try {
      const res = await listMcpServers();
      servers.value = res.servers;
      enabled.value = res.enabled;
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      loading.value = false;
    }
  }

  async function add(config: McpServerConfigInput): Promise<McpServerStatus> {
    const status = await addMcpServer(config);
    _upsert(status);
    return status;
  }

  async function remove(name: string): Promise<void> {
    await removeMcpServer(name);
    servers.value = servers.value.filter((s: McpServerStatus) => s.name !== name);
  }

  async function test(name: string): Promise<McpServerStatus> {
    testing.value = { ...testing.value, [name]: true };
    try {
      const status = await testMcpServer(name);
      _upsert(status);
      return status;
    } finally {
      const next = { ...testing.value };
      delete next[name];
      testing.value = next;
    }
  }

  function _upsert(status: McpServerStatus): void {
    const idx = servers.value.findIndex(
      (s: McpServerStatus) => s.name === status.name,
    );
    if (idx >= 0) {
      servers.value.splice(idx, 1, status);
    } else {
      servers.value.push(status);
    }
  }

  /** Load the curated marketplace catalog (cached; re-callable to refresh). */
  async function loadCatalog(): Promise<void> {
    catalogLoading.value = true;
    try {
      const res = await listCatalog();
      catalog.value = res.entries;
      catalogSources.value = res.sources;
      catalogRegistryError.value = res.registry_error ?? "";
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      catalogLoading.value = false;
    }
  }

  /**
   * Force a dynamic re-fetch of the catalog (bypasses server-side TTL). Updates
   * the same state as `loadCatalog`. Errors propagate so the caller can toast.
   */
  async function refreshCatalog(): Promise<void> {
    catalogLoading.value = true;
    try {
      const res = await apiRefreshCatalog();
      catalog.value = res.entries;
      catalogSources.value = res.sources;
      catalogRegistryError.value = res.registry_error ?? "";
    } finally {
      catalogLoading.value = false;
    }
  }

  /** Install a curated catalog entry (materialise + connect). */
  async function install(
    entryId: string,
    body: McpInstallBody,
  ): Promise<McpServerStatus> {
    const status = await installFromCatalog(entryId, body);
    _upsert(status);
    return status;
  }

  /** Flip one server's per-server enabled switch (on→connect, off→drop). */
  async function toggleEnabled(
    name: string,
    enabledValue: boolean,
  ): Promise<McpServerStatus> {
    const status = await setServerEnabled(name, enabledValue);
    _upsert(status);
    return status;
  }

  /**
   * Flip the GLOBAL master switch (on→connect all, off→disconnect all). Updates
   * `enabled` + the server list from the response. Errors propagate so the
   * caller can toast.
   */
  async function setGlobalEnabled(enabledValue: boolean): Promise<void> {
    const res = await apiSetGlobalEnabled(enabledValue);
    enabled.value = res.enabled;
    servers.value = res.servers;
  }

  /**
   * Browse the online (registry) source with search + cursor pagination.
   *
   * `reset` (or a search that differs from the current one) clears the loaded
   * online entries + cursor and fetches the FIRST page; otherwise the current
   * `marketNextCursor` is used to APPEND the next page ("load more"). Degrades
   * to a soft `marketRegistryError` banner on failure (never throws for a
   * registry outage — the backend returns 200 with an empty page).
   */
  async function browseRegistry(opts?: {
    search?: string;
    reset?: boolean;
  }): Promise<void> {
    const nextSearch = opts?.search ?? marketSearch.value;
    const searchChanged = nextSearch !== marketSearch.value;
    const reset = opts?.reset === true || searchChanged;
    // Record the (new) search term up-front so a concurrent input change is
    // captured, but do NOT clear the already-loaded results here — a failed
    // fetch must keep the current list visible (L1). We only REPLACE on a
    // SUCCESSFUL reset fetch below.
    if (reset) {
      marketSearch.value = nextSearch;
    } else if (marketNextCursor.value === null && marketOnline.value.length > 0) {
      // No more pages and not a reset → nothing to do.
      return;
    }
    marketLoading.value = true;
    try {
      const res = await apiBrowseRegistry({
        search: marketSearch.value || undefined,
        cursor: reset ? undefined : marketNextCursor.value ?? undefined,
        // Larger page → fewer "load more" round-trips (backend clamps to 100).
        limit: MARKET_PAGE_LIMIT,
      });
      if (reset) {
        // Success → replace with the fresh first page (new-search semantics).
        marketOnline.value = res.entries;
      } else {
        // Load-more → append, de-duped by id.
        const existing = new Set(marketOnline.value.map((e) => e.id));
        const fresh = res.entries.filter((e) => !existing.has(e.id));
        marketOnline.value = [...marketOnline.value, ...fresh];
      }
      marketNextCursor.value = res.next_cursor ?? null;
      marketRegistryError.value = res.registry_error ?? "";
    } catch (e) {
      // Failure → keep the current results (never wiped) + record the error.
      marketRegistryError.value = e instanceof Error ? e.message : String(e);
    } finally {
      marketLoading.value = false;
    }
  }

  /** Begin polling status (called when the panel mounts / becomes visible). */
  function startPolling(): void {
    if (pollTimer !== null) return;
    void refresh();
    pollTimer = setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
  }

  /** Stop polling (called when the panel unmounts / hides). */
  function stopPolling(): void {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  return {
    servers,
    enabled,
    loading,
    error,
    testing,
    catalog,
    catalogSources,
    catalogLoading,
    catalogRegistryError,
    marketOnline,
    marketNextCursor,
    marketSearch,
    marketLoading,
    marketRegistryError,
    refresh,
    add,
    remove,
    test,
    loadCatalog,
    refreshCatalog,
    install,
    toggleEnabled,
    setGlobalEnabled,
    browseRegistry,
    startPolling,
    stopPolling,
  };
});
