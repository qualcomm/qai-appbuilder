// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Cross-component bridge for "programmatically set the chat composer's input
 * text and submit it from outside the composer".
 *
 * Why this exists (V1 parity, architecture rationale):
 * The App Builder's `useAppBuilderChatBridge.sendToChat()` needs to inject a
 * composed user message into chat AND trigger an actual LLM turn — V1
 * `app.js:600-625` did exactly that via
 *   `chatComposable.inputText.value = text;`
 *   `nextTick(() => chatComposable.sendMessage());`
 * i.e. it set the shared composer input then ran the composer's normal submit
 * (which uploads pending images, prepends the image prefix, and dispatches the
 * turn through the transport).
 *
 * In V2 the composer's `text` ref + submit lifecycle live inside
 * `ChatComposer.vue` / `useComposerSubmit` and are NOT reachable from a sibling
 * composable. Routing the bridge straight at `useChatTransport` would (a) break
 * the chat ⇄ app_builder context isolation (AGENTS §3.2 — the app-builder
 * bridge must stay within the chat front-end surface and not own the transport)
 * and (b) skip the composer's pending-image upload + image-prefix step that V1
 * `sendMessage()` performed.
 *
 * This module provides the same tiny **intent queue** pattern as
 * `usePendingChatImages` (`pendingFileIntake`):
 *   - producers (app-builder bridge) push a prompt string via
 *     `enqueueChatSubmit`,
 *   - the consumer (`ChatComposer`) watches the queue, drains it, writes the
 *     text into its own `text` ref, and runs its existing `onSubmit()` — so the
 *     submit goes through the EXACT same path as a user-typed message
 *     (image upload + `emit("submit")` → `ChatView.onSubmit` → `pushUserMessage`
 *     + `transport.send`, which triggers the LLM).
 *
 * The queue is a plain module-scope `ref`; subscribers `watch` it for length
 * changes. Producers never read back. Transport ownership stays in `ChatView`;
 * the bridge only writes a string here.
 */
import { ref } from "vue";

const queue = ref<string[]>([]);

/** Queue a prompt for the chat composer to set + submit on next watch tick. */
export function enqueueChatSubmit(prompt: string): void {
  const trimmed = prompt.trim();
  if (trimmed === "") return;
  queue.value = [...queue.value, trimmed];
}

/** Reactive ref the consumer watches. Read-only contract for callers. */
export const pendingChatSubmit = queue;

/** Drain the queue (consumer use only). Returns queued prompts and clears. */
export function drainChatSubmit(): string[] {
  if (queue.value.length === 0) return [];
  const items = queue.value;
  queue.value = [];
  return items;
}

/**
 * Decide how the chat composer should dispatch a programmatically-enqueued
 * prompt given the active tab's status, mirroring V1's `sendMessage` /
 * `handleEnter` gating (useChat.js:1552-1564 / 2810-2835):
 *
 *   - `"streaming"` / `"aborting"` → `"enqueue"`: a turn is already in flight
 *     here, so queue it (V1 handleEnter:2820); ChatView's streaming→idle
 *     dequeue watcher auto-sends it when the turn finishes.
 *   - `"error"` → `"reset-and-submit"`: a failed turn must NOT block the next
 *     send (V1 reset `isStreaming` to false after a failure and never
 *     error-gated). The composer resets the tab to idle then submits.
 *   - `"idle"` (or unknown) → `"submit"`: dispatch immediately.
 *
 * Kept as a pure function so the regression (programmatic send silently
 * dropped while the tab was in `error` / `streaming`) is unit-testable without
 * mounting the composer.
 */
export type ExternalSubmitAction = "submit" | "enqueue" | "reset-and-submit";

export function decideExternalSubmitAction(
  status: string,
): ExternalSubmitAction {
  if (status === "streaming" || status === "aborting") return "enqueue";
  if (status === "error") return "reset-and-submit";
  return "submit";
}
