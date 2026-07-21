<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * FavoritesDialog — 我的收藏 library modal.
 *
 * A mid-size centered modal listing every favorited conversation, grouped by
 * the SAME five time buckets as the sidebar (复用 `useConversationGrouping`),
 * with an in-dialog search box whose behaviour is aligned with the main
 * sidebar history search (debounced backend full-text search over title +
 * message body via `GET /api/conversations/search`), scoped to favorites.
 *
 * Data source (correctness): the dialog fetches the COMPLETE favorites set
 * from `GET /api/chat/conversations?favorite=true&limit=500` each time it
 * opens — it does NOT read the sidebar store, which is capped at the most
 * recent 50 conversations and would silently hide favorites that have aged
 * out of that window. Search then filters within this complete set.
 *
 * Interactions:
 *   • click a row  → emits `select(conv)` (host opens the conversation and
 *     closes the dialog);
 *   • click the ⭐ → emits `toggle-favorite(conv)` (host unfavorites it) and
 *     the row is removed from the local list immediately;
 *   • backdrop / ✕ / Esc → emits `close`.
 *
 * No window.confirm/alert/prompt (AGENTS.md §3.9.2): unfavorite is a tiny
 * reversible action, applied directly with no confirm step.
 */
import { computed, ref, watch, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { type ConversationSummary } from "@/stores/conversations";
import {
  useConversationGrouping,
  type HistoryGroup,
} from "@/composables/useConversationGrouping";
import { useFocusTrap } from "@/composables/useFocusTrap";

const props = defineProps<{ visible: boolean }>();
const emit = defineEmits<{
  (e: "select", conv: ConversationSummary): void;
  (e: "toggle-favorite", conv: ConversationSummary): void;
  (e: "close"): void;
}>();

const { t } = useI18n();

// The COMPLETE favorites set, fetched from the backend when the dialog opens
// (not the 50-capped sidebar store). Local list so an unfavorite removes the
// row immediately without a refetch.
const favorites = ref<ConversationSummary[]>([]);
const loading = ref(false);
const loadFailed = ref(false);

// In-dialog search query + the set of conversation ids the backend full-text
// search matched (aligned with the main sidebar history search). When the
// query is blank we ignore this set and show ALL favorites grouped by time.
const query = ref("");
const matchedIds = ref<Set<string> | null>(null);
const isSearching = ref(false);

interface ConversationListResponse {
  items: ConversationSummary[];
}

let loadSeq = 0;

async function loadFavorites(): Promise<void> {
  const seq = ++loadSeq;
  loading.value = true;
  loadFailed.value = false;
  try {
    const res = await apiJson<ConversationListResponse>(
      "GET",
      "/api/chat/conversations?favorite=true&limit=500",
    );
    if (seq !== loadSeq) return;
    favorites.value = res.items ?? [];
  } catch (err) {
    void err;
    if (seq !== loadSeq) return;
    favorites.value = [];
    loadFailed.value = true;
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

// Favorites narrowed by the active search (intersect with backend matches);
// blank query → all favorites.
const filteredFavorites = computed<ConversationSummary[]>(() => {
  const ids = matchedIds.value;
  if (query.value.trim() === "" || ids === null) return favorites.value;
  return favorites.value.filter((c) => ids.has(c.id));
});

// Reuse the sidebar's five-bucket time grouping so categories/ordering match
// exactly (复用 > 重造). The dedicated 置顶 group only applies to the sidebar
// list, so we strip the pinned flag for grouping purposes via a plain copy.
const groupingSource = computed<ConversationSummary[]>(() =>
  filteredFavorites.value.map((c) => ({ ...c, pinned: false })),
);
const { groupedConversations } = useConversationGrouping(groupingSource);
const groups = computed<HistoryGroup[]>(() => groupedConversations.value);

const hasFavorites = computed(() => favorites.value.length > 0);
const hasResults = computed(() => filteredFavorites.value.length > 0);

// ── Search (aligned with useConversationSearch: 300ms debounce + stale-guard,
//    same backend endpoint) ──────────────────────────────────────────────────
let searchTimer: ReturnType<typeof setTimeout> | null = null;
let searchSeq = 0;

interface ConversationSearchResponse {
  results: { id: string }[];
  total: number;
  query: string;
}

async function runSearch(q: string): Promise<void> {
  const seq = ++searchSeq;
  isSearching.value = true;
  try {
    const data = await apiJson<ConversationSearchResponse>(
      "GET",
      `/api/conversations/search?q=${encodeURIComponent(q)}&limit=500`,
    );
    if (seq !== searchSeq) return;
    matchedIds.value = new Set((data.results ?? []).map((r) => r.id));
  } catch (err) {
    void err;
    if (seq === searchSeq) matchedIds.value = new Set();
  } finally {
    if (seq === searchSeq) isSearching.value = false;
  }
}

watch(query, (next) => {
  if (searchTimer !== null) clearTimeout(searchTimer);
  const q = next.trim();
  if (q === "") {
    matchedIds.value = null;
    isSearching.value = false;
    return;
  }
  searchTimer = setTimeout(() => {
    void runSearch(q);
  }, 300);
});

// Fetch the complete favorites set + reset search each time the dialog opens.
watch(
  () => props.visible,
  (open) => {
    if (open) {
      query.value = "";
      matchedIds.value = null;
      isSearching.value = false;
      void loadFavorites();
    }
  },
);

onBeforeUnmount(() => {
  if (searchTimer !== null) {
    clearTimeout(searchTimer);
    searchTimer = null;
  }
});

// ── Focus trap + Esc close (mirrors ConfirmDialog) ──────────────────────────
const dialogEl = ref<HTMLElement | null>(null);
const visibleRef = computed(() => props.visible);
useFocusTrap(dialogEl, { active: visibleRef, onEscape: () => emit("close") });

function onRowClick(conv: ConversationSummary): void {
  emit("select", conv);
}
function onUnfavorite(conv: ConversationSummary): void {
  // Remove from the local list immediately (the host toggles the store +
  // backend). Re-fetching is unnecessary and would cause a flash.
  favorites.value = favorites.value.filter((c) => c.id !== conv.id);
  emit("toggle-favorite", conv);
}

/** Relative time, mirroring the sidebar's formatV1Time output shape. */
function formatTime(iso: string): string {
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "";
  const d = new Date(ts);
  const now = new Date();
  const todayStart = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const hhmm = `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
  if (ts >= todayStart) return `${t("time.today")} ${hhmm}`;
  if (ts >= todayStart - 86400000) return `${t("time.yesterday")} ${hhmm}`;
  return d.toLocaleDateString();
}
</script>

<template>
  <div
    v-if="visible"
    class="confirm-overlay"
    @click.self="emit('close')"
  >
    <div
      ref="dialogEl"
      class="favorites-dialog"
      role="dialog"
      aria-modal="true"
      :aria-label="t('favorites.title')"
    >
      <div class="favorites-dialog-header">
        <span class="favorites-dialog-title">⭐ {{ t("favorites.title") }}</span>
        <button
          type="button"
          class="btn btn-ghost btn-xs favorites-close-btn"
          :title="t('common.close')"
          :aria-label="t('common.close')"
          @click="emit('close')"
        >
          ✕
        </button>
      </div>

      <div class="favorites-dialog-search">
        <input
          v-model="query"
          type="text"
          class="conv-search-input"
          :placeholder="t('favorites.searchPlaceholder')"
        />
        <span
          v-if="isSearching"
          class="conv-search-spinner"
          aria-hidden="true"
        ></span>
      </div>

      <div class="favorites-dialog-body">
        <!-- Loading the complete favorites set. -->
        <div
          v-if="loading"
          class="favorites-empty"
        >
          <div class="favorites-empty-text">{{ t("chat.messages.loading") }}</div>
        </div>

        <!-- Load failed. -->
        <div
          v-else-if="loadFailed"
          class="favorites-empty"
        >
          <div class="favorites-empty-text">{{ t("chat.historyLoadFailed") }}</div>
          <button
            type="button"
            class="btn btn-ghost btn-xs"
            @click="loadFavorites()"
          >
            {{ t("common.retry") }}
          </button>
        </div>

        <!-- Empty: no favorites at all. -->
        <div
          v-else-if="!hasFavorites"
          class="favorites-empty"
        >
          <div class="favorites-empty-icon">☆</div>
          <div class="favorites-empty-text">{{ t("favorites.emptyHint") }}</div>
        </div>

        <!-- Empty: favorites exist but none match the search. -->
        <div
          v-else-if="!hasResults"
          class="favorites-empty"
        >
          <div class="favorites-empty-text">{{ t("favorites.noResults") }}</div>
        </div>

        <!-- Grouped favorites (same five time buckets as the sidebar). -->
        <template
          v-for="group in groups"
          v-else
          :key="group.key"
        >
          <div class="conv-group-header">{{ group.label }}</div>
          <div
            v-for="conv in group.items"
            :key="conv.id"
            class="conv-item favorites-row"
            @click="onRowClick(conv)"
          >
            <span
              class="conv-channel-icon"
              aria-hidden="true"
            >💬</span>
            <span class="conv-item-title">
              <span class="conv-item-title-text">{{ conv.title || t("chat.tab.untitled") }}</span>
              <div class="conv-item-meta">
                <span class="conv-item-time">{{ formatTime(conv.updated_at) }}</span>
                <span
                  v-if="(conv.round_count ?? conv.message_count ?? 0) > 0"
                  class="conv-badge conv-badge--info"
                >{{ conv.round_count ?? conv.message_count }}{{ t("sidebar.roundsSuffix") }}</span>
                <span
                  v-if="(conv.tool_call_count ?? 0) > 0"
                  class="conv-badge conv-badge--info"
                >🔧{{ conv.tool_call_count }}</span>
              </div>
            </span>
            <button
              type="button"
              class="btn btn-ghost btn-xs favorites-row-star"
              :title="t('sidebar.unfavoriteConversation')"
              :aria-label="t('sidebar.unfavoriteConversation')"
              @click.stop="onUnfavorite(conv)"
            >
              ⭐
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.favorites-dialog {
  display: flex;
  flex-direction: column;
  width: min(680px, 92vw);
  max-height: 72vh;
  background: var(--bg-elevated, var(--bg-secondary));
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
  border-radius: 12px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.favorites-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border, rgba(255, 255, 255, 0.06));
}
.favorites-dialog-title {
  font-weight: 600;
  font-size: 1.02em;
}
.favorites-close-btn {
  flex-shrink: 0;
}
.favorites-dialog-search {
  position: relative;
  display: flex;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border, rgba(255, 255, 255, 0.06));
}
.favorites-dialog-search .conv-search-input {
  width: 100%;
}
.favorites-dialog-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px 8px 12px;
}
.favorites-row {
  cursor: pointer;
}
.favorites-row-star {
  flex-shrink: 0;
  color: var(--accent, #f5b301);
}
.favorites-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 24px;
  color: var(--text-secondary);
  text-align: center;
}
.favorites-empty-icon {
  font-size: 2.2em;
  opacity: 0.5;
}
.favorites-empty-text {
  max-width: 38ch;
  line-height: 1.5;
}
</style>
