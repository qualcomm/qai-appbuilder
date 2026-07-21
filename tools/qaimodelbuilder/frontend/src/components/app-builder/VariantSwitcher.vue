<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * VariantSwitcher — switch between model precision variants.
 *
 * V1 parity (`QAIModelBuilder_v1_pure/frontend/js/components/app-builder/
 * VariantSwitcher.js`, behaviour source of truth):
 *   - display mode by count: <=1 hidden / <=3 segmented chips / >3 dropdown
 *     (VariantSwitcher.js:74-79). The <=1-hidden case is handled by the parent
 *     `v-if="variantOptions.length >= 2"` guard, so this component only renders
 *     when there are >=2 variants; it still derives `mode` defensively.
 *   - runtime-disable + lock: variant `status !== "Ready"` → greyed chip +
 *     lock icon + "Not installed" tooltip + not clickable
 *     (VariantSwitcher.js:81-90,178-188,222).
 *   - default mark: default variant shows a "●" mark (segmented) / "Default"
 *     tag (dropdown) (VariantSwitcher.js:187,224).
 *   - selecting the already-selected chip does not re-emit
 *     (VariantSwitcher.js:116-122).
 *
 * Implementation is a focused Vue 3 `<script setup>` component (vs V1's
 * Options-API global-Vue object): local refs for open/root, computed for
 * mode/effective selection, pure helpers for status/title — no global state.
 *
 * Uses the global `.ab-variant-*` classes (real design tokens only).
 */

import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useClickOutside, useEscClose } from "@/composables/useClickOutside";

interface Variant {
  id: string;
  label: string;
  description?: string;
  /** Long label shown in the dropdown rows (falls back to label/id). */
  longLabel?: string;
  /**
   * Per-variant install/runtime status. Absent → treated as "Ready" (old packs
   * without a status projection stay clickable). Anything other than "Ready"
   * renders the chip disabled + locked.
   */
  status?: string;
  /** Whether this is the model's default variant (gets the "●"/"Default" mark). */
  isDefault?: boolean;
}

interface Props {
  variants: Variant[];
  modelValue: string;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "update:modelValue": [id: string];
}>();

const { t } = useI18n();

const open = ref(false);
const rootEl = ref<HTMLElement | null>(null);

// Default variant id: explicit `isDefault`, else first entry (V1 _defaultVariantId).
const defaultVariantId = computed<string | null>(() => {
  const list = props.variants;
  if (list.length === 0) return null;
  const def = list.find((v) => v.isDefault === true);
  return def?.id ?? list[0]?.id ?? null;
});

// Fallback to default when no explicit selection so a chip is always highlighted.
const effectiveSelected = computed<string>(
  () => props.modelValue || defaultVariantId.value || "",
);

// Display mode (V1 VariantSwitcher.js:74-79). Parent already hides at <=1.
const mode = computed<"hidden" | "segmented" | "dropdown">(() => {
  const n = props.variants.length;
  if (n <= 1) return "hidden";
  if (n <= 3) return "segmented";
  return "dropdown";
});

// Absent status → "Ready" (old packs without status projection stay usable).
function isReady(v: Variant): boolean {
  return (v.status ?? "Ready") === "Ready";
}

function variantTitle(v: Variant): string {
  const base = v.longLabel || v.label || v.id;
  return isReady(v) ? base : `${base} — ${t("appBuilder.variant.missing")}`;
}

const selectedVariant = computed<Variant | null>(
  () => props.variants.find((v) => v.id === effectiveSelected.value) ?? null,
);

const triggerLabel = computed<string>(() => {
  const v = selectedVariant.value;
  return v?.label || v?.id || "";
});

function onChip(v: Variant): void {
  if (!isReady(v)) return; // disabled variant — reject (V1 :115)
  if (v.id === effectiveSelected.value) {
    open.value = false; // already selected → just close, no re-emit (V1 :116-119)
    return;
  }
  emit("update:modelValue", v.id);
  open.value = false;
}

function toggle(): void {
  open.value = !open.value;
}

// Dismiss: outside-press (V1 parity uses mousedown capture, kept via the
// `event: "mousedown"` option) + ESC. Both gated by `open.value` so the
// listeners are no-ops while the dropdown is closed.
useClickOutside(
  rootEl,
  () => {
    open.value = false;
  },
  { event: "mousedown", when: () => open.value },
);
useEscClose(
  (ev) => {
    ev.preventDefault();
    open.value = false;
  },
  () => open.value,
);
</script>

<template>
  <span
    v-if="mode === 'hidden'"
    class="ab-variant-switcher ab-variant-switcher--hidden"
    aria-hidden="true"
  ></span>

  <div
    v-else-if="mode === 'segmented'"
    ref="rootEl"
    class="ab-variant-switcher ab-variant-switcher--segmented"
    role="radiogroup"
    :aria-label="t('appBuilder.variant.title')"
  >
    <button
      v-for="variant in variants"
      :key="variant.id"
      type="button"
      role="radio"
      :aria-checked="variant.id === effectiveSelected ? 'true' : 'false'"
      :tabindex="variant.id === effectiveSelected ? 0 : -1"
      :title="variantTitle(variant)"
      :disabled="!isReady(variant)"
      class="ab-variant-chip"
      :class="{
        'is-active': variant.id === effectiveSelected,
        'is-disabled': !isReady(variant),
      }"
      @click="onChip(variant)"
    >
      <span
        v-if="!isReady(variant)"
        class="ab-variant-lock"
        aria-hidden="true"
      >&#128274;</span>
      <span class="ab-variant-chip-label">{{ variant.label || variant.id }}</span>
      <span
        v-if="variant.isDefault"
        class="ab-variant-default-mark"
        :title="t('appBuilder.variant.default')"
        aria-hidden="true"
      >&#9679;</span>
    </button>
  </div>

  <div
    v-else
    ref="rootEl"
    class="ab-variant-switcher ab-variant-switcher--dropdown"
    :class="{ open }"
  >
    <button
      type="button"
      class="ab-variant-chip ab-variant-chip--trigger"
      :class="{ 'is-active': open }"
      aria-haspopup="listbox"
      :aria-expanded="open ? 'true' : 'false'"
      :title="t('appBuilder.variant.switcher', { label: triggerLabel })"
      @click="toggle"
    >
      <span class="ab-variant-chip-label">{{ triggerLabel }}</span>
      <span
        class="ab-variant-chip-caret"
        aria-hidden="true"
      >&#9662;</span>
    </button>
    <div
      v-if="open"
      class="ab-variant-dropdown"
      role="listbox"
      :aria-label="t('appBuilder.variant.title')"
    >
      <button
        v-for="variant in variants"
        :key="variant.id"
        type="button"
        role="option"
        :aria-selected="variant.id === effectiveSelected ? 'true' : 'false'"
        :title="variantTitle(variant)"
        :disabled="!isReady(variant)"
        class="ab-variant-dropdown-item"
        :class="{
          'is-active': variant.id === effectiveSelected,
          'is-disabled': !isReady(variant),
        }"
        @click="onChip(variant)"
      >
        <span
          v-if="!isReady(variant)"
          class="ab-variant-lock"
          aria-hidden="true"
        >&#128274;</span>
        <span class="ab-variant-dropdown-label">{{
          variant.longLabel || variant.label || variant.id
        }}</span>
        <span
          v-if="variant.isDefault"
          class="ab-variant-default-tag"
        >{{
          t("appBuilder.variant.default")
        }}</span>
      </button>
    </div>
  </div>
</template>
