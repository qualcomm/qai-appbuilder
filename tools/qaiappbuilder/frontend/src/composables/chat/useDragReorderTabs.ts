// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useDragReorderTabs — reusable "native HTML5 drag-to-reorder" state machine.
 *
 * Extracted from `ChatTabStrip.vue` (where the top chat tab strip lets the
 * user reorder tabs by dragging one over another). The composable owns the
 * fiddly HTML5 DnD wiring — `draggingId` + `dragOverId` state, the
 * `preventDefault` gate that allows drops, and the ordering — while leaving
 * the actual reorder call and any highlight styling to the consumer.
 *
 * ID-based, NOT index-based
 * -------------------------
 * The reorder callback receives the SOURCE and TARGET item ids, never
 * indices. This is deliberate — the consumer may render a computed
 * projection of the store's `tabs[]` (e.g. a filtered / re-ordered view),
 * so the array indices the strip sees may not correspond to the store's
 * indices. Stable ids let the consumer resolve the target in whichever
 * list it considers authoritative.
 *
 * (β note: pre-β `ChatTabStrip` used to filter sub-agent tabs OUT of the
 * top strip via `tabs.filter(t => t.kind !== "subagent")` — indices then
 * definitely mismatched. The β flat-strip refactor no longer filters, but
 * the id-based contract stays as the future-proof abstraction.)
 *
 * What it owns
 *   - `draggingId` — id of the item being dragged (or `null`). Used by the
 *     consumer to render the "being dragged" style (dimmed opacity in the
 *     tab strip's case).
 *   - `dragOverId` — id of the item currently under the pointer (or `null`).
 *     Used by the consumer to render the drop-target style (insertion bar
 *     in the tab strip's case).
 *   - `preventDefault` on `dragover` so the drop is permitted (browsers
 *     default to REJECTING drops; without this call, `drop` never fires).
 *   - `effectAllowed = "move"` on dragstart + `dropEffect = "move"` on
 *     dragover so the cursor shows the correct "move" glyph.
 *   - Firing `onReorder(fromId, toId)` on drop (only if both ids are known
 *     and different), then clearing the drag state.
 *   - Clearing drag state on `dragend` (covers the "drag cancelled outside
 *     any drop target" case that never emits `drop`).
 *
 * What it deliberately does NOT own (stays in the consumer)
 *   - The actual reorder — the consumer wires `onReorder` to its store
 *     action (`useChatTabs.reorderTabs(fromId, toId)` in the tab strip).
 *   - CSS classes for the drag / drag-over highlight (consumer-specific).
 *   - Rules about which items are draggable (e.g. the tab strip disables
 *     drag on the tab currently being renamed via `:draggable="…"`) — the
 *     consumer sets `draggable` per-item and only wires the handlers on
 *     items that should participate.
 *
 * Wiring in the template (see ChatTabStrip.vue for the reference):
 *
 * ```
 * <div
 *   v-for="tab in visibleTabs"
 *   :key="tab.id"
 *   :draggable="canDrag(tab)"
 *   :class="{ dragging: drag.isDragging(tab.id), dragover: drag.isDragOver(tab.id) }"
 *   @dragstart="(ev) => drag.onDragStart(tab.id, ev)"
 *   @dragover="(ev) => drag.onDragOver(tab.id, ev)"
 *   @drop="(ev) => drag.onDrop(tab.id, ev)"
 *   @dragend="drag.onDragEnd"
 * >
 * ```
 */
import { ref, type Ref } from "vue";

export interface UseDragReorderTabsOptions<TId> {
  /**
   * Called once when the user drops the dragged item onto a different
   * target item. Never called if the source and target are the same, or if
   * the source id is somehow lost between dragstart and drop. The consumer
   * calls its store's reorder action here.
   */
  onReorder: (fromId: TId, toId: TId) => void;
}

export interface UseDragReorderTabsReturn<TId> {
  /** Id of the item currently being dragged, `null` when idle. */
  draggingId: Ref<TId | null>;
  /** Id of the item currently under the pointer during a drag, `null` when idle. */
  dragOverId: Ref<TId | null>;
  /** `true` iff `id` is the item currently being dragged. */
  isDragging: (id: TId) => boolean;
  /** `true` iff `id` is the item currently under the pointer during a drag. */
  isDragOver: (id: TId) => boolean;

  /** `@dragstart` handler; wires up `dataTransfer` and captures the source id. */
  onDragStart: (id: TId, ev: DragEvent) => void;
  /**
   * `@dragover` handler; calls `preventDefault` (required to allow the
   * drop) and tracks the current target. A `dragover` on the source item
   * itself is ignored — you can't drop a tab onto itself.
   */
  onDragOver: (id: TId, ev: DragEvent) => void;
  /**
   * `@drop` handler; fires `onReorder(sourceId, targetId)` (if source and
   * target differ) and clears the drag state.
   */
  onDrop: (id: TId, ev: DragEvent) => void;
  /**
   * `@dragend` handler; clears drag state. Fires even if the user cancels
   * the drag by dropping outside any valid target (no `drop` event in that
   * case), so the visual "dragging" state doesn't linger.
   */
  onDragEnd: () => void;
}

export function useDragReorderTabs<TId = string>(
  opts: UseDragReorderTabsOptions<TId>,
): UseDragReorderTabsReturn<TId> {
  const draggingId = ref<TId | null>(null) as Ref<TId | null>;
  const dragOverId = ref<TId | null>(null) as Ref<TId | null>;

  function isDragging(id: TId): boolean {
    return draggingId.value === id;
  }

  function isDragOver(id: TId): boolean {
    return dragOverId.value === id;
  }

  function clearDragState(): void {
    draggingId.value = null;
    dragOverId.value = null;
  }

  function onDragStart(id: TId, ev: DragEvent): void {
    draggingId.value = id;
    if (ev.dataTransfer !== null) {
      ev.dataTransfer.effectAllowed = "move";
      // Some browsers require SOME data set for the drag to proceed. The
      // string id is a sane default; consumers that need richer payloads
      // can attach their own before / after this call. `String(id)` covers
      // the case where TId is not a string.
      ev.dataTransfer.setData("text/plain", String(id));
    }
  }

  function onDragOver(id: TId, ev: DragEvent): void {
    // Not currently dragging (spurious dragover) or dragging over the
    // source itself → no-op (no drop-target highlight, no preventDefault
    // → browser shows the "no-drop" cursor).
    if (draggingId.value === null || draggingId.value === id) {
      return;
    }
    // preventDefault is what permits the subsequent `drop`; without it,
    // browsers reject the drop and `@drop` never fires.
    ev.preventDefault();
    if (ev.dataTransfer !== null) {
      ev.dataTransfer.dropEffect = "move";
    }
    dragOverId.value = id;
  }

  function onDrop(id: TId, ev: DragEvent): void {
    // preventDefault the drop itself so browsers don't try to interpret the
    // drop as e.g. "navigate to the dragged text" — same as the original
    // ChatTabStrip flow.
    ev.preventDefault();
    const from = draggingId.value;
    if (from !== null && from !== id) {
      opts.onReorder(from, id);
    }
    clearDragState();
  }

  function onDragEnd(): void {
    clearDragState();
  }

  return {
    draggingId,
    dragOverId,
    isDragging,
    isDragOver,
    onDragStart,
    onDragOver,
    onDrop,
    onDragEnd,
  };
}
