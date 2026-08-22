<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChatOverflowPopover — "…" overflow-menu popover used by the top chat tab
 * strip (`ChatTabStrip`) AND the sub-agent rail (`SubAgentRail`) when the
 * open items don't fit horizontally. The sub-agent rail's 32px
 * `overflow: hidden` container is what originally forced this Teleport
 * pattern — an inline popover would be clipped out of view.
 *
 * WHY THIS COMPONENT EXISTS (Teleport pattern)
 * --------------------------------------------
 * The overflow popover is TELEPORTED to `<body>` (escaping every ancestor's
 * overflow / stacking context) and positioned via viewport coordinates
 * derived from `props.triggerEl.getBoundingClientRect()`. This avoids the
 * clipping problem where a `position: absolute` child would be hidden by
 * an ancestor's `overflow: hidden` — the DOM was mounted, click handlers
 * were wired, but the user saw "click has no effect".
 *
 * The trigger `⋯` button STAYS in the parent component (keeps existing
 * styling, tabindex order, and its aria-label i18n key); this component
 * owns ONLY the panel + its outside-click / Esc / resize / scroll wiring.
 *
 * PATTERN PARITY
 * --------------
 * The Teleport + `position: fixed` + `getBoundingClientRect()` recipe
 * follows the established pattern in `TaxonomyPickerDropdown.vue` (single
 * owner of both trigger and popover). The DIFFERENCE here is that the
 * trigger lives in a DIFFERENT component (parent) so we accept it as a
 * prop and coordinate outside-click detection to explicitly whitelist
 * BOTH the popover panel AND the passed-in trigger element. The consuming
 * parent no longer needs to manage document-level click listeners at all.
 */
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";

interface Props {
  /** Whether the popover is open. Controlled by the parent (`v-model:open`
   *  friendly via `emit("close")` — parent flips `open` to `false`). */
  open: boolean;
  /**
   * The `⋯` trigger button in the parent. Used both to (a) position the
   * popover relative to it (viewport coords) and (b) whitelist clicks on
   * the trigger so opening/closing via the trigger doesn't fight with our
   * outside-click dismissal.
   */
  triggerEl: HTMLElement | null;
  /**
   * Horizontal alignment: `"right"` (default) aligns the popover's RIGHT
   * edge to the trigger's right edge — matches the ChatTabStrip layout.
   * `"left"` aligns left edges.
   */
  align?: "left" | "right";
  /** Max panel height. Default `60vh`. */
  maxHeight?: string;
  /** Min panel width. Default 200px (ChatTabStrip's value). */
  minWidth?: string;
  /** Max panel width. Default 320px. */
  maxWidth?: string;
  /**
   * Additional class(es) applied to the popover panel `<div>`. Lets the
   * parent keep any strip-specific typography / hover / footer styling that
   * was previously nested under the inline `.__overflow-panel` selector.
   */
  panelClass?: string;
  /** ARIA label for the popover (role=menu). */
  ariaLabel?: string;
  /** `data-testid` on the popover panel. Consumers pass their own test
   *  hook (e.g. `chat-tab-overflow-panel`) so their spec assertions can
   *  target the teleported panel under `document.body`. */
  testid?: string;
}

const props = withDefaults(defineProps<Props>(), {
  align: "right",
  maxHeight: "60vh",
  minWidth: "200px",
  maxWidth: "360px",
  panelClass: "",
  ariaLabel: undefined,
  testid: undefined,
});

const emit = defineEmits<{
  /** Fired when the user requests the popover close (outside click / Esc /
   *  window resize-scroll — see NOTE below). Parent flips its `open` state. */
  (e: "close"): void;
}>();

// Panel DOM ref — used for outside-click whitelist and (in tests) as the
// anchor for popover-scoped queries.
const panelEl = ref<HTMLElement | null>(null);

// Viewport-coordinate position, applied inline via `:style="panelStyle"`. We
// use `fixed` positioning against the viewport since the panel is teleported
// to `<body>` and MUST NOT inherit any ancestor transform / offset.
const panelStyle = ref<Record<string, string>>({});

function updatePosition(): void {
  const trg = props.triggerEl;
  if (trg === null) {
    return;
  }
  const rect = trg.getBoundingClientRect();
  // 4px vertical gap between the trigger's bottom edge and the popover's
  // top edge — matches the legacy inline `top: calc(100% + 4px)`.
  const top = rect.bottom + 4;
  const style: Record<string, string> = {
    position: "fixed",
    top: `${top.toString()}px`,
    // z-index chosen to sit above the top tab strip (which is not a modal
    // layer). 1000 aligns with the existing `.qai-toast` / high-priority
    // overlay tier used elsewhere (see `styles/layout/layout.css:317`,
    // `styles/components/components.css:543`). It stays BELOW the modal
    // tier (2000+ / 9999) so open dialogs remain on top.
    "z-index": "1000",
    "max-height": props.maxHeight,
    "min-width": props.minWidth,
    "max-width": props.maxWidth,
  };
  if (props.align === "right") {
    // Anchor the popover's RIGHT edge to the trigger's right edge (matches
    // legacy `right: 0` behaviour inside a right-flushed overflow wrap).
    style.right = `${(window.innerWidth - rect.right).toString()}px`;
  } else {
    style.left = `${rect.left.toString()}px`;
  }
  panelStyle.value = style;
}

// Outside-click dismissal — capture-phase `mousedown` so we win the race
// against inner-item click handlers (parity with `TaxonomyPickerDropdown`,
// `SessionToolsPopover`). We WHITELIST both the popover panel itself AND
// the trigger element so clicking the trigger to toggle the popover doesn't
// self-dismiss BEFORE the trigger's own click handler runs.
function onDocMouseDown(ev: MouseEvent): void {
  if (!props.open) {
    return;
  }
  const target = ev.target as Node | null;
  if (target === null) {
    return;
  }
  const panel = panelEl.value;
  if (panel !== null && panel.contains(target)) {
    return;
  }
  const trg = props.triggerEl;
  if (trg !== null && trg.contains(target)) {
    return;
  }
  emit("close");
}

function onKeydown(ev: KeyboardEvent): void {
  if (ev.key === "Escape" && props.open) {
    ev.stopPropagation();
    emit("close");
  }
}

// Reposition on window resize + capture-phase scroll (a scroll ANYWHERE in
// an ancestor scroll container would otherwise leave the popover pinned to
// stale viewport coordinates while the trigger scrolled away).
function onWinChange(): void {
  if (props.open) {
    updatePosition();
  }
}

onMounted(() => {
  document.addEventListener("mousedown", onDocMouseDown, true);
  document.addEventListener("keydown", onKeydown, true);
  window.addEventListener("resize", onWinChange);
  window.addEventListener("scroll", onWinChange, true);
});

onBeforeUnmount(() => {
  document.removeEventListener("mousedown", onDocMouseDown, true);
  document.removeEventListener("keydown", onKeydown, true);
  window.removeEventListener("resize", onWinChange);
  window.removeEventListener("scroll", onWinChange, true);
});

// Recompute position each time the popover transitions to open (the trigger
// element's rect may have shifted while closed — new tabs opened, sidebar
// toggled, etc.). Also recompute on trigger reference change.
watch(
  () => [props.open, props.triggerEl] as const,
  ([nextOpen]) => {
    if (nextOpen) {
      void nextTick(updatePosition);
    }
  },
  { immediate: true },
);

// Merge the panel-specific class list (parent-provided) with our base class.
// A single computed keeps the template terse and stable.
const mergedClass = computed(() => {
  const base = "chat-overflow-popover";
  const extra = props.panelClass.trim();
  return extra === "" ? base : `${base} ${extra}`;
});
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      ref="panelEl"
      :class="mergedClass"
      :style="panelStyle"
      role="menu"
      :aria-label="ariaLabel"
      :data-testid="testid"
      @mousedown.stop
    >
      <slot />
    </div>
  </Teleport>
</template>

<style scoped>
/* Baseline visual (colours / border / shadow / rounded corners / vertical
 * scrolling for long lists). Positional properties (top/right/left/z-index/
 * width caps) come from the inline `:style="panelStyle"` computed above so
 * the popover tracks the trigger's viewport coordinates in real time.
 *
 * NB: `position: fixed` + `top`/`right` on the STYLE side is intentional so
 * no ancestor's `overflow: hidden` can clip us — that's the entire reason
 * this component exists.
 */
.chat-overflow-popover {
  overflow-y: auto;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-lg, var(--shadow-md));
  padding: 4px 0;
}
</style>
