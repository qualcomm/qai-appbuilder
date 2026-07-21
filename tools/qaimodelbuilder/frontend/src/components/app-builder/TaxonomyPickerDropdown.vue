<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * TaxonomyPickerDropdown — App Builder setup bar taxonomy selector.
 *
 * Replicates V1 TaxonomyPicker behavior:
 *   - Button trigger: [group icon] GroupLabel / TaskLabel ▾
 *   - Teleport-to-body popover (660×380 dual-column grid)
 *   - Search input (cross-group task filter)
 *   - Left panel: group list with icons + model counts
 *   - Right panel: task list with counts + keyboard highlight
 *   - Keyboard: ↑↓ task, ←→ group, Enter select, Esc close, `/` open
 *   - "View all N tasks →" footer link
 *   - Click outside closes popover
 *
 * Uses global `.ab-taxonomy-*` CSS classes from app-builder.css (no scoped styles needed).
 */

import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";

// ─── Types ────────────────────────────────────────────────────────────────────

export interface TaxTask {
  id: string;
  label: string;
  description?: string;
}

export interface TaxGroup {
  id: string;
  label: string;
  icon?: string;
  tasks: TaxTask[];
}

export interface TaxonomyData {
  groups: TaxGroup[];
}

export interface SelectionPayload {
  groupId: string;
  taskId: string;
}

interface Props {
  taxonomy: TaxonomyData;
  selectedGroupId?: string | null;
  selectedTaskId?: string | null;
  /** taskId → model count */
  modelCounts?: Record<string, number>;
}

// ─── Props & Emits ────────────────────────────────────────────────────────────

const props = withDefaults(defineProps<Props>(), {
  selectedGroupId: null,
  selectedTaskId: null,
  modelCounts: () => ({}),
});

const emit = defineEmits<{
  "update:selection": [payload: SelectionPayload];
}>();

const { t } = useI18n();

// ─── Refs ─────────────────────────────────────────────────────────────────────

const open = ref(false);
const rootEl = ref<HTMLElement | null>(null);
const triggerEl = ref<HTMLButtonElement | null>(null);
const popoverEl = ref<HTMLElement | null>(null);
const query = ref("");
const popoverStyle = ref<{ top: string; left: string }>({ top: "0px", left: "0px" });
const browseGroupId = ref<string | null>(null);
const highlightedTaskIndex = ref(0);
const showAll = ref(false);

// ─── Computed ─────────────────────────────────────────────────────────────────

const groups = computed<TaxGroup[]>(() => {
  return props.taxonomy?.groups ?? [];
});

function countOfTask(taskId: string): number {
  return props.modelCounts?.[taskId] ?? 0;
}

function countOfGroup(g: TaxGroup): number {
  return (g.tasks ?? []).reduce((sum, t) => sum + countOfTask(t.id), 0);
}

const selectedGroup = computed<TaxGroup | null>(() => {
  if (!props.selectedGroupId) return null;
  return groups.value.find((g) => g.id === props.selectedGroupId) ?? null;
});

const selectedTask = computed<TaxTask | null>(() => {
  if (!props.selectedTaskId) return null;
  // Try within selected group first
  if (selectedGroup.value) {
    const found = selectedGroup.value.tasks.find((t) => t.id === props.selectedTaskId);
    if (found) return found;
  }
  // Fallback: search all groups
  for (const g of groups.value) {
    const found = g.tasks.find((t) => t.id === props.selectedTaskId);
    if (found) return found;
  }
  return null;
});

const activeBrowseGroup = computed<TaxGroup | null>(() => {
  const id = browseGroupId.value ?? props.selectedGroupId ?? groups.value[0]?.id;
  return groups.value.find((g) => g.id === id) ?? groups.value[0] ?? null;
});

interface FilteredItem {
  group: TaxGroup;
  task: TaxTask;
}

const searchResults = computed<FilteredItem[]>(() => {
  const q = query.value.trim().toLowerCase();
  if (!q) return [];
  const out: FilteredItem[] = [];
  for (const g of groups.value) {
    for (const task of g.tasks) {
      const hay = `${task.label} ${task.id} ${task.description ?? ""}`.toLowerCase();
      if (hay.includes(q)) out.push({ group: g, task });
    }
  }
  return out;
});

const filteredTasks = computed<FilteredItem[]>(() => {
  if (query.value.trim()) return searchResults.value;
  if (showAll.value) {
    const out: FilteredItem[] = [];
    for (const g of groups.value) {
      for (const task of g.tasks) out.push({ group: g, task });
    }
    return out;
  }
  const g = activeBrowseGroup.value;
  if (!g) return [];
  return g.tasks.map((task) => ({ group: g, task }));
});

const totalTaskCount = computed<number>(() => {
  return groups.value.reduce((n, g) => n + g.tasks.length, 0);
});

const activeBrowseGroupPacks = computed<number>(() => {
  return activeBrowseGroup.value ? countOfGroup(activeBrowseGroup.value) : 0;
});

// ─── Icon SVG helpers ─────────────────────────────────────────────────────────

function iconSvg(name: string, size = 12): string {
  const s = String(size);
  const stroke = "currentColor";
  const common = `width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="${stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"`;
  switch (name) {
    case "audio":
      return `<svg ${common}><line x1="6" y1="9" x2="6" y2="15"/><line x1="10" y1="6" x2="10" y2="18"/><line x1="14" y1="3" x2="14" y2="21"/><line x1="18" y1="8" x2="18" y2="16"/></svg>`;
    case "vision":
      return `<svg ${common}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/></svg>`;
    case "spark":
      return `<svg ${common}><path d="M12 3l1.6 5L19 9.6 14 11.2 12 16.5 10 11.2 5 9.6 10.4 8z"/></svg>`;
    case "stack":
      return `<svg ${common}><polygon points="12 3 21 8 12 13 3 8"/><polyline points="3 13 12 18 21 13"/><polyline points="3 18 12 23 21 18"/></svg>`;
    case "caret":
      return `<svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="${stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="2 4 6 8 10 4"/></svg>`;
    case "search":
      return `<svg ${common}><circle cx="10.5" cy="10.5" r="6.5"/><line x1="20" y1="20" x2="15.5" y2="15.5"/></svg>`;
    default:
      return `<svg ${common}><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>`;
  }
}

const buttonGroupIcon = computed(() => {
  return iconSvg(selectedGroup.value?.icon ?? "dot", 11);
});

const caretIcon = iconSvg("caret");
const searchIconSvg = iconSvg("search", 14);

// ─── Popover positioning ──────────────────────────────────────────────────────

function updatePopoverPosition(): void {
  const btn = triggerEl.value;
  if (!btn) return;
  const rect = btn.getBoundingClientRect();
  const POPOVER_W = 660;
  const POPOVER_H = 380;
  const GUTTER = 12;
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  let left = rect.left;
  if (left + POPOVER_W + GUTTER > vw) {
    left = Math.max(GUTTER, vw - POPOVER_W - GUTTER);
  }
  let top = rect.bottom + 6;
  if (top + POPOVER_H + GUTTER > vh) {
    top = Math.max(GUTTER, rect.top - POPOVER_H - 6);
  }
  popoverStyle.value = { top: `${top}px`, left: `${left}px` };
}

// ─── Open / Close ─────────────────────────────────────────────────────────────

function openPopover(): void {
  open.value = true;
  browseGroupId.value = props.selectedGroupId ?? groups.value[0]?.id ?? null;
  nextTick(() => {
    updatePopoverPosition();
    // Initialize highlight to current selection
    const list = filteredTasks.value;
    let idx = 0;
    if (props.selectedTaskId) {
      const found = list.findIndex((x) => x.task.id === props.selectedTaskId);
      if (found >= 0) idx = found;
    }
    highlightedTaskIndex.value = idx;
    // Focus popover for keyboard navigation
    popoverEl.value?.focus({ preventScroll: true });
  });
}

function closePopover(): void {
  open.value = false;
  query.value = "";
  highlightedTaskIndex.value = 0;
  showAll.value = false;
}

function toggle(): void {
  if (open.value) closePopover();
  else openPopover();
}

// ─── Selection handlers ───────────────────────────────────────────────────────

function onClickGroup(g: TaxGroup): void {
  browseGroupId.value = g.id;
  highlightedTaskIndex.value = 0;
  showAll.value = false;
}

function onSelectTask(g: TaxGroup, task: TaxTask): void {
  emit("update:selection", { groupId: g.id, taskId: task.id });
  closePopover();
}

function onShowAll(): void {
  query.value = "";
  showAll.value = true;
  highlightedTaskIndex.value = 0;
  nextTick(() => updatePopoverPosition());
}

// ─── Keyboard navigation ──────────────────────────────────────────────────────

function moveHighlight(delta: number): void {
  const n = filteredTasks.value.length;
  if (n === 0) return;
  let i = highlightedTaskIndex.value + delta;
  if (i < 0) i = n - 1;
  if (i >= n) i = 0;
  highlightedTaskIndex.value = i;
}

function moveGroup(delta: number): void {
  const list = groups.value;
  if (list.length === 0) return;
  const curId = activeBrowseGroup.value?.id ?? list[0]!.id;
  let idx = list.findIndex((g) => g.id === curId);
  if (idx < 0) idx = 0;
  idx = (idx + delta + list.length) % list.length;
  browseGroupId.value = list[idx]!.id;
  highlightedTaskIndex.value = 0;
}

function commitHighlighted(): void {
  const item = filteredTasks.value[highlightedTaskIndex.value];
  if (!item) return;
  onSelectTask(item.group, item.task);
}

function onPopoverKeydown(e: KeyboardEvent): void {
  if (!open.value) return;
  const inInput = (e.target as HTMLElement)?.tagName === "INPUT";
  switch (e.key) {
    case "ArrowDown":
      e.preventDefault();
      moveHighlight(1);
      break;
    case "ArrowUp":
      e.preventDefault();
      moveHighlight(-1);
      break;
    case "ArrowLeft":
      if (inInput && query.value) return;
      e.preventDefault();
      moveGroup(-1);
      break;
    case "ArrowRight":
      if (inInput && query.value) return;
      e.preventDefault();
      moveGroup(1);
      break;
    case "Enter":
      e.preventDefault();
      commitHighlighted();
      break;
  }
}

// ─── Global event handlers ────────────────────────────────────────────────────

function onDocClick(e: MouseEvent): void {
  if (!open.value) return;
  const target = e.target as HTMLElement;
  if (rootEl.value?.contains(target)) return;
  if (target?.closest?.(".ab-taxonomy-popover--floating")) return;
  closePopover();
}

function onGlobalKeydown(e: KeyboardEvent): void {
  if (e.key === "Escape" && open.value) {
    e.preventDefault();
    closePopover();
    return;
  }
  // `/` shortcut: open popover when not in an input
  if (e.key === "/" && !open.value) {
    const tag = (document.activeElement?.tagName ?? "").toUpperCase();
    const editable = (document.activeElement as HTMLElement)?.isContentEditable;
    if (tag !== "INPUT" && tag !== "TEXTAREA" && !editable) {
      e.preventDefault();
      openPopover();
    }
  }
}

function onWinChange(): void {
  if (open.value) updatePopoverPosition();
}

onMounted(() => {
  document.addEventListener("mousedown", onDocClick, true);
  document.addEventListener("keydown", onGlobalKeydown);
  window.addEventListener("resize", onWinChange);
  window.addEventListener("scroll", onWinChange, true);
});

onBeforeUnmount(() => {
  document.removeEventListener("mousedown", onDocClick, true);
  document.removeEventListener("keydown", onGlobalKeydown);
  window.removeEventListener("resize", onWinChange);
  window.removeEventListener("scroll", onWinChange, true);
});

// ─── Watchers ─────────────────────────────────────────────────────────────────

watch(
  () => props.selectedGroupId,
  (v) => {
    if (v) browseGroupId.value = v;
  },
);

watch(filteredTasks, (list) => {
  if (highlightedTaskIndex.value >= list.length) {
    highlightedTaskIndex.value = 0;
  }
});
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- v-html only renders the static, hard-coded inline SVG icon strings from iconSvg()/buttonGroupIcon (no user input); not an XSS vector. -->
  <div
    ref="rootEl"
    class="ab-taxonomy-picker"
    :class="{ 'is-open': open }"
  >
    <!-- Trigger button -->
    <button
      ref="triggerEl"
      type="button"
      class="ab-taxonomy-btn"
      :class="{ 'is-empty': !selectedTask, active: open }"
      :aria-haspopup="'dialog'"
      :aria-expanded="open ? 'true' : 'false'"
      @click="toggle"
    >
      <span
        class="ab-taxonomy-btn-lead"
        aria-hidden="true"
        v-html="buttonGroupIcon"
      />
      <template v-if="selectedTask">
        <span class="ab-taxonomy-btn-group">{{ selectedGroup?.label ?? "" }}</span>
        <span class="ab-taxonomy-btn-sep">/</span>
        <span class="ab-taxonomy-btn-task">{{ selectedTask.label }}</span>
      </template>
      <template v-else-if="selectedGroup">
        <span class="ab-taxonomy-btn-group">{{ selectedGroup.label }}</span>
        <span class="ab-taxonomy-btn-sep">/</span>
        <span class="ab-taxonomy-btn-task is-placeholder">{{
          t("appBuilder.taxonomy.choose", "Choose a task")
        }}</span>
      </template>
      <template v-else>
        <span class="ab-taxonomy-btn-task is-placeholder">{{
          t("appBuilder.taxonomy.choose", "Choose a task")
        }}</span>
      </template>
      <span
        class="ab-taxonomy-btn-caret"
        aria-hidden="true"
        v-html="caretIcon"
      />
    </button>

    <!-- Popover (teleported to body) -->
    <Teleport to="body">
      <div
        v-if="open"
        ref="popoverEl"
        class="ab-taxonomy-popover ab-taxonomy-popover--floating"
        :style="popoverStyle"
        tabindex="-1"
        role="dialog"
        :aria-label="t('appBuilder.aria.taxonomy')"
        @keydown="onPopoverKeydown"
        @mousedown.stop
      >
        <!-- Search row -->
        <div class="ab-taxonomy-search">
          <span
            class="ab-taxonomy-search-icon"
            aria-hidden="true"
            v-html="searchIconSvg"
          />
          <input
            v-model="query"
            type="text"
            tabindex="-1"
            :placeholder="
              t(
                'appBuilder.taxonomy.searchPlaceholder',
                'Search tasks or models, e.g. whisper / OCR / depth',
              )
            "
          />
          <kbd>/</kbd>
        </div>

        <!-- Left: group list -->
        <div
          class="ab-taxonomy-group-list"
          role="tablist"
        >
          <div
            v-for="g in groups"
            :key="g.id"
            class="ab-taxonomy-group-row"
            :class="{ active: activeBrowseGroup?.id === g.id }"
            role="tab"
            :aria-selected="activeBrowseGroup?.id === g.id ? 'true' : 'false'"
            @click="onClickGroup(g)"
          >
            <span
              class="ab-taxonomy-group-icon"
              aria-hidden="true"
              v-html="iconSvg(g.icon ?? 'dot', 12)"
            />
            <span class="ab-taxonomy-group-label">{{
              t("appBuilder.taxonomy.group." + g.id, g.label)
            }}</span>
            <span class="ab-taxonomy-group-count">{{ countOfGroup(g) }}</span>
          </div>
          <div class="ab-taxonomy-group-foot">
            {{ groups.length }} GROUPS · {{ totalTaskCount }} TASKS
          </div>
        </div>

        <!-- Right: task list -->
        <div
          class="ab-taxonomy-task-list"
          role="tabpanel"
        >
          <div
            v-if="query.trim()"
            class="ab-taxonomy-task-head"
          >
            <span>SEARCH · {{ filteredTasks.length }} HITS</span>
          </div>
          <div
            v-else-if="showAll"
            class="ab-taxonomy-task-head"
          >
            <span>ALL · {{ filteredTasks.length }} TASKS</span>
          </div>
          <div
            v-else-if="activeBrowseGroup"
            class="ab-taxonomy-task-head"
          >
            <span>{{
              (
                t("appBuilder.taxonomy.group." + activeBrowseGroup.id, activeBrowseGroup.label) ??
                ""
              ).toUpperCase()
            }}
              · {{ activeBrowseGroup.tasks.length }} TASKS</span>
            <span class="ab-taxonomy-task-head-count">{{ activeBrowseGroupPacks }} packs</span>
          </div>

          <div
            v-for="(item, idx) in filteredTasks"
            :key="item.group.id + ':' + item.task.id"
            class="ab-taxonomy-task-row"
            :class="{
              active: selectedTask?.id === item.task.id,
              'is-empty': countOfTask(item.task.id) === 0,
              'is-highlighted': idx === highlightedTaskIndex,
            }"
            role="option"
            :aria-selected="selectedTask?.id === item.task.id ? 'true' : 'false'"
            :title="item.task.description ?? ''"
            @click="onSelectTask(item.group, item.task)"
            @mouseenter="highlightedTaskIndex = idx"
          >
            <span class="ab-taxonomy-task-name">
              <span
                v-if="query.trim() || showAll"
                class="ab-taxonomy-task-group-tag"
              >{{
                t("appBuilder.taxonomy.group." + item.group.id, item.group.label)
              }}</span>
              <b>{{ t("appBuilder.taxonomy.task." + item.task.id, item.task.label) }}</b>
              <span
                v-if="item.task.id === 'speech-recognition'"
                class="ab-taxonomy-task-suffix"
              >— ASR</span>
              <span
                v-else-if="item.task.id === 'audio-generation'"
                class="ab-taxonomy-task-suffix"
              >— TTS</span>
            </span>
            <span
              class="ab-taxonomy-task-count"
              :class="{ has: countOfTask(item.task.id) > 0 }"
            >
              {{ countOfTask(item.task.id) }}
            </span>
          </div>

          <div
            v-if="filteredTasks.length === 0"
            class="ab-taxonomy-task-empty"
          >
            {{ t("appBuilder.taxonomy.empty", "No models in this task") }}
          </div>
        </div>

        <!-- Footer -->
        <div class="ab-taxonomy-foot">
          <span class="ab-taxonomy-foot-hint">
            <kbd>↑</kbd><kbd>↓</kbd> {{ t("appBuilder.taxonomy.kbd.task", "Task") }} ·
            <kbd>←</kbd><kbd>→</kbd> {{ t("appBuilder.taxonomy.kbd.group", "Group") }} ·
            <kbd>Enter</kbd> {{ t("appBuilder.taxonomy.kbd.select", "Select") }}
          </span>
          <a
            v-if="!showAll"
            href="#"
            @click.prevent="onShowAll"
          >
            {{ t("appBuilder.taxonomy.viewAll", { count: totalTaskCount }) }} →
          </a>
        </div>
      </div>
    </Teleport>
  </div>
  <!-- eslint-enable vue/no-v-html -->
</template>
