<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Application sidebar — V1 UI structure.
 *
 * Uses global CSS classes from styles/layout/layout.css (.sidebar,
 * .sidebar-header, .nav-item, .sidebar-footer, etc.).
 * SVG icons replace emoji. Active state via router-link active-class.
 *
 * P4-A (T2): the "Recent Chats" section now reads from the backend
 * (`GET /api/chat/conversations`) instead of in-memory tabs, supports
 * search, time-grouping (today / this week / this month / earlier),
 * inline rename and delete. Click a row → bind to a tab (opening one
 * if needed) and lazy-load historical messages.
 */
import { computed, onMounted, ref, watch } from "vue";
import { RouterLink, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useUiStore } from "@/stores/ui";
import { useCommandPaletteStore } from "@/stores/commandPalette";
import { useChatTabsStore, type ChatTabStatus } from "@/stores/chatTabs";
import {
  useConversationsStore,
  type ConversationSummary,
} from "@/stores/conversations";
import { useSkillsStore } from "@/stores/skills";
import { useDownloadsStore } from "@/stores/downloads";
import { useAuthStore } from "@/stores/auth";
import { useFontSize } from "@/composables/useFontSize";
import { useConfirm } from "@/composables/useConfirm";
import { useToast } from "@/composables/useToast";
import { useConversationGrouping, CONV_GROUP_CAP } from "@/composables/useConversationGrouping";
import { useConversationRename } from "@/composables/useConversationRename";
import { useConversationWorkspace } from "@/composables/useConversationWorkspace";
import { useConversationSearch } from "@/composables/useConversationSearch";
import { useConversationPin } from "@/composables/useConversationPin";
import { useLanguageSwitch } from "@/composables/useLanguageSwitch";
import { useFontSizePopover } from "@/composables/useFontSizePopover";
import { useReboot } from "@/composables/useReboot";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { apiJson, ApiError } from "@/api";
import RenameDialog from "@/components/chat/RenameDialog.vue";
import ConversationWorkspaceDialog from "@/components/chat/ConversationWorkspaceDialog.vue";
import FavoritesDialog from "@/components/chat/FavoritesDialog.vue";
import SidebarUserButton from "@/components/layout/SidebarUserButton.vue";
const { t } = useI18n();
const toast = useToast();
const router = useRouter();
const ui = useUiStore();
// SSO snapshot — decides whether the Restart footer icon is shown
// (only when the user-avatar menu, which now carries Restart, is absent).
const auth = useAuthStore();

// On mobile the sidebar slides over the content; once the user navigates
// anywhere (RouterLink to Settings/Channels/… or a programmatic push) the
// overlay should dismiss so the destination view is visible. A single route
// watcher covers every navigation path uniformly. No-op on desktop (the flag
// stays false there).
watch(
  () => router.currentRoute.value.fullPath,
  () => {
    if (ui.mobileSidebarOpen) ui.setMobileSidebarOpen(false);
  },
);
const palette = useCommandPaletteStore();
const chatTabs = useChatTabsStore();
const conversationsStore = useConversationsStore();
const skillsStore = useSkillsStore();
const fontSizeCtl = useFontSize();
const { confirm } = useConfirm();
const { requestReboot } = useReboot();
const { toolbarModules, load: loadForgeConfig } = useForgeConfig();

// ─── Brand title (V1 SidebarPanel.js:298-302 `brandTitleKey`) ────────────────
// The sidebar brand title defaults to "QAI AppBuilder" (`sidebar.titleAppBuilder`)
// and only flips to the ModelBuilder brand while the user is actively inside a
// Model Builder mode (`activeToolMode` ∈ {`model-build`, `pro`}). Any other
// state — the default landing (`null`), App Builder, Code, Translate, PPT —
// shows "QAI AppBuilder". Exiting Model Builder mode restores the App Builder
// brand. This is purely a reactive label change driven by `activeToolMode`;
// selecting the i18n key here keeps every locale free to localise (V2 i18n:
// `sidebar.title` / `sidebar.titleAppBuilder` / `sidebar.titlePro`). Read-only
// from the ui store.
//
// MB Pro (internal-only): when the build ships the MB Pro / 增强 capability,
// the Model Builder brand becomes "QAI ModelBuilder Pro" (`sidebar.titlePro`).
// Detection follows the project's edition-agnostic convention (NO frontend
// edition flag — see ModelDropdown 查询服务 precedent): we read whether the
// backend forge-config exposed the ``pro`` toolbar module, which it injects
// ONLY on internal editions (forge_config.py `_inject_toolbar_defaults`,
// gated by `settings.is_internal`). On external/Release builds the ``pro``
// key is absent (the frontend defaults carry no `pro` fallback), so the Model
// Builder brand stays "QAI ModelBuilder".
const mbProAvailable = computed<boolean>(
  () => toolbarModules.value["pro"]?.enabled === true,
);
const isModelBuilderMode = computed<boolean>(
  () =>
    ui.activeToolMode === "model-build" || ui.activeToolMode === "pro",
);
const brandTitle = computed(() => {
  if (isModelBuilderMode.value) {
    return mbProAvailable.value ? t("sidebar.titlePro") : t("sidebar.title");
  }
  return t("sidebar.titleAppBuilder");
});

// ─── App version (from package.json via vite define) ─────────────────────────
const appVersion = __APP_VERSION__;

// ─── Downloads nav activity indicator (V1 SidebarPanel.js:54 +
//     app.js:2003-2006 `downloading`) ───────────────────────────────────────
// V1 lights the `nav-dl-ring` + `nav-dl-dot` animation on the Downloads nav
// item whenever a download/install is in flight:
//   downloading = downloadCenter.isAnyDownloading || downloadCenter.isAnyInstalling
// V2 split that single V1 composable into the shared Download Center
// orchestrator singleton (`useDownloadsStore`). The orchestrator is the same
// instance the Download Center panel uses, so an SSE download started from the
// panel reflects here even though this component lives in the always-mounted
// sidebar (mirrors V1's top-level `downloadCenterComposable`). We light the
// indicator when any task is preparing/downloading (`activeDownloads`) OR the
// aria2c bootstrap is installing (V1's `isAnyInstalling` equivalent).
const downloadsStore = useDownloadsStore();
const downloadActive = computed<boolean>(
  () =>
    downloadsStore.activeDownloads.value.length > 0 ||
    downloadsStore.aria2c.status.value.install_status === "installing",
);

// ─── Recent Chats (backend-backed, shared store) ─────────────────────────────
// C-2 (V1 useChat.js:529-557): the conversation list lives in a shared
// pinia store so a newly created / renamed / deleted chat reflects in
// the sidebar immediately (mirrors V1's single in-memory ref).

const conversations = computed(() => conversationsStore.conversations);
const historyLoading = computed(() => conversationsStore.loading);
const historyLoadFailed = computed(() => conversationsStore.loadFailed);

async function fetchConversations(): Promise<void> {
  await conversationsStore.fetch();
}

// ─── Backend full-text conversation search (V1 useChat.js:3064-3080 +
//     SidebarPanel.js:69-106) ──────────────────────────────────────────────
// State + 300ms debounce + stale-guard + on-cleanup-clear-timer all live in
// `useConversationSearch`; this component just feeds the click callback.
const {
  chatSearchQuery,
  searchResults,
  isSearching,
  searchActive,
  selectSearchResult,
} = useConversationSearch({ onSelect: selectConversation });

// ─── Rename dialog (H-1: V1 RenameDialog.js — replaces window.prompt) ─────────
// State + handlers extracted to useConversationRename composable so this
// file stays within the cohesion budget. Behaviour is identical (zero
// template change — RenameDialog props bind the same names).
//
// Declared BEFORE useConversationGrouping so `renameOpen` can be passed as the
// grouping `freeze` flag (bug1 fix — see below): while the rename dialog is
// open we stop re-sorting the conversation list so a `.conv-item` cannot move
// out from under the pointer and make the click land on the dialog backdrop.
const {
  renameOpen,
  renameValue,
  renameLoading,
  renameTarget,
  renameConversation,
  cancelRename,
  confirmRename,
} = useConversationRename();

// ─── Session workspace dialog (V2 enhancement — no V1 equivalent) ────────────
// Lets a conversation override the global default write directory with its own
// session-level workspace. State + handlers extracted to
// useConversationWorkspace (mirrors useConversationRename); the dialog is a
// sibling of RenameDialog in the template. Declared before
// useConversationGrouping so it can join the grouping `freeze` flag (same bug1
// fix as rename — keep the list ordering stable while a dialog is open so a
// `.conv-item` can't slide out from under the pointer onto the backdrop).
const {
  workspaceOpen,
  workspaceValue,
  workspaceLoading,
  setConversationWorkspace,
  cancelWorkspace,
  confirmWorkspace,
} = useConversationWorkspace();

// ─── Pin / favorite (置顶 / 收藏) ───────────────────────────────────────────
// Toggle a conversation's pin (lifts it to the top "📌 置顶" group) or
// favorite (surfaces it in the favorites library dialog). Both persist in
// conversation.meta server-side; the composable does the optimistic store
// flip + backend PATCH + rollback-on-failure. Tiny reversible actions → no
// confirm dialog.
const { togglePin, toggleFavorite } = useConversationPin();

// ─── Per-row "⋯" expand-in-place (방안 D) ────────────────────────────────────
// Hover-toolbar default shows 3 buttons (📌 / 🗑️ / ⋯) to minimise click-
// target collisions with the row body. Clicking ⋯ expands the SAME toolbar
// in place to the full 5 buttons (📌 / ✏️ / 📁 / ⭐ / 🗑️) — no floating
// popover, no second-stage positioning, just the toolbar growing. The ⋯
// button itself disappears once expanded (its job is done).
//
// We remember the expanded row by conversation id (null = none expanded).
// At most one row is expanded at any time; expanding another row implicitly
// collapses the previous one. Mouse-leave on a row also collapses it so the
// next hover starts fresh in the compact 3-button state — without this the
// row would silently remember its expanded state and surprise the user on
// the next hover-and-mis-click.
const convActionsExpandedFor = ref<string | null>(null);
function expandConvActions(convId: string): void {
  convActionsExpandedFor.value = convId;
}
function collapseConvActions(convId: string): void {
  if (convActionsExpandedFor.value === convId) {
    convActionsExpandedFor.value = null;
  }
}

// Favorites library dialog (⭐ 我的收藏) — opened from the footer icon row.
const favoritesOpen = ref(false);
function openFavorites(): void {
  favoritesOpen.value = true;
}
// Open the conversation a favorites-dialog row points at (reuse the same
// open/bind path as a normal sidebar click), then close the dialog.
async function selectFavorite(conv: ConversationSummary): Promise<void> {
  await selectConversation(conv);
  favoritesOpen.value = false;
}

// Freeze grouping while either the rename OR the workspace dialog is open.
const convDialogOpen = computed(
  () => renameOpen.value || workspaceOpen.value,
);

// V1 parity (useChat.js:288-378): five-bucket time grouping with
// per-group cap + expand/collapse, extracted to a composable so the
// type, constants, computed and toggle stay in one cohesive file.
// `freeze: convDialogOpen` keeps the list ordering stable while a dialog is
// open (bug1).
const { groupedConversations, toggleGroupExpanded } = useConversationGrouping(
  conversations,
  { freeze: convDialogOpen },
);

/** Rounds for a conversation — prefer backend round_count (user turns),
 *  fall back to message_count when absent. (V1 SidebarPanel uses
 *  roundCount = user-message count.) */
function convRounds(conv: ConversationSummary): number {
  return conv.round_count ?? conv.message_count ?? 0;
}

/** V1 threshold colouring (SidebarPanel.js:142): >=30 danger / >=20 warn. */
function roundsBadgeClass(n: number): string {
  if (n >= 30) return "conv-badge--danger";
  if (n >= 20) return "conv-badge--warn";
  return "conv-badge--info";
}

// ─── Multi-tab live status dot (V2 enhancement — V1 sidebar has no such
//     indicator) ──────────────────────────────────────────────────────────
// Part of the "multiple sessions in parallel" UX: when a conversation is open
// as a tab AND that tab is mid-flight (streaming / aborting / error), the
// sidebar row shows a small coloured dot so the user can see at a glance which
// background sessions are running. A conversation maps to AT MOST one tab
// (selectConversation reuses an existing tab by conversationId), so the first
// match is authoritative. Derived live from the shared `useChatTabsStore`
// reactive `tabs` array — no extra component-local state, no global ref; the
// store is the single source of truth (mirrors ChatTabStrip's per-tab dot).
//
// Returns `null` when the conversation has no open tab OR its tab is `idle`,
// so idle/closed conversations render no dot (avoids visual noise). Only the
// three "busy/attention" states surface a dot.
function convLiveStatus(convId: string): ChatTabStatus | null {
  const tab = chatTabs.tabs.find((t) => t.conversationId === convId);
  if (tab === undefined) return null;
  if (tab.status === "idle") return null;
  return tab.status;
}

/** i18n key for the sr-only status label, reusing the existing tab keys. */
function convStatusKey(status: ChatTabStatus): string {
  switch (status) {
    case "streaming":
      return "chat.tab.statusStreaming";
    case "aborting":
      return "chat.tab.statusAborting";
    case "error":
      return "chat.tab.statusError";
    default:
      return "chat.tab.statusIdle";
  }
}

// ─── Sub-agent expansion (V2 enhancement — no V1 equivalent) ─────────────────
// Each conversation row can be expanded to lazily reveal the sub-agents it
// dispatched (`GET /api/chat/conversations/{id}/subagents`). Clicking a
// sub-agent row opens it in a new tab (chatTabs.openSubAgentTab), where the
// user can inspect the transcript and take over the conversation. State is
// component-local (no global ref): a Set of expanded ids + per-conv item /
// loading maps, all keyed by conversation id.
interface SubAgentSummary {
  subagent_id: string;
  root_conversation_id: string;
  parent_message_id?: string;
  subagent_type?: string;
  title?: string | null;
  prompt_preview?: string | null;
  status: string;
  owner?: string;
  rounds?: number;
  created_at?: string;
  updated_at?: string;
}

const expandedConvIds = ref<Set<string>>(new Set());
const subAgentsByConv = ref<Record<string, SubAgentSummary[]>>({});
const subAgentsLoading = ref<Set<string>>(new Set());

function isConvExpanded(convId: string): boolean {
  return expandedConvIds.value.has(convId);
}

function subAgentsFor(convId: string): SubAgentSummary[] {
  return subAgentsByConv.value[convId] ?? [];
}

function isSubAgentsLoading(convId: string): boolean {
  return subAgentsLoading.value.has(convId);
}

async function loadSubAgents(convId: string): Promise<void> {
  subAgentsLoading.value = new Set(subAgentsLoading.value).add(convId);
  try {
    const res = await apiJson<{ items: SubAgentSummary[] }>(
      "GET",
      `/api/chat/conversations/${encodeURIComponent(convId)}/subagents`,
    );
    subAgentsByConv.value = {
      ...subAgentsByConv.value,
      [convId]: res.items ?? [],
    };
  } catch {
    // Degrade gracefully — show "no sub-agents" rather than an error row.
    subAgentsByConv.value = { ...subAgentsByConv.value, [convId]: [] };
  } finally {
    const next = new Set(subAgentsLoading.value);
    next.delete(convId);
    subAgentsLoading.value = next;
  }
}

/** Toggle the sub-agent expansion for a conversation row. First expand lazily
 *  fetches the sub-agent list. Click does NOT select the conversation
 *  (`@click.stop` on the toggle). */
function toggleConvExpanded(convId: string): void {
  const next = new Set(expandedConvIds.value);
  if (next.has(convId)) {
    next.delete(convId);
  } else {
    next.add(convId);
    if (subAgentsByConv.value[convId] === undefined) {
      void loadSubAgents(convId);
    }
  }
  expandedConvIds.value = next;
}

/** Open a sub-agent in a new tab (inspect + take over the conversation).
 *
 *  两级栏语义（2026-07-02 恢复）：一级栏 `ChatTabStrip` 只显示 main tab
 *  （kind !== "subagent"）；二级栏 `SubAgentRail` 平铺当前 main tab 下所有
 *  sub-agent（任意深度，用 `subagentMeta.rootConversationId` 过滤
 *  `state.subAgentIndex`——后端历史列表缓存，即便 chip 关闭也保留）。
 *  `openSubAgentTab` 创建（或复用）一个 `kind:"subagent"` tab 并 `switchTab`
 *  到它；ChatMessageList 挂在 `store.activeTab` 上，自然渲染新 tab 的
 *  `messages[]`。一级栏的焦点回落由 `activeMainTabId` getter 走
 *  `rootConversationId` 上溯到父 main tab，`SubAgentRail` 内部通过
 *  `railHighlightSid` computed 高亮当前 sub-agent chip——两个投影都从 store
 *  单一真值源派生。
 *
 *  跨父场景：sidebar 列出所有 conversation，选中的 sub-agent 可能属于
 *  **非当前主 tab** 的会话；store 会切到那个 sub-agent tab，其
 *  `subagentMeta.rootConversationId` 携带真实根会话 id，二级栏会随之切到
 *  对应父的 sub-agent 列表；如需在父会话上下文里查看，用户可再点主 tab。
 */
async function openSubAgentFromSidebar(subagentId: string): Promise<void> {
  try {
    await chatTabs.openSubAgentTab(subagentId);
  } catch (err) {
    console.error("[AppSidebar] openSubAgentTab failed", err);
    return;
  }
  _ensureChatRoute();
}

/**
 * V1 utils.js:84-98 `formatRelativeTime` — renders the conversation
 * timestamp shown in the sidebar:
 *   • Today    → "Today HH:MM" (locale-formatted clock)
 *   • Yesterday → "Yesterday"
 *   • Within last 7 days → "{n} days ago"
 *   • Older   → locale-formatted date (e.g. "5/14/2026")
 *
 * V1 input is a millisecond epoch; V2 backend returns ISO 8601, so we
 * parse it once here. Boundaries are calendar-based (since 00:00),
 * matching V1 exactly so that "Today" doesn't drift relative to the
 * "today" bucket header.
 */
function formatV1Time(iso: string): string {
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "";
  const now = new Date();
  const todayStart = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const yesterdayStart = todayStart - 86400000;
  if (ts >= todayStart) {
    return (
      t("util.todayPrefix") +
      new Date(ts).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })
    );
  }
  if (ts >= yesterdayStart) {
    return t("util.yesterday");
  }
  if (ts >= todayStart - 7 * 86400000) {
    // V1: Math.floor((todayStart - ts) / 86400000 + 1)  (so "yesterday + 1"
    // would already have been returned above; this branch is "2..7 days ago").
    const n = Math.floor((todayStart - ts) / 86400000 + 1);
    return t("util.daysAgo", { n });
  }
  return new Date(ts).toLocaleDateString();
}

async function selectConversation(conv: ConversationSummary): Promise<void> {
  // Reuse an existing tab if we already opened this conversation as a
  // MAIN-agent tab. A sub-agent tab (`kind: "subagent"`) is intentionally
  // bound to its PARENT conversation id, so it shares `conversationId` with
  // the main conversation — but it is NOT the main-agent tab. Excluding it
  // here is what lets the user open the main conversation even while its
  // sub-agent tab is already open (otherwise the find() below would match the
  // sub-agent tab and switchTab to it, making "open the main agent" a no-op).
  const existing = chatTabs.tabs.find(
    (tab) => tab.conversationId === conv.id && tab.kind !== "subagent",
  );
  if (existing) {
    chatTabs.switchTab(existing.id);
    _ensureChatRoute();
    return;
  }
  // If the workspace currently holds exactly ONE tab and it is a pristine,
  // unused "New Chat" (no conversation bound, no messages yet, idle), auto-
  // close it after we open the picked history conversation. Rationale: a
  // freshly-launched workspace (or one where the user closed everything) is
  // guaranteed by `closeTab` / `closeAllTabs` to keep at least one blank tab
  // alive (`chatTabs.ts:1008-1014`). When the user then picks a history
  // conversation from the sidebar, that blank tab is just placeholder noise —
  // collapsing it makes the picked history the only visible tab, matching
  // user expectation (no dangling empty "New Chat" tab beside the freshly
  // opened history). Safety: only fires when there is exactly one tab AND
  // that tab is the one we're about to displace — never touches tabs the
  // user has been working in.
  //
  // The check must run BEFORE `openTab` so `tabs.length === 1` refers to the
  // pre-open state. We capture the candidate id and close it AFTER the new
  // tab is created and activated (so closing it doesn't trigger
  // `closeTab`'s "last tab auto-reopens a blank one" fallback).
  const onlyTab = chatTabs.tabs.length === 1 ? chatTabs.tabs[0] : null;
  const blankTabToClose =
    onlyTab !== null &&
    onlyTab !== undefined &&
    onlyTab.kind !== "subagent" &&
    onlyTab.conversationId === null &&
    onlyTab.messages.length === 0 &&
    onlyTab.status === "idle"
      ? onlyTab.id
      : null;

  const tab = chatTabs.openTab({
    title: conv.title,
    conversationId: conv.id,
  });
  if (blankTabToClose !== null) {
    // Default `"keep"` mode is correct here: the blank tab has no
    // conversationId, no persisted tool override and no saved scroll, so
    // `keep` vs `destroy` are equivalent in effect; pick the non-destructive
    // semantic to match intent ("close an unused tab", not "the conversation
    // was deleted").
    chatTabs.closeTab(blankTabToClose);
  }
  // Lazy-load full message history (no-op if already populated).
  await chatTabs.loadHistoryMessages(tab.id);
  _ensureChatRoute();
}

/**
 * Navigate back to the chat view when the user picks a conversation from a
 * non-chat page (Settings / Security / Service / …).
 *
 * The conversation id lives purely in the ``chatTabs`` Pinia store (not a
 * route param), and ``AppSidebar`` is a sibling of ``<RouterView>``. So on a
 * non-chat route, updating the store alone changes nothing visible because
 * ``ChatView`` isn't mounted. Pushing ``/chat`` mounts it so the freshly
 * activated tab renders. Navigation is a view-orchestration concern, kept in
 * the component layer to preserve ``chatTabs.ts`` purity (it deliberately has
 * no router dependency).
 */
function _ensureChatRoute(): void {
  // On mobile, picking a conversation should also dismiss the slide-in
  // sidebar so the freshly opened chat is visible (the sidebar overlays the
  // content at ≤768px). No-op on desktop where the flag stays false.
  ui.setMobileSidebarOpen(false);
  if (router.currentRoute.value.name !== "chat") {
    void router.push({ name: "chat" });
  }
}

// ─── Rename dialog handlers ──────────────────────────────────────────────────
// (state + handlers come from useConversationRename, destructured above so
// `renameOpen` can feed the grouping freeze flag.)

async function deleteConversation(conv: ConversationSummary): Promise<void> {
  // V1 parity (`app.js:showConfirm` / `useChat.js:deleteConversation`): use
  // the global custom <ConfirmDialog> rather than the browser-native
  // `window.confirm`. Native dialogs are forbidden project-wide — see
  // `docs/80-operations/dev-environment.md` §"定制对话框" + AGENTS.md §3.9.
  const ok = await confirm({
    icon: "🗑️",
    title: t("chat.confirmDeleteTitle"),
    message: t("chat.confirmDeleteBody"),
    confirmText: t("common.delete", "Delete"),
    cancelText: t("common.cancel", "Cancel"),
    confirmStyle: "danger",
  });
  if (!ok) return;
  try {
    await apiJson<void>(
      "DELETE",
      `/api/chat/conversations/${encodeURIComponent(conv.id)}`,
    );
    conversationsStore.remove(conv.id);
    // If the deleted conv is the active tab, switch to a fresh tab.
    const tab = chatTabs.tabs.find((t) => t.conversationId === conv.id);
    if (tab) {
      chatTabs.closeTab(tab.id, "destroy");
      if (chatTabs.tabs.length === 0) {
        chatTabs.openTab({ title: t("chat.tab.untitled") });
      }
    }
  } catch (err) {
    // V1 parity: a failed delete must not fail silently — surface it via a
    // toast so the user knows the conversation is still there.
    const msg = err instanceof ApiError ? err.message : String(err);
    toast.error(t("app.deleteFailed", { msg }));
  }
}

onMounted(() => {
  void loadHistoryWithRetry();
  void skillsStore.ensureLoaded();
  // Ensure forge-config is loaded so the brand title can reflect MB Pro
  // availability even when the user's first screen is NOT the chat view (the
  // chat composer also loads it, but it may not be mounted yet). Idempotent —
  // useForgeConfig is a module singleton, so this never double-fetches.
  void loadForgeConfig();
});

// ─── History retry with backoff (改造 C) ─────────────────────────────────────
const historyRetries = ref(0);
const historyMaxRetries = 3;
const historyRetrying = ref(false);

async function loadHistoryWithRetry(): Promise<void> {
  historyRetries.value = 0;
  historyRetrying.value = true;
  while (historyRetries.value < historyMaxRetries) {
    await conversationsStore.fetch();
    if (!conversationsStore.loadFailed) {
      historyRetrying.value = false;
      return;
    }
    historyRetries.value++;
    if (historyRetries.value < historyMaxRetries) {
      await new Promise((r) => setTimeout(r, 5000));
    }
  }
  historyRetrying.value = false;
}

function retryHistory(): void {
  void loadHistoryWithRetry();
}

// ─── Footer / theme / lang ───────────────────────────────────────────────────

const nextTheme = computed(() => (ui.resolvedTheme === "dark" ? "light" : "dark"));

function toggleTheme(): void {
  ui.setTheme(nextTheme.value);
}

// ─── Language switcher (V1 LanguageSwitcher.js:25-106) ───────────────────────
// V1 renders a globe icon + short label (EN / 简中 / 繁中) that toggles a
// dropdown listing the full language names with a ✓ on the active one.
// V2 previously had a plain text button that just cycled the locale with a
// hard-coded English title. We restore the V1 globe + dropdown here, reusing
// the global `.lang-switcher*` classes from styles/components/components.css.
// ─── Language switcher (V1 LanguageSwitcher.js parity) ──────────────────────
// State + click-outside/ESC dismiss extracted to useLanguageSwitch
// composable. See `composables/useLanguageSwitch.ts`.
const {
  SUPPORTED_LANGS,
  LANG_FULL,
  langDropdownOpen,
  langWrapEl,
  currentLangLabel,
  toggleLangDropdown,
  selectLang,
} = useLanguageSwitch();

function reboot(): void {
  // V1 app.js:3382-3395 (`triggerReboot`): confirm → POST reboot → enter the
  // full-screen overlay + health-poll + auto-refresh transition. All of that
  // is owned by the shared `useReboot` controller (see composables/useReboot).
  void requestReboot();
}

// ─── Font-size popover (V1 SidebarPanel.js:215-260) ──────────────────────────
// State + click-outside/ESC dismiss extracted to useFontSizePopover
// composable. See `composables/useFontSizePopover.ts`.
const { fontSizePopoverOpen, fontSizeWrapEl, toggleFontSizePopover } =
  useFontSizePopover();

</script>

<template>
  <aside
    class="sidebar"
    :class="{ collapsed: ui.sidebarCollapsed, 'mobile-open': ui.mobileSidebarOpen }"
    :aria-label="t('layout.sidebar_aria')"
  >
    <!-- ── Header ─────────────────────────────────────────────────── -->
    <div class="sidebar-header">
      <div class="sidebar-logo">
        <!-- V2 brand mark (sidebar variant) — neural-network + NPU chip motif.
             Same design as welcome screen / favicon. Brand colors:
             #7c6cff (violet) → #60a5fa (sky-blue) gradient.
             Sized at 36x36 via .sidebar-logo-glyph in layout.css. -->
        <svg
          class="sidebar-logo-glyph"
          viewBox="0 0 40 40"
          fill="none"
        >
          <defs>
            <linearGradient id="sidebar-brand-grad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stop-color="#7c6cff" />
              <stop offset="100%" stop-color="#60a5fa" />
            </linearGradient>
          </defs>
          <rect
            class="sidebar-logo-tile"
            x="2"
            y="2"
            width="36"
            height="36"
            rx="9"
          />
          <!-- Network connection lines -->
          <line x1="12" y1="12" x2="20" y2="20" class="sidebar-logo-line" />
          <line x1="28" y1="12" x2="20" y2="20" class="sidebar-logo-line" />
          <line x1="12" y1="28" x2="20" y2="20" class="sidebar-logo-line" />
          <line x1="28" y1="28" x2="20" y2="20" class="sidebar-logo-line" />
          <line x1="10" y1="20" x2="20" y2="20" class="sidebar-logo-line" />
          <line x1="30" y1="20" x2="20" y2="20" class="sidebar-logo-line" />
          <!-- Center chip node -->
          <rect x="16" y="16" width="8" height="8" rx="1.5" fill="url(#sidebar-brand-grad)" />
          <!-- Outer nodes -->
          <circle cx="12" cy="12" r="2.5" fill="url(#sidebar-brand-grad)" opacity="0.8" />
          <circle cx="28" cy="12" r="2.5" fill="url(#sidebar-brand-grad)" opacity="0.8" />
          <circle cx="12" cy="28" r="2.5" fill="url(#sidebar-brand-grad)" opacity="0.8" />
          <circle cx="28" cy="28" r="2.5" fill="url(#sidebar-brand-grad)" opacity="0.8" />
          <circle cx="10" cy="20" r="2" fill="url(#sidebar-brand-grad)" opacity="0.6" />
          <circle cx="30" cy="20" r="2" fill="url(#sidebar-brand-grad)" opacity="0.6" />
        </svg>
      </div>
      <!-- V1 SidebarPanel.js:30-33 — title + subtitle wrapper. V1 has
           no inline overflow:hidden here; the parent .sidebar already
           clips. -->
      <div v-show="!ui.sidebarCollapsed">
        <div class="sidebar-title">
          {{ brandTitle }}
        </div>
        <div class="sidebar-subtitle">
          {{ t('sidebar.subtitle') }} <span class="brand-version">v{{ appVersion }}</span>
        </div>
      </div>
    </div>

    <!-- ── Navigation ─────────────────────────────────────────────── -->
    <nav
      class="sidebar-nav"
      :aria-label="t('nav.aria_label')"
    >
      <!-- Chat -->
      <RouterLink
        to="/chat"
        class="nav-item"
        active-class="active"
      >
        <span
          class="nav-icon-svg"
          aria-hidden="true"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        </span>
        <span
          v-show="!ui.sidebarCollapsed"
          class="nav-label"
        >{{ t("nav.chat") }}</span>
      </RouterLink>

      <!-- Skills -->
      <RouterLink
        to="/skills"
        class="nav-item"
        active-class="active"
      >
        <span
          class="nav-icon-svg"
          aria-hidden="true"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
          </svg>
        </span>
        <span
          v-show="!ui.sidebarCollapsed"
          class="nav-label"
        >{{ t("nav.skills") }}</span>
        <!-- Enabled-skills badge (V1 app.js:2000-2001) — shows the count
             of skills with mode !== 'off'; hidden when 0. Same shared
             store as the composer `⚡ N skills active` indicator. -->
        <span
          v-if="!ui.sidebarCollapsed && skillsStore.enabledSkillsCount > 0"
          class="nav-badge"
          aria-hidden="true"
        >{{ skillsStore.enabledSkillsCount }}</span>
      </RouterLink>

      <!-- Channels -->
      <RouterLink
        to="/channels"
        class="nav-item"
        active-class="active"
      >
        <span
          class="nav-icon-svg"
          aria-hidden="true"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <line
              x1="4"
              y1="6"
              x2="20"
              y2="6"
            />
            <line
              x1="4"
              y1="12"
              x2="20"
              y2="12"
            />
            <line
              x1="4"
              y1="18"
              x2="14"
              y2="18"
            />
          </svg>
        </span>
        <span
          v-show="!ui.sidebarCollapsed"
          class="nav-label"
        >{{ t("nav.channels") }}</span>
      </RouterLink>

      <!-- Settings -->
      <RouterLink
        to="/settings"
        class="nav-item"
        active-class="active"
      >
        <span
          class="nav-icon-svg"
          aria-hidden="true"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <circle
              cx="12"
              cy="12"
              r="3"
            />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68 1.65 1.65 0 0 0 10 3.17V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </span>
        <span
          v-show="!ui.sidebarCollapsed"
          class="nav-label"
        >{{ t("nav.settings") }}</span>
      </RouterLink>

      <!-- Security -->
      <RouterLink
        to="/security"
        class="nav-item"
        active-class="active"
      >
        <span
          class="nav-icon-svg"
          aria-hidden="true"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
        </span>
        <span
          v-show="!ui.sidebarCollapsed"
          class="nav-label"
        >{{ t("nav.security") }}</span>
      </RouterLink>

      <!-- Downloads -->
      <RouterLink
        to="/downloads"
        class="nav-item"
        active-class="active"
      >
        <span
          class="nav-icon-svg nav-dl-host"
          aria-hidden="true"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line
              x1="12"
              y1="15"
              x2="12"
              y2="3"
            />
          </svg>
          <!-- V1 SidebarPanel.js:54 — active-download indicator (ping ring +
               pulse dot). Rendered only while a download / aria2c install is
               in flight (V1 app.js:2003-2006 `downloading`). The
               `nav-dl-ring` / `nav-dl-dot` animations are owned by the global
               styles/layout/layout.css; we only host them here. -->
          <template v-if="downloadActive">
            <span class="nav-dl-ring"></span>
            <span class="nav-dl-dot"></span>
          </template>
        </span>
        <span
          v-show="!ui.sidebarCollapsed"
          class="nav-label"
        >{{ t("nav.downloads") }}</span>
      </RouterLink>

      <!-- Service -->
      <RouterLink
        to="/service"
        class="nav-item"
        active-class="active"
      >
        <span
          class="nav-icon-svg"
          aria-hidden="true"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <polygon points="5 3 19 12 5 21 5 3" />
          </svg>
        </span>
        <span
          v-show="!ui.sidebarCollapsed"
          class="nav-label"
        >{{ t("nav.service") }}</span>
      </RouterLink>

      <!-- ── Conversation history (V1 SidebarPanel.js:60-178) ─────────
           V1 places the divider, recent-chats label, search box, grouped
           list, and empty hint *directly inside* <nav class="sidebar-nav">
           (not in a separate wrapper div). This is what makes the whole
           "nav menu + history" region a single overflow-y:auto scroll
           container — there is no inner conv-list scrollbar in V1. -->
      <template v-if="!ui.sidebarCollapsed">
        <!-- V1 line 60 — divider between top nav menu and history block. -->
        <div class="divider"></div>
        <!-- V1 line 64 — section label "RECENT CHATS"; V1 class
             `.conv-section-label` (channels.css:529-536). -->
        <div class="conv-section-label">
          {{ t("chat.historyTitle") }}
        </div>
        <!-- V1 line 69-84 — search box with magnifying-glass icon
             absolutely positioned on the left and 28px input
             padding-left so the icon does not overlap the placeholder.
             V1 SidebarPanel.js:80-82 — a spinner sits on the right edge
             while a backend search is in flight. -->
        <div class="conv-search-wrap">
          <div class="conv-search-box">
            <svg
              class="conv-search-icon"
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <circle
                cx="11"
                cy="11"
                r="8"
              />
              <line
                x1="21"
                y1="21"
                x2="16.65"
                y2="16.65"
              />
            </svg>
            <input
              v-model="chatSearchQuery"
              type="text"
              class="conv-search-input"
              :placeholder="t('chat.historySearchPlaceholder')"
            />
            <!-- V1 SidebarPanel.js:80-82 — searching spinner (right edge). -->
            <span
              v-if="isSearching"
              class="conv-search-spinner"
              aria-hidden="true"
            ></span>
          </div>
          <!-- V1 SidebarPanel.js:84-102 — independent backend search-result
               list. Shown only while a (debounced) query is active; replaces
               the grouped list. Each row reuses `.conv-item` + a 🔍 icon and
               renders the backend `snippet` (already HTML-escaped + <mark>
               highlighted server-side) via v-html. -->
          <div
            v-if="searchActive && !isSearching && searchResults.length > 0"
            class="conv-search-results"
          >
            <div
              v-for="result in searchResults"
              :key="result.id"
              class="conv-item"
              @click="selectSearchResult(result)"
            >
              <span
                class="conv-channel-icon"
                aria-hidden="true"
              >🔍</span>
              <span class="conv-item-title">
                <span class="conv-item-title-text">{{ result.title || t("chat.tab.untitled") }}</span>
                <div
                  v-if="result.snippet"
                  class="conv-item-meta"
                >
                  <!-- Backend snippet is HTML-escaped + <mark>-highlighted
                       server-side (trusted); see
                       interfaces/http/routes/conversations_search.py. -->
                  <!-- eslint-disable vue/no-v-html -->
                  <span
                    class="conv-item-time conv-search-snippet"
                    v-html="result.snippet"
                  ></span>
                  <!-- eslint-enable vue/no-v-html -->
                </div>
              </span>
            </div>
          </div>
          <!-- V1 SidebarPanel.js:104-106 — no-results hint. -->
          <div
            v-if="searchActive && !isSearching && searchResults.length === 0"
            class="conv-search-empty"
          >
            {{ t("harness.search.noResults") }}
          </div>
        </div>
        <div
          v-if="historyRetrying"
          class="history-loading-muted"
        >
          <span class="history-loading-spinner"></span>
          <span>{{ t("chat.messages.loading") }}</span>
        </div>
        <div
          v-else-if="historyLoadFailed"
          class="history-error-muted"
        >
          <span>{{ t("chat.historyLoadFailed") }}</span>
          <button
            class="btn-ghost btn-xs"
            type="button"
            @click="retryHistory"
          >
            {{ t("common.retry") }}
          </button>
        </div>
        <!-- V1 SidebarPanel.js:108 — the grouped conversation list and its
             empty hints are hidden while a search query is active; the search
             results list above takes over. -->
        <template v-if="!searchActive">
          <!-- V1 line 111-173 — grouped conversation list. V1 has NO
               `.conv-list` wrapper here (the `.conv-list` rule in
               channels.css:365 is for the Channels view, not the sidebar).
               Each group's header / items / more-btn render directly as
               children of <nav>, so left edges align to nav's 8px padding
               instead of being indented an extra 8px. -->
          <template
            v-for="group in groupedConversations"
            :key="group.key"
          >
            <!-- V1 line 112 — per-bucket header (TODAY / YESTERDAY / …). -->
            <div class="conv-group-header">
              {{ group.label }}
            </div>
            <template
              v-for="conv in group.items"
              :key="conv.id"
            >
            <div
              class="conv-item"
              :class="[
                { active: chatTabs.activeTab?.conversationId === conv.id },
                conv.meta?.source === 'wechat' ? 'wechat-conv-item' : conv.meta?.source === 'feishu' ? 'feishu-conv-item' : '',
              ]"
              @click="selectConversation(conv)"
            >
              <!-- V2 enhancement (no V1 equivalent) — sub-agent expand toggle.
                   Click reveals/hides the conversation's dispatched sub-agents
                   (lazy-loaded). `@click.stop` so it does not also select the
                   conversation. Shown ONLY when the conversation actually
                   spawned sub-agents (backend `subagent_count` > 0) so plain
                   conversations stay visually clean. -->
              <button
                v-if="(conv.subagent_count ?? 0) > 0"
                type="button"
                class="conv-expand-btn"
                :class="{ expanded: isConvExpanded(conv.id) }"
                :title="t('sidebar.toggleSubAgents')"
                :aria-label="t('sidebar.toggleSubAgents')"
                :aria-expanded="isConvExpanded(conv.id)"
                @click.stop="toggleConvExpanded(conv.id)"
              >▶</button>
              <!-- V1 SidebarPanel.js:120-132 — wechat → green SVG,
                   feishu → orange SVG, other → 💬 -->
              <span
                v-if="conv.meta?.source === 'wechat'"
                class="conv-channel-icon"
                aria-hidden="true"
              >
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                >
                  <ellipse
                    cx="9"
                    cy="9"
                    rx="7"
                    ry="5.5"
                    fill="#07C160"
                  />
                  <ellipse
                    cx="16"
                    cy="15"
                    rx="6"
                    ry="4.5"
                    fill="#07C160"
                    opacity="0.65"
                  />
                </svg>
              </span>
              <span
                v-else-if="conv.meta?.source === 'feishu'"
                class="conv-channel-icon"
                aria-hidden="true"
              >
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                >
                  <path
                    d="M20 4c-3 1-7 3-10 7l-2 3 2-1-1 3 3-2-1 3 3-3-1 3 4-5-2 1 1-3 2-2c1.5-2 2-3.5 2-5z"
                    fill="#f97316"
                  />
                  <path
                    d="M10 11c-2 3-3 6-2 9"
                    stroke="#f97316"
                    stroke-width="1.2"
                    stroke-linecap="round"
                  />
                </svg>
              </span>
              <span
                v-else
                class="conv-channel-icon"
                aria-hidden="true"
              >💬</span>
              <!-- V2 enhancement (no V1 equivalent) — live multi-tab status
                   dot. Only rendered when this conversation is open as a tab
                   that is streaming / aborting / errored, so idle or closed
                   conversations show nothing. Pure visual (aria-hidden); the
                   adjacent sr-only span announces the state to AT. The
                   streaming variant breathes via the conv-status-pulse
                   keyframes; aborting/error are static. -->
              <template v-if="convLiveStatus(conv.id) !== null">
                <span
                  class="conv-status-dot"
                  :class="`conv-status-dot--${convLiveStatus(conv.id)}`"
                  aria-hidden="true"
                ></span>
                <span class="conv-status-sr-only">{{ t(convStatusKey(convLiveStatus(conv.id)!)) }}</span>
              </template>
              <!-- V1 line 133-151 — title <span> contains the title text
                 inline followed by a block-level <div class="conv-item-meta">
                 that wraps to a second line beneath the title. -->
              <span class="conv-item-title">
                <span class="conv-item-title-text">{{ conv.title || t("chat.tab.untitled") }}</span>
                <div class="conv-item-meta">
                  <!-- V1 line 137 — relative time. -->
                  <span class="conv-item-time">{{ formatV1Time(conv.updated_at) }}</span>
                  <!-- V1 line 139-143 — rounds badge with V1 colour
                     thresholds: >=30 danger / >=20 warn / info. -->
                  <span
                    v-if="convRounds(conv) > 0"
                    class="conv-badge"
                    :class="roundsBadgeClass(convRounds(conv))"
                    :title="t('sidebar.rounds', { n: convRounds(conv) })"
                  >{{ convRounds(conv) }}{{ t('sidebar.roundsSuffix') }}</span>
                  <!-- V1 line 144-149 — tool-call badge, only when > 0. -->
                  <span
                    v-if="(conv.tool_call_count ?? 0) > 0"
                    class="conv-badge"
                    :class="(conv.tool_call_count ?? 0) >= 20 ? 'conv-badge--warn' : 'conv-badge--info'"
                    :title="t('sidebar.toolCalls', { n: conv.tool_call_count ?? 0 })"
                  >🔧{{ conv.tool_call_count }}</span>
                  <!-- Favorite status marker (⭐) — shown when the conversation
                       is favorited so the user sees its state without hovering;
                       managed from the favorites dialog or the ⋯ overflow menu
                       in `.conv-item-actions`. -->
                  <span
                    v-if="conv.favorite === true"
                    class="conv-fav-marker"
                    :title="t('sidebar.favoritedHint')"
                    aria-hidden="true"
                  >⭐</span>
                </div>
              </span>
              <!-- Hover-slide actions (V1 line 152-156 — V1 used emoji
                   glyphs not SVG). 방안 D — expand-in-place toolbar:
                   default shows 3 buttons (📌 / 🗑️ / ⋯); clicking ⋯
                   expands the SAME toolbar in place to all 5 buttons
                   (📌 / ✏️ / 📁 / ⭐ / 🗑️) — no popover, no second-stage
                   positioning. Mouse-leave on the row collapses it back to
                   the 3-button compact state. Combined with the 500ms show
                   delay (channels.css) the toolbar only appears when the
                   pointer actually rests on a row, never on a quick fly-by. -->
              <div
                class="conv-item-actions"
                :class="{ 'conv-item-actions--expanded': convActionsExpandedFor === conv.id }"
                @mouseleave="collapseConvActions(conv.id)"
              >
                <button
                  type="button"
                  class="btn btn-ghost btn-xs"
                  :class="{ 'conv-action-on': conv.pinned === true }"
                  :title="conv.pinned === true ? t('sidebar.unpinConversation') : t('sidebar.pinConversation')"
                  :aria-label="conv.pinned === true ? t('sidebar.unpinConversation') : t('sidebar.pinConversation')"
                  :aria-pressed="conv.pinned === true"
                  @click.stop="togglePin(conv)"
                >
                  📌
                </button>
                <!-- Expanded-only actions (revealed by clicking ⋯). Order
                     keeps the original 5-button sequence intact. -->
                <template v-if="convActionsExpandedFor === conv.id">
                  <button
                    type="button"
                    class="btn btn-ghost btn-xs"
                    :title="t('sidebar.renameConversation')"
                    :aria-label="t('sidebar.renameConversation')"
                    @click.stop="renameConversation(conv)"
                  >
                    ✏️
                  </button>
                  <button
                    type="button"
                    class="btn btn-ghost btn-xs"
                    :title="t('sessionWorkspace.actionLabel')"
                    :aria-label="t('sessionWorkspace.actionLabel')"
                    @click.stop="setConversationWorkspace(conv)"
                  >
                    📁
                  </button>
                  <button
                    type="button"
                    class="btn btn-ghost btn-xs"
                    :class="{ 'conv-action-on': conv.favorite === true }"
                    :title="conv.favorite === true ? t('sidebar.unfavoriteConversation') : t('sidebar.favoriteConversation')"
                    :aria-label="conv.favorite === true ? t('sidebar.unfavoriteConversation') : t('sidebar.favoriteConversation')"
                    :aria-pressed="conv.favorite === true"
                    @click.stop="toggleFavorite(conv)"
                  >
                    {{ conv.favorite === true ? "⭐" : "☆" }}
                  </button>
                </template>
                <button
                  type="button"
                  class="btn btn-ghost btn-xs"
                  :title="t('sidebar.deleteConversation')"
                  :aria-label="t('sidebar.deleteConversation')"
                  @click.stop="deleteConversation(conv)"
                >
                  🗑️
                </button>
                <!-- ⋯ trigger: only shown in the compact state. Clicking
                     it expands the toolbar above; once expanded it removes
                     itself (its job is done). -->
                <button
                  v-if="convActionsExpandedFor !== conv.id"
                  type="button"
                  class="btn btn-ghost btn-xs"
                  :title="t('sidebar.convMoreActions')"
                  :aria-label="t('sidebar.convMoreActions')"
                  :aria-expanded="false"
                  @click.stop="expandConvActions(conv.id)"
                >
                  ⋯
                </button>
              </div>
            </div>
            <!-- V2 enhancement — sub-agent sub-list (shown when expanded). -->
            <div
              v-if="isConvExpanded(conv.id)"
              class="conv-subagent-list"
            >
              <div
                v-if="isSubAgentsLoading(conv.id)"
                class="conv-subagent-empty"
              >
                {{ t("chat.messages.loading") }}
              </div>
              <template v-else>
                <div
                  v-for="sa in subAgentsFor(conv.id)"
                  :key="sa.subagent_id"
                  class="conv-subagent-item"
                  :class="`conv-subagent-status-${sa.status}`"
                  :title="sa.prompt_preview || sa.title || sa.subagent_id"
                  @click.stop="openSubAgentFromSidebar(sa.subagent_id)"
                >
                  <span
                    class="conv-subagent-icon"
                    aria-hidden="true"
                  >🤖</span>
                  <span class="conv-subagent-title">{{ sa.title || sa.prompt_preview || sa.subagent_id }}</span>
                </div>
                <div
                  v-if="subAgentsFor(conv.id).length === 0"
                  class="conv-subagent-empty"
                >
                  {{ t("sidebar.noSubAgents") }}
                </div>
              </template>
            </div>
            </template>
            <!-- V1 line 159-172 — per-bucket cap toggle. -->
            <button
              v-if="group.total > CONV_GROUP_CAP && !group.expanded"
              type="button"
              class="conv-more-btn"
              @click="toggleGroupExpanded(group.key, true)"
            >
              {{ t('sidebar.moreItems', { n: group.total - CONV_GROUP_CAP }) }}
            </button>
            <button
              v-if="group.expanded"
              type="button"
              class="conv-more-btn"
              @click="toggleGroupExpanded(group.key, false)"
            >
              {{ t('sidebar.foldItems') }}
            </button>
          </template>
          <!-- V1 line 175-177 — empty hint; V1 class is `conv-empty-hint`
             (channels.css:563-567), not `conv-empty`. -->
          <div
            v-if="!searchActive && !historyLoading && conversations.length === 0 && !historyLoadFailed"
            class="conv-empty-hint"
          >
            {{ t("chat.historyEmpty") }}
          </div>
          <div
            v-if="!searchActive && historyLoading && conversations.length === 0"
            class="conv-empty-hint"
          >
            {{ t("chat.messages.loading") }}
          </div>
        </template>
      </template>
    </nav>

    <!-- ── Slot for view-specific content ─────────────────────────── -->
    <div
      v-show="!ui.sidebarCollapsed"
      style="overflow-y: auto;"
    >
      <slot />
    </div>

    <!-- ── Footer ─────────────────────────────────────────────────── -->
    <div class="sidebar-footer">
      <div class="sidebar-footer-row">
        <!-- Collapsed rail: signed-in user avatar sits ABOVE the expand
             arrow (mirrors the reference UI). Clicking it opens the same
             account popover as the expanded footer. Renders itself only
             when SSO is on + authenticated. -->
        <SidebarUserButton
          v-if="ui.sidebarCollapsed"
          :collapsed="true"
        />

        <!-- Collapse toggle (always visible, even when collapsed —
             V1 SidebarPanel.js:185 keeps it as the sole footer control
             in the collapsed state). -->
        <button
          v-if="ui.sidebarCollapsed"
          class="btn btn-icon"
          :title="t('layout.expand_sidebar')"
          @click="ui.toggleSidebar()"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <polyline points="13 7 18 12 13 17" />
            <line
              x1="6"
              y1="12"
              x2="18"
              y2="12"
            />
          </svg>
        </button>

        <!-- V1 SidebarPanel.js:197 — every footer control besides the
             collapse toggle is hidden while the sidebar is collapsed
             (the collapsed rail shows only the collapse/expand button).
             Wrap the remaining controls in a single <template v-if>
             so the DOM matches V1 in both states. -->
        <template v-if="!ui.sidebarCollapsed">
          <!-- ─── All footer buttons displayed inline (no more-tools popover) ─── -->

          <!-- Collapse sidebar -->
          <button
            class="btn btn-icon"
            :title="t('layout.collapse_sidebar')"
            @click="ui.toggleSidebar()"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.5"
            >
              <polyline points="11 17 6 12 11 7" />
              <line
                x1="18"
                y1="12"
                x2="6"
                y2="12"
              />
            </svg>
          </button>

          <!-- Command Palette -->
          <button
            class="btn btn-icon"
            :title="t('layout.command_palette_shortcut')"
            @click="palette.show()"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.5"
            >
              <path d="M18 3a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3H6a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3V6a3 3 0 0 0-3-3 3 3 0 0 0-3 3 3 3 0 0 0 3 3h12a3 3 0 0 0 3-3 3 3 0 0 0-3-3z" />
            </svg>
          </button>

          <!-- Theme toggle -->
          <button
            class="btn btn-icon"
            :title="nextTheme === 'dark' ? t('layout.switch_to_dark') : t('layout.switch_to_light')"
            @click="toggleTheme()"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.5"
            >
              <!-- Sun icon for dark mode, moon for light -->
              <template v-if="ui.resolvedTheme === 'dark'">
                <circle
                  cx="12"
                  cy="12"
                  r="5"
                />
                <line
                  x1="12"
                  y1="1"
                  x2="12"
                  y2="3"
                />
                <line
                  x1="12"
                  y1="21"
                  x2="12"
                  y2="23"
                />
                <line
                  x1="4.22"
                  y1="4.22"
                  x2="5.64"
                  y2="5.64"
                />
                <line
                  x1="18.36"
                  y1="18.36"
                  x2="19.78"
                  y2="19.78"
                />
                <line
                  x1="1"
                  y1="12"
                  x2="3"
                  y2="12"
                />
                <line
                  x1="21"
                  y1="12"
                  x2="23"
                  y2="12"
                />
                <line
                  x1="4.22"
                  y1="19.78"
                  x2="5.64"
                  y2="18.36"
                />
                <line
                  x1="18.36"
                  y1="5.64"
                  x2="19.78"
                  y2="4.22"
                />
              </template>
              <template v-else>
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
              </template>
            </svg>
          </button>

          <!-- Language switch -->
          <div
            ref="langWrapEl"
            class="lang-switcher"
          >
            <button
              class="btn btn-icon"
              :title="t('language.switch') + ' / ' + currentLangLabel"
              :aria-label="t('language.switch')"
              aria-haspopup="listbox"
              :aria-expanded="langDropdownOpen"
              @click.stop="toggleLangDropdown()"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <circle
                  cx="12"
                  cy="12"
                  r="10"
                />
                <line
                  x1="2"
                  y1="12"
                  x2="22"
                  y2="12"
                />
                <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
              </svg>
            </button>
            <div
              v-if="langDropdownOpen"
              class="lang-switcher-dropdown"
              role="listbox"
              :aria-label="t('language.selectLabel')"
            >
              <div
                v-for="locale in SUPPORTED_LANGS"
                :key="locale"
                class="lang-switcher-option"
                :class="{ active: ui.locale === locale }"
                role="option"
                :aria-selected="ui.locale === locale"
                tabindex="0"
                @click.stop="selectLang(locale)"
                @keydown.enter="selectLang(locale)"
                @keydown.space.prevent="selectLang(locale)"
              >
                <span
                  class="lang-switcher-option-check"
                  aria-hidden="true"
                >{{ ui.locale === locale ? '✓' : '' }}</span>
                <span class="lang-switcher-option-label">{{ LANG_FULL[locale] }}</span>
              </div>
            </div>
          </div>

          <!-- Font size -->
          <div
            ref="fontSizeWrapEl"
            class="font-size-popover-wrap"
          >
            <button
              class="btn btn-icon"
              :class="{ 'font-size-btn--active': fontSizePopoverOpen }"
              :title="t('fontSize.label') + ' (' + fontSizeCtl.fontSizeLabel.value + ')'"
              :aria-label="t('fontSize.label')"
              :aria-expanded="fontSizePopoverOpen"
              data-testid="font-size-btn"
              @click.stop="toggleFontSizePopover()"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
              >
                <path d="M4 7V4h16v3" />
                <line
                  x1="12"
                  y1="4"
                  x2="12"
                  y2="20"
                />
                <line
                  x1="8"
                  y1="20"
                  x2="16"
                  y2="20"
                />
              </svg>
            </button>
            <transition name="font-popover">
              <div
                v-if="fontSizePopoverOpen"
                class="font-size-popover"
                data-testid="font-size-popover"
                @click.stop
              >
                <div class="font-size-popover-header">
                  <span class="font-size-popover-title">{{ t('fontSize.label') }}</span>
                  <span
                    class="font-size-popover-value"
                    :title="t('fontSize.reset')"
                    @click="fontSizeCtl.resetFontSize()"
                  >{{ fontSizeCtl.fontSizeLabel.value }}</span>
                </div>
                <div class="font-size-popover-slider">
                  <button
                    class="font-size-popover-btn"
                    type="button"
                    :disabled="!fontSizeCtl.canDecrease.value"
                    :title="t('fontSize.decrease')"
                    :aria-label="t('fontSize.decrease')"
                    data-testid="font-size-decrease"
                    @click="fontSizeCtl.decreaseFontSize()"
                  >
                    A<span class="font-size-popover-btn-minus">−</span>
                  </button>
                  <div class="font-size-popover-track">
                    <div
                      class="font-size-popover-track-fill"
                      :style="{ width: fontSizeCtl.fontSizePercent.value + '%' }"
                    ></div>
                    <div
                      class="font-size-popover-thumb"
                      :style="{ left: fontSizeCtl.fontSizePercent.value + '%' }"
                    ></div>
                  </div>
                  <button
                    class="font-size-popover-btn font-size-popover-btn--lg"
                    type="button"
                    :disabled="!fontSizeCtl.canIncrease.value"
                    :title="t('fontSize.increase')"
                    :aria-label="t('fontSize.increase')"
                    data-testid="font-size-increase"
                    @click="fontSizeCtl.increaseFontSize()"
                  >
                    A<span class="font-size-popover-btn-plus">+</span>
                  </button>
                </div>
                <div class="font-size-popover-reset">
                  <button
                    class="font-size-popover-reset-btn"
                    type="button"
                    :title="t('fontSize.reset')"
                    data-testid="font-size-reset"
                    @click="fontSizeCtl.resetFontSize()"
                  >
                    ↺ {{ t('fontSize.reset') }}
                  </button>
                </div>
              </div>
            </transition>
          </div>

          <!-- ⭐ Favorites library (我的收藏) — opens the favorites dialog. -->
          <button
            class="btn btn-icon"
            :title="t('sidebar.openFavorites')"
            :aria-label="t('sidebar.openFavorites')"
            data-testid="open-favorites"
            @click="openFavorites()"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linejoin="round"
            >
              <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
            </svg>
          </button>

          <!-- Reboot — kept as a footer icon ONLY when the user avatar
               menu is NOT shown (SSO off / not signed in). When signed in
               the Restart action lives inside the avatar popover instead,
               so we hide this icon to avoid a duplicate + keep the footer
               to one row. This guarantees Restart always has a footer
               entry point (no functional regression when SSO is off). -->
          <button
            v-if="!auth.showUserButton"
            class="btn btn-icon"
            :title="t('sidebar.reboot')"
            @click="reboot()"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.5"
            >
              <polyline points="23 4 23 10 17 10" />
              <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
            </svg>
          </button>

          <!-- Okta SSO — signed-in user avatar (occupies the slot the
               Reboot button used to hold). Its popover carries the
               Restart + Sign-out actions. Renders itself only when
               auth.enabled && authenticated. -->
          <SidebarUserButton />
        </template>
      </div>
    </div>
  </aside>

  <RenameDialog
    v-model="renameValue"
    :visible="renameOpen"
    :title="t('chat.renamePromptTitle')"
    :placeholder="t('chat.renamePlaceholder')"
    :loading="renameLoading"
    @confirm="confirmRename"
    @cancel="cancelRename"
  />

  <ConversationWorkspaceDialog
    v-model="workspaceValue"
    :visible="workspaceOpen"
    :loading="workspaceLoading"
    @confirm="confirmWorkspace"
    @cancel="cancelWorkspace"
  />

  <FavoritesDialog
    :visible="favoritesOpen"
    @select="selectFavorite"
    @toggle-favorite="toggleFavorite"
    @close="favoritesOpen = false"
  />
</template>

<style>
/* Pin hover-action "on" state + favorite status marker.
   The action buttons live in `.conv-item-actions` (hover-slide); when the
   pin flag is set we tint the glyph so the toggled state reads at a glance.
   The `.conv-fav-marker` is a small persistent ⭐ on the meta line so a
   favorited conversation is recognisable without hovering. */
.conv-item-actions .btn.conv-action-on {
  color: var(--accent, #f5b301);
  opacity: 1;
}
.conv-fav-marker {
  margin-left: 4px;
  font-size: 0.78em;
  line-height: 1;
  flex-shrink: 0;
}

/* V2 enhancement (no V1 equivalent) — sub-agent expand toggle + sub-list.
   The toggle is a small caret prepended to each conversation row; the
   sub-list renders the conversation's dispatched sub-agents, indented under
   the row, each opening in a new tab on click. */
.conv-expand-btn {
  flex-shrink: 0;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 0.7em;
  line-height: 1;
  padding: 2px 4px;
  margin-right: 2px;
  border-radius: 4px;
  transition: transform var(--transition), background var(--transition), color var(--transition);
}
.conv-expand-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.conv-expand-btn.expanded {
  transform: rotate(90deg);
}
.conv-subagent-list {
  padding: 2px 0 6px 24px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.conv-subagent-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  border-left: 2px solid var(--border);
  transition: background var(--transition), color var(--transition);
}
.conv-subagent-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.conv-subagent-item.conv-subagent-status-running { border-left-color: var(--info); }
.conv-subagent-item.conv-subagent-status-done    { border-left-color: var(--success); }
.conv-subagent-item.conv-subagent-status-error   { border-left-color: var(--error); }
.conv-subagent-icon { flex-shrink: 0; }
.conv-subagent-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv-subagent-empty {
  padding: 4px 8px;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

/* V1 SidebarPanel.js:51-55 — the active-download ring/dot are
   position:absolute and need a positioned host. V1 nests them inside
   `.nav-icon` (which layout.css makes position:relative). V2's Downloads
   item uses `.nav-icon-svg` (a 18px inline-grid box with no positioning
   context), so opt this one icon into relative positioning. The ring/dot
   visuals + keyframes themselves live in the global
   styles/layout/layout.css (`.nav-dl-ring` / `.nav-dl-dot`); this rule
   only supplies the containing block so the -3px/-5px offsets anchor to
   the icon. */
.nav-item .nav-icon-svg.nav-dl-host {
  position: relative;
  overflow: visible;
}

/* V1 SidebarPanel.js:69-84 — search box: 28px left-padding for the
   absolutely-positioned magnifying-glass icon, --bg-secondary fill,
   1px --border, 6px radius. Inline style in V1; promoted to a real
   class here so V2 can match the legacy look 1:1. */
.conv-search-wrap {
  padding: 0 10px 8px;
}
.conv-search-box {
  position: relative;
  display: flex;
  align-items: center;
}
.conv-search-icon {
  position: absolute;
  left: 8px;
  opacity: 0.5;
  pointer-events: none;
  color: var(--text-secondary);
}
.conv-search-input {
  width: 100%;
  padding: 6px 8px 6px 28px;
  font-size: var(--text-xs);
  /* V1 inherits body's `line-height: 1.6` on the input, giving the
     box ~31px total height (11 × 1.6 + 12 padding + 1 border × 2).
     V2's CSS reset / user-agent style leaves <input> at
     `line-height: normal`, which collapses to ~27px. Set it
     explicitly so the search box has the same proportion as V1. */
  line-height: 1.6;
  border: 1px solid var(--border, rgba(255, 255, 255, 0.1));
  border-radius: 6px;
  background: var(--bg-secondary, rgba(0, 0, 0, 0.15));
  color: var(--text-primary);
  outline: none;
}
/* V1 SidebarPanel.js:80 — V1 keeps the search input visually
   unchanged on focus (only `outline: none`; no border/shadow change).
   The previous V2 rule `border-color: var(--accent)` made the input
   look heavily highlighted on focus, which V1 never does. Re-state
   the same border + outline here so :focus has no visual delta. */
.conv-search-input:focus,
.conv-search-input:focus-visible {
  border: 1px solid var(--border, rgba(255, 255, 255, 0.1));
  outline: none;
  box-shadow: none;
}
.conv-load-banner {
  display: none;
}

/* ── Brand version (inline after subtitle) ────────────────────────────────── */
.brand-version {
  font-size: 10px;
  color: var(--text-muted);
  opacity: 0.7;
  margin-left: 4px;
}


/* ── 改造 C: History error muted style ─────────────────────────────────────── */
.history-error-muted {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #6b7a99);
}
.history-error-muted .btn-ghost.btn-xs {
  font-size: var(--text-xs, 11px);
  padding: 2px 8px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border, #2a2750);
  background: transparent;
  color: var(--text-secondary, #b6b8d6);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.history-error-muted .btn-ghost.btn-xs:hover {
  border-color: var(--accent, #a594ff);
  color: var(--accent, #a594ff);
}
.history-loading-muted {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #6b7a99);
}
.history-loading-spinner {
  width: 12px;
  height: 12px;
  border: 2px solid var(--border, rgba(255, 255, 255, 0.18));
  border-top-color: var(--accent, #a594ff);
  border-radius: 50%;
  animation: conv-search-spin 0.7s linear infinite;
}

/* ── Multi-tab live status dot (V2 enhancement — no V1 equivalent) ────────── */
/* A small coloured dot rendered between the channel icon and the title for
   conversations whose open tab is mid-flight. Sits in the `.conv-item` flex
   row (flex-shrink:0 so it never squeezes the title). Colours reuse the same
   semantic theme tokens as ChatTabStrip's per-tab dot (--success / --warning /
   --error), so it tracks light/dark theme automatically. Streaming breathes
   via a pulse animation to draw attention to active generation; aborting and
   error are static. */
.conv-status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--text-muted);
}
.conv-status-dot--streaming {
  background: var(--success, #16a34a);
  animation: conv-status-pulse 1.4s ease-in-out infinite;
}
.conv-status-dot--aborting {
  background: var(--warning, #f59e0b);
}
.conv-status-dot--error {
  background: var(--error, #dc2626);
}
@keyframes conv-status-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.4;
    transform: scale(0.7);
  }
}
/* Respect reduced-motion: keep the dot visible but stop the breathing. */
@media (prefers-reduced-motion: reduce) {
  .conv-status-dot--streaming {
    animation: none;
  }
}
/* sr-only label announcing the live status to assistive tech (the dot itself
   is aria-hidden). Mirrors ChatTabStrip's __status-sr-only pattern. */
.conv-status-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* ── Backend conversation search (V1 SidebarPanel.js:80-106) ──────────────── */
/* V1 SidebarPanel.js:80-82 — small spinner pinned to the right edge of the
   search box while a backend search is in flight. V1 reuses its global
   `.spinner` (12px, 2px border, rotating). V2's global `.spinner` lives in
   styles/components; restate the V1 inline sizing + position here so it sits
   inside `.conv-search-box`. */
.conv-search-spinner {
  position: absolute;
  right: 8px;
  width: 12px;
  height: 12px;
  border: 2px solid var(--border, rgba(255, 255, 255, 0.18));
  border-top-color: var(--accent, #a594ff);
  border-radius: 50%;
  pointer-events: none;
  animation: conv-search-spin 0.7s linear infinite;
}
@keyframes conv-search-spin {
  to {
    transform: rotate(360deg);
  }
}
/* V1 SidebarPanel.js:84 — independent search-result list sits just below the
   search input (margin-top:4px in V1). Items reuse the global `.conv-item`. */
.conv-search-results {
  margin-top: 4px;
}
/* V1 SidebarPanel.js:98-100 — the snippet line; backend returns it with a
   <mark> highlight wrapper. Keep it to a single ellipsised line so long
   message excerpts don't blow up the row height. */
.conv-search-snippet {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv-search-snippet mark {
  background: var(--accent-muted, rgba(165, 148, 255, 0.22));
  color: var(--accent, #a594ff);
  border-radius: 2px;
  padding: 0 1px;
}
/* V1 SidebarPanel.js:104-106 — centred "no matching conversations" hint. */
.conv-search-empty {
  padding: 6px 4px;
  font-size: var(--text-xs);
  color: var(--text-muted, #6b7a99);
  text-align: center;
}

/* Inner ellipsis wrapper for the title's first line. V1 puts the bare
   title text + a block-level <div class="conv-item-meta"> directly
   inside <span class="conv-item-title">; we wrap the text in this
   span so we can apply nowrap+ellipsis to it specifically without
   relying on inheritance from .conv-item-title (which the global
   channels.css already styles for V1 parity). */
.conv-item-title-text {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Conv-item action buttons (V1 line 154-155) — V1 uses
   `<button class="btn btn-ghost btn-xs">✏️</button>` with emoji. The
   `btn / btn-ghost / btn-xs` styles are provided globally by V2's
   button primitives in `styles/components/buttons.css`; the
   absolute-positioned `.conv-item-actions` container with the gradient
   mask + opacity-transition is owned by the global
   `styles/channels/channels.css:582-602`. No SFC-local rules are
   required here for parity with V1. */

/* ── Font-size popover (V1 SidebarPanel.js:215-260) ─────────────────────── */
.font-size-popover-wrap {
  position: relative;
  display: inline-flex;
}
.font-size-btn--active {
  color: var(--accent, #a594ff);
  border-color: var(--accent, #a594ff);
}
.font-size-popover {
  position: absolute;
  z-index: 200;
  bottom: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  min-width: 220px;
  background: var(--bg-secondary, #161430);
  border: 1px solid var(--border, #2a2750);
  border-radius: var(--radius-md, 8px);
  box-shadow: var(--shadow-md, 0 8px 24px rgba(0, 0, 0, 0.35));
  padding: var(--space-3, 12px);
  display: flex;
  flex-direction: column;
  gap: var(--space-2, 8px);
}
.font-size-popover-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.font-size-popover-title {
  font-size: var(--text-sm, 12px);
  font-weight: 600;
  color: var(--text-primary);
}
.font-size-popover-value {
  font-size: var(--text-sm, 12px);
  color: var(--accent, #a594ff);
  cursor: pointer;
  font-variant-numeric: tabular-nums;
}
.font-size-popover-slider {
  display: flex;
  align-items: center;
  gap: var(--space-2, 8px);
}
.font-size-popover-btn {
  display: inline-flex;
  align-items: baseline;
  justify-content: center;
  border: 1px solid var(--border, #2a2750);
  background: transparent;
  color: var(--text-primary);
  border-radius: var(--radius-sm, 6px);
  cursor: pointer;
  padding: 2px 6px;
  font-size: var(--text-sm, 12px);
  line-height: 1;
  transition: border-color 0.15s, color 0.15s;
}
.font-size-popover-btn--lg {
  font-size: var(--text-md, 14px);
}
.font-size-popover-btn:hover:not(:disabled) {
  border-color: var(--accent, #a594ff);
  color: var(--accent, #a594ff);
}
.font-size-popover-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.font-size-popover-btn-minus,
.font-size-popover-btn-plus {
  font-size: 0.7em;
  margin-left: 1px;
}
.font-size-popover-track {
  position: relative;
  flex: 1;
  height: 4px;
  border-radius: 2px;
  background: var(--border, #2a2750);
}
.font-size-popover-track-fill {
  position: absolute;
  left: 0;
  top: 0;
  height: 100%;
  border-radius: 2px;
  background: var(--accent, #a594ff);
}
.font-size-popover-thumb {
  position: absolute;
  top: 50%;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--accent, #a594ff);
  transform: translate(-50%, -50%);
  box-shadow: 0 0 0 2px var(--bg-secondary, #161430);
}
.font-size-popover-reset {
  display: flex;
  justify-content: center;
}
.font-size-popover-reset-btn {
  border: none;
  background: transparent;
  color: var(--text-secondary, #b6b8d6);
  cursor: pointer;
  font-size: var(--text-xs, 11px);
  padding: 2px 6px;
  border-radius: var(--radius-sm, 6px);
}
.font-size-popover-reset-btn:hover {
  color: var(--accent, #a594ff);
}
.font-popover-enter-active,
.font-popover-leave-active {
  transition: opacity 0.12s ease, transform 0.12s ease;
}
.font-popover-enter-from,
.font-popover-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(4px);
}
</style>
