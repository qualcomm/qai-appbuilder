<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * UiTabs — generic tabs utility component.
 *
 * Renders a horizontal tab strip with configurable labels,
 * sizes, and variants. Supports v-model for active tab.
 *
 * V1 parity for the `--underline` variant: an absolutely-positioned
 * `.ui-tabs__indicator` slides under the active tab (CSS transition on
 * `left` / `width`). The active tab itself has *no* `border-bottom` —
 * the indicator alone draws the underline. Default / pills variants
 * keep their per-tab borders unchanged.
 */
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
  type ComponentPublicInstance,
} from "vue";

interface TabItem {
  id: string;
  label: string;
  disabled?: boolean;
  /**
   * Optional count/status badge rendered after the label (e.g. Security's
   * pending-patch count, Downloads' model count). Falsy values
   * (`undefined` / `null` / `0` / `""`) render no badge. A numeric `0` is
   * intentionally hidden so callers can pass a raw count without guarding.
   */
  badge?: string | number;
  /**
   * Optional unsaved-changes dot indicator (V1 security-tab-dot parity).
   * When true, renders a small orange dot after the label to signal
   * pending unsaved changes (e.g. Allow Lists tab).
   */
  dot?: boolean;
}

interface Props {
  tabs: TabItem[];
  modelValue: string;
  size?: "sm" | "md" | "lg";
  variant?: "default" | "pills" | "underline" | "segmented";
  label?: string;
}

const props = withDefaults(defineProps<Props>(), {
  size: "md",
  variant: "default",
  label: undefined,
});

const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();

const activeId = computed(() => props.modelValue);

function select(id: string): void {
  emit("update:modelValue", id);
}

// ── Sliding indicator (underline variant) — V1 `UiTabs.js:40-58` parity ──
const tabsRef = ref<HTMLElement | null>(null);
const tabEls = new Map<string, HTMLElement>();
const indicatorStyle = ref<{ left: string; width: string; opacity: number }>({
  left: "0px",
  width: "0px",
  opacity: 0,
});

function setTabRef(
  el: Element | ComponentPublicInstance | null,
  id: string,
): void {
  if (el instanceof HTMLElement) {
    tabEls.set(id, el);
  } else {
    tabEls.delete(id);
  }
}

function updateIndicator(): void {
  if (props.variant !== "underline") return;
  void nextTick(() => {
    const container = tabsRef.value;
    const active = tabEls.get(activeId.value);
    if (!container || !active) {
      indicatorStyle.value = { ...indicatorStyle.value, opacity: 0 };
      return;
    }
    const cRect = container.getBoundingClientRect();
    const tRect = active.getBoundingClientRect();
    indicatorStyle.value = {
      left: `${tRect.left - cRect.left + container.scrollLeft}px`,
      width: `${tRect.width}px`,
      opacity: 1,
    };
  });
}

let ro: ResizeObserver | null = null;
onMounted(() => {
  // Slight delay so first-paint measurements land after the layout has
  // settled (V1 `UiTabs.js:98` uses the same 50 ms guard).
  setTimeout(updateIndicator, 50);
  if (typeof ResizeObserver !== "undefined" && tabsRef.value) {
    ro = new ResizeObserver(updateIndicator);
    ro.observe(tabsRef.value);
  }
});
onBeforeUnmount(() => {
  ro?.disconnect();
  ro = null;
});
watch(() => props.modelValue, updateIndicator);
watch(() => props.tabs, updateIndicator, { deep: true });
</script>

<template>
  <div
    ref="tabsRef"
    class="ui-tabs"
    :class="[`ui-tabs--${size}`, `ui-tabs--${variant}`]"
    role="tablist"
    :aria-label="label"
  >
    <button
      v-for="tab in tabs"
      :key="tab.id"
      :ref="(el) => setTabRef(el, tab.id)"
      type="button"
      role="tab"
      class="ui-tabs__tab"
      :class="{ 'ui-tabs__tab--active': tab.id === activeId }"
      :aria-selected="tab.id === activeId"
      :disabled="tab.disabled"
      @click="select(tab.id)"
    >
      {{ tab.label }}
      <span
        v-if="tab.badge !== undefined && tab.badge !== null && tab.badge !== 0 && tab.badge !== ''"
        class="ui-tabs__badge"
        aria-hidden="true"
      >{{ tab.badge }}</span>
      <span
        v-if="tab.dot"
        class="ui-tabs__dot"
        aria-hidden="true"
      />
    </button>
    <!-- V1 .ui-tab-indicator (utilities.css:162-171): absolute 2px slider
         under the active tab; the active tab itself has no border-bottom
         in this variant so the indicator alone draws the underline. -->
    <div
      v-if="variant === 'underline'"
      class="ui-tabs__indicator"
      :style="indicatorStyle"
      aria-hidden="true"
    />
  </div>
</template>

<style scoped>
.ui-tabs {
  display: flex;
  gap: 2px;
  border-bottom: 1px solid var(--border);
}

.ui-tabs--pills {
  border-bottom: none;
  gap: var(--space-1);
}

.ui-tabs--underline {
  /* Anchor for the absolutely-positioned slider. */
  position: relative;
  gap: var(--space-2);
}

/* V1 parity (`.ui-tab` legacy): the underline variant matches V1's tab
   strip on Settings & Downloads — taller (~h39 from pad 8/16), flat
   (no top radius), and inactive/active both use font-weight 500 (the
   slider — see `.ui-tabs__indicator` below — replaces the per-tab
   border-bottom that other variants use). */
.ui-tabs--underline .ui-tabs__tab {
  padding: var(--space-2) var(--space-4);
  border-radius: 0;
  font-weight: 500;
}

.ui-tabs__tab {
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  padding: var(--space-1) var(--space-2);
  border-radius: 4px 4px 0 0;
  transition: color 0.15s, background 0.15s;
}

.ui-tabs--sm .ui-tabs__tab {
  font-size: var(--text-sm);
  padding: 4px 8px;
}

.ui-tabs--lg .ui-tabs__tab {
  font-size: var(--text-md);
  padding: var(--space-2) var(--space-3);
}

.ui-tabs__tab:hover:not(:disabled) {
  color: var(--text-primary);
}

.ui-tabs__tab--active {
  color: var(--accent);
  font-weight: 600;
  border-bottom: 2px solid var(--accent);
}

.ui-tabs--pills .ui-tabs__tab {
  border-radius: 6px;
}

.ui-tabs--pills .ui-tabs__tab--active {
  background: var(--accent);
  color: #fff;
  border-bottom: none;
}

/* Underline variant: indicator replaces the active tab's bottom border so
   only one underline shows (otherwise indicator + border would render two
   stacked 2px lines at the same position). V1 `.ui-tab.active` likewise
   has no border-bottom. */
.ui-tabs--underline .ui-tabs__tab--active {
  font-weight: 500;
  border-bottom: none;
}

.ui-tabs__indicator {
  position: absolute;
  bottom: 0;
  height: 2px;
  background: var(--accent);
  border-radius: 1px;
  pointer-events: none;
  /* V1 utilities.css uses `0.2s cubic-bezier(0.16, 1, 0.3, 1)` (a soft
     ease-out with a touch of overshoot); we mirror it 1:1 so the slide
     timing is indistinguishable from V1. */
  transition:
    left 0.2s cubic-bezier(0.16, 1, 0.3, 1),
    width 0.2s cubic-bezier(0.16, 1, 0.3, 1),
    opacity 0.15s;
}

.ui-tabs__tab:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Count/status badge after the label. Neutral accent-tinted pill that reads
   as a secondary count (Downloads model count, Security pending patches…).
   Reuses global tokens so it follows the active theme. */
.ui-tabs__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--accent-soft, var(--bg-tertiary));
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1;
  vertical-align: middle;
}

/* Unsaved-changes dot indicator (V1 security-tab-dot parity).
   Small orange circle shown on the Allow Lists tab when there are
   pending unsaved changes. */
.ui-tabs__dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-left: 5px;
  border-radius: 50%;
  background: var(--warning, var(--orange, #f59e0b));
  vertical-align: middle;
  flex-shrink: 0;
}

/* Segmented variant — V1 parity (SettingsPanel.js AI Coding sub-tabs):
   connected button group with shared border, no gap between buttons.
   Active button gets accent background; inactive buttons are transparent. */
.ui-tabs--segmented {
  border-bottom: none;
  gap: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  display: flex;
}

.ui-tabs--segmented .ui-tabs__tab {
  flex: 1;
  text-align: center;
  border-radius: 0;
  border-right: 1px solid var(--border);
  border-bottom: none;
  padding: 4px 12px;
  font-size: var(--text-sm);
}

.ui-tabs--segmented .ui-tabs__tab:last-child {
  border-right: none;
}

.ui-tabs--segmented .ui-tabs__tab--active {
  background: var(--accent-light);
  color: var(--accent);
  border-bottom: none;
  font-weight: 600;
}

.ui-tabs--segmented .ui-tabs__tab:hover:not(:disabled):not(.ui-tabs__tab--active) {
  background: var(--bg-hover, rgba(255, 255, 255, 0.06));
  color: var(--text-primary);
}
</style>
