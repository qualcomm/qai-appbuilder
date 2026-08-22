// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Cross-component bridge for "drop a File into the chat composer's pending
 * images queue from outside the composer".
 *
 * Why this exists (V1 parity, architecture rationale):
 * The App Builder's `useAppBuilderChatBridge.sendToChat()` needs to forward
 * generated images from a model run to the chat as real attachments (V1
 * `app.js:604-619` did this via `chatComposable.addImageFile(file)` on a
 * shared composable). In V2 the chat composer's `pendingImages` is a local
 * `ref` inside `ChatComposer.vue:231` — not reachable from a sibling
 * composable. Lifting it into the `chatTabs` Pinia store would couple the
 * store to a chat-input concern (per-tab transient UI state), which the
 * store explicitly avoids.
 *
 * This module provides a tiny **intent queue** instead:
 *   - producers (app-builder bridge) push File items via `enqueuePendingImage`,
 *   - the consumer (`ChatComposer`) drains the queue with `drainPendingImages`
 *     in an `onMounted` + `watch(pendingFileIntake)` cycle and integrates the
 *     File into its existing local `pendingImages[]`.
 *
 * The queue is a plain module-scope `ref`; subscribers `watch` it for
 * length changes. Producers never read back. This keeps the ChatComposer
 * the sole owner of the rendered pending-images state (no duplication) while
 * giving the bridge a non-coupling write path — simpler than a Pinia store
 * action and confined to chat-input scope.
 */
import { ref } from "vue";

const queue = ref<File[]>([]);

/** Queue an image file for the chat composer to pick up on next watch tick. */
export function enqueuePendingImage(file: File): void {
  queue.value = [...queue.value, file];
}

/** Reactive ref the consumer watches. Read-only contract for callers. */
export const pendingFileIntake = queue;

/** Drain the queue (consumer use only). Returns the queued files and clears. */
export function drainPendingImages(): File[] {
  if (queue.value.length === 0) return [];
  const files = queue.value;
  queue.value = [];
  return files;
}
