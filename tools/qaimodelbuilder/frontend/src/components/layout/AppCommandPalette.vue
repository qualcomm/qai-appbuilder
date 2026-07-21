<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AppCommandPalette — the global, VS Code-style command launcher overlay
 * mounted by `App.vue` (opened via Ctrl/Cmd+. — see App.vue `useKeymap`).
 *
 * V1 parity source of truth: `frontend/js/components/CommandPalette.js`
 * (+ `css/utilities.css` `.cmd-palette*`, migrated to
 * `styles/common/utilities.css`). The palette groups its results into three
 * sections — Actions / Skills / Models — each with a leading group icon +
 * uppercase label, and every item renders an icon, a label and an optional
 * keyboard shortcut. Keyboard navigation (↑/↓/Enter/Esc) walks the flattened
 * group → item order and scrolls the active item into view, exactly like V1's
 * moveDown / moveUp / scrollActiveIntoView.
 *
 * Implementation: behaviour is sourced from V1, but the structure is a typed
 * Vue 3 SFC that derives the three command groups from `useAppCommands`
 * (which in turn reads the real V2 stores/composables — theme, font-size,
 * skills registry, model catalog) instead of V1's global-ref injection. The
 * palette only has to:
 *   1. register the derived commands into the shared `useCommandPalette`
 *      command list (so the existing fuzzy filter / store query apply), and
 *   2. group the filtered flat list by `category` for rendering.
 *
 * Visuals are owned by the global `.cmd-palette*` rules so the card stays 1:1
 * with V1 — no scoped CSS / `--qai-*` tokens.
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  registerPaletteCommand,
  useCommandPalette,
  type PaletteCommand,
} from "@/composables/useCommandPalette";
import { useAppCommands } from "@/composables/useAppCommands";
import { useCommandPaletteStore } from "@/stores/commandPalette";

const { t } = useI18n();
const store = useCommandPaletteStore();
const palette = useCommandPalette({ includeNavCommands: false });

// ── Command sources (Actions / Skills / Models) ─────────────────────────────
// `useAppCommands` derives the three category lists; we register each command
// into the shared palette command list so the store query / fuzzy filter
// already wired into `useCommandPalette` applies uniformly. Registrations are
// reactive to the underlying stores (skills/models load asynchronously) and
// are torn down on unmount.
const { commands: appCommands, ensureLoaded } = useAppCommands();

const disposers = ref<Array<() => void>>([]);
watch(
  appCommands,
  (cmds) => {
    for (const dispose of disposers.value) dispose();
    disposers.value = cmds.map((c) => registerPaletteCommand(c));
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  for (const dispose of disposers.value) dispose();
  disposers.value = [];
});

const inputRef = ref<HTMLInputElement | null>(null);
/** Flat index across all group items (V1 activeGroupIdx + activeItemIdx
 *  collapse into a single running index here). */
const activeIndex = ref(0);

// ── Grouping (V1 groupedResults, CommandPalette.js:142-154) ─────────────────
// Order Actions → Skills → Models for the known categories; any other
// category is appended afterwards in first-seen order. Each group carries its
// localized label + leading icon.
interface PaletteGroup {
  key: string;
  label: string;
  icon: string;
  items: readonly PaletteCommand[];
}

const GROUP_META: Record<string, { labelKey: string; icon: string }> = {
  actions: { labelKey: "commandPalette.group.actions", icon: "\u2699" }, // ⚙
  skills: { labelKey: "commandPalette.group.skills", icon: "\u26A1" }, // ⚡
  models: { labelKey: "commandPalette.group.models", icon: "\u{1F916}" }, // 🤖
};
const GROUP_ORDER = ["actions", "skills", "models"];

const groups = computed<PaletteGroup[]>(() => {
  const byCategory = new Map<string, PaletteCommand[]>();
  for (const cmd of palette.filtered.value) {
    const key =
      (cmd.category ?? "").trim() === "" ? "actions" : (cmd.category as string);
    const existing = byCategory.get(key);
    if (existing === undefined) byCategory.set(key, [cmd]);
    else existing.push(cmd);
  }

  const ordered: string[] = [];
  for (const k of GROUP_ORDER) {
    if (byCategory.has(k)) ordered.push(k);
  }
  for (const k of byCategory.keys()) {
    if (!ordered.includes(k)) ordered.push(k);
  }

  return ordered.map((key) => {
    const meta = GROUP_META[key];
    return {
      key,
      label: meta ? t(meta.labelKey) : key,
      icon: meta ? meta.icon : "\u2699",
      items: byCategory.get(key) ?? [],
    };
  });
});

/** Flattened item order across groups — drives keyboard navigation and maps a
 *  flat active index back to an item for the `active` class. */
const flatItems = computed<PaletteCommand[]>(() =>
  groups.value.flatMap((g) => g.items),
);

function isActive(cmd: PaletteCommand): boolean {
  return flatItems.value[activeIndex.value]?.id === cmd.id;
}

watch(
  () => store.open,
  async (open) => {
    if (open) {
      activeIndex.value = 0;
      // Lazily load skill/model lists so the Skills/Models groups populate.
      void ensureLoaded();
      await nextTick();
      inputRef.value?.focus();
    }
  },
);

// Keep the active index valid as the filtered result set changes.
watch(
  () => flatItems.value.length,
  (len) => {
    if (activeIndex.value >= len) activeIndex.value = Math.max(0, len - 1);
  },
);

function onInput(event: Event): void {
  store.setQuery((event.target as HTMLInputElement).value);
  activeIndex.value = 0;
}

function runCommand(cmd: PaletteCommand): void {
  store.hide();
  void cmd.run();
}

function setActive(cmd: PaletteCommand): void {
  const idx = flatItems.value.findIndex((c) => c.id === cmd.id);
  if (idx >= 0) activeIndex.value = idx;
}

function scrollActiveIntoView(): void {
  void nextTick(() => {
    const el = document.querySelector(".cmd-palette-item.active");
    if (el) el.scrollIntoView({ block: "nearest" });
  });
}

function onKeydown(event: KeyboardEvent): void {
  const len = flatItems.value.length;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    if (len > 0) activeIndex.value = (activeIndex.value + 1) % len;
    scrollActiveIntoView();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    if (len > 0) activeIndex.value = (activeIndex.value - 1 + len) % len;
    scrollActiveIntoView();
  } else if (event.key === "Enter") {
    event.preventDefault();
    const cmd = flatItems.value[activeIndex.value];
    if (cmd) runCommand(cmd);
  } else if (event.key === "Escape") {
    store.hide();
  }
}

function onBackdropClick(): void {
  store.hide();
}
</script>

<template>
  <div
    v-if="store.open"
    class="cmd-palette-overlay"
    role="dialog"
    aria-modal="true"
    :aria-label="t('layout.command_palette_title')"
    @click.self="onBackdropClick"
  >
    <div
      class="cmd-palette"
      @keydown="onKeydown"
    >
      <div class="cmd-palette-input-wrap">
        <svg
          class="cmd-palette-icon"
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
          ref="inputRef"
          type="text"
          class="cmd-palette-input"
          :placeholder="t('commandPalette.placeholder')"
          :aria-label="t('layout.command_palette_title')"
          :value="store.query"
          @input="onInput"
        />
        <kbd class="cmd-palette-kbd">Esc</kbd>
      </div>

      <div
        v-if="flatItems.length > 0"
        class="cmd-palette-results"
        role="listbox"
      >
        <div
          v-for="group in groups"
          :key="group.key"
          class="cmd-palette-group"
        >
          <div class="cmd-palette-group-label">
            {{ group.icon }} {{ group.label }}
          </div>
          <div
            v-for="cmd in group.items"
            :key="cmd.id"
            class="cmd-palette-item"
            :class="{ active: isActive(cmd) }"
            role="option"
            :aria-selected="isActive(cmd)"
            @click="runCommand(cmd)"
            @mouseenter="setActive(cmd)"
          >
            <span
              v-if="cmd.icon"
              class="cmd-palette-item-icon"
            >{{ cmd.icon }}</span>
            <span class="cmd-palette-item-label">{{ cmd.label }}</span>
            <kbd
              v-if="cmd.shortcut"
              class="cmd-palette-item-kbd"
            >{{ cmd.shortcut }}</kbd>
          </div>
        </div>
      </div>
      <div
        v-else
        class="cmd-palette-empty"
      >
        {{ t("commandPalette.noResults") }}
      </div>
    </div>
  </div>
</template>

<!-- No scoped CSS: visuals are owned by the global styles/common/utilities.css
     `.cmd-palette*` rules so they stay 1:1 with V1. -->
