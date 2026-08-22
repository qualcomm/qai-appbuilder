<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AdvancedCard — V1 ``svc-cfg-svc-cfg-card--advanced`` variant of CollapsibleCard.
 *
 * V1 (``settings.css:789-793`` + ``:807-818``) renders rarely-touched / expert
 * config groups as a collapsible svc-cfg-card with a **dashed** border and a small
 * "Advanced" badge inside the title row, to visually de-emphasise them next to
 * regular cards. V2 expresses the same behaviour by composing the existing
 * ``CollapsibleCard`` (collapse arrow / open-state / a11y already done there)
 * and layering on the ``.svc-cfg-card--advanced`` class + ``.svc-cfg-advanced-badge``
 * span — no duplicated collapse logic, no new global state.
 *
 * The ``.svc-cfg-card--advanced`` rule already lives in ``service-config.css`` (lines
 * 394-412) using ``var(--border)`` / ``var(--bg-tertiary)`` tokens — no
 * hard-coded colours in this component. The "Advanced" label uses the
 * existing i18n key ``serviceConfig.advancedBadge`` if the caller wants to
 * override; the default (``"Advanced"``) is intentionally English-fallback
 * to keep this component self-contained for places where the i18n key is not
 * yet wired (Wave 3 tabs will pass ``:badge-text="t('serviceConfig.advancedBadge')"``
 * once locale entries land).
 *
 * Usage::
 *
 *   <AdvancedCard>
 *     <template #title><span>📊 Metrics</span></template>
 *     <!-- fields -->
 *   </AdvancedCard>
 *
 * Slots:
 *   • ``title`` — passed through to CollapsibleCard's title slot, then the
 *     "Advanced" badge is appended automatically.
 *   • default — svc-cfg-card body.
 */
import CollapsibleCard from "./CollapsibleCard.vue";

withDefaults(
  defineProps<{
    /** Whether the svc-cfg-card body is initially expanded.

        V1 parity (useConfig.js:39/44): V1's `collapsedGroups` starts as an
        empty map and `isCollapsed(id)` returns `!!collapsedGroups[id]`, so
        EVERY group — including all `.svc-cfg-card--advanced` cards — renders
        EXPANDED on first open. The dashed border + "Advanced" badge is the
        only visual de-emphasis; the body is NOT collapsed by default. So the
        default here must be `true` to match the V1 user-perceived state. */
    defaultOpen?: boolean;
    /** Localised label for the badge; caller should pass ``t(...)`` to keep
        the badge translated. Falls back to the English-literal so the
        component is usable before i18n keys land. */
    badgeText?: string;
  }>(),
  { defaultOpen: true, badgeText: "Advanced" },
);
</script>

<template>
  <CollapsibleCard
    class="svc-cfg-card--advanced"
    :default-open="defaultOpen"
  >
    <template #title>
      <slot name="title" />
      <span class="svc-cfg-advanced-badge">{{ badgeText }}</span>
    </template>
    <slot />
  </CollapsibleCard>
</template>

<style scoped src="./service-config.css"></style>
