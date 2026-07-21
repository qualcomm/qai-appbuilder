<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChatTabStrip — top-of-view tab bar for the multi-tab chat workspace.
 *
 * Renders the array of open tabs from `useChatTabsStore`, surfaces the
 * 4-state machine via a coloured status dot, and offers click-to-switch
 * + close button per tab. A trailing "+" button opens a new tab.
 *
 * TWO-LEVEL tab model — the top strip carries only MAIN-agent tabs; every
 * sub-agent (at any depth) is surfaced under its active main tab via the
 * separate `SubAgentRail` component (see `ChatView.vue`). This is the SOLE
 * filter point: rendering, drag-target enumeration, overflow menu and every
 * other loop walk the same `visibleTabs`, so hidden sub-agent tabs cannot be
 * clicked / dragged / closed from this strip. The active highlight uses the
 * shared `store.activeMainTabId` getter so the strip lights up the parent
 * main tab even while the user is reading a sub-agent's transcript.
 */
import { computed, nextTick, onMounted, ref, watch } from "vue";
import type { ComponentPublicInstance } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson, ApiError } from "@/api";
import ChatOverflowPopover from "@/components/chat/ChatOverflowPopover.vue";
import { useChatTabs } from "@/composables/chat/useChatTabs";
import { useDragReorderTabs } from "@/composables/chat/useDragReorderTabs";
import { useHorizontalOverflow } from "@/composables/chat/useHorizontalOverflow";
import { useInlineRename } from "@/composables/chat/useInlineRename";
import { useConfirm } from "@/composables/useConfirm";
import { useToast } from "@/composables/useToast";
import { MAX_OPEN_TABS, useChatTabsStore } from "@/stores/chatTabs";
import type { ChatTab, ChatTabStatus, TabId } from "@/stores/chatTabs";
import {
  useConversationsStore,
  type ConversationSummary,
} from "@/stores/conversations";

const { t } = useI18n();
const tabs = useChatTabs();
const { confirm } = useConfirm();
const toast = useToast();
const conversationsStore = useConversationsStore();
const chatTabsStore = useChatTabsStore();

// Title lookup by conversation id, kept as a reactive map so the tab strip
// re-renders the moment the sidebar's authoritative title changes.
const titleByConversation = computed<Map<string, string>>(() => {
  const map = new Map<string, string>();
  for (const conv of conversationsStore.conversations) {
    map.set(conv.id, conv.title);
  }
  return map;
});

// Two-level tab model: the top strip renders MAIN-agent tabs only. Sub-agent
// tabs (`kind === "subagent"`) live under the active main tab in the separate
// `SubAgentRail` — filtered out here. Single filter point: `visibleTabs` is
// the walk source for rendering / drag / overflow menu / count semantics.
const visibleTabs = computed<readonly ChatTab[]>(() =>
  tabs.tabs.value.filter((t) => t.kind !== "subagent"),
);

/**
 * Effective "active main tab id" — when the raw active tab is a sub-agent
 * (filtered out of `visibleTabs`), the highlight walks to its parent main
 * tab so the top strip still lights up the parent while the user reads the
 * sub-agent's transcript. Reads the shared `store.activeMainTabId` getter
 * (single source of truth; ChatView's rail context uses the same getter,
 * so both surfaces agree on which main tab is "focused").
 */
const activeMainTabId = computed<TabId | null>(() => chatTabsStore.activeMainTabId);

// ── Drag-to-reorder (native HTML5 DnD, no extra dependency) ──────────────────
// State + handlers live in `useDragReorderTabs`; we hook `onReorder` to the
// store's reorder action.
const dragReorder = useDragReorderTabs<TabId>({
  onReorder: (fromId, toId) => tabs.reorderTabs(fromId, toId),
});

function statusKey(status: ChatTabStatus): string {
  switch (status) {
    case "streaming":
      return "chat.tab.statusStreaming";
    case "aborting":
      return "chat.tab.statusAborting";
    case "error":
      return "chat.tab.statusError";
    case "idle":
    default:
      return "chat.tab.statusIdle";
  }
}

function onSelect(tab: ChatTab): void {
  tabs.switchTab(tab.id);
}

function onClose(tab: ChatTab, ev: Event): void {
  ev.stopPropagation();
  tabs.closeTab(tab.id);
}

function onKeydown(tab: ChatTab, ev: KeyboardEvent): void {
  if (ev.key === "Enter" || ev.key === " ") {
    ev.preventDefault();
    tabs.switchTab(tab.id);
  }
}

// ── Inline rename (double-click a tab to edit its title in place) ────────────
// Mirrors the sidebar "重命名" flow EXACTLY for persistence: on commit it
// PATCHes `/api/chat/conversations/{id}` then updates BOTH stores
// (`conversationsStore.rename` + `chatTabsStore.renameTabsByConversation`) so
// the new title stays the single source of truth across the tab strip AND
// the RECENT CHATS list. Only CONVERSATION-BOUND, non-subagent tabs are
// editable (unbound tabs have no DB row; sub-agent titles are their own
// derived identity — see `visibleTitle`).
//
// UI-interaction (draft buffer / Enter / Escape / blur / IME guard / loading
// gate) lives in `useInlineRename`. Persistence + toast + double-store fan-out
// stays here via `onCommit` — that boundary keeps the "composable never
// imports stores" convention of the chat composables directory intact.
const rename = useInlineRename<TabId>({
  onCommit: async (tabId, newTitle) => {
    // Look up the tab at commit time — state may have shifted while the
    // editor was open (sidebar activity, sub-agent tab arriving, etc.).
    const tab = tabs.tabs.value.find((t) => t.id === tabId);
    if (tab === undefined) return;
    const conversationId = tab.conversationId;
    if (conversationId === null || conversationId === undefined) return;
    rename.beginLoading();
    try {
      const updated = await apiJson<ConversationSummary>(
        "PATCH",
        `/api/chat/conversations/${encodeURIComponent(conversationId)}`,
        { title: newTitle },
      );
      const finalTitle = updated.title ?? newTitle;
      // Persist to BOTH stores like the sidebar rename so the title sticks
      // everywhere (the tab strip derives its display from the conversation
      // store via `visibleTitle`).
      conversationsStore.rename(conversationId, finalTitle);
      chatTabsStore.renameTabsByConversation(conversationId, finalTitle);
    } catch (err) {
      // Throw to keep the editor open for retry (composable guards its
      // post-commit cancel() on "callback returned without throwing").
      void (err instanceof ApiError ? err.code : err);
      toast.error(t("chat.renameFailed"));
      throw err;
    } finally {
      rename.endLoading();
    }
  },
});
// Function template-ref: a string `ref` inside `v-for` would collect into an
// ARRAY in Vue 3, so use a function ref that captures only the single editing
// input (and clears it on unmount). Deterministic regardless of list context.
const editInputEl = ref<HTMLInputElement | null>(null);
function setEditInputRef(el: Element | ComponentPublicInstance | null): void {
  editInputEl.value = el instanceof HTMLInputElement ? el : null;
}

function isEditable(tab: ChatTab): boolean {
  return (
    tab.kind !== "subagent" &&
    tab.conversationId !== null &&
    tab.conversationId !== undefined
  );
}

function onDblClick(tab: ChatTab): void {
  if (!isEditable(tab) || rename.isLoading.value) {
    return;
  }
  rename.start(tab.id, visibleTitle(tab));
  void nextTick(() => {
    const el = editInputEl.value;
    if (el !== null) {
      el.focus();
      el.select();
    }
  });
}

function onNew(): void {
  if (tabs.atTabLimit.value) {
    return;
  }
  tabs.openTab();
}

function visibleTitle(tab: ChatTab): string {
  // For a tab BOUND to a conversation, the sidebar's `conversationsStore` is
  // the single source of truth for the title — the tab's own `title` is only a
  // seed snapshot taken when the tab was opened and can drift (e.g. the async
  // AI auto-title or a rename only reached one of the two stores). Deriving the
  // displayed title from the conversation store here keeps the tab strip and
  // the RECENT CHATS list permanently in sync instead of relying on every
  // update path remembering to double-write both stores.
  //
  // An UNBOUND (blank) tab has no conversation yet, so fall back to its own
  // local draft title.
  //
  // EXCEPTION — a sub-agent tab (`kind: "subagent"`): its `conversationId` is
  // the ROOT conversation (for transport / interrupt routing), but its title
  // (`"SubAgent: <prompt>"`) is its OWN identity and must NOT be overwritten
  // by the root conversation's authoritative title — otherwise every sub-agent
  // tab would display the parent's title, becoming indistinguishable from the
  // main-agent tab. Keep the tab's own `title` for sub-agent tabs.
  if (
    tab.kind !== "subagent" &&
    tab.conversationId !== null &&
    tab.conversationId !== undefined
  ) {
    const authoritative = titleByConversation.value.get(tab.conversationId);
    if (authoritative !== undefined && authoritative.trim() !== "") {
      return authoritative;
    }
  }
  return tab.title === "" ? t("chat.tab.untitled") : tab.title;
}

/** Sub-agent count for the parent-tab badge — reads the store's
 *  `subAgentIndex` cache (per-root-conversation full list of sub-agent
 *  sessions ever spawned), so the badge accurately reflects "how many
 *  sub-agents this conversation ever had" even when their tabs aren't open.
 *  Zero for a tab without a conversationId, or when the index hasn't been
 *  fetched yet (badge hides). */
function subAgentBadgeCount(tab: ChatTab): number {
  const convId = tab.conversationId;
  if (convId === null || convId === undefined || convId === "") return 0;
  return (chatTabsStore.subAgentIndex[convId] ?? []).length;
}

// ── Overflow handling ────────────────────────────────────────────────────────
// Tabs overflow to a hidden horizontal scroll track (see the CSS block) plus
// a "⋯" popover listing all tabs. `stripScrollEl` binds to `scrollEl`;
// outside-click dismissal is owned by `ChatOverflowPopover`. Only the
// tab-specific `scrollTabIntoView` (below) + the item-list re-measure watch
// stay local.
const {
  scrollEl: stripScrollEl,
  isOverflowing,
  canScrollLeft,
  canScrollRight,
  overflowOpen,
  toggleOverflow,
  closeOverflow,
  recompute: recomputeOverflow,
} = useHorizontalOverflow();

// Trigger `<button>` ref → `ChatOverflowPopover` (viewport-rect positioning
// + outside-click trigger whitelist).
const overflowTriggerEl = ref<HTMLElement | null>(null);

onMounted(() => {
  void nextTick(() => {
    // The strip is mounted lazily (`v-if="isChatView"`), so an active tab
    // may already exist before this component (and its `activeTabId` watch)
    // does. Scroll it into view once on mount to cover that path.
    const id = tabs.activeTabId.value;
    if (id !== null) scrollTabIntoView(id);
  });
});
// Tab count / titles changing can flip overflow without a container resize.
// Keyed on the DISPLAYED title (`visibleTitle`, derived from the conversation
// store) so authoritative title changes re-measure even when the tab's own
// seed `title` is untouched.
watch(
  () =>
    visibleTabs.value
      .map((tb) => `${tb.id}:${visibleTitle(tb)}:${subAgentBadgeCount(tb)}`)
      .join("|"),
  () => void nextTick(recomputeOverflow),
);

// Scroll the tab with the given id back into the visible region of the strip.
// Shared by `pickFromOverflow` (overflow menu) and the `activeTabId` watch
// below so that EVERY activation path (sidebar click, sub-agent open, "+",
// overflow pick, close-fallback) keeps the active tab visible.
function scrollTabIntoView(id: TabId): void {
  const el = stripScrollEl.value;
  if (el === null) return;
  const tabEl = el.querySelector<HTMLElement>(
    `[data-tab-id="${CSS.escape(id)}"]`,
  );
  tabEl?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function pickFromOverflow(tab: ChatTab): void {
  tabs.switchTab(tab.id);
  closeOverflow();
  // Scroll the just-selected tab back into view so focus is visibly on it.
  void nextTick(() => scrollTabIntoView(tab.id));
}

// Close a single session from the overflow list (per-row "×").
// `stopPropagation` so the row's own click (switch) does NOT fire — user
// wanted to close, not switch. Menu stays open so several closes in a row
// are convenient; auto-closes once the strip no longer overflows.
function closeFromOverflow(tab: ChatTab, ev: Event): void {
  ev.stopPropagation();
  tabs.closeTab(tab.id);
}

// "Close all sessions" — destructive, so it goes through the custom confirm
// dialog (never window.confirm; §3.9.2).
async function closeAllSessions(): Promise<void> {
  const count = visibleTabs.value.length;
  const ok = await confirm({
    title: t("chat.tab.closeAllConfirmTitle"),
    message: t("chat.tab.closeAllConfirm", { n: count }),
    confirmText: t("chat.tab.closeAllConfirmBtn"),
    confirmStyle: "danger",
  });
  if (!ok) {
    return;
  }
  tabs.closeAllTabs();
  closeOverflow();
}

const isActive = computed(() => (id: TabId) => id === activeMainTabId.value);

// When the active tab changes, scroll it into the visible region so the
// active highlight is never left clipped off-screen. `nextTick` waits for
// newly-created tabs to be rendered by `v-for` first.
watch(
  () => tabs.activeTabId.value,
  (id) => {
    if (id === null) return;
    void nextTick(() => scrollTabIntoView(id));
  },
);
</script>

<template>
  <div
    class="chat-tab-strip-wrap"
  >
    <div
      ref="stripScrollEl"
      class="chat-tab-strip"
      :class="{
        'chat-tab-strip--overflowing': isOverflowing,
        'chat-tab-strip--fade-left': canScrollLeft,
        'chat-tab-strip--fade-right': canScrollRight,
      }"
      role="tablist"
      :aria-label="t('chat.tab.tabsAria')"
    >
      <div
        v-for="tab in visibleTabs"
        :key="tab.id"
        role="tab"
        :draggable="!rename.isRenaming(tab.id)"
        class="chat-tab-strip__tab"
        :class="{
          'chat-tab-strip__tab--active': tab.id === activeMainTabId,
          'chat-tab-strip__tab--dragging': dragReorder.isDragging(tab.id),
          'chat-tab-strip__tab--dragover': dragReorder.isDragOver(tab.id),
          [`chat-tab-strip__tab--${tab.status}`]: true,
        }"
        :aria-selected="tab.id === activeMainTabId ? 'true' : 'false'"
        :data-tab-id="tab.id"
        :data-tab-status="tab.status"
        :data-tab-kind="tab.kind ?? 'chat'"
        :tabindex="tab.id === activeMainTabId ? 0 : -1"
        @click="onSelect(tab)"
        @dblclick="onDblClick(tab)"
        @keydown="(ev) => onKeydown(tab, ev)"
        @dragstart="(ev) => dragReorder.onDragStart(tab.id, ev)"
        @dragover="(ev) => dragReorder.onDragOver(tab.id, ev)"
        @drop="(ev) => dragReorder.onDrop(tab.id, ev)"
        @dragend="dragReorder.onDragEnd"
      >
        <span
          class="chat-tab-strip__dot"
          :class="`chat-tab-strip__dot--${tab.status}`"
          aria-hidden="true"
        />
        <input
          v-if="rename.isRenaming(tab.id)"
          :ref="setEditInputRef"
          v-model="rename.draft.value"
          class="chat-tab-strip__title-edit"
          type="text"
          :disabled="rename.isLoading.value"
          :aria-label="t('chat.tab.renameAria')"
          @click.stop
          @dblclick.stop
          @keydown.enter.stop.prevent="rename.onEnter"
          @keydown.escape.stop.prevent="rename.onEscape"
          @blur="rename.onBlur"
          @compositionstart="rename.onCompositionStart"
          @compositionend="rename.onCompositionEnd"
        />
        <span
          v-else
          class="chat-tab-strip__title"
        >{{ visibleTitle(tab) }}</span>
        <!-- Sub-agent count badge: shown only when this main tab has one or
             more sub-agents in the store's `subAgentIndex` (persisted cache
             of ALL sub-agent sessions rooted at this conversation, including
             ones whose tab isn't open). Non-interactive — clicks bubble up
             to the tab's own switch handler. -->
        <span
          v-if="subAgentBadgeCount(tab) > 0"
          class="chat-tab-strip__sub-badge"
          :title="t('chat.subAgent.parentTab.badgeTooltip', { count: subAgentBadgeCount(tab) })"
          data-testid="chat-tab-sub-badge"
          aria-hidden="true"
        >{{ subAgentBadgeCount(tab) }}</span>
        <span class="chat-tab-strip__status-sr-only">
          {{ t(statusKey(tab.status)) }}
        </span>
        <button
          class="chat-tab-strip__close"
          type="button"
          :aria-label="t('chat.tab.closeAria', { title: visibleTitle(tab) })"
          @click="(ev) => onClose(tab, ev)"
        >
          ×
        </button>
      </div>
    </div>

    <!-- Trailing controls live OUTSIDE the horizontal scroll container so they
         stay pinned in the viewport (flex-shrink:0) and never get clipped or
         scrolled out of reach when many tabs overflow. -->
    <button
      class="chat-tab-strip__new"
      type="button"
      :disabled="tabs.atTabLimit.value"
      :aria-label="
        tabs.atTabLimit.value
          ? t('chat.tab.newTabDisabledAria', { max: MAX_OPEN_TABS })
          : t('chat.tab.newTabAria')
      "
      :title="
        tabs.atTabLimit.value
          ? t('chat.tab.limitHint', { max: MAX_OPEN_TABS })
          : t('chat.tab.newTabAria')
      "
      data-testid="chat-new-tab"
      @click="onNew"
    >
      +
    </button>

    <!-- Overflow "⋯" trigger + dropdown — shown only when tabs don't fit.
         Panel is Teleported to `<body>` via `ChatOverflowPopover`; trigger
         stays here for local styling/i18n. -->
    <div
      v-if="isOverflowing"
      class="chat-tab-strip__overflow"
    >
      <button
        ref="overflowTriggerEl"
        type="button"
        class="chat-tab-strip__overflow-trigger"
        :class="{ 'chat-tab-strip__overflow-trigger--open': overflowOpen }"
        :aria-label="t('chat.tab.overflowAria')"
        :title="t('chat.tab.overflowAria')"
        :aria-expanded="overflowOpen"
        aria-haspopup="menu"
        data-testid="chat-tab-overflow"
        @click="toggleOverflow"
        @keydown.esc="closeOverflow"
      >
        ⋯
      </button>
      <ChatOverflowPopover
        :open="overflowOpen"
        :trigger-el="overflowTriggerEl"
        align="right"
        min-width="200px"
        max-width="320px"
        panel-class="chat-tab-strip__overflow-panel"
        :aria-label="t('chat.tab.overflowAria')"
        testid="chat-tab-overflow-panel"
        @close="closeOverflow"
      >
        <button
          v-for="tab in visibleTabs"
          :key="tab.id"
          type="button"
          role="menuitem"
          class="chat-tab-strip__overflow-item"
          :class="{
            'chat-tab-strip__overflow-item--active': isActive(tab.id),
          }"
          @click="pickFromOverflow(tab)"
        >
          <span
            class="chat-tab-strip__dot"
            :class="`chat-tab-strip__dot--${tab.status}`"
            aria-hidden="true"
          />
          <span class="chat-tab-strip__overflow-item-title">{{ visibleTitle(tab) }}</span>
          <span
            class="chat-tab-strip__overflow-item-close"
            role="button"
            tabindex="0"
            :aria-label="t('chat.tab.closeAria', { title: visibleTitle(tab) })"
            :title="t('chat.tab.closeAria', { title: visibleTitle(tab) })"
            @click="(ev) => closeFromOverflow(tab, ev)"
            @keydown.enter.stop.prevent="(ev) => closeFromOverflow(tab, ev)"
            @keydown.space.stop.prevent="(ev) => closeFromOverflow(tab, ev)"
          >
            ×
          </span>
        </button>
        <div class="chat-tab-strip__overflow-footer">
          <button
            type="button"
            role="menuitem"
            class="chat-tab-strip__overflow-close-all"
            :aria-label="t('chat.tab.closeAllAria')"
            data-testid="chat-tab-close-all"
            @click="closeAllSessions"
          >
            <span class="chat-tab-strip__overflow-close-all-icon" aria-hidden="true">×</span>
            <span>{{ t("chat.tab.closeAll") }}</span>
          </button>
        </div>
      </ChatOverflowPopover>
    </div>
  </div>
</template>

<style scoped>
.chat-tab-strip-wrap {
  display: flex;
  align-items: center;
  gap: var(--space-1, 4px);
  min-width: 0;
  flex: 1 1 auto;
}

.chat-tab-strip {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 0;
  padding: var(--space-1, 4px) 0;
  min-width: 0;
  flex: 1 1 auto;
  /* Tabs that don't fit are reachable via mouse-wheel horizontal scroll
     (see `onWheel`), drag/scrollIntoView, and the "⋯" overflow menu, so the
     native scrollbar is hidden (it clashed with the app's visual style and is
     awkward to operate). */
  overflow-x: auto;
  scrollbar-width: none; /* Firefox */
  -ms-overflow-style: none; /* legacy Edge */
}
.chat-tab-strip::-webkit-scrollbar {
  display: none; /* WebKit/Blink */
}

/* When tabs overflow, fade the edge(s) that have more tabs hidden so the
   clipped tab isn't hard-cut and the user gets a subtle "there's more" cue.
   The fade follows the scroll position (left fade once scrolled away from the
   start, right fade while more remains) — like Notepad++ / browser tab bars.
   Tabs are reachable by mouse-wheel horizontal scroll, drag, and the "⋯" menu.
   `mask-image` keeps the visuals theme-agnostic (works on any background). */
.chat-tab-strip--fade-right {
  -webkit-mask-image: linear-gradient(
    to right,
    #000 calc(100% - 24px),
    transparent 100%
  );
  mask-image: linear-gradient(
    to right,
    #000 calc(100% - 24px),
    transparent 100%
  );
}

.chat-tab-strip--fade-left {
  -webkit-mask-image: linear-gradient(
    to right,
    transparent 0,
    #000 24px
  );
  mask-image: linear-gradient(to right, transparent 0, #000 24px);
}

/* Both edges clipped (scrolled to the middle): fade left AND right. */
.chat-tab-strip--fade-left.chat-tab-strip--fade-right {
  -webkit-mask-image: linear-gradient(
    to right,
    transparent 0,
    #000 24px,
    #000 calc(100% - 24px),
    transparent 100%
  );
  mask-image: linear-gradient(
    to right,
    transparent 0,
    #000 24px,
    #000 calc(100% - 24px),
    transparent 100%
  );
}

.chat-tab-strip__tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1, 4px);
  padding: 5px 4px 5px 6px;
  min-height: 28px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text-primary);
  font: inherit;
  /* Width sizing uses `ch` (character-based) units instead of fixed px so the
     tab GROWS with the user's font size — a browser zoom / larger base font
     now yields proportionally wider tabs that keep showing ≥ ~10 title chars
     (problems 3 & 4). `min-width: 8ch` gives short titles ("测试") a readable
     floor that ALSO scales with zoom (problem 3), without bloating them.
     `max-width: clamp(min, preferred, max)`:
       - min 10ch  → the ceiling never drops below the min-width floor;
       - preferred 32ch → the working width. At the app's ~12–13px font this is
         ≈ 224px, i.e. ≥ the previous fixed 220px ceiling, so titles show at
         least as much text as before at 100% zoom AND scale up with zoom
         (problem 4 — never LESS than before);
       - max 40ch  → a single very long title can't hog the whole strip.
     The title inside still ellipsises within this ceiling. Do NOT add
     `flex-grow` here (it would defeat the `flex: 0 0 auto` overflow probe —
     see the flex note below). */
  min-width: 8ch;
  max-width: clamp(10ch, 32ch, 40ch);
  /* Width hugs the title (plus dot + close) up to its `max-width` ceiling, and
     STAYS at its content width once that ceiling is hit — `flex-shrink: 0`
     (`flex: 0 0 auto`) is critical here. If the tab were allowed to shrink
     (`flex: 0 1 auto`, the historical setting), a strip full of medium-length
     titles would never trigger horizontal overflow: the strip is a
     `display: flex` container with `overflow-x: auto`, so children with
     `flex-shrink: 1` and a `min-width: 0` title shrink themselves down until
     `scrollWidth === clientWidth`. The overflow probe
     (`useHorizontalOverflow.recompute`) reads `scrollWidth - clientWidth > 1`,
     so it would NEVER fire — every tab would render as a tiny ellipsis stub
     and the "⋯" button + edge fade + wheel-scroll affordance would all stay
     hidden. With `flex-shrink: 0` each tab keeps its content width (up to the
     `clamp` ceiling), the strip overflows for real once it runs out of room,
     and the shared overflow-menu / fade / wheel-scroll machinery works as
     designed. The title inside still ellipsises within its own ceiling. */
  flex: 0 0 auto;
  /* Drag-to-reorder: avoid selecting the title text while dragging. */
  user-select: none;
}

.chat-tab-strip__tab:hover {
  background: var(--bg-secondary, rgba(0, 0, 0, 0.04));
}

.chat-tab-strip__tab--active {
  background: var(--bg-hover, rgba(0, 122, 255, 0.08));
  border-color: var(--accent, rgba(0, 122, 255, 0.4));
}

/* The tab being dragged: dimmed so the drop target reads clearly. */
.chat-tab-strip__tab--dragging {
  opacity: 0.4;
}

/* The tab currently under the pointer during a drag: a left insertion bar
   cue (theme accent) showing where the dragged tab will land. */
.chat-tab-strip__tab--dragover {
  border-color: var(--accent, rgba(0, 122, 255, 0.4));
  box-shadow: inset 2px 0 0 0 var(--accent, rgba(0, 122, 255, 0.6));
}

.chat-tab-strip__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
}

.chat-tab-strip__dot--idle {
  background: var(--text-muted);
}

.chat-tab-strip__dot--streaming {
  background: var(--success, #16a34a);
}

.chat-tab-strip__dot--aborting {
  background: var(--warning, #f59e0b);
}

.chat-tab-strip__dot--error {
  background: var(--error, #dc2626);
}

.chat-tab-strip__title {
  flex: 1 1 auto;
  /* min-width:0 lets the title shrink (and ellipsis) instead of pushing the
     trailing close button out past the tab's max-width and clipping it. */
  min-width: 0;
  text-overflow: ellipsis;
  overflow: hidden;
  white-space: nowrap;
  font-size: var(--text-sm);
}

/* In-place rename input (shown on double-click). Sized to fill the same slot
   as the title span so the tab doesn't jump width while editing. */
.chat-tab-strip__title-edit {
  flex: 1 1 auto;
  min-width: 0;
  font: inherit;
  font-size: var(--text-sm);
  color: var(--text-primary);
  background: var(--bg-primary, var(--bg-secondary));
  border: 1px solid var(--accent, rgba(0, 122, 255, 0.6));
  border-radius: 4px;
  padding: 1px 4px;
  margin: -2px 0;
  outline: none;
}

.chat-tab-strip__title-edit:disabled {
  opacity: 0.6;
  cursor: progress;
}

/* Sub-agent count badge appended to a main tab's title when its
 * conversation has one or more sub-agents (open OR closed). Micro accent
 * pill, non-interactive; deepens on hover/active so it tracks the tab's
 * emphasis. Uses `var(--accent)` with `color-mix` so it stays theme-correct
 * without a new CSS token. */
.chat-tab-strip__sub-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  height: 14px;
  min-width: 14px;
  padding: 0 5px;
  margin-left: 4px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent, rgba(0, 122, 255, 1)) 18%, transparent);
  color: var(--accent, rgba(0, 122, 255, 1));
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
  pointer-events: none;
  user-select: none;
}

.chat-tab-strip__tab:hover .chat-tab-strip__sub-badge,
.chat-tab-strip__tab--active .chat-tab-strip__sub-badge {
  background: color-mix(in srgb, var(--accent, rgba(0, 122, 255, 1)) 28%, transparent);
}

.chat-tab-strip__status-sr-only {
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

.chat-tab-strip__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  background: none;
  border: 0;
  border-radius: 50%;
  color: var(--text-muted);
  cursor: pointer;
  padding: 0;
  font-size: 0.95rem;
  line-height: 1;
  /* Pinned within the tab: the title shrinks first (ellipsis), so the close
     button is always reachable even at the tab's max-width. */
  flex-shrink: 0;
  /* Browser-tab style: stay hidden until the tab is hovered, keyboard-focused,
     or active, so a row of many tabs reads cleanly instead of showing a column
     of "×" glyphs. It still reserves no extra width (it's always laid out), so
     revealing it doesn't shift the title. */
  opacity: 0;
  transition:
    opacity 0.12s ease,
    background-color 0.12s ease,
    color 0.12s ease;
}

.chat-tab-strip__tab:hover .chat-tab-strip__close,
.chat-tab-strip__tab:focus-within .chat-tab-strip__close,
.chat-tab-strip__tab--active .chat-tab-strip__close {
  opacity: 1;
}

.chat-tab-strip__close:hover {
  background: var(--bg-secondary, rgba(0, 0, 0, 0.08));
  color: var(--text-primary);
}

.chat-tab-strip__new {
  background: none;
  border: 1px dashed var(--border);
  border-radius: 6px;
  cursor: pointer;
  padding: 4px 10px;
  color: var(--text-muted);
  font: inherit;
  /* Pinned trailing control: never shrink or get scrolled out of view, so a
     new tab can always be created regardless of how many tabs are open. */
  flex: 0 0 auto;
}

.chat-tab-strip__new:hover {
  color: var(--text-primary);
  border-color: var(--text-muted);
}

.chat-tab-strip__new:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.chat-tab-strip__new:disabled:hover {
  color: var(--text-muted);
  border-color: var(--border);
}

/* ── Overflow "⋯" menu (replaces the native horizontal scrollbar) ───────── */
.chat-tab-strip__overflow {
  position: relative;
  flex: 0 0 auto;
}

.chat-tab-strip__overflow-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 26px;
  padding: 0 8px;
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  color: var(--text-secondary);
  font: inherit;
  font-size: 18px;
  font-weight: bold;
  line-height: 1;
  letter-spacing: 1px;
}

.chat-tab-strip__overflow-trigger:hover,
.chat-tab-strip__overflow-trigger--open {
  color: var(--text-primary);
  border-color: var(--accent);
}

/* Overflow-menu row/footer styles — applied to slot content of
   `<ChatOverflowPopover>`. Slot content is compiled by THIS component so
   the scoped hash matches without `:deep()`. Panel chrome lives in
   `ChatOverflowPopover`'s stylesheet. */
.chat-tab-strip__overflow-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  text-align: left;
}

.chat-tab-strip__overflow-item:hover {
  background: var(--bg-hover, rgba(0, 0, 0, 0.06));
}

.chat-tab-strip__overflow-item--active {
  background: var(--bg-hover, rgba(0, 122, 255, 0.08));
  color: var(--text-primary);
  font-weight: 600;
}

.chat-tab-strip__overflow-item-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Per-row close button. A small round hit-target that stays subtle until the
   row is hovered/focused (or the row is active), so the list reads cleanly but
   closing any single session is one click away — like browser tab overflow
   menus. Uses a <span role="button"> (not a nested <button>) because it lives
   inside the row's own <button>; nested buttons are invalid HTML. */
.chat-tab-strip__overflow-item-close {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  margin-left: 4px;
  border-radius: 50%;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  /* Hidden until the row is interacted with, so the list stays uncluttered. */
  opacity: 0;
  transition:
    opacity 0.12s ease,
    background-color 0.12s ease,
    color 0.12s ease;
}

.chat-tab-strip__overflow-item:hover .chat-tab-strip__overflow-item-close,
.chat-tab-strip__overflow-item:focus-within .chat-tab-strip__overflow-item-close,
.chat-tab-strip__overflow-item--active .chat-tab-strip__overflow-item-close {
  opacity: 1;
}

.chat-tab-strip__overflow-item-close:hover {
  background: var(--error, #dc2626);
  color: #fff;
}

/* ── "Close all sessions" footer ──────────────────────────────────────────
   A pinned destructive action separated from the session list by a divider,
   so it never gets lost among the rows and is clearly distinct from a normal
   session pick. */
.chat-tab-strip__overflow-footer {
  margin-top: 4px;
  padding-top: 4px;
  border-top: 1px solid var(--border);
}

.chat-tab-strip__overflow-close-all {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  text-align: left;
  transition:
    background-color 0.12s ease,
    color 0.12s ease;
}

.chat-tab-strip__overflow-close-all:hover {
  background: color-mix(in srgb, var(--error, #dc2626) 14%, transparent);
  color: var(--error, #dc2626);
}

.chat-tab-strip__overflow-close-all-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  font-size: 15px;
  line-height: 1;
  flex-shrink: 0;
}
</style>
