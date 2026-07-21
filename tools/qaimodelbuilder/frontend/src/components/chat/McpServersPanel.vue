<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * McpServersPanel — manage MCP (Model Context Protocol) servers.
 *
 * An MCP server is an external tool provider (a local `stdio` subprocess or a
 * remote `sse` / `http` endpoint) that exposes extra TOOLS. Once connected,
 * those tools are advertised to the chat LLM alongside the built-in tools —
 * the model decides when to call them and results flow back through the same
 * tool pipeline.
 *
 * Backend contract (interfaces/http/routes/chat/_mcp.py):
 *   GET    /api/chat/mcp/servers            → { servers: [...], enabled }
 *   POST   /api/chat/mcp/servers            → status (connect + discover)
 *   DELETE /api/chat/mcp/servers/{name}     → 204
 *   POST   /api/chat/mcp/servers/{name}/test → status (re-probe)
 *
 * Secure-by-default: when `enabled` is false (chat_mcp_enabled gate off),
 * servers can be configured but are not connected — the panel shows a banner.
 *
 * PURE V2 enhancement (V1 has no MCP; AGENTS.md 细则 4-bis protected).
 * Destructive actions use `<ConfirmDialog>` via `useConfirm()` — NEVER the
 * native `window.confirm` (AGENTS.md §3.9.2). MCP is keyed by a unique server
 * `name` — no per-user instance id (§3.9.1).
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { useMcpServersStore } from "@/stores/mcpServers";
import type {
  McpCatalogEntry,
  McpInstallBody,
  McpServerConfigInput,
  McpServerStatus,
  McpTransport,
} from "@/api/mcpServers";
import { useToast } from "@/composables/useToast";
import { useConfirm } from "@/composables/useConfirm";
// Shared help-manual affordance — see components/common/HelpButton.vue.
// Docs live under `frontend/src/help-content/mcp-add-server.<locale>.md`
// with the wireframe + troubleshooting SVGs under
// `frontend/public/help-images/mcp-add-server/`.
import HelpButton from "@/components/common/HelpButton.vue";

const { t } = useI18n();
const toast = useToast();
const { confirm } = useConfirm();
const store = useMcpServersStore();

const servers = computed<McpServerStatus[]>(() => store.servers);
const loading = computed(() => store.loading);
const gateEnabled = computed(() => store.enabled);

// Persistent per-server "last test result" (client-only UI state) so the user
// can glance at a card and know whether the last test passed, without having to
// catch the transient toast. Keyed by server name.
interface LastTestResult {
  ok: boolean;
  toolCount: number;
  resourceCount?: number;
  at: number;
  error?: string;
}
const lastTest = reactive<Record<string, LastTestResult>>({});
const catalog = computed<McpCatalogEntry[]>(() => store.catalog);
const catalogSources = computed<string[]>(() => store.catalogSources);
const catalogLoading = computed(() => store.catalogLoading);
const catalogRegistryError = computed<string>(() => store.catalogRegistryError);
// Online (registry-source) entries browsed via search / load-more, plus the
// cursor + browse-in-flight + degradation banner from the store.
const marketOnline = computed<McpCatalogEntry[]>(() => store.marketOnline);
const marketNextCursor = computed<string | null>(() => store.marketNextCursor);
const marketLoading = computed(() => store.marketLoading);
const marketRegistryError = computed<string>(() => store.marketRegistryError);
// The set of already-installed server names, so the marketplace can mark a
// catalog entry as installed and offer re-install vs disable.
const installedNames = computed(
  () => new Set(servers.value.map((s) => s.name)),
);

// Whether a market entry is already installed. Default install name = entry.id
// (both curated and registry: install_from_catalog defaults name=entry.id), so
// entry.id is the primary match; entry.name (the friendly title) is also matched
// to reduce mislabelling when it happens to equal the chosen server name. A
// user who explicitly renames on install may still not be marked (accepted
// small limitation — a rename is an intentional divergence from the catalog id).
function isEntryInstalled(entry: McpCatalogEntry): boolean {
  return installedNames.value.has(entry.id) || installedNames.value.has(entry.name);
}

// ─── Global master switch ────────────────────────────────────────────────────
const globalToggling = ref(false);

async function onToggleGlobal(): Promise<void> {
  if (globalToggling.value) return;
  const next = !gateEnabled.value;
  globalToggling.value = true;
  try {
    await store.setGlobalEnabled(next);
    toast.success(
      t(next ? "mcpServers.toast.globalEnabled" : "mcpServers.toast.globalDisabled"),
    );
  } catch (e) {
    toast.error(
      t("mcpServers.toast.globalFailed", {
        error: e instanceof Error ? e.message : String(e),
      }),
    );
  } finally {
    globalToggling.value = false;
  }
}

// ─── Marketplace source filter (built-in + online registry source) ───────────
//
// The online (registry) source is USER-DRIVEN: the backend never auto-fetches
// it. GET /catalog returns built-in (curated) + any already-cached registry
// entries (cold start → built-in only), and `sources` is always
// ["curated","registry"] so the selector always offers the online option.
// Browsing the online source (search / load-more) hits the network only on the
// user's action. We default the selector to "all".
const selectedSource = ref<string>("all");

// The marketplace entries actually SHOWN, assembled from two clean sources:
//   • the BUILT-IN (curated) entries come from GET /catalog (source==='curated');
//   • the ONLINE (registry) entries come ONLY from `marketOnline` — the CURRENT
//     browse/search result.
// We deliberately do NOT surface catalog.value's registry entries: that part
// mirrors the backend's `self._registry_cache`, which ACCUMULATES every
// previously-browsed item (install-resolution cache) and would otherwise
// pollute a fresh search with stale, non-matching cards (problem #3). The
// online list is thus driven purely by the live browse result + cursor.
const displayCatalog = computed<McpCatalogEntry[]>(() => {
  const curated = catalog.value.filter((e) => e.source === "curated");
  const online = marketOnline.value.filter((e) => e.source === "registry");
  return [...curated, ...online];
});

// The catalog filtered by the selected source ("all" shows everything).
const filteredCatalog = computed<McpCatalogEntry[]>(() => {
  if (selectedSource.value === "all") return displayCatalog.value;
  return displayCatalog.value.filter((e) => e.source === selectedSource.value);
});

/** i18n label for a source badge, falling back to the raw source id. */
function sourceBadgeLabel(source: string): string {
  const key = `mcpServers.market.sourceBadge.${source}`;
  const label = t(key);
  // vue-i18n returns the key path unchanged when the message is missing.
  return label === key ? source : label;
}

/** Best display name for a market entry (title-ish name, falls back to id). */
function marketCardName(entry: McpCatalogEntry): string {
  return entry.name || entry.id;
}

/** The command-namespace prefix of a registry name, shown as an author hint. */
function marketAuthorHint(entry: McpCatalogEntry): string {
  // Registry ids are slugs; the descriptive namespace is not always present.
  // Use the homepage host when available as a light provenance hint.
  if (entry.source !== "registry" || !entry.homepage) return "";
  try {
    return new URL(entry.homepage).host;
  } catch {
    return "";
  }
}

// Whether the online source is offered at all (always true under the new
// backend contract — sources is always ["curated","registry"]). Gates the
// source selector so a hypothetical built-in-only backend still hides it.
const hasDynamicSource = computed(() => catalogSources.value.length > 1);

// Whether the online source has actually been browsed — true once the current
// browse result holds at least one entry. Driven ONLY by `marketOnline` (the
// live result), NOT by catalog.value's install-resolution cache, so the online
// list + its empty-state/load affordances track the actual search/browse state.
const registryLoaded = computed(() => marketOnline.value.length > 0);

// Show the online empty-state guide as a full replacement for the grid when the
// user filters to the online source ALONE but nothing has been loaded yet.
const showRegistryEmpty = computed(
  () =>
    hasDynamicSource.value &&
    !registryLoaded.value &&
    !refreshingCatalog.value &&
    !marketLoading.value &&
    selectedSource.value === "registry",
);

// Show the "load more" affordance only when an online cursor is pending and the
// current view includes the online source.
const showLoadMore = computed(
  () =>
    marketNextCursor.value !== null &&
    (selectedSource.value === "all" || selectedSource.value === "registry"),
);

// The registry degradation banner text (prefer the live browse error, else the
// catalog-load error).
const registryErrorText = computed(
  () => marketRegistryError.value || catalogRegistryError.value,
);

// Refresh-in-flight flag for the "Load catalog" button.
const refreshingCatalog = ref(false);

// ─── Search (debounced, name substring, server-side) ─────────────────────────
const searchInput = ref<string>("");
let searchTimer: ReturnType<typeof setTimeout> | null = null;

function onSearchInput(): void {
  if (searchTimer !== null) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    void runSearch();
  }, 300);
}

function onSearchEnter(): void {
  if (searchTimer !== null) clearTimeout(searchTimer);
  void runSearch();
}

async function runSearch(): Promise<void> {
  try {
    await store.browseRegistry({ search: searchInput.value.trim(), reset: true });
  } catch (e) {
    toast.error(
      t("mcpServers.toast.searchFailed", {
        error: e instanceof Error ? e.message : String(e),
      }),
    );
  }
}

async function onLoadMore(): Promise<void> {
  if (marketLoading.value) return;
  await store.browseRegistry();
}

// ─── Infinite scroll ────────────────────────────────────────────────────────
// A sentinel <div> is rendered at the bottom of the market grid (only while
// there IS a next page). When it scrolls into view we auto-load the next page,
// so the user does not have to click "load more". The button remains as an
// explicit fallback (and for environments without IntersectionObserver).
const loadMoreSentinel = ref<HTMLElement | null>(null);
let marketObserver: IntersectionObserver | null = null;

function onSentinelIntersect(entries: IntersectionObserverEntry[]): void {
  // Guard: only auto-load when the sentinel is actually visible, a next page
  // exists, and we are not already loading (prevents duplicate/racing fetches).
  if (!entries.some((e) => e.isIntersecting)) return;
  if (!showLoadMore.value || marketLoading.value) return;
  void onLoadMore();
}

function setupMarketObserver(): void {
  if (typeof IntersectionObserver === "undefined") return; // graceful fallback
  if (!marketObserver) {
    // rootMargin pre-loads a little before the sentinel is fully visible so the
    // next page is ready by the time the user reaches the end.
    marketObserver = new IntersectionObserver(onSentinelIntersect, {
      rootMargin: "200px",
    });
  }
  marketObserver.disconnect();
  if (loadMoreSentinel.value) marketObserver.observe(loadMoreSentinel.value);
}

// Re-attach the observer whenever the sentinel is (re)rendered — it only exists
// while `showLoadMore` is true, so the ref goes null↔element as pages load.
watch(loadMoreSentinel, () => setupMarketObserver());

async function onRefreshCatalog(): Promise<void> {
  if (refreshingCatalog.value) return;
  refreshingCatalog.value = true;
  try {
    // A first-page browse (respecting the current search) loads the online
    // source without discarding the built-in list.
    await store.browseRegistry({ search: searchInput.value.trim(), reset: true });
    const count = marketOnline.value.length;
    toast.success(t("mcpServers.toast.refreshed", { count }));
  } catch (e) {
    toast.error(
      t("mcpServers.toast.refreshFailed", {
        error: e instanceof Error ? e.message : String(e),
      }),
    );
  } finally {
    refreshingCatalog.value = false;
  }
}

// ─── Add-server modal state ──────────────────────────────────────────────────

const modalOpen = ref(false);
const saving = ref(false);

interface DraftHeader {
  key: string;
  value: string;
}

const draft = reactive<{
  name: string;
  transport: McpTransport;
  command: string;
  args: string; // whitespace-separated in the UI
  url: string;
  headers: DraftHeader[];
  timeout_s: number;
}>({
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  url: "",
  headers: [],
  timeout_s: 30,
});

function resetDraft(): void {
  draft.name = "";
  draft.transport = "stdio";
  draft.command = "";
  draft.args = "";
  draft.url = "";
  draft.headers = [];
  draft.timeout_s = 30;
}

function openAddModal(): void {
  resetDraft();
  modalOpen.value = true;
}

function closeModal(): void {
  modalOpen.value = false;
}

function addHeaderRow(): void {
  draft.headers.push({ key: "", value: "" });
}

function removeHeaderRow(index: number): void {
  draft.headers.splice(index, 1);
}

const isStdio = computed(() => draft.transport === "stdio");

const canSubmit = computed(() => {
  if (!draft.name.trim()) return false;
  if (isStdio.value) return draft.command.trim().length > 0;
  return draft.url.trim().length > 0;
});

function buildConfig(): McpServerConfigInput {
  const headers: Record<string, string> = {};
  for (const h of draft.headers) {
    const k = h.key.trim();
    if (k) headers[k] = h.value;
  }
  return {
    name: draft.name.trim(),
    transport: draft.transport,
    command: isStdio.value ? draft.command.trim() : null,
    args: isStdio.value
      ? draft.args.split(/\s+/).filter((a) => a.length > 0)
      : [],
    url: isStdio.value ? null : draft.url.trim(),
    headers,
    timeout_s: Number(draft.timeout_s) || 30,
  };
}

async function submitAdd(): Promise<void> {
  if (!canSubmit.value || saving.value) return;
  saving.value = true;
  try {
    const status = await store.add(buildConfig());
    if (status.connected) {
      toast.success(
        t("mcpServers.toast.added", {
          name: status.name,
          count: status.tool_count,
        }),
      );
    } else if (!gateEnabled.value) {
      toast.info(t("mcpServers.toast.savedDisabled", { name: status.name }));
    } else {
      toast.error(
        t("mcpServers.toast.connectFailed", {
          name: status.name,
          error: status.error,
        }),
      );
    }
    closeModal();
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  } finally {
    saving.value = false;
  }
}

// ─── Row actions ─────────────────────────────────────────────────────────────

async function onTest(server: McpServerStatus): Promise<void> {
  try {
    const status = await store.test(server.name);
    if (status.connected) {
      lastTest[server.name] = {
        ok: true,
        toolCount: status.tool_count,
        resourceCount: status.resource_count,
        at: Date.now(),
      };
      toast.success(
        t("mcpServers.toast.tested", {
          name: status.name,
          count: status.tool_count,
        }),
      );
    } else {
      lastTest[server.name] = {
        ok: false,
        toolCount: status.tool_count,
        at: Date.now(),
        error: status.error,
      };
      toast.error(
        t("mcpServers.toast.connectFailed", {
          name: status.name,
          error: status.error,
        }),
      );
    }
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    lastTest[server.name] = { ok: false, toolCount: 0, at: Date.now(), error: msg };
    toast.error(msg);
  }
}

async function onRemove(server: McpServerStatus): Promise<void> {
  const ok = await confirm({
    icon: "🗑️",
    title: t("mcpServers.confirm.removeTitle"),
    message: t("mcpServers.confirm.removeMessage", { name: server.name }),
    confirmText: t("mcpServers.action.remove"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
  });
  if (!ok) return;
  try {
    await store.remove(server.name);
    toast.success(t("mcpServers.toast.removed", { name: server.name }));
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

function isTesting(name: string): boolean {
  return Boolean(store.testing[name]);
}

// ─── Marketplace: install flow + per-server enabled toggle ───────────────────

// Install-args modal (opened when a catalog entry has any required input).
const installModalOpen = ref(false);
const installing = ref(false);
const installEntry = ref<McpCatalogEntry | null>(null);
// Reactive map of placeholder → user value for the entry being installed.
const installArgs = reactive<Record<string, string>>({});
// Reactive maps for env / header credential values (stdio-keyed / remote).
const installEnv = reactive<Record<string, string>>({});
const installHeaders = reactive<Record<string, string>>({});

function _clearInstallArgs(): void {
  for (const k of Object.keys(installArgs)) delete installArgs[k];
  for (const k of Object.keys(installEnv)) delete installEnv[k];
  for (const k of Object.keys(installHeaders)) delete installHeaders[k];
}

// The env field names an entry needs (union of env_required + env_schema).
function envFieldsOf(entry: McpCatalogEntry): string[] {
  const names = [...(entry.env_required ?? []), ...(entry.env_schema ?? [])];
  return [...new Set(names)];
}

// The header field names an entry needs (union of headers_required + schema).
function headerFieldsOf(entry: McpCatalogEntry): string[] {
  const names = [
    ...(entry.headers_required ?? []),
    ...(entry.headers_schema ?? []),
  ];
  return [...new Set(names)];
}

const installEnvFields = computed<string[]>(() =>
  installEntry.value ? envFieldsOf(installEntry.value) : [],
);
const installHeaderFields = computed<string[]>(() =>
  installEntry.value ? headerFieldsOf(installEntry.value) : [],
);

/** True when the given field name is secret (render as password input). */
function isSecretField(name: string): boolean {
  return Boolean(installEntry.value?.secret_fields?.includes(name));
}

/** Whether an entry needs the install dialog (any required input present). */
function needsInstallDialog(entry: McpCatalogEntry): boolean {
  return (
    entry.requires_args.length > 0 ||
    (entry.env_required?.length ?? 0) > 0 ||
    (entry.headers_required?.length ?? 0) > 0
  );
}

async function onInstall(entry: McpCatalogEntry): Promise<void> {
  if (needsInstallDialog(entry)) {
    // Needs user-supplied input → open the install dialog.
    installEntry.value = entry;
    _clearInstallArgs();
    for (const ph of entry.requires_args) installArgs[ph] = "";
    for (const name of envFieldsOf(entry)) installEnv[name] = "";
    for (const name of headerFieldsOf(entry)) installHeaders[name] = "";
    installModalOpen.value = true;
    return;
  }
  // No required input → install directly.
  await _doInstall(entry, {});
}

function closeInstallModal(): void {
  installModalOpen.value = false;
  installEntry.value = null;
  _clearInstallArgs();
}

const canSubmitInstall = computed(() => {
  const entry = installEntry.value;
  if (!entry) return false;
  const argsOk = entry.requires_args.every(
    (ph) => (installArgs[ph] ?? "").trim().length > 0,
  );
  const envOk = (entry.env_required ?? []).every(
    (name) => (installEnv[name] ?? "").trim().length > 0,
  );
  const headersOk = (entry.headers_required ?? []).every(
    (name) => (installHeaders[name] ?? "").trim().length > 0,
  );
  return argsOk && envOk && headersOk;
});

async function submitInstall(): Promise<void> {
  const entry = installEntry.value;
  if (!entry || !canSubmitInstall.value || installing.value) return;
  const body: McpInstallBody = {};
  const argValues: Record<string, string> = {};
  for (const ph of entry.requires_args)
    argValues[ph] = (installArgs[ph] ?? "").trim();
  if (Object.keys(argValues).length > 0) body.arg_values = argValues;

  const envValues: Record<string, string> = {};
  for (const name of envFieldsOf(entry)) {
    const v = installEnv[name] ?? "";
    if (v !== "") envValues[name] = v;
  }
  if (Object.keys(envValues).length > 0) body.env_values = envValues;

  const headerValues: Record<string, string> = {};
  for (const name of headerFieldsOf(entry)) {
    const v = installHeaders[name] ?? "";
    if (v !== "") headerValues[name] = v;
  }
  if (Object.keys(headerValues).length > 0) body.header_values = headerValues;

  await _doInstall(entry, body);
}

async function _doInstall(
  entry: McpCatalogEntry,
  body: McpInstallBody,
): Promise<void> {
  installing.value = true;
  try {
    const status = await store.install(entry.id, {
      arg_values: {},
      ...body,
      // Pin the install to the source the user actually clicked so a shared
      // entry id (curated vs registry, e.g. both "git") is disambiguated.
      source: entry.source,
    });
    if (status.connected) {
      toast.success(
        t("mcpServers.toast.installed", {
          name: status.name,
          count: status.tool_count,
        }),
      );
    } else if (!gateEnabled.value) {
      toast.info(t("mcpServers.toast.savedDisabled", { name: status.name }));
    } else {
      toast.error(
        t("mcpServers.toast.connectFailed", {
          name: status.name,
          error: status.error,
        }),
      );
    }
    closeInstallModal();
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  } finally {
    installing.value = false;
  }
}

async function onToggleEnabled(server: McpServerStatus): Promise<void> {
  const next = !(server.enabled ?? true);
  try {
    await store.toggleEnabled(server.name, next);
    toast.success(
      t(next ? "mcpServers.toast.enabled" : "mcpServers.toast.disabled", {
        name: server.name,
      }),
    );
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

function statusClass(server: McpServerStatus): string {
  if (server.connected) return "mcp-dot--ok";
  if (server.error) return "mcp-dot--err";
  return "mcp-dot--idle";
}

function statusTitle(server: McpServerStatus): string {
  if (server.connected) return t("mcpServers.status.connected");
  if (server.error) return server.error;
  return t("mcpServers.status.idle");
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(() => {
  store.startPolling();
  void store.loadCatalog();
});

onUnmounted(() => {
  store.stopPolling();
  marketObserver?.disconnect();
  marketObserver = null;
});
</script>

<template>
  <div
    class="mcp-panel"
    data-testid="mcp-servers-panel"
  >
    <header class="mcp-panel__head">
      <div>
        <div class="mcp-panel__title-row">
          <h3 class="mcp-panel__title">{{ t("mcpServers.title") }}</h3>
          <!-- Inline help affordance next to the panel title so users
               discovering MCP for the first time can open the manual
               without leaving the settings screen. External link points
               at the MCP protocol homepage. -->
          <HelpButton
            doc-key="mcp-add-server"
            external-url="https://modelcontextprotocol.io/"
            size="sm"
          />
        </div>
        <p class="mcp-panel__subtitle">{{ t("mcpServers.subtitle") }}</p>
      </div>
      <div class="mcp-panel__head-actions">
        <button
          type="button"
          class="mcp-global-toggle"
          :class="{ 'mcp-global-toggle--on': gateEnabled }"
          role="switch"
          :aria-checked="gateEnabled ? 'true' : 'false'"
          :aria-label="gateEnabled ? t('mcpServers.global.disable') : t('mcpServers.global.enable')"
          :disabled="globalToggling"
          data-testid="mcp-global-toggle"
          @click="onToggleGlobal"
        >
          <span class="mcp-global-toggle__track">
            <span class="mcp-global-toggle__thumb" />
          </span>
          <span class="mcp-global-toggle__label">
            {{ gateEnabled ? t("mcpServers.global.on") : t("mcpServers.global.off") }}
          </span>
        </button>
        <button
          type="button"
          class="btn btn-primary btn-sm"
          data-testid="mcp-add-btn"
          @click="openAddModal"
        >
          + {{ t("mcpServers.action.add") }}
        </button>
      </div>
    </header>

    <!-- Master-gate disabled banner — points at the switch above (no operator
         / chat_mcp_enabled wording; the user can flip it themselves). -->
    <p
      v-if="!gateEnabled"
      class="mcp-panel__gate-warning"
      data-testid="mcp-disabled-banner"
    >
      ⚠️ {{ t("mcpServers.gateDisabled") }}
    </p>

    <!-- ═══ Group A: my MCP connections (added) ═══ -->
    <section class="mcp-group" data-testid="mcp-group-added">
      <h4 class="mcp-group__title">{{ t("mcpServers.group.added") }}</h4>

      <!-- Loading skeleton -->
      <div
        v-if="loading && servers.length === 0"
        class="mcp-panel__loading"
      >
        {{ t("common.loading") }}
      </div>

      <!-- Empty state -->
      <div
        v-else-if="servers.length === 0"
        class="mcp-panel__empty"
        data-testid="mcp-empty"
      >
        <p class="mcp-panel__empty-title">{{ t("mcpServers.empty.title") }}</p>
        <p class="mcp-panel__empty-hint">{{ t("mcpServers.empty.hint") }}</p>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          @click="openAddModal"
        >
          + {{ t("mcpServers.action.add") }}
        </button>
      </div>

      <!-- Server card list -->
      <ul
        v-else
        class="mcp-list"
      >
      <li
        v-for="server in servers"
        :key="server.name"
        class="mcp-card"
        :class="{ 'mcp-card--disabled': (server.enabled ?? true) === false }"
        data-testid="mcp-card"
      >
        <div class="mcp-card__main">
          <span
            class="mcp-dot"
            :class="statusClass(server)"
            :title="statusTitle(server)"
          />
          <div class="mcp-card__info">
            <div class="mcp-card__name-row">
              <span class="mcp-card__name">{{ server.name }}</span>
              <span class="mcp-badge">{{ server.transport }}</span>
              <span
                v-if="(server.enabled ?? true) === false"
                class="mcp-badge mcp-badge--off"
              >
                {{ t("mcpServers.status.disabledBadge") }}
              </span>
              <span
                v-if="server.connected"
                class="mcp-card__tools"
                :title="t('mcpServers.capabilities.toolsTooltip')"
              >
                {{ t("mcpServers.toolCount", { count: server.tool_count }) }}
              </span>
              <span
                v-if="server.connected && (server.resource_count ?? 0) > 0"
                class="mcp-card__tools"
                :title="t('mcpServers.capabilities.resourcesTooltip')"
              >
                {{ t("mcpServers.resourceCount", { count: server.resource_count ?? 0 }) }}
              </span>
              <span
                v-if="server.connected && (server.prompt_count ?? 0) > 0"
                class="mcp-card__tools"
                :title="t('mcpServers.capabilities.promptsTooltip')"
              >
                {{ t("mcpServers.promptCount", { count: server.prompt_count ?? 0 }) }}
              </span>
            </div>
            <div class="mcp-card__endpoint">
              {{ server.transport === "stdio" ? server.command : server.url }}
            </div>
            <div
              v-if="server.error"
              class="mcp-card__error"
            >
              {{ server.error }}
            </div>
            <div
              v-if="isTesting(server.name) || lastTest[server.name]"
              class="mcp-card__last-test"
              :class="{
                'mcp-card__last-test--ok':
                  !isTesting(server.name) && lastTest[server.name]?.ok,
                'mcp-card__last-test--err':
                  !isTesting(server.name) && lastTest[server.name] && !lastTest[server.name]?.ok,
              }"
              :data-testid="`mcp-last-test-${server.name}`"
              :title="!isTesting(server.name) && lastTest[server.name] && !lastTest[server.name]?.ok
                ? (lastTest[server.name]?.error || '')
                : ''"
            >
              <template v-if="isTesting(server.name)">
                {{ t("mcpServers.lastTest.testing") }}
              </template>
              <template v-else-if="lastTest[server.name]?.ok">
                {{
                  (lastTest[server.name]?.toolCount ?? 0) > 0
                    ? t("mcpServers.lastTest.passWithTools", {
                        count: lastTest[server.name]?.toolCount ?? 0,
                      })
                    : t("mcpServers.lastTest.pass")
                }}
              </template>
              <template v-else>
                {{ t("mcpServers.lastTest.fail") }}
              </template>
            </div>
          </div>
        </div>
        <div class="mcp-card__actions">
          <button
            type="button"
            class="btn btn-ghost btn-sm mcp-card__toggle"
            :class="{ 'mcp-card__toggle--off': (server.enabled ?? true) === false }"
            data-testid="mcp-toggle-btn"
            @click="onToggleEnabled(server)"
          >
            {{ (server.enabled ?? true) ? t("mcpServers.action.disable") : t("mcpServers.action.enable") }}
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="isTesting(server.name)"
            data-testid="mcp-test-btn"
            @click="onTest(server)"
          >
            <span
              v-if="isTesting(server.name)"
              class="mcp-spinner"
            />
            {{ isTesting(server.name) ? t("mcpServers.action.testing") : t("mcpServers.action.test") }}
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-sm mcp-card__remove"
            data-testid="mcp-remove-btn"
            @click="onRemove(server)"
          >
            {{ t("mcpServers.action.remove") }}
          </button>
        </div>
      </li>
    </ul>
    </section>

    <!-- ═══ Group B: browse marketplace (built-in + online) ═══ -->
    <section class="mcp-group mcp-market" data-testid="mcp-market">
      <header class="mcp-market__head">
        <div>
          <h4 class="mcp-group__title">{{ t("mcpServers.market.title") }}</h4>
          <p class="mcp-market__subtitle">{{ t("mcpServers.market.subtitle") }}</p>
        </div>
        <div class="mcp-market__controls">
          <!-- Name search (debounced) — browses the online source server-side. -->
          <input
            v-model="searchInput"
            type="search"
            class="config-input mcp-market__search"
            :placeholder="t('mcpServers.market.search')"
            data-testid="mcp-market-search"
            @input="onSearchInput"
            @keyup.enter="onSearchEnter"
          />
          <!-- Source selector — appears once an online source is offered. -->
          <select
            v-if="hasDynamicSource"
            v-model="selectedSource"
            class="config-input mcp-market__source"
            data-testid="mcp-source-select"
          >
            <option value="all">{{ t("mcpServers.market.sourceAll") }}</option>
            <option v-for="src in catalogSources" :key="src" :value="src">
              {{ sourceBadgeLabel(src) }}
            </option>
          </select>
          <!-- Load / refresh the online catalog — a user-driven network fetch.
               Label reads "Load catalog" until loaded, then "Refresh". -->
          <button
            v-if="hasDynamicSource"
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="refreshingCatalog || marketLoading"
            data-testid="mcp-refresh-btn"
            @click="onRefreshCatalog"
          >
            {{
              refreshingCatalog
                ? t("mcpServers.market.refreshing")
                : registryLoaded
                  ? t("mcpServers.market.refresh")
                  : t("mcpServers.market.loadRegistry")
            }}
          </button>
        </div>
      </header>

      <!-- Online-source degradation banner — soft, non-native. -->
      <p
        v-if="registryErrorText"
        class="mcp-panel__gate-warning"
        data-testid="mcp-registry-error"
      >
        ⚠️ {{ t("mcpServers.market.registryError", { error: registryErrorText }) }}
      </p>

      <div
        v-if="(catalogLoading || marketLoading) && filteredCatalog.length === 0"
        class="mcp-panel__loading"
      >
        {{ t("common.loading") }}
      </div>

      <!-- Online empty-state guide — full replacement for the grid when the
           user filters to the online source ALONE but nothing has loaded yet. -->
      <div
        v-else-if="showRegistryEmpty"
        class="mcp-registry-empty"
        data-testid="mcp-registry-empty"
      >
        <p class="mcp-registry-empty__title">
          {{ t("mcpServers.market.registryEmpty") }}
        </p>
        <p class="mcp-registry-empty__hint">
          {{ t("mcpServers.market.registryEmptyHint") }}
        </p>
        <button
          type="button"
          class="btn btn-primary btn-sm"
          :disabled="refreshingCatalog || marketLoading"
          data-testid="mcp-registry-load-btn"
          @click="onRefreshCatalog"
        >
          {{
            refreshingCatalog || marketLoading
              ? t("mcpServers.market.refreshing")
              : t("mcpServers.market.loadRegistry")
          }}
        </button>
      </div>

      <template v-else>
        <ul class="mcp-market__grid">
          <li
            v-for="entry in filteredCatalog"
            :key="entry.source + ':' + entry.id"
            class="mcp-market-card"
            data-testid="mcp-market-card"
          >
            <div class="mcp-market-card__head">
              <span class="mcp-market-card__name">{{ marketCardName(entry) }}</span>
              <span class="mcp-market-card__badges">
                <span
                  v-if="entry.source && entry.source !== 'curated'"
                  class="mcp-badge mcp-badge--source"
                  data-testid="mcp-source-badge"
                >
                  {{ sourceBadgeLabel(entry.source) }}
                </span>
                <span class="mcp-badge">{{ entry.install_type }}</span>
              </span>
            </div>
            <p
              v-if="marketAuthorHint(entry)"
              class="mcp-market-card__author"
            >
              {{ marketAuthorHint(entry) }}
            </p>
            <p class="mcp-market-card__desc">{{ entry.description }}</p>
            <div class="mcp-market-card__foot">
              <a
                v-if="entry.homepage"
                class="mcp-market-card__link"
                :href="entry.homepage"
                target="_blank"
                rel="noopener noreferrer"
              >{{ t("mcpServers.market.docs") }}</a>
              <button
                type="button"
                class="btn btn-primary btn-sm"
                :disabled="installing || isEntryInstalled(entry)"
                data-testid="mcp-install-btn"
                @click="onInstall(entry)"
              >
                {{
                  isEntryInstalled(entry)
                    ? t("mcpServers.market.added")
                    : t("mcpServers.market.install")
                }}
              </button>
            </div>
          </li>
        </ul>

        <!-- Infinite scroll: an observed sentinel auto-loads the next page when
             it scrolls into view; the button below is the explicit fallback. -->
        <div
          v-if="showLoadMore"
          ref="loadMoreSentinel"
          class="mcp-market__sentinel"
          data-testid="mcp-market-sentinel"
          aria-hidden="true"
        />
        <!-- Load more (online cursor pagination) — fallback / manual trigger. -->
        <div
          v-if="showLoadMore"
          class="mcp-market__more"
        >
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="marketLoading"
            data-testid="mcp-market-load-more"
            @click="onLoadMore"
          >
            {{ marketLoading ? t("mcpServers.market.loadingMore") : t("mcpServers.market.loadMore") }}
          </button>
        </div>
      </template>
    </section>

    <!-- Add-server modal -->
    <Teleport to="body">
      <div
        v-if="modalOpen"
        class="mcp-modal-overlay"
        data-testid="mcp-add-modal"
        @click.self="closeModal"
      >
        <div
          class="mcp-modal"
          role="dialog"
          aria-modal="true"
        >
          <header class="mcp-modal__head">
            <h4 class="mcp-modal__title">{{ t("mcpServers.modal.title") }}</h4>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :aria-label="t('common.cancel')"
              @click="closeModal"
            >
              ✕
            </button>
          </header>

          <div class="mcp-modal__body">
            <div class="config-field">
              <label class="config-label">{{ t("mcpServers.field.name") }}</label>
              <input
                v-model="draft.name"
                class="config-input"
                :placeholder="t('mcpServers.placeholder.name')"
                data-testid="mcp-field-name"
              />
            </div>

            <div class="config-field">
              <label class="config-label">{{ t("mcpServers.field.transport") }}</label>
              <div class="mcp-radio-row">
                <label
                  v-for="tr in (['stdio', 'sse', 'http'] as McpTransport[])"
                  :key="tr"
                  class="mcp-radio"
                >
                  <input
                    v-model="draft.transport"
                    type="radio"
                    :value="tr"
                  />
                  {{ tr }}
                </label>
              </div>
            </div>

            <!-- stdio fields -->
            <template v-if="isStdio">
              <div class="config-field">
                <label class="config-label">{{ t("mcpServers.field.command") }}</label>
                <input
                  v-model="draft.command"
                  class="config-input mono"
                  :placeholder="t('mcpServers.placeholder.command')"
                  data-testid="mcp-field-command"
                />
              </div>
              <div class="config-field">
                <label class="config-label">{{ t("mcpServers.field.args") }}</label>
                <input
                  v-model="draft.args"
                  class="config-input mono"
                  :placeholder="t('mcpServers.placeholder.args')"
                />
              </div>
            </template>

            <!-- sse / http fields -->
            <template v-else>
              <div class="config-field">
                <label class="config-label">{{ t("mcpServers.field.url") }}</label>
                <input
                  v-model="draft.url"
                  class="config-input mono"
                  :placeholder="t('mcpServers.placeholder.url')"
                  data-testid="mcp-field-url"
                />
              </div>
              <div class="config-field">
                <label class="config-label">{{ t("mcpServers.field.headers") }}</label>
                <div
                  v-for="(h, i) in draft.headers"
                  :key="i"
                  class="mcp-header-row"
                >
                  <input
                    v-model="h.key"
                    class="config-input mono"
                    :placeholder="t('mcpServers.placeholder.headerKey')"
                  />
                  <input
                    v-model="h.value"
                    class="config-input mono"
                    type="password"
                    :placeholder="t('mcpServers.placeholder.headerValue')"
                  />
                  <button
                    type="button"
                    class="btn btn-ghost btn-sm"
                    @click="removeHeaderRow(i)"
                  >
                    🗑️
                  </button>
                </div>
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  @click="addHeaderRow"
                >
                  + {{ t("mcpServers.action.addHeader") }}
                </button>
              </div>
            </template>

            <div class="config-field">
              <label class="config-label">{{ t("mcpServers.field.timeout") }}</label>
              <input
                v-model.number="draft.timeout_s"
                class="config-input"
                type="number"
                min="1"
                max="600"
              />
            </div>
          </div>

          <footer class="mcp-modal__footer">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="closeModal"
            >
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              class="btn btn-primary btn-sm"
              :disabled="!canSubmit || saving"
              data-testid="mcp-submit-btn"
              @click="submitAdd"
            >
              {{ saving ? t("mcpServers.action.connecting") : t("mcpServers.action.connect") }}
            </button>
          </footer>
        </div>
      </div>
    </Teleport>

    <!-- Install-args modal (for catalog entries needing e.g. a path) -->
    <Teleport to="body">
      <div
        v-if="installModalOpen"
        class="mcp-modal-overlay"
        data-testid="mcp-install-modal"
        @click.self="closeInstallModal"
      >
        <div class="mcp-modal" role="dialog" aria-modal="true">
          <header class="mcp-modal__head">
            <h4 class="mcp-modal__title">
              {{ t("mcpServers.market.installTitle", { name: installEntry?.name ?? "" }) }}
            </h4>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :aria-label="t('common.cancel')"
              @click="closeInstallModal"
            >✕</button>
          </header>
          <div class="mcp-modal__body">
            <p class="mcp-market__hint">{{ t("mcpServers.market.installHint") }}</p>
            <div
              v-for="ph in (installEntry?.requires_args ?? [])"
              :key="ph"
              class="config-field"
            >
              <label class="config-label">{{ ph }}</label>
              <input
                v-model="installArgs[ph]"
                class="config-input mono"
                :placeholder="ph"
                data-testid="mcp-install-arg"
              />
            </div>

            <!-- Env credential fields (stdio-keyed entries). -->
            <template v-if="installEnvFields.length > 0">
              <p class="mcp-market__hint" data-testid="mcp-env-hint">
                {{ t("mcpServers.market.envFieldsHint") }}
              </p>
              <div
                v-for="name in installEnvFields"
                :key="'env-' + name"
                class="config-field"
              >
                <label class="config-label">{{ name }}</label>
                <input
                  v-model="installEnv[name]"
                  class="config-input mono"
                  :type="isSecretField(name) ? 'password' : 'text'"
                  :placeholder="name"
                  data-testid="mcp-install-env"
                />
              </div>
            </template>

            <!-- Header credential fields (remote sse/http entries). -->
            <template v-if="installHeaderFields.length > 0">
              <p class="mcp-market__hint" data-testid="mcp-header-hint">
                {{ t("mcpServers.market.headerFieldsHint") }}
              </p>
              <div
                v-for="name in installHeaderFields"
                :key="'hdr-' + name"
                class="config-field"
              >
                <label class="config-label">{{ name }}</label>
                <input
                  v-model="installHeaders[name]"
                  class="config-input mono"
                  :type="isSecretField(name) ? 'password' : 'text'"
                  :placeholder="name"
                  data-testid="mcp-install-header"
                />
              </div>
            </template>
          </div>
          <footer class="mcp-modal__footer">
            <button type="button" class="btn btn-ghost btn-sm" @click="closeInstallModal">
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              class="btn btn-primary btn-sm"
              :disabled="!canSubmitInstall || installing"
              data-testid="mcp-install-submit"
              @click="submitInstall"
            >
              {{ installing ? t("mcpServers.market.installing") : t("mcpServers.market.install") }}
            </button>
          </footer>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.mcp-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.mcp-panel__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.mcp-panel__title {
  font-size: var(--text-md, var(--text-base));
  font-weight: 700;
  margin: 0;
  color: var(--text-primary);
}
/* Title + inline HelpButton row. Keeps the title-baseline alignment so the
 * ℹ️ affordance reads as a first-class part of the title, not a floating
 * action. Uses gap tokens so light/dark themes and rem-scale layouts are
 * unaffected. */
.mcp-panel__title-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.mcp-panel__subtitle {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: var(--space-1) 0 0;
}
.mcp-panel__gate-warning {
  font-size: var(--text-sm);
  color: var(--warning, var(--error));
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--warning, var(--border));
  border-radius: var(--radius-md, 8px);
  background: var(--bg-secondary);
  line-height: 1.4;
}
.mcp-panel__loading,
.mcp-panel__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-6) var(--space-4);
  border: 1px dashed var(--border);
  border-radius: var(--radius-md, 8px);
  color: var(--text-secondary);
}
.mcp-panel__empty-title {
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}
.mcp-panel__empty-hint {
  font-size: var(--text-sm);
  margin: 0;
  text-align: center;
}

/* ── Server cards ─────────────────────────────────────────────────────── */
.mcp-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  list-style: none;
  margin: 0;
  padding: 0;
}
.mcp-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 8px);
  background: var(--bg-secondary);
}
.mcp-card__main {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  min-width: 0;
}
.mcp-dot {
  flex: 0 0 auto;
  width: 10px;
  height: 10px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--text-secondary);
}
.mcp-dot--ok {
  background: var(--success, #22c55e);
}
.mcp-dot--err {
  background: var(--error, #ef4444);
}
.mcp-dot--idle {
  background: var(--text-secondary, #9ca3af);
}
.mcp-card__info {
  min-width: 0;
}
.mcp-card__name-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.mcp-card__name {
  font-weight: 600;
  color: var(--text-primary);
}
.mcp-badge {
  font-size: var(--text-xs, var(--text-sm));
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  text-transform: uppercase;
  /* Never let a multi-word badge (e.g. "在线获取" / "Built-in") wrap to a
     vertical stack in a tight card. */
  white-space: nowrap;
}
.mcp-card__tools {
  font-size: var(--text-xs, var(--text-sm));
  color: var(--text-secondary);
}
.mcp-card__endpoint {
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs, var(--text-sm));
  color: var(--text-secondary);
  margin-top: var(--space-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}
.mcp-card__error {
  font-size: var(--text-xs, var(--text-sm));
  color: var(--error);
  margin-top: var(--space-1);
}
.mcp-card__last-test {
  font-size: var(--text-xs, var(--text-sm));
  color: var(--text-secondary);
  margin-top: var(--space-1);
}
.mcp-card__last-test--ok {
  color: var(--success, #22c55e);
}
.mcp-card__last-test--err {
  color: var(--error, #ef4444);
}
.mcp-card__actions {
  display: flex;
  gap: var(--space-2);
  flex: 0 0 auto;
}
.mcp-card__remove {
  color: var(--error);
}

/* ── Add modal ────────────────────────────────────────────────────────── */
/* Reuses the project-wide dialog tokens (same as `.rename-dialog-overlay`
   in styles/components/components.css): `--overlay-bg` backdrop + blur,
   z-index 9000, `--bg-secondary` surface. Teleported to <body> so it is not
   trapped inside the panel's stacking context. */
.mcp-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--overlay-bg, rgba(0, 0, 0, 0.5));
  backdrop-filter: blur(4px);
  padding: var(--space-4);
}
.mcp-modal {
  width: 100%;
  max-width: 520px;
  max-height: 85vh;
  overflow-y: auto;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg, 12px);
  box-shadow: var(--shadow-lg, 0 10px 30px rgba(0, 0, 0, 0.3));
  display: flex;
  flex-direction: column;
}
.mcp-modal__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4);
  border-bottom: 1px solid var(--border);
}
.mcp-modal__title {
  margin: 0;
  font-size: var(--text-md, var(--text-base));
  font-weight: 700;
  color: var(--text-primary);
}
.mcp-modal__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
}
.mcp-modal__footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-4);
  border-top: 1px solid var(--border);
}
.mcp-radio-row {
  display: flex;
  gap: var(--space-4);
}
.mcp-radio {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-sm);
  color: var(--text-primary);
  cursor: pointer;
  text-transform: uppercase;
}
.mcp-header-row {
  display: flex;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}
.mcp-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  margin-right: var(--space-1);
  border: 2px solid var(--border);
  border-top-color: var(--accent, var(--text-primary));
  border-radius: 50%;
  animation: mcp-spin 0.7s linear infinite;
  vertical-align: middle;
}
@keyframes mcp-spin {
  to {
    transform: rotate(360deg);
  }
}

/* ── Per-server enabled toggle + disabled state ───────────────────────── */
.mcp-card--disabled {
  opacity: 0.55;
}
.mcp-badge--off {
  background: var(--bg-secondary);
  color: var(--text-secondary);
}
.mcp-card__toggle--off {
  color: var(--text-secondary);
  opacity: 0.85;
}

/* ── Marketplace (curated catalog) ────────────────────────────────────── */
.mcp-market {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.mcp-market__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.mcp-market__head > :first-child {
  /* The title/subtitle block shrinks so it never pushes the controls out of
     the container's right edge. */
  flex: 1 1 auto;
  min-width: 0;
}
.mcp-market__title {
  font-size: var(--text-md, var(--text-base));
  font-weight: 700;
  margin: 0;
  color: var(--text-primary);
}
.mcp-market__subtitle {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: var(--space-1) 0 0;
}
.mcp-market__source {
  flex: 0 1 auto;
  max-width: 180px;
  min-width: 0;
}
.mcp-market__controls {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  /* Keep the search + source select + load button on one line, hugging the
     right edge; they may wrap on a narrow panel. */
  flex: 1 1 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.mcp-market__controls > .btn {
  flex: 0 0 auto;
  white-space: nowrap;
}
.mcp-market-card__badges {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  /* Keep the badges together on the right; they may wrap as a group but each
     badge stays on one line (never squeezed to a vertical single-char stack). */
  flex: 0 0 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.mcp-badge--source {
  background: var(--accent-soft, var(--bg-tertiary));
  color: var(--accent, var(--text-secondary));
}
.mcp-market__grid {
  display: grid;
  /* Cards need a comfortable minimum so the head (long name + badges) never
     collapses; auto-fill packs as many as fit per row. */
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--space-4);
  list-style: none;
  margin: 0;
  padding: 0;
}
.mcp-market-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 8px);
  background: var(--bg-secondary);
}
.mcp-market-card__head {
  display: flex;
  /* Top-align so a wrapped long title and the badges line up cleanly. */
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
}
.mcp-market-card__name {
  font-weight: 600;
  color: var(--text-primary);
  /* Let the name shrink + wrap (long slugs like "ac.tandem/docs-mcp") instead
     of pushing the badges off-card. */
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.mcp-market-card__desc {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: 0;
  flex: 1 1 auto;
}
.mcp-market-card__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.mcp-market-card__link {
  font-size: var(--text-sm);
  color: var(--accent, var(--text-primary));
  text-decoration: none;
}
.mcp-market-card__link:hover {
  text-decoration: underline;
}
.mcp-market__hint {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin: 0;
}

/* ── Registry empty-state guide (user-driven load) ────────────────────── */
.mcp-registry-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-6) var(--space-4);
  border: 1px dashed var(--border);
  border-radius: var(--radius-md, 8px);
  color: var(--text-secondary);
  text-align: center;
}
.mcp-registry-empty__title {
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}
.mcp-registry-empty__hint {
  font-size: var(--text-sm);
  margin: 0;
}

/* ── Global master switch ─────────────────────────────────────────────── */
.mcp-panel__head-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 0 0 auto;
}
.mcp-global-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 8px);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  cursor: pointer;
  font-size: var(--text-sm);
}
.mcp-global-toggle:disabled {
  opacity: 0.6;
  cursor: default;
}
.mcp-global-toggle--on {
  color: var(--text-primary);
  border-color: var(--success, var(--accent, var(--border)));
}
.mcp-global-toggle__track {
  position: relative;
  display: inline-block;
  width: 34px;
  height: 18px;
  border-radius: 9px;
  background: var(--bg-tertiary);
  transition: background 0.15s ease;
}
.mcp-global-toggle--on .mcp-global-toggle__track {
  background: var(--success, var(--accent, #22c55e));
}
.mcp-global-toggle__thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--bg-primary, #fff);
  transition: transform 0.15s ease;
}
.mcp-global-toggle--on .mcp-global-toggle__thumb {
  transform: translateX(16px);
}
.mcp-global-toggle__label {
  white-space: nowrap;
}

/* ── Groups (one list, two sections) ──────────────────────────────────── */
.mcp-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.mcp-group__title {
  font-size: var(--text-sm);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin: 0;
  color: var(--text-secondary);
}

/* ── Search + load-more ───────────────────────────────────────────────── */
.mcp-market__search {
  flex: 1 1 160px;
  min-width: 0;
  max-width: 260px;
}
.mcp-market__more {
  display: flex;
  justify-content: center;
  padding-top: var(--space-2);
}
.mcp-market__sentinel {
  width: 100%;
  height: 1px;
}
.mcp-market-card__author {
  font-size: var(--text-xs, var(--text-sm));
  color: var(--text-secondary);
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
