<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Chat view — multi-tab workspace (PR-054).
 *
 * Replaces the PR-053 placeholders (`data-pr-054-slot`) with real
 * components driven by the `useChatTabsStore` Pinia store and the
 * `useChatTransport` composable.
 *
 * Per refactor-plan §10.6 invariants:
 *   - Each tab owns its AbortController and transport in the store
 *     (per-tab maps), so cancelling tab A leaves tab B's stream
 *     untouched (inv 2).
 *   - Closing a tab tears down its transport but leaves other tabs'
 *     streams running (inv 3).
 *   - Status transitions only happen when the store accepts them
 *     (inv 4): `confirmDone` refuses to apply unless status is
 *     `streaming`.
 *
 * The view itself is thin: scrolling message list + composer at bottom,
 * with a visible tab strip on top for multi-session switching. Per-tab
 * transports are now owned by an app-level singleton manager
 * (`useChatTransports`), NOT by this component — so leaving the /chat route
 * no longer tears down background streams (V1 parity: a conversation
 * switched away from keeps streaming in the background, useChat.js:759-760).
 */
import { computed, defineAsyncComponent, nextTick, useTemplateRef, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore, type TabId, type ChatMessage } from "@/stores/chatTabs";
import { useUiStore } from "@/stores/ui";
import { useChatTransports } from "@/composables/chat/useChatTransports";
import {
  loadTabsLayout,
  saveTabsLayout,
} from "@/stores/chatTabs/chatTabsPersistence";
import { useClaudeCode } from "@/composables/useClaudeCode";
import { useOpenCode } from "@/composables/useOpenCode";
import { useChatTurnSubmit } from "@/composables/chat/useChatTurnSubmit";
import { useChatControlChannel } from "@/composables/chat/useChatControlChannel";
import { useToast } from "@/composables/useToast";
import { useHeaderActions } from "@/composables/useHeaderActions";
import type { HeaderAction } from "@/stores/headerActions";
import { useConversationsStore, type ConversationSummary } from "@/stores/conversations";
import { useDiscussionStore } from "@/stores/discussion";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { useConversationWorkspace } from "@/composables/useConversationWorkspace";
import { useModeFrameTriggers } from "@/composables/useModeFrameTriggers";
import { usePromoteReadyDetection } from "@/composables/usePromoteReadyDetection";
import ConversationWorkspaceDialog from "@/components/chat/ConversationWorkspaceDialog.vue";
import ChatMessageList from "@/components/chat/ChatMessageList.vue";
import ChatComposer from "@/components/chat/ChatComposer.vue";
import ModeIntroCard from "@/components/chat/ModeIntroCard.vue";
import type { IntroMode } from "@/composables/useModeIntroCardVisibility";
import PromoteReadyNotice from "@/components/chat/PromoteReadyNotice.vue";
import MessageQueuePanel from "@/components/chat/MessageQueuePanel.vue";
import TaskListBar from "@/components/chat/TaskListBar.vue";
// SubAgentRail — second-level navigator rendered directly below the parent
// main-agent tab (whose ChatTabStrip lives in AppHeader.vue). Restores the
// two-level tab bar UX after β incorrectly flattened everything into a single
// tab strip. See `railEntries` computed for the data model (backend index +
// open-tab live status merge).
import SubAgentRail, {
  type RailEntry,
} from "@/components/chat/SubAgentRail.vue";
// Mode-switch-only surfaces — only mounted when the active tab enters Claude
// Code / Open Code / App Builder mode (all gated by v-if/v-else-if in the
// template, default OFF). Static imports forced their entire (very large)
// component subtrees into the ChatView chunk for every user, even though the
// default chat surface never renders them. defineAsyncComponent code-splits
// each into its own chunk that is fetched only on first mode switch — this is
// the single biggest contributor to ChatView's bundle size. The default chat
// path (ChatMessageList + ChatComposer above) stays static so first paint and
// the ChatView smoke tests are unchanged.
const ChatViewClaudeCode = defineAsyncComponent(
  () => import("@/views/ChatViewClaudeCode.vue"),
);
const ChatViewOpenCode = defineAsyncComponent(
  () => import("@/views/ChatViewOpenCode.vue"),
);
const AppBuilderWorkbenchOverlay = defineAsyncComponent(
  () => import("@/components/app-builder/AppBuilderWorkbenchOverlay.vue"),
);
import ImplementationPanel from "@/components/chat/ImplementationPanel.vue";
import {
  ICON_FOLDER,
  ICON_CHEVRON_DOWN,
  ICON_CHEVRON_UP,
  ICON_DOWNLOAD,
  ICON_TRASH,
} from "@/components/icons/topbarIcons";

const { t } = useI18n();
const store = useChatTabsStore();
const toast = useToast();
const ui = useUiStore();
const claudeCode = useClaudeCode();
const openCode = useOpenCode();
const turnSubmit = useChatTurnSubmit();

// App Builder heavy workbench is retained but hidden by default; it only
// shows when the user opts in via Settings → App Config
// (`ui.app_builder.show_workbench`). Load the forge config once so the gate
// reflects the persisted preference (shared singleton — no-op if loaded).
const { appBuilderShowWorkbench, load: loadForgeConfig } = useForgeConfig();
void loadForgeConfig();

// Session-level workspace (V2 enhancement). Mirrors the sidebar 📁 entry but
// surfaced as a top-of-chat header action for discoverability (the sidebar
// entry only appears on hover over a conversation row).
const conversationsStore = useConversationsStore();
const discussionStore = useDiscussionStore();
const {
  workspaceOpen,
  workspaceValue,
  workspaceLoading,
  setConversationWorkspace,
  cancelWorkspace,
  confirmWorkspace,
} = useConversationWorkspace();

// The active tab's conversation id (null for a brand-new empty tab that has
// not created a conversation row yet).
const activeConversationId = computed<string | null>(
  () => store.activeTab?.conversationId ?? null,
);

// ── Promote-ready CTA (Sprint 2, feedback 7B) ────────────────────────────────
// Detect when the ACTIVE tab is in Model Builder / App Builder mode AND the
// conversation references a model workspace whose `output/` contains scanned-
// eligible precision variants. When so, surface an inline notice above the
// composer whose CTA reuses the shared `requestOpenPromote` trigger (same wire
// the ModeIntroCard chip uses). See `usePromoteReadyDetection` for the full
// state machine — including sessionStorage-scoped dismissal per workdir so the
// user is not nagged repeatedly for the same model.
const promoteReady = usePromoteReadyDetection();
function onPromoteReadyPromoted(): void {
  // Same effect as an explicit dismiss — the user just acted on the CTA, so
  // hide it. The mode-frame's Promote popover was popped open by
  // `requestOpenPromote` (fired inside the notice component itself).
  promoteReady.dismiss();
}


// Lazily create a conversation for the active tab when it has none yet, so the
// 📁 button stays usable BEFORE the first message is sent (the workspace is
// then attached to the freshly created conversation). Mirrors
// `useDiscussion.ensureConversation`: create empty row → bind to tab → seed the
// sidebar (State-Truth-First). Returns the new id, or null on failure / no tab.
async function ensureActiveConversation(): Promise<string | null> {
  const tab = store.activeTab;
  if (tab === undefined || tab === null) return null;
  const existing = tab.conversationId ?? null;
  if (existing !== null && existing !== "") return existing;
  try {
    const summary = await discussionStore.createConversation(
      tab.title || t("chat.tab.untitled"),
    );
    const newId = typeof summary.id === "string" ? summary.id : null;
    if (newId === null || newId === "") return null;
    store.setConversationId(tab.id, newId);
    // Seed the sidebar so the just-created conversation shows up immediately
    // (mirrors the transport's upsert-on-create).
    conversationsStore.upsert(summary as never);
    return newId;
  } catch {
    return null;
  }
}

function openWorkspaceDialog(): void {
  const id = activeConversationId.value;
  if (id !== null && id !== "") {
    // Existing conversation: prefer the real summary from the conversations
    // store (carries meta / current workspace); fall back to a minimal stub
    // so the dialog still works if the list hasn't loaded this row yet.
    const found = conversationsStore.conversations.find((c) => c.id === id);
    const summary: ConversationSummary =
      found ??
      {
        id,
        title: store.activeTab?.title ?? "",
        status: "active",
        created_at: "",
        updated_at: "",
        message_count: 0,
      };
    setConversationWorkspace(summary);
    return;
  }
  // Brand-new tab with no conversation yet: open the dialog against an empty
  // (id-less) stub and hand the composable an `ensureConversation` resolver so
  // it can materialise a real conversation on confirm before persisting.
  const stub: ConversationSummary = {
    id: "",
    title: store.activeTab?.title ?? "",
    status: "active",
    created_at: "",
    updated_at: "",
    message_count: 0,
  };
  setConversationWorkspace(stub, ensureActiveConversation);
}

// App Builder mode: show the workbench overlay above the message list
// when the active tab is in `app-builder` mode (block-4).
const appBuilderActive = computed(
  () => store.activeTab?.activeMode === "app-builder",
);

// --- App-level per-tab transport manager ----------------------------------
//
// Each tab gets its OWN ChatTransport instance — strict isolation
// (refactor-plan §10.6 inv 2). The cache + close-pruning now live in the
// app-level `useChatTransports` singleton instead of a component-local Map,
// so a tab that is still streaming keeps its transport alive even when this
// view is unmounted (e.g. the user navigates to Settings). Closed tabs are
// pruned by the manager's own watcher.
const { peekTransport } = useChatTransports();

const activeTab = computed(() => store.activeTab);
const activeTabId = computed(() => store.activeTabId);

// Mode-intro card visibility. Rendered as an on-demand overlay attached
// to a top-right ⓘ button (see the template + ModeIntroCard for the full
// rationale). The card only makes sense when the tab ALREADY has messages
// — an empty tab is covered by the mode's dedicated empty-state
// (App Builder / GoMaster / Pro / Code / Model Builder), which provides
// the same 3-step guide as a full-screen welcome; showing a second overlay
// on top of that would be redundant and cluttered.
//
// Note: this is a POSITION-only change. The 3-tier visibility gate
// (`useModeIntroCardVisibility` — permanent / session / clear) still runs
// inside ModeIntroCard itself, so the "× closes it, checkbox makes it
// permanent, settings can restore" UX is untouched.
const introMode = computed<IntroMode | null>(() => {
  const tab = activeTab.value;
  if (tab === null) return null;
  // Empty conversation → the mode's dedicated empty-state already provides
  // full onboarding, so suppress the overlay. Fires the moment the user
  // sends their first turn (activeMessages.length > 0), at which point the
  // empty-state disappears and the ⓘ overlay button takes over.
  if (tab.messages.length === 0 && tab.streamingContent === "") {
    return null;
  }
  const mode = tab.activeMode;
  if (
    mode === "app-builder" ||
    mode === "gomaster" ||
    mode === "model-build" ||
    mode === "model-hub" ||
    mode === "pro" ||
    mode === "code"
  ) {
    return mode;
  }
  return null;
});

// ── SubAgentRail data source ────────────────────────────────────────────────
//
// Two-level tab bar: the SubAgentRail below the top ChatTabStrip carries
// every sub-agent (at any depth) rooted at the active main tab's
// conversation. The merged data source is:
//
//   1. `store.subAgentIndex[rootConvId]` — persisted backend list of ALL
//      sub-agent sessions under this conversation (open OR closed).
//   2. `store.tabs.filter(kind=subagent && root=convId)` — open sub-agent
//      tabs, providing LIVE status ("running" / "aborting") that overrides
//      the persisted status on the index entry for that sid.
//
// Rail chips render for both cases; a closed sub-agent (index-only, no open
// tab) shows a greyed chip that one-click hydrates via `openSubAgentTab`.
// This is the "keep the chip visible after close" semantic the user asked
// for: closing a chip's × unloads the memory but preserves the reference.
//
// Graceful degradation: when the index isn't loaded yet (initial mount,
// fetch failure), fall back to just the open-tab list so the rail is never
// empty when open sub-agents exist.

/** The main-agent tab whose sub-agents the rail should render. When the
 *  active tab IS a sub-agent, we walk its `subagentMeta.rootConversationId`
 *  back to the parent main tab. Reads `store.activeMainTabId` (the shared
 *  single-source-of-truth getter; ChatTabStrip uses the same one for its
 *  active-highlight, so both surfaces agree). */
const activeMainTab = computed(() => {
  const id = store.activeMainTabId;
  if (id === null) return null;
  return store.tabs.find((t) => t.id === id) ?? null;
});

/** Merged rail entry list — persisted index + open-tab live status. Falls
 *  back to just open-tab filter when the index cache isn't populated yet
 *  (graceful degradation; rail never disappears while open sub-agents
 *  exist). */
const railEntries = computed<RailEntry[]>(() => {
  const main = activeMainTab.value;
  if (main === null || main.conversationId === null) return [];
  const rootConvId = main.conversationId;
  // Open sub-agent tabs indexed by id for live-status override lookup.
  const openTabsBySid = new Map(
    store.tabs
      .filter(
        (t) =>
          t.kind === "subagent" &&
          t.subagentMeta?.rootConversationId === rootConvId,
      )
      .map((t) => [t.subagentMeta!.subagentId, t]),
  );
  const indexEntries = store.subAgentIndex[rootConvId] ?? [];
  if (indexEntries.length > 0) {
    return indexEntries.map((entry) => {
      const openTab = openTabsBySid.get(entry.subagentId);
      const liveStatus = openTab?.subagentMeta?.status;
      const liveTitle = openTab?.title;
      return {
        subagentId: entry.subagentId,
        title:
          (typeof liveTitle === "string" && liveTitle !== ""
            ? liveTitle
            : entry.title) || entry.subagentId,
        status:
          typeof liveStatus === "string" && liveStatus !== ""
            ? liveStatus
            : entry.status,
        isOpen: openTab !== undefined,
        depth: entry.depth,
      };
    });
  }
  // Fallback: derive purely from open tabs. Runs while the index cache is
  // still loading (or when the backend fetch failed). Chips show as `isOpen:
  // true` because they ARE open — closed sub-agents can't be surfaced
  // without the backend list.
  return Array.from(openTabsBySid.values()).map((tab) => ({
    subagentId: tab.subagentMeta!.subagentId,
    title: tab.title,
    status: tab.subagentMeta?.status ?? "idle",
    isOpen: true,
    depth: tab.subagentMeta?.depth ?? 1,
  }));
});

/** Highlighted sub-agent id on the rail — non-null only when the active tab
 *  itself IS a sub-agent (the user has drilled into a chip). */
const railHighlightSid = computed<string | null>(() => {
  const active = store.activeTab;
  return active?.kind === "subagent"
    ? active.subagentMeta?.subagentId ?? null
    : null;
});

// ── SubAgentRail handlers ────────────────────────────────────────────────

/** "⌂ Main" pressed: switch focus back to the parent main-agent tab.
 *  No-op when already on a main tab. */
function onSelectMainAgent(): void {
  const active = store.activeTab;
  if (active === null || active.kind !== "subagent") return;
  const rootConvId = active.subagentMeta?.rootConversationId ?? "";
  if (rootConvId === "") return;
  const parent = store.tabs.find(
    (t) => t.kind !== "subagent" && t.conversationId === rootConvId,
  );
  if (parent !== undefined) store.switchTab(parent.id);
}

/** A sub-agent chip (open OR closed) was picked on the rail. Always routes
 *  through `store.openSubAgentTab(sid)` — for an OPEN chip that's a reuse +
 *  switch; for a CLOSED chip (index-only) it fetches the detail + rebuilds
 *  the tab. Errors surface a toast (project custom, §3.9.2). */
async function onSelectSubAgent(sid: string): Promise<void> {
  try {
    await store.openSubAgentTab(sid);
  } catch (err) {
    console.error("[ChatView] onSelectSubAgent failed", err);
    toast.error(t("chat.subAgent.openFailed"));
  }
}

/** × on a chip = MEMORY UNLOAD only (backend session is preserved). The
 *  chip stays on the rail (as `isOpen: false`) because `state.subAgentIndex`
 *  is not touched by `closeTab`; the user can re-hydrate by clicking it.
 *  See SubAgentIndexEntry docstring for the full lifecycle. */
function onCloseSubAgent(sid: string): void {
  const openTab = store.tabs.find(
    (t) => t.kind === "subagent" && t.subagentMeta?.subagentId === sid,
  );
  if (openTab !== undefined) {
    store.closeTab(openTab.id);
  }
}

// Floating Stop button visibility: shown while the active tab is
// streaming. Hosted at the ChatView level (not inside ChatMessageList)
// so it anchors to the non-scrolling `.chat-view` rather than the
// scrollable `.messages-container` — fixes "时有时无 / 位置不固定"
// (the float used to scroll away with the message list when the user
// scrolled up).
// Busy = generating OR mid-abort. Read the SHARED store getter so the
// floating stop button and the composer's send/stop button use the exact
// same truth (V1 parity: a single `isStreaming` drove all stop UI). Reading
// only `=== "streaming"` here made the float vanish during the `aborting`
// window while the composer button stayed in stop state — out of sync.
const isStreaming = computed(() => store.isActiveTabBusy);

function ensureFirstTab(): void {
  if (store.tabs.length === 0) {
    store.openTab({ title: t("chat.tab.untitled") });
  }
}

// Restore the tab layout persisted from a previous session (reload / restart)
// so the user's open tabs + order + active tab survive a refresh. Only the
// skeleton (conversationId + title + order) is persisted; each restored tab's
// messages are lazily fetched from the backend by conversationId below. Falls
// back to a single default tab when nothing was persisted.
function restoreOrInitTabs(): void {
  if (store.tabs.length > 0) {
    return;
  }
  const layout = loadTabsLayout();
  const restored = layout !== null ? store.restoreLayout(layout) : 0;
  if (restored === 0) {
    ensureFirstTab();
    return;
  }
  // Lazily load each restored tab's history from the backend (the store
  // guards against double-loading and never clobbers live messages).
  for (const tab of store.tabs) {
    if (tab.conversationId !== null && tab.conversationId !== "") {
      void store.loadHistoryMessages(tab.id);
    }
  }
}

// Restore (or open a default tab) on mount so the composer is usable.
restoreOrInitTabs();

// Persist the tab layout whenever it changes (open / close / switch / rename /
// reorder / conversation binding). Debounced via Vue's batched watcher; only
// the lightweight skeleton is written (see chatTabsPersistence).
watch(
  () => store.persistedLayout,
  (layout) => {
    saveTabsLayout(layout);
  },
  { deep: true },
);

/**
 * Submit a prompt to a SPECIFIC tab (by id), independent of which tab is
 * currently active. This is the single source of truth for "send a turn to
 * tab X"; both the interactive composer path (`onSubmit`, targets the active
 * tab) and the auto-dequeue watcher (targets the tab that just finished its
 * turn) route through here.
 *
 * Why an explicit `id` matters: a queued message belongs to the tab it was
 * enqueued on. When that tab's turn finishes the watcher dequeues from THAT
 * tab and must re-send to THAT SAME tab — NOT the active tab. With multiple
 * tabs each streaming + each holding a queued message, the finishing tab is
 * frequently NOT the active tab, so binding the re-send to the active tab
 * dropped the dequeued item (it left the finishing tab's queue irreversibly,
 * then hit the idle-gate of the still-streaming active tab and was silently
 * discarded → "排队任务卡住/消失"). Passing the dequeued tab's own id fixes
 * both symptoms.
 */
async function submitToTab(id: string, prompt: string): Promise<void> {
  // Single source of truth for "send a turn to tab X" now lives in the
  // `useChatTurnSubmit` composable (extracted verbatim so the interactive
  // composer submit, the auto-dequeue watcher, AND the scheduled-continuation
  // timer all route through the SAME slash-command → error-reset →
  // pushUserMessage → transport.send path). See its module doc for the
  // explicit-`id` rationale (background-tab sends must not be dropped by the
  // active tab's idle-gate).
  await turnSubmit.submitToTab(id, prompt);
}

async function onSubmit(prompt: string): Promise<void> {
  const id = activeTabId.value;
  if (id === null) {
    return;
  }
  await submitToTab(id, prompt);
}

// Backend transient-race signature for a queued re-send that landed a hair
// too early (see the watcher note below + streaming.py:1929-1936). The prior
// turn's tab had not yet been observed IDLE by the re-send's request when it
// called `start_stream()`. The backend now releases the abort handle eagerly
// at completion so this should virtually never fire, but we keep a single
// front-end retry as a belt-and-suspenders backstop (AGENTS.md 🔴
// State-Truth-First 铁律3: success/failure must reconcile against the real
// settled state, not the first optimistic attempt).
const RESEND_RACE_ERROR = "requires status=IDLE";

/**
 * Re-send a dequeued queued message to its OWN tab, retrying exactly once if
 * the send fails with the transient "tab not yet IDLE" backend race.
 *
 * Why a dedicated helper (not plain `submitToTab`): the retry is scoped ONLY
 * to the auto-dequeue path. An interactive composer submit that errors must
 * surface the error to the user (and is itself a user action they can retry),
 * whereas a queued re-send firing on the `done`→idle edge can legitimately
 * lose a race with the just-finished turn's backend cleanup — and silently
 * dropping it would strand the queued message (the reported bug:
 * `start_stream() requires status=IDLE, got streaming`, "排队消息没发出去").
 */
async function resendQueuedItem(id: string, text: string): Promise<void> {
  await submitToTab(id, text);
  // submitToTab swallows transport errors (they surface via recordError →
  // tab.status="error"). Detect the specific transient race and retry once.
  const tab = store.tabs.find((t) => t.id === id);
  const msg = tab?.lastError?.message ?? "";
  if (tab?.status === "error" && msg.includes(RESEND_RACE_ERROR)) {
    // Clear the error and retry after a tick so the backend has fully settled
    // the prior turn (released its abort handle + persisted the IDLE tab).
    store.resetError(id);
    await nextTick();
    await submitToTab(id, text);
  }
}

function onCancel(): void {
  const tab = store.activeTab;
  if (tab === null) return;
  const id = tab.id;
  // 🔴 State-Truth-First (AGENTS.md §7 铁律 1): an inflight transport IS the
  // direct evidence "this tab's stream is in flight" — so it MUST win over any
  // indirect inference from `tab.kind`. There are TWO kinds of "running thing"
  // a sub-agent tab may host, and they register their cooperative-abort flag
  // in TWO DIFFERENT backend registries — picking the wrong path is a silent
  // no-op (the reported "tab ⏹ 按了无效、模型继续输出" bug).
  //
  // P1 — TAKE-OVER turn driven by THIS tab's composer:
  //   A sub-agent target (legacy `kind === "subagent"` independent tab OR the
  //   PR-3 main-tab-with-active-sub-agent path) can itself send new turns
  //   (the user resumes the conversation). That turn flows through
  //   `useChatTransport` → `POST /api/chat/stream` (with `?subagent_id=…`
  //   query) → `StreamChatUseCase._run`, which registers in
  //   `stream_abort_registry` keyed by **tab_id** (streaming.py:2295).
  //   It is NOT registered in `subagent_abort_registry`. So a Stop here must
  //   go through `transport.cancel()` (WS `{type:"stop"}` + best-effort
  //   POST `/api/chat/stop` → hits stream_abort_registry/tab_id → real abort).
  //   Routing this to `interruptSubAgent(subagentId)` would POST
  //   `/api/chat/subagents/{id}/interrupt` → check the wrong registry → miss
  //   → return `aborted=false` → the optimistic-rollback in `interruptSubAgent`
  //   flips the UI back to `streaming` → user sees nothing change and the
  //   model keeps emitting.
  //
  // P2 — Sub-agent run SPAWNED by a parent conversation, observed here:
  //   The tab is opened to follow a sub-agent that was spawned by a parent
  //   conversation's `agent` tool call and runs inside
  //   `AgentToolHandler._iter_loop` on the parent's turn. THIS tab has NO
  //   transport (it only subscribes to a read-only WS for live frames). The
  //   sub-agent's cancellation flag IS registered in `subagent_abort_registry`
  //   keyed by **subagent_id** (agent_tool.py:1192-1194). So a Stop here must
  //   go through `interruptSubAgent(subagentId)` → POST
  //   `/api/chat/subagents/{id}/interrupt` → hits subagent_abort_registry →
  //   real abort.
  //
  // ⚠️ Subtlety (the SUBAGENT-STOP-RESIDUAL-TRANSPORT race, fixed here):
  //   `peekTransport` is a module-level Map keyed by tab id — once a tab
  //   take-over has happened it leaves a transport behind for the LIFETIME
  //   of the tab (pruned only on tab close, useChatTransports.ts:88-100).
  //   So a P2-style external wake on a tab that EARLIER had a take-over
  //   would see `peekTransport(id) !== undefined` even though that leftover
  //   transport is idle and unrelated to the current run. The truthful
  //   discriminator is therefore not "transport exists" but "transport is
  //   ACTIVELY running" — i.e. `isInFlight()` (introduced on the transport
  //   interface for this exact purpose). An idle leftover transport falls
  //   through to the P2 branch so the Stop hits `subagent_abort_registry`
  //   instead of being silently absorbed by a no-op `cancel()`.
  //
  //   sub-agent's OWN `messages` array AND has no transport of its own
  //   (it only subscribes to a read-only WS for live frames). For Stop on
  //   such a tab we read `tab.subagentMeta?.subagentId` and POST
  //   `/api/chat/subagents/{id}/interrupt` via `interruptSubAgent`.
  //
  // The optimistic `streaming → aborting` UI flip for P1 happens inside
  // `transport.cancel()` → store `requestCancel` (chatTabs.ts), matching the
  // previous P1 behaviour byte-for-byte.
  const transport = peekTransport(id);
  if (transport !== undefined && transport.isInFlight()) {
    transport.cancel();
    return;
  }

  // No actively-running transport for this tab → either an ordinary chat tab
  // that has never sent a turn, or a tab whose visible activity is a
  // parent-spawned sub-agent run (P2). For a sub-agent tab read its id off
  // `tab.subagentMeta` and route through `interruptSubAgent`, which keeps
  // the optimistic `streaming → aborting` / `running → aborting` UI flip
  // for the standalone tab AND any SubAgentBlock instances on parent tabs
  // (rollback on `aborted=false` / network error — see chatTabs.ts
  // interruptSubAgent).
  if (tab.kind === "subagent") {
    const sid = tab.subagentMeta?.subagentId;
    if (sid !== undefined && sid !== "") {
      void store.interruptSubAgent(sid);
      return;
    }
  }

  // No transport, no sub-agent target → nothing to cancel.
}

// Composer instance handle — used to recall a pending queue item back into the
// composer draft for re-editing (the MessageQueuePanel ✎ "edit" affordance, F6).
const composerRef = useTemplateRef<{ recallToDraft: (text: string) => void }>(
  "composerRef",
);

// ChatMessageList instance handle — used by the composer-toolbar
// UserMessageJumpPopover to scroll the conversation viewport to a chosen user
// message (the bubble-with-lines button in `.rit-right`). The list component
// exposes `scrollToMessage(id)` via `defineExpose`; the composer keeps no
// reference to the list — this view routes the event so cross-component
// coupling stays at the parent level.
const messageListRef = useTemplateRef<{ scrollToMessage: (id: string) => void }>(
  "messageListRef",
);

function onJumpToMessage(messageId: string): void {
  messageListRef.value?.scrollToMessage(messageId);
}

/**
 * MessageQueuePanel `@edit` → recall the (already-dequeued) pending item's
 * text into the composer draft for re-editing. The panel removed the item from
 * the queue before emitting; here we just route the text into the composer
 * (which appends to the current draft + focuses the textarea end).
 */
function onRecallToComposer(text: string): void {
  composerRef.value?.recallToDraft(text);
}

/**
 * Withdraw a still-pending mid-turn injection from the live run + remove its
 * grey bubble (the bubble's ✕ "cancel" affordance, V2 enhancement). This is
 * what finally REACHES the previously-orphaned `inject_cancel` control frame /
 * backend `withdraw` path from the UI.
 *
 * Order (State-Truth-First): send the best-effort `inject_cancel` frame FIRST
 * so the run won't fold this text in, THEN remove the optimistic bubble.
 * `sendInjectCancel` returns `false` when the control WS is not ready — we
 * still honour the user's cancel locally (remove the bubble) but surface an
 * informational toast that the injection MAY already have been delivered (we
 * can't guarantee withdrawal). The backend `inject_cancel` is best-effort /
 * idempotent: if the text was already drained, a later `injected_message`
 * frame simply re-commits a (now non-pending) bubble — acceptable, because the
 * model genuinely saw it (conversation truth).
 *
 * Returns whether the withdrawal frame was dispatched, so `onInjectEdit` can
 * reuse the same path before refilling the composer.
 */
function withdrawPendingInjection(id: string, text: string): boolean {
  const tabId = activeTabId.value;
  if (tabId === null) {
    return false;
  }
  const dispatched = useChatControlChannel().sendInjectCancel(tabId, text);
  store.removePendingInjection(tabId, id);
  if (!dispatched) {
    toast.info(
      t(
        "chat.injectMaybeDelivered",
        "Injection removed locally; it may already have been delivered to the run.",
      ),
    );
  }
  return dispatched;
}

/** ChatMessageList `@inject-cancel` → withdraw + remove the pending bubble. */
function onInjectCancel(payload: { id: string; text: string }): void {
  withdrawPendingInjection(payload.id, payload.text);
}

/**
 * ChatMessageList `@inject-edit` → withdraw + remove the pending bubble, THEN
 * refill the composer draft with its text for re-editing (reuses the SAME
 * `recallToDraft` wiring the queue panel's ✎ edit uses, so the text is appended
 * to the current draft + the textarea focused).
 */
function onInjectEdit(payload: { id: string; text: string }): void {
  withdrawPendingInjection(payload.id, payload.text);
  composerRef.value?.recallToDraft(payload.text);
}

/**
 * Quick-prompt chip → V1 behaviour `sendQuickMessage(chip.prompt)`:
 * the chip text is sent immediately as a user message (NOT inserted
 * into the composer for editing). Mirrors V1 useChat.sendQuickMessage.
 */
async function onQuickPrompt(prompt: string): Promise<void> {
  await onSubmit(prompt);
}

/**
 * App Builder empty-state example chip → FILL the composer draft (no submit),
 * so the user can review/edit the starter authoring prompt before sending.
 * Reuses the same `recallToDraft` wiring as the queue-panel ✎ edit path.
 */
function onFillPrompt(prompt: string): void {
  composerRef.value?.recallToDraft(prompt);
}

/**
 * ModeIntroCard action chip → route the stable `id` to the corresponding
 * mode-frame trigger (Plan §7 decision 5 — C+D combo). The action ids are
 * the same across modes so the ModeIntroCard stays presentational and the
 * concrete "open my apps menu / open promote panel / open optimize drawer"
 * side-effects live where they always did (the three mode-frame toolbars).
 *
 * We route via a shared bump-token composable (`useModeFrameTriggers`) so
 * the mode-frame components' local `ref` panel state stays intact — they
 * just `watch` the token and flip their own `menuOpen` ref on bump.
 *
 * Unknown ids are silently ignored (defensive — no runtime error if a
 * future chip is added and not yet wired here).
 */
const {
  requestOpenMyApps,
  requestOpenPromote,
  requestOpenOptimize,
  requestOpenProSettings,
  requestOpenProConnect,
  requestOpenCodePersona,
  requestOpenCodeContext,
} = useModeFrameTriggers();
function onModeIntroAction(id: string): void {
  switch (id) {
    case "open-my-apps":
      requestOpenMyApps();
      break;
    case "open-promote":
      requestOpenPromote();
      break;
    case "open-optimize":
      requestOpenOptimize();
      break;
    case "open-pro-settings":
      requestOpenProSettings();
      break;
    case "open-pro-connect":
      requestOpenProConnect();
      break;
    case "open-code-persona":
      requestOpenCodePersona();
      break;
    case "open-code-context":
      requestOpenCodeContext();
      break;
    default:
      // Unknown action id → no-op. Keeps forward compatibility with future
      // ModeIntroCard chips.
      break;
  }
}

/**
 * Retry a send-failed user message (V1 `retryLastMessage`,
 * useChat.js:2800-2807). Removes the failed user message (which resets
 * the tab to idle) and re-sends its original content as a fresh turn,
 * so a new user message + clean error state are produced.
 */
async function onRetryMessage(payload: {
  id: string;
  content: string;
}): Promise<void> {
  const id = activeTabId.value;
  if (id === null) {
    return;
  }
  // Drop the failed message; this also clears tab.lastError and returns
  // the tab to `idle` so `pushUserMessage` (inside onSubmit) accepts.
  store.removeMessage(id, payload.id);
  // State-Truth-First: defer the re-send by one tick — the SAME race the
  // message-queue auto-dequeue guards against (see the long note on the
  // `nextTick` deferral below). `removeMessage` flips the tab back to `idle`
  // from `confirmDone`/error while the failed turn's transport may still be
  // unwinding inside its `finally`; calling `onSubmit` synchronously here flips
  // the tab back to `streaming` before that unwind returns, so the prior turn's
  // EOF fall-through guard fires `confirmDone()` on the NEW turn and commits an
  // empty assistant bubble (total ≈ 0.01s). Awaiting a tick lets the prior turn
  // settle first, so retry produces exactly one clean turn (no empty bubbles).
  await nextTick();
  await onSubmit(payload.content);
}

// NOTE: transports are owned by the app-level `useChatTransports` singleton
// and are pruned only when a tab is closed — NOT when this view unmounts.
// This is what lets a background tab keep streaming after the user navigates
// away from /chat (V1 parity, useChat.js:759-760). Do NOT dispose transports
// in onBeforeUnmount here.

// ─── Message queue auto-dequeue (V1 useChat.js:2788-2796 parity) ─────────────
//
// V1 processes the pending queue in the `sendMessage` finally block: once
// the in-flight turn finishes it shifts the head off `messageQueue` and
// re-sends it. The V2 store keeps the queue per-tab; here we watch each
// tab's status and, on a streaming→idle transition (the V2 equivalent of
// V1's "turn finished" moment), dequeue one item and re-submit it through
// the SAME onSubmit path so it goes through slash-command handling +
// transport exactly like a fresh send. One item per transition keeps the
// FIFO order intact (the re-send moves the tab back to streaming, so the
// next item waits for the following idle transition — mirroring V1's
// recursive `sendMessage()` chain).
//
// ── State-Truth-First: defer the re-send by one tick (root-cause fix) ──────
// V1 re-sends at the TAIL of `sendMessage`'s finally — AFTER the in-flight
// turn has fully unwound (`isStreaming=false`, the SSE read settled) and an
// `await nextTick()` (useChat.js:2792). It does NOT re-send synchronously
// inside the "turn finished" moment.
//
// The bug: the streaming→idle transition fires from `confirmDone` while the
// PREVIOUS turn's `apiSSE` is still inside its `finally { await
// reader.cancel() }` (it has not returned yet). If we call `onSubmit`
// synchronously here, `pushUserMessage` flips the tab BACK to `streaming`
// before that prior `apiSSE` returns. The prior turn's EOF fall-through
// guard (`useChatTransport.ts` "loop exited without onDone" branch) then
// sees `status === "streaming"` again and wrongly fires
// `flushTurnPerf()` + `confirmDone()` on the NEW turn — committing an empty
// assistant bubble with `total ≈ 0.01s` (the new turn's freshly-reset
// timing) and forcing the new turn back to `idle`, so its real reply is
// dropped. Deferring the re-send with `await nextTick()` lets the prior
// turn's `apiSSE` fully return (and skip its EOF guard against the correct
// `idle` status) BEFORE the new turn moves the tab to `streaming`, exactly
// mirroring V1's "settle → nextTick → re-send" ordering.
//
// We pop the queue head SYNCHRONOUSLY (so a re-entrant flush of this deep
// watch — triggered when `dequeueMessage` patches the tab — does not pop the
// same item twice), then submit it after the tick.
//
// Note: a user-pressed Stop transitions the tab streaming → aborting → idle
// (requestCancel → confirmAbort). The queue is NO LONGER cleared on Stop
// (V1 useChat.js:2779-2796 — only a conversation-switch abort clears it, which
// V2's per-tab model never routes through requestCancel). So the queue must be
// drained on BOTH terminal transitions back to idle — `streaming→idle` (normal
// turn finished) AND `aborting→idle` (turn finished after a Stop) — otherwise a
// Stop would strand the queued messages forever ("队列中所有内容消失/不处理").
// This mirrors V1, whose `sendMessage` finally tail runs the dequeue after a
// user Stop just as it does after a normal completion.
watch(
  () => store.tabs.map((tab) => ({ id: tab.id, status: tab.status })),
  (next, prev) => {
    const prevById = new Map((prev ?? []).map((p) => [p.id, p.status]));
    for (const { id, status } of next) {
      const before = prevById.get(id);
      // Drain on any terminal transition back to idle: a normal finish
      // (streaming→idle) OR a finish after a user Stop (aborting→idle).
      const becameIdle =
        status === "idle" && (before === "streaming" || before === "aborting");
      if (becameIdle) {
        const item = store.dequeueMessage(id);
        if (item !== null) {
          // Recombine the uploaded-image markdown prefix (if any) with the
          // text so an image-bearing queued message replays as a full
          // multimodal turn — the re-send goes through the SAME
          // transport.send → WS/SSE `_extract_image_refs` → vision-block
          // resolution as a fresh image submit. `imagePrefix` is "" for a
          // text-only item, so this is a no-op there.
          const prompt = `${item.imagePrefix}${item.text}`;
          // Defer to the next tick so the just-finished turn's transport has
          // fully settled before the re-send flips the tab back to streaming
          // (see the State-Truth-First note above; V1 useChat.js:2792).
          //
          // CRITICAL: re-send to THIS tab (`id` — the one that just finished
          // and that we dequeued from), NOT the active tab. With multiple tabs
          // each streaming + each holding a queued message, the finishing tab
          // is often a BACKGROUND tab; routing the re-send through the active
          // tab (the old `onSubmit` behaviour) dropped the dequeued item and
          // left queues stranded ("排队任务卡住/消失"). `submitToTab(id, ...)`
          // binds the re-send to the dequeued tab's own id.
          //
          // `resendQueuedItem` adds a single retry against the transient
          // "tab not yet IDLE" backend race (belt-and-suspenders backstop to
          // the backend's eager abort-handle release; see its note above).
          void nextTick().then(() => resendQueuedItem(id, prompt));
        }
      }
    }
  },
  { deep: true },
);



// ─── Topbar action buttons (Wave 1, V1 parity) ──────────────────────────────
//
// Migrated out of AppHeader.vue (Wave 1, 2026-06-05). The five chat
// toolbar buttons used to be hardcoded inside AppHeader; they now live
// here because the click handlers and disabled flags are chat-scoped.
// AppHeader renders whatever this composable publishes via the
// headerActions Pinia store.
//
// Behaviour parity with the previous AppHeader implementation:
//   - Collapse/Expand Tool Cards → bulk-collapses every ToolExecPanel via
//                          ui.setToolCardsCollapsed. Method B semantics
//                          (2026-07-20): every card is forced to the
//                          chosen state AND its per-card `userToggled` is
//                          set so the running→done auto-collapse watcher
//                          stops firing — "I want to see every detail,
//                          don't fold anything". Disabled when tool cards
//                          are hidden altogether (Settings → Chat
//                          Display → Show tool-call cards = off).
//   - Export             → buildMarkdown + Blob/object-URL download;
//                          disabled when active tab has 0 messages,
//                          tooltip becomes `chat.exportEmptyHint`
//   - Clear              → chatTabs.clearMessages(activeTabId) — V1
//                          useChat.js:984-998 behaviour: clears the
//                          front-end buffer only, no confirm prompt,
//                          no backend delete
//   - New Conversation   → chatTabs.openTab(); rendered as the page CTA
//                          (variant: "primary")
//
// The "Tool Calls" pill was retired 2026-07-20: users almost never change
// this preference so the toolbar slot was expensive; moved to Settings →
// App Config → Chat Display (localStorage-persisted). `ui.showToolMessages`
// itself is unchanged — same state, same consumers.
//
// Topbar action icons now come from the shared `topbarIcons` module so the
// whole header shares one consistent monochrome line-icon family (see import
// above). They are static literal strings, safe for the `iconSvg` v-html
// escape-hatch documented in stores/headerActions.ts.

// --- Export helpers (V1 parity, migrated from AppHeader) ------------------

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

function nowYmd(): string {
  const d = new Date();
  return `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}`;
}

function nowReadable(): string {
  const d = new Date();
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function slugify(input: string): string {
  return (
    input
      .toLowerCase()
      .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "untitled"
  );
}

function roleLabel(role: ChatMessage["role"]): string {
  switch (role) {
    case "user":
      return "User";
    case "assistant":
      return "Assistant";
    case "system":
      return "System";
    case "tool":
    case "tool_indicator":
      return "Tool";
    default:
      return role;
  }
}

function buildMarkdown(
  title: string,
  messages: ChatMessage[],
  modelName: string,
): string {
  const lines: string[] = [];
  lines.push(`# ${title}`);
  lines.push("");
  lines.push(`> Exported on ${nowReadable()}`);
  lines.push(`> Model: ${modelName}`);
  lines.push("");
  for (const msg of messages) {
    lines.push(`**${roleLabel(msg.role)}**:`);
    lines.push("");
    lines.push(msg.content);
    lines.push("");
  }
  return lines.join("\n");
}

function handleToggleCollapseToolCards(): void {
  // ?? false ensures the initial null (never-clicked) state treats the
  // first click as "start collapsing" — matches user intuition "the button
  // shows Collapse when nothing is collapsed yet".
  ui.setToolCardsCollapsed(!(ui.toolCardsCollapsed ?? false));
}

function handleExport(): void {
  const tab = store.activeTab;
  if (tab === null) return;
  if (tab.messages.length === 0) return;
  const title = tab.title || t("chat.tab.untitled");
  // V1 parity (useChat.js export): include the active tab's model in the
  // markdown frontmatter so an exported transcript records which model
  // produced the assistant turns. Falls back to "(unknown)" when the tab
  // has no modelId set (defensive — shouldn't happen post-PR-054).
  const modelName = tab.modelId || "(unknown)";
  const md = buildMarkdown(title, tab.messages, modelName);
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  try {
    const filename = `${t("chat.exportFilenamePrefix")}-${slugify(title)}-${nowYmd()}.md`;
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

function handleClear(): void {
  // V1 useChat.js:984-998: clear the front-end buffer only; no
  // confirmation prompt, no backend delete. Reload by clicking the
  // conversation in the sidebar.
  const tab = store.activeTab;
  if (tab === null || tab.messages.length === 0) return;
  store.clearMessages(tab.id);
}

const exportDisabled = computed(() => {
  const tab = store.activeTab;
  return tab === null || tab.messages.length === 0;
});

const clearDisabled = computed(() => {
  const tab = store.activeTab;
  return tab === null || tab.messages.length === 0;
});

useHeaderActions((): HeaderAction[] => [
  {
    id: "chat.workspace",
    label: t("sessionWorkspace.headerLabel"),
    iconSvg: ICON_FOLDER,
    title: t("sessionWorkspace.headerTitle"),
    onClick: openWorkspaceDialog,
    testId: "chat-workspace-btn",
  },
  {
    id: "chat.collapseToolCards",
    label: ui.toolCardsCollapsed === true
      ? t("chat.expandToolCards")
      : t("chat.collapseToolCards"),
    title: !ui.showToolMessages
      ? t("chat.toolCardsHiddenHint")
      : ui.toolCardsCollapsed === true
        ? t("layout.expand_tool_cards")
        : t("layout.collapse_tool_cards"),
    iconSvg: ui.toolCardsCollapsed === true ? ICON_CHEVRON_DOWN : ICON_CHEVRON_UP,
    extraClass: "chat-toolbar-toggle",
    pressed: ui.toolCardsCollapsed === true,
    disabled: !ui.showToolMessages,
    onClick: handleToggleCollapseToolCards,
    testId: "chat-collapse-tool-cards-btn",
  },
  {
    id: "chat.export",
    label: t("chat.exportBtn"),
    iconSvg: ICON_DOWNLOAD,
    title: exportDisabled.value
      ? t("chat.exportEmptyHint")
      : t("layout.export"),
    disabled: exportDisabled.value,
    onClick: handleExport,
    testId: "chat-export-btn",
    overflow: true,
  },
  {
    id: "chat.clear",
    label: t("chat.clearBtn"),
    iconSvg: ICON_TRASH,
    title: clearDisabled.value
      ? t("chat.exportEmptyHint")
      : t("layout.clear"),
    disabled: clearDisabled.value,
    onClick: handleClear,
    testId: "chat-clear-btn",
    overflow: true,
  },
  // NOTE: the "New conversation" topbar button was removed (方案 A). The tab
  // strip's "+" (in the topbar, via AppHeader) is now the single new-chat
  // entry point, avoiding a duplicate affordance.
]);
</script>

<template>
  <section
    class="chat-view"
    :aria-label="t('views.chat.title')"
  >
    <!-- Claude Code mode (T2.7-B) — V1 parity (index.html:961-962).
         When `useClaudeCode.isCCMode` is true the entire chat surface
         (message list + composer/sidebar/tool-exec) is replaced by the
         CC view; the ChatComposer's Claude Code pill remains in the
         normal toolbar inside ChatViewClaudeCode for the exit gesture. -->
    <template v-if="claudeCode.isCCMode.value">
      <ChatViewClaudeCode />
    </template>

    <template v-else-if="openCode.isOCMode.value">
      <ChatViewOpenCode />
    </template>

    <template v-else>
      <!-- Multi-session tab strip lives in the topbar (see AppHeader.vue).
           TWO-LEVEL model: top strip has main-agent tabs only; the rail
           just below carries this main tab's sub-agents (open + closed). -->
      <!-- SubAgentRail — second-level navigator rendered directly below the
           parent main-agent tab. Data source is `railEntries` (merged
           backend index + open-tab live status), so closed sub-agents
           remain reachable as greyed chips that one-click re-hydrate.
           `v-if="activeMainTab"` only guards the brief mount→hydrate
           window when the store hasn't produced an active tab yet — when
           there are no sub-agents the rail STAYS MOUNTED and collapses
           itself via `.sub-agent-rail--empty { max-height: 0 }` so the
           in/out animation is smooth. -->
      <SubAgentRail
        v-if="activeMainTab"
        :sub-agents="railEntries"
        :active-sub-agent-id="railHighlightSid"
        @select-main="onSelectMainAgent"
        @select-subagent="onSelectSubAgent"
        @close-subagent="onCloseSubAgent"
      />
      <!-- Task-list pill (V2 enhancement): a compact pill floating at the
           top-right of the conversation that always shows the LATEST
           todowrite state; click to drop down an overlay panel that covers
           (does not push) the conversation. Self-hides when no tasks.
           Absolutely positioned against `.chat-view`, so its template order
           does not affect layout. -->
      <TaskListBar />

      <ChatMessageList
        ref="messageListRef"
        @quick-prompt="onQuickPrompt"
        @fill-prompt="onFillPrompt"
        @retry="onRetryMessage"
        @stop="onCancel"
        @inject-cancel="onInjectCancel"
        @inject-edit="onInjectEdit"
      />

      <!-- App Builder mode workbench overlay — RETAINED but hidden by
           default. Only mounts when the user opts in via Settings → App
           Config (`ui.app_builder.show_workbench`). Entering App Builder
           mode no longer pops the heavy console. -->
      <AppBuilderWorkbenchOverlay
        v-if="appBuilderActive && appBuilderShowWorkbench"
      />

      <!-- Implementation-run observability + control panel (DISC-1 §22.9).
           Self-hides when there is no active implementation plan
           (phase === "none"), so ordinary chat / discussion is unchanged. -->
      <ImplementationPanel />

      <!-- Mode-intro overlay: a top-right ⓘ button that pops out a
           3-step reference card on click. Anchored via
           `.chat-view__intro-anchor` (positioned in <style>). Only
           renders when `introMode` is non-null (see the computed for
           the full gate — currently: mode has an intro AND the tab
           already has messages, so empty tabs get their dedicated
           empty-state onboarding without a second overlay on top). -->
      <div v-if="introMode !== null" class="chat-view__intro-anchor">
        <ModeIntroCard
          :mode="introMode"
          @fill-prompt="onFillPrompt"
          @action="onModeIntroAction"
        />
      </div>

      <!-- Promote-ready CTA (Sprint 2, feedback 7B). Inline strip that
           appears above the composer when the active tab is in Model Builder,
           Model Hub or App Builder mode AND `usePromoteReadyDetection` sees
           promote-eligible precision variants (.bin/.dlc) under the
           conversation's model workspace. Self-hides otherwise; not persistent
           (session-scoped dismissal per workdir — see composable). -->
      <PromoteReadyNotice
        :visible="promoteReady.shouldShow.value"
        :variants="promoteReady.detectedVariants.value"
        :workdir="promoteReady.detectedWorkdir.value"
        @dismiss="promoteReady.dismiss"
        @promote="onPromoteReadyPromoted"
      />

      <ChatComposer
        ref="composerRef"
        :data-active-tab="activeTab?.id ?? ''"
        @submit="onSubmit"
        @cancel="onCancel"
        @jump-to-message="onJumpToMessage"
      />

      <!-- Pending message queue (behaviour: V1 index.html:864-890). V2
           layout: floating overlay stacked at the TOP-RIGHT of the chat
           view, directly below the task-list bar (see MessageQueuePanel /
           chat.css .queue-float). Self-hides when the active tab's queue is
           empty. The `edit` event recalls a pending item back into the
           composer draft for re-editing (F6). -->
      <MessageQueuePanel @edit="onRecallToComposer" />

      <!-- Session workspace dialog (V2 enhancement): set the active
           conversation's working directory. Same dialog the sidebar 📁
           entry uses; opened here from the top-bar header action. -->
      <ConversationWorkspaceDialog
        v-model="workspaceValue"
        :visible="workspaceOpen"
        :loading="workspaceLoading"
        @confirm="confirmWorkspace"
        @cancel="cancelWorkspace"
      />

      <!-- Floating Stop button (V1 index.html:922-937). Anchored to the
           non-scrolling `.chat-view` (NOT the scrollable message list) and
           pinned just above the composer via `--qai-chat-input-h`
           (published by ChatComposer's ResizeObserver). Visible only while
           streaming; clicking routes into the same `onCancel` path as the
           composer's send→⏹ button. -->
      <transition name="scroll-btn-fade">
        <div
          v-if="isStreaming"
          class="stop-streaming-float"
          data-testid="stop-streaming-float"
        >
          <button
            type="button"
            class="stop-streaming-btn"
            :title="t('chat.stopTooltip')"
            data-testid="stop-streaming-btn"
            @click="onCancel"
          >
            <span style="font-size:14px;line-height:1">■</span>
            {{ t("chat.stop") }}
          </button>
        </div>
      </transition>
    </template>
  </section>
</template>


