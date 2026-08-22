// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * App Builder mode UI state (block-4).
 *
 * Module-level reactive flags shared between the chat-input sub-toolbar
 * (`ModeFrameAppBuilder.vue`) and the workbench overlay
 * (`AppBuilderWorkbenchOverlay.vue`). V1 kept these on the
 * `useAppBuilder` singleton (`abWorkbenchCollapsed`, `historyOpen`,
 * `abPromptDialogOpen`); the run/selection state proper lives in the
 * Pinia `appBuilder` store. These three are pure view toggles so they
 * stay out of the store.
 */
import { ref } from "vue";

/** Workbench overlay open (rendered above the message list). */
export const workbenchOpen = ref(true);
/** Run History modal open. */
export const historyOpen = ref(false);
/** Edit-Prompt dialog open. */
export const promptDialogOpen = ref(false);

export function toggleWorkbench(): void {
  workbenchOpen.value = !workbenchOpen.value;
}

export function toggleHistory(): void {
  historyOpen.value = !historyOpen.value;
}

export function togglePromptDialog(): void {
  promptDialogOpen.value = !promptDialogOpen.value;
}
