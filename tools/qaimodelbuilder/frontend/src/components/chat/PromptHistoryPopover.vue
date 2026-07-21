<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * PromptHistoryPopover — recent + favorite prompt picker for the composer.
 *
 * Anchored above the composer's clock-icon "history" button (the
 * `.rit-history` button in `.rit-right`). Mirrors the toolbar-pill popover
 * pattern of `WhisperEnginePopover.vue`: `open` prop + `update:open` emit,
 * bottom-anchored, closes on an outside `mousedown`.
 *
 * Two groups, favorites first (user spec — default shows up to
 * FAV_DEFAULT_VISIBLE favorites), then recent history:
 *   - Click a row's text          → emit `fill` (composer writes it into the
 *                                    textarea, NOT sent) and close.
 *   - Click a row's ⭐/☆          → toggle favorite, popover stays open.
 *   - Recent row ✕ (hover)        → remove that one entry.
 *   - Header "clear"              → confirm dialog → clear all recent history.
 *
 * All state lives in the shared `usePromptHistory` composable; this component
 * is a thin view over it. Colors come entirely from design tokens (light/dark
 * safe). No native confirm()/alert() — uses the project's `useConfirm`.
 */
import { computed, onBeforeUnmount, onMounted, ref, useTemplateRef, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useConfirm } from "@/composables/useConfirm";
import {
  usePromptHistory,
  FAV_DEFAULT_VISIBLE,
  RECENT_VISIBLE,
  type PromptEntry,
} from "@/composables/chat/usePromptHistory";

interface Props {
  open: boolean;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "update:open": [value: boolean];
  /** A prompt was chosen — the composer fills it into the textarea. */
  fill: [text: string];
}>();

const { t } = useI18n();
const { confirm } = useConfirm();
const {
  recent,
  favorites,
  isFavorite,
  toggleFavorite,
  removeRecent,
  removeFavorite,
  clearRecent,
} = usePromptHistory();

const popoverRef = useTemplateRef<HTMLDivElement>("popover");
const query = ref("");
const showAllFavorites = ref(false);

function matches(e: PromptEntry): boolean {
  const q = query.value.trim().toLowerCase();
  if (q === "") {
    return true;
  }
  return e.text.toLowerCase().includes(q);
}

const filteredFavorites = computed(() => favorites.value.filter(matches));
const filteredRecent = computed(() =>
  recent.value
    // Hide recent rows that are already favorited (avoid showing twice) only
    // when not searching, so a search still finds everything by text.
    .filter((e) => query.value.trim() !== "" || !isFavorite(e.text))
    .filter(matches),
);

const visibleFavorites = computed(() => {
  if (showAllFavorites.value || query.value.trim() !== "") {
    return filteredFavorites.value;
  }
  return filteredFavorites.value.slice(0, FAV_DEFAULT_VISIBLE);
});
const hasMoreFavorites = computed(
  () =>
    query.value.trim() === "" &&
    filteredFavorites.value.length > FAV_DEFAULT_VISIBLE,
);
const visibleRecent = computed(() =>
  filteredRecent.value.slice(0, RECENT_VISIBLE),
);

const isEmpty = computed(
  () => favorites.value.length === 0 && recent.value.length === 0,
);
const hasResults = computed(
  () => visibleFavorites.value.length > 0 || visibleRecent.value.length > 0,
);

function pick(text: string): void {
  emit("fill", text);
  close();
}

function onToggleFav(text: string): void {
  toggleFavorite(text);
}

function close(): void {
  emit("update:open", false);
}

async function onClear(): Promise<void> {
  const ok = await confirm({
    title: t("promptHistory.clearTitle"),
    message: t("promptHistory.clearConfirm"),
    confirmText: t("promptHistory.clear"),
    confirmStyle: "danger",
  });
  if (ok) {
    clearRecent();
  }
}

function onDocMouseDown(ev: MouseEvent): void {
  if (!props.open) {
    return;
  }
  const el = popoverRef.value;
  if (el === null) {
    return;
  }
  const target = ev.target as Node | null;
  if (target === null) {
    return;
  }
  if (el.contains(target)) {
    return;
  }
  const wrap = el.parentElement;
  if (wrap !== null && wrap.contains(target)) {
    return;
  }
  close();
}

// Reset the transient search + expand state each time the popover opens so it
// always starts from the default "favorites first, collapsed" view.
watch(
  () => props.open,
  (next: boolean) => {
    if (next) {
      query.value = "";
      showAllFavorites.value = false;
    }
  },
);

function onKeydown(ev: KeyboardEvent): void {
  if (ev.key === "Escape" && props.open) {
    ev.stopPropagation();
    close();
  }
}

onMounted(() => {
  document.addEventListener("mousedown", onDocMouseDown, true);
});
onBeforeUnmount(() => {
  document.removeEventListener("mousedown", onDocMouseDown, true);
});
</script>

<template>
  <div
    v-if="open"
    ref="popover"
    class="rit-prompt-history-menu riph-menu"
    role="dialog"
    :aria-label="t('promptHistory.title')"
    data-testid="prompt-history-popover"
    @mousedown.stop
    @keydown="onKeydown"
  >
    <div class="riph-search">
      <svg
        class="riph-search-icon"
        width="14"
        height="14"
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
          r="7"
        />
        <line
          x1="21"
          y1="21"
          x2="16.65"
          y2="16.65"
        />
      </svg>
      <input
        v-model="query"
        type="text"
        class="riph-search-input"
        :placeholder="t('promptHistory.searchPlaceholder')"
        data-testid="prompt-history-search"
      />
    </div>
    <div
      v-if="isEmpty"
      class="riph-empty"
      data-testid="prompt-history-empty"
    >
      {{ t("promptHistory.empty") }}
    </div>

    <div
      v-else-if="!hasResults"
      class="riph-empty"
    >
      {{ t("promptHistory.noResults") }}
    </div>

    <template v-else>
      <!-- ── Favorites ──────────────────────────────────────────────── -->
      <div class="riph-group">
        <div class="riph-group-head">
          <span class="riph-group-title">
            <span aria-hidden="true">⭐</span> {{ t("promptHistory.favorites") }}
          </span>
        </div>
        <div
          v-if="visibleFavorites.length === 0"
          class="riph-group-empty"
        >
          {{ t("promptHistory.favEmpty") }}
        </div>
        <ul
          v-else
          class="riph-list"
        >
          <li
            v-for="e in visibleFavorites"
            :key="e.id"
            class="riph-row"
            data-testid="prompt-history-fav-row"
          >
            <button
              type="button"
              class="riph-row-text"
              :title="e.text"
              @click="pick(e.text)"
            >
              {{ e.text }}
            </button>
            <button
              type="button"
              class="riph-row-star is-fav"
              :title="t('promptHistory.unfav')"
              :aria-label="t('promptHistory.unfav')"
              @click="removeFavorite(e.id)"
            >
              <svg
                width="15"
                height="15"
                viewBox="0 0 24 24"
                fill="currentColor"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
              </svg>
            </button>
          </li>
        </ul>
        <button
          v-if="hasMoreFavorites"
          type="button"
          class="riph-more"
          @click="showAllFavorites = !showAllFavorites"
        >
          {{
            showAllFavorites
              ? t("promptHistory.showFewerFavorites")
              : t("promptHistory.showAllFavorites")
          }}
        </button>
      </div>

      <!-- ── Recent ─────────────────────────────────────────────────── -->
      <div
        v-if="visibleRecent.length > 0"
        class="riph-group"
      >
        <div class="riph-group-head">
          <span class="riph-group-title">
            <span aria-hidden="true">🕐</span> {{ t("promptHistory.recent") }}
          </span>
          <button
            type="button"
            class="riph-clear"
            :title="t('promptHistory.clear')"
            data-testid="prompt-history-clear"
            @click="onClear"
          >
            {{ t("promptHistory.clear") }}
          </button>
        </div>
        <ul class="riph-list">
          <li
            v-for="e in visibleRecent"
            :key="e.id"
            class="riph-row"
            data-testid="prompt-history-recent-row"
          >
            <button
              type="button"
              class="riph-row-text"
              :title="e.text"
              @click="pick(e.text)"
            >
              {{ e.text }}
            </button>
            <button
              type="button"
              class="riph-row-star"
              :class="{ 'is-fav': isFavorite(e.text) }"
              :title="
                isFavorite(e.text)
                  ? t('promptHistory.unfav')
                  : t('promptHistory.fav')
              "
              :aria-label="
                isFavorite(e.text)
                  ? t('promptHistory.unfav')
                  : t('promptHistory.fav')
              "
              @click="onToggleFav(e.text)"
            >
              <svg
                width="15"
                height="15"
                viewBox="0 0 24 24"
                :fill="isFavorite(e.text) ? 'currentColor' : 'none'"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
              </svg>
            </button>
            <button
              type="button"
              class="riph-row-del"
              :title="t('promptHistory.removeRecent')"
              :aria-label="t('promptHistory.removeRecent')"
              @click="removeRecent(e.id)"
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <line
                  x1="18"
                  y1="6"
                  x2="6"
                  y2="18"
                />
                <line
                  x1="6"
                  y1="6"
                  x2="18"
                  y2="18"
                />
              </svg>
            </button>
          </li>
        </ul>
      </div>
    </template>
  </div>
</template>

<!-- Styles intentionally not scoped: the `.rit-prompt-history-menu` / `.riph-*`
     selectors live in `frontend/src/styles/chat/chat.css` alongside the other
     composer popovers (`.rit-voice-engine-menu*`). Reusing the global stylesheet
     + design tokens keeps the popover visually consistent with its neighbours
     and light/dark safe (no per-component magic colors). -->
