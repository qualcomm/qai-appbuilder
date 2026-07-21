<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ToggleSwitch — controlled toggle (V1 ``.toggle`` / ``.toggle-slider`` pill).
 *
 * V1 (``components.css:75-100`` + ``ServiceConfigPanel.js:111-114`` and many
 * other call sites) renders every "enabled?" boolean in the service-config
 * modal as ``<label class="toggle"><input type="checkbox"><span
 * class="toggle-slider"></span></label>``. The visuals are already shipped
 * globally in V2 (``styles/components/components.css`` lines 75-106, identical
 * to V1), so this component is a thin, accessible wrapper that:
 *
 *   • exposes a clean ``v-model`` API (``modelValue`` + ``update:modelValue``)
 *     so Tab templates stop spelling out the checkbox/slider markup at every
 *     toggle (~30+ occurrences across the 7 tabs);
 *   • forwards ``disabled`` and ``aria-label`` / ``id`` so the caller can wire
 *     a sibling ``<label>`` (``aria-describedby``) without losing semantics;
 *   • uses the global tokens (``var(--accent)`` / ``var(--bg-tertiary)`` etc.)
 *     via the existing ``.toggle`` class — no hard-coded colours here.
 *
 * Single responsibility: render a controlled boolean toggle. Layout (where the
 * toggle sits in a row, ``margin-left: auto`` to push to the right edge) is
 * the caller's concern; this component only renders the pill.
 */
defineProps<{
  modelValue: boolean;
  disabled?: boolean;
  ariaLabel?: string;
  /** Optional id for the underlying <input>; pair with a sibling <label for=...>. */
  inputId?: string;
}>();

defineEmits<{ (e: "update:modelValue", value: boolean): void }>();
</script>

<template>
  <label class="toggle">
    <input
      :id="inputId"
      type="checkbox"
      :checked="modelValue"
      :disabled="disabled"
      :aria-label="ariaLabel"
      @change="$emit('update:modelValue', ($event.target as HTMLInputElement).checked)"
    />
    <span class="toggle-slider"></span>
  </label>
</template>
