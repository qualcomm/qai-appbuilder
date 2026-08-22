<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ComposerModeIcon — the single source of truth for toolbar-module icons.
 *
 * Renders the inline SVG glyph for a toolbar module by its snake_case `key`
 * (`model_builder` / `app_builder` / `code` / `translate` / `ppt` / `pro`),
 * falling back to a generic square for unknown keys. Extracted so BOTH the
 * chat-input toolbar (`ComposerModeButtons.vue`) and the Settings → Toolbar
 * Modules list (`AppConfigPanel.vue`) render identical icons from one place
 * (previously the settings list used mismatched / missing emoji prefixes).
 *
 * Icons stroke `currentColor` and default to 13px (the toolbar size); callers
 * may override via the `size` prop. Mirrors V1 index.html:1492-1497.
 */
withDefaults(
  defineProps<{
    /** Module key (snake_case), e.g. `model_builder`. */
    iconKey: string;
    /** Square icon size in px. Defaults to the 13px toolbar glyph. */
    size?: number;
  }>(),
  { size: 13 },
);

const KNOWN: ReadonlySet<string> = new Set([
  "model_builder",
  "model_hub",
  "app_builder",
  "code",
  "translate",
  "ppt",
  "pro",
  "gomaster",
]);

function resolve(key: string): string {
  return KNOWN.has(key) ? key : "default";
}
</script>

<template>
  <!-- model_builder — stacked layers ("build a model") -->
  <svg
    v-if="resolve(iconKey) === 'model_builder'"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path d="M12 2L2 7l10 5 10-5-10-5z" />
    <path d="M2 17l10 5 10-5" />
    <path d="M2 12l10 5 10-5" />
  </svg>
  <!-- app_builder — 2x2 grid of app tiles -->
  <svg
    v-else-if="resolve(iconKey) === 'app_builder'"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    aria-hidden="true"
  ><rect
    x="3"
    y="3"
    width="7"
    height="7"
    rx="1"
  /><rect
    x="14"
    y="3"
    width="7"
    height="7"
    rx="1"
  /><rect
    x="3"
    y="14"
    width="7"
    height="7"
    rx="1"
  /><rect
    x="14"
    y="14"
    width="7"
    height="7"
    rx="1"
  /></svg>
  <!-- code — chevrons "</>" -->
  <svg
    v-else-if="resolve(iconKey) === 'code'"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  ><polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" /></svg>
  <!-- translate — glyph "文A" strokes -->
  <svg
    v-else-if="resolve(iconKey) === 'translate'"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  ><path d="M5 8l6 6" /><path d="M4 14l6-6 2-3" /><path d="M2 5h12" /><path d="M7 2v3" /><path d="M22 22l-5-10-5 10" /><path d="M14 18h6" /></svg>
  <!-- ppt — presentation screen -->
  <svg
    v-else-if="resolve(iconKey) === 'ppt'"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  ><rect
    x="2"
    y="3"
    width="20"
    height="14"
    rx="2"
  /><line
    x1="8"
    y1="21"
    x2="16"
    y2="21"
  /><line
    x1="12"
    y1="17"
    x2="12"
    y2="21"
  /></svg>
  <!-- pro (增强) — GPU rack -->
  <svg
    v-else-if="resolve(iconKey) === 'pro'"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  ><rect
    x="2"
    y="6"
    width="20"
    height="12"
    rx="2"
  /><path d="M6 6V4" /><path d="M10 6V4" /><path d="M14 6V4" /><path
    d="M18 6V4"
  /><path d="M6 20v-2" /><path d="M10 20v-2" /><path d="M14 20v-2" /><path
    d="M18 20v-2"
  /><rect
    x="6"
    y="9"
    width="12"
    height="6"
    rx="1"
  /></svg>
  <!-- gomaster (GoMaster 在线) — neural-network graph (three connected nodes) -->
  <svg
    v-else-if="resolve(iconKey) === 'gomaster'"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  ><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle
    cx="12"
    cy="18"
    r="2.5"
  /><path d="M7.7 7.7l2.6 8.1M16.3 7.7l-2.6 8.1M8 6h8" /></svg>
  <!-- model_hub (模型市场) — download cloud ("pull pre-compiled models") -->
  <svg
    v-else-if="resolve(iconKey) === 'model_hub'"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  ><path d="M20 16.2A4.5 4.5 0 0 0 17.5 8h-1.8A7 7 0 1 0 4 14.9" /><polyline points="8 17 12 21 16 17" /><line
    x1="12"
    y1="12"
    x2="12"
    y2="21"
  /></svg>
  <!-- fallback — generic rounded square -->
  <svg
    v-else
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    aria-hidden="true"
  ><rect
    x="3"
    y="3"
    width="18"
    height="18"
    rx="2"
  /></svg>
</template>
