// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useLightbox` — image lightbox state + interactions.
 *
 * Ported from V1 `frontend/js/app.js:168-213` ("Image Lightbox" block) and
 * `index.html:137-148` (overlay markup). It provides the full-screen image
 * preview that opens when a thumbnail (pending-image bar in the composer) or
 * an inline message image is clicked.
 *
 * Behaviour replicated 1:1 from V1 so the user-perceived experience matches:
 *   - open(url)         → show overlay with the given image source
 *   - close()           → hide overlay and reset scale/offset
 *   - wheel             → zoom ±10% per notch, clamped to [0.2, 10]
 *   - mousedown + drag  → pan the image (offset accumulates from drag start)
 *   - dblclick          → reset scale & offset (V1 `lightboxReset`)
 *   - Escape            → close (when an Escape handler is wired by the caller)
 *
 * Shared by `ChatMessageList.vue` (inline message images) and
 * `ChatComposer.vue` (pending-image thumbnails) so the lightbox logic lives in
 * one place instead of being duplicated per component.
 */
import {
  computed,
  ref,
  type CSSProperties,
  type ComputedRef,
  type Ref,
} from "vue";

export interface UseLightbox {
  /** Current image source, or `null` when the lightbox is closed. */
  src: Ref<string | null>;
  /** `true` while the overlay is visible. */
  isOpen: ComputedRef<boolean>;
  /** Inline style (transform + cursor) for the `<img>` element. */
  imageStyle: ComputedRef<CSSProperties>;
  /** Open the lightbox showing `url`. */
  open: (url: string) => void;
  /** Close the lightbox and reset scale/offset (V1 `closeLightbox`). */
  close: () => void;
  /** Zoom on wheel (±10% per notch, clamped 0.2–10). */
  onWheel: (event: WheelEvent) => void;
  /** Reset scale & offset (V1 `lightboxReset`, bound to dblclick). */
  reset: () => void;
  /** Begin dragging to pan (V1 `lightboxDragStart`, bound to mousedown). */
  onDragStart: (event: MouseEvent) => void;
  /** Escape-key handler — closes when open (V1 parity). */
  onKeydown: (event: KeyboardEvent) => void;
}

// V1 app.js:189 clamps scale to [0.2, 10]; wheel uses ×0.9 / ×1.1 per notch.
const SCALE_MIN = 0.2;
const SCALE_MAX = 10;
const WHEEL_ZOOM_OUT = 0.9;
const WHEEL_ZOOM_IN = 1.1;

export function useLightbox(): UseLightbox {
  const src = ref<string | null>(null);
  const scale = ref(1);
  const offsetX = ref(0);
  const offsetY = ref(0);
  const dragging = ref(false);

  // Drag origin captured on mousedown (V1 `_lbDragStart*`).
  let dragStartX = 0;
  let dragStartY = 0;
  let dragStartOffX = 0;
  let dragStartOffY = 0;

  const isOpen = computed<boolean>(() => src.value !== null);

  // V1 app.js:176-179 — translate then scale; grab/grabbing cursor.
  const imageStyle = computed<CSSProperties>(() => ({
    transform: `translate(${offsetX.value}px, ${offsetY.value}px) scale(${scale.value})`,
    cursor: dragging.value ? "grabbing" : "grab",
  }));

  function open(url: string): void {
    src.value = url;
  }

  // V1 `closeLightbox` (app.js:181-186): clear src + reset transform.
  function close(): void {
    src.value = null;
    scale.value = 1;
    offsetX.value = 0;
    offsetY.value = 0;
  }

  // V1 `lightboxWheel` (app.js:187-190).
  function onWheel(event: WheelEvent): void {
    const delta = event.deltaY > 0 ? WHEEL_ZOOM_OUT : WHEEL_ZOOM_IN;
    scale.value = Math.min(SCALE_MAX, Math.max(SCALE_MIN, scale.value * delta));
  }

  // V1 `lightboxReset` (app.js:191-195).
  function reset(): void {
    scale.value = 1;
    offsetX.value = 0;
    offsetY.value = 0;
  }

  // V1 `_lightboxDragMove` (app.js:205-208).
  function onDragMove(event: MouseEvent): void {
    offsetX.value = dragStartOffX + (event.clientX - dragStartX);
    offsetY.value = dragStartOffY + (event.clientY - dragStartY);
  }

  // V1 `_lightboxDragEnd` (app.js:209-213).
  function onDragEnd(): void {
    dragging.value = false;
    document.removeEventListener("mousemove", onDragMove);
    document.removeEventListener("mouseup", onDragEnd);
  }

  // V1 `lightboxDragStart` (app.js:196-204).
  function onDragStart(event: MouseEvent): void {
    dragging.value = true;
    dragStartX = event.clientX;
    dragStartY = event.clientY;
    dragStartOffX = offsetX.value;
    dragStartOffY = offsetY.value;
    document.addEventListener("mousemove", onDragMove);
    document.addEventListener("mouseup", onDragEnd);
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape" && src.value !== null) {
      close();
    }
  }

  return {
    src,
    isOpen,
    imageStyle,
    open,
    close,
    onWheel,
    reset,
    onDragStart,
    onKeydown,
  };
}
