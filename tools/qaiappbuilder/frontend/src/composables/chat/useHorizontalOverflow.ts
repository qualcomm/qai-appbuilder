// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useHorizontalOverflow — reusable "horizontal overflow strip" mechanics.
 *
 * Extracted from `ChatTabStrip.vue` so the fiddly overflow machinery
 * (measured overflow detection, edge-fade cues, wheel translation, rAF
 * throttling, resize observing, popover open/close state) lives in ONE
 * tested source, letting any future horizontal strip reuse it without
 * re-implementing (or degrading it to a count-threshold heuristic that
 * misfires when items happen to fit).
 *
 * Consumers: `ChatTabStrip` (top main-agent tab strip) + `SubAgentRail`
 * (second-level sub-agent chip strip under the active main tab). Both use
 * the same measured overflow probe so the "⋯" trigger + edge fades behave
 * consistently across the two-level tab bar.
 *
 * What it owns (item-agnostic mechanics only):
 *   - MEASURED overflow detection (`scrollWidth - clientWidth > 1`, with 1px
 *     sub-pixel slack) → `isOverflowing`. This is the single behavioural fix
 *     vs. a count threshold: a strip only shows the "⋯" trigger when the
 *     items GENUINELY don't fit the current width.
 *   - Directional edge-fade cues (`canScrollLeft` / `canScrollRight`).
 *   - Mouse-wheel vertical → horizontal scroll translation.
 *   - rAF-throttled re-measure on scroll.
 *   - ResizeObserver on the scroll container (width changes flip overflow
 *     without any item-list change).
 *   - Overflow popover open/close STATE ONLY (`overflowOpen` + `toggle`/`close`).
 *
 * What it deliberately does NOT own (stays in the consuming component):
 *   - Outside-click dismissal. Historically this composable also owned a
 *     document-capture click listener that closed the popover when the click
 *     fell outside a stable outer wrapper. That worked when the popover was
 *     `position: absolute` inside the wrapper, but broke as soon as the
 *     popover was Teleported to `<body>` (every popover click became an
 *     "outside" click and self-dismissed). The `ChatOverflowPopover`
 *     component now owns outside-click / Esc / resize-scroll wiring — it
 *     whitelists both the panel AND the trigger element passed in as a
 *     prop. Leaving those concerns to a SINGLE owner avoids the two-
 *     listener race that would otherwise close the popover before the
 *     trigger's toggle click fired.
 *
 *     As a consequence the outer-wrap ref (`overflowWrapEl`) that this
 *     composable used to expose has been retired: nothing in the composable
 *     read it after the migration, and leaving it in the return type kept
 *     both consumers binding a template `ref="overflowWrapEl"` for a value
 *     nobody used — dead API. Consumers now render their outer wrap as a
 *     plain `<div class="…">` (no ref).
 *   - "scroll a specific item into view" (depends on the item's id/DOM) —
 *     `ChatTabStrip.scrollTabIntoView` keeps that locally.
 *   - Re-measuring when the ITEM LIST changes: the consumer watches its own
 *     reactive list and calls `recompute()`; this composable can't know what
 *     "the list" is.
 *
 * Binding contract (consumer template):
 *   - `ref="scrollEl"` → the horizontal `overflow-x: auto` scroll container.
 */
import { onBeforeUnmount, onMounted, nextTick, ref, type Ref } from "vue";

export interface UseHorizontalOverflowReturn {
  /** bind to the scroll container via `ref=` */
  scrollEl: Ref<HTMLElement | null>;
  /** true when scrollWidth > clientWidth (REAL overflow, measured) */
  isOverflowing: Ref<boolean>;
  /** directional fade cues */
  canScrollLeft: Ref<boolean>;
  canScrollRight: Ref<boolean>;
  /** overflow popover open state + controls */
  overflowOpen: Ref<boolean>;
  toggleOverflow: () => void;
  closeOverflow: () => void;
  /** force a re-measure (call from a watch on the item list) */
  recompute: () => void;
}

export function useHorizontalOverflow(): UseHorizontalOverflowReturn {
  const scrollEl = ref<HTMLElement | null>(null);
  const isOverflowing = ref(false);
  const overflowOpen = ref(false);
  // Whether there is clipped content to the left / right of the current
  // scroll position (drives the directional edge fades).
  const canScrollLeft = ref(false);
  const canScrollRight = ref(false);

  function recomputeOverflow(): void {
    const el = scrollEl.value;
    if (el === null) {
      isOverflowing.value = false;
      canScrollLeft.value = false;
      canScrollRight.value = false;
      return;
    }
    // 1px slack avoids flicker from sub-pixel rounding.
    const maxScroll = el.scrollWidth - el.clientWidth;
    const next = maxScroll > 1;
    if (next !== isOverflowing.value) isOverflowing.value = next;
    // Directional cues: is there content scrolled off to the left / right?
    canScrollLeft.value = next && el.scrollLeft > 1;
    canScrollRight.value = next && el.scrollLeft < maxScroll - 1;
    // If we just stopped overflowing, make sure the menu doesn't linger open.
    if (!next && overflowOpen.value) overflowOpen.value = false;
  }

  // Mouse wheel → horizontal scroll. A plain mouse wheel only emits `deltaY`
  // (vertical), which a horizontal `overflow-x` container ignores by default.
  // Map the dominant vertical delta onto `scrollLeft` so the user can flick
  // the strip left/right with the wheel (like Notepad++ / browser tab bars).
  // Trackpad horizontal swipes already produce `deltaX` and scroll natively,
  // so only translate when the wheel is predominantly vertical.
  let _rafPending = false;
  function scheduleOverflowRecompute(): void {
    if (_rafPending) return;
    _rafPending = true;
    requestAnimationFrame(() => {
      _rafPending = false;
      recomputeOverflow();
    });
  }

  function onWheel(ev: WheelEvent): void {
    const el = scrollEl.value;
    if (el === null || !isOverflowing.value) return;
    if (Math.abs(ev.deltaY) <= Math.abs(ev.deltaX)) return; // native horizontal
    el.scrollLeft += ev.deltaY;
    ev.preventDefault(); // stop the page from consuming this vertical wheel
    scheduleOverflowRecompute();
  }

  function onScroll(): void {
    scheduleOverflowRecompute();
  }

  // Outside-click dismissal used to live here (bound to a stable outer-wrap
  // ref), but has been moved to `ChatOverflowPopover` so the Teleport-to-body
  // popover isn't self-dismissed by clicks INSIDE itself (from the
  // composable's perspective, a body-mounted popover click always fell
  // outside the wrap → immediate close). See the file header.

  function toggleOverflow(): void {
    overflowOpen.value = !overflowOpen.value;
  }

  function closeOverflow(): void {
    overflowOpen.value = false;
  }

  let _resizeObs: ResizeObserver | null = null;
  onMounted(() => {
    const el = scrollEl.value;
    if (typeof ResizeObserver !== "undefined" && el !== null) {
      _resizeObs = new ResizeObserver(() => recomputeOverflow());
      _resizeObs.observe(el);
    }
    if (el !== null) {
      // `passive:false` so `onWheel` may call `preventDefault()`.
      el.addEventListener("wheel", onWheel, { passive: false });
      el.addEventListener("scroll", onScroll, { passive: true });
    }
    void nextTick(recomputeOverflow);
  });

  onBeforeUnmount(() => {
    if (_resizeObs !== null) {
      _resizeObs.disconnect();
      _resizeObs = null;
    }
    const el = scrollEl.value;
    if (el !== null) {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("scroll", onScroll);
    }
  });

  return {
    scrollEl,
    isOverflowing,
    canScrollLeft,
    canScrollRight,
    overflowOpen,
    toggleOverflow,
    closeOverflow,
    recompute: recomputeOverflow,
  };
}
