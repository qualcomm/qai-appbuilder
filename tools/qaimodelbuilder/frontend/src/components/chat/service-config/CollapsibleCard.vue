<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CollapsibleCard — a V1-parity collapsible config svc-cfg-card.
 *
 * V1 (`ServiceConfigPanel.js:62-66`) renders each config group as a
 * `svc-cfg-card` whose header is clickable to toggle a `svc-cfg-collapse-arrow`
 * (▼) and `v-show` the body. V2 expresses the same user-perceived behaviour as
 * a reusable, self-contained component so every Tab reuses one collapse
 * implementation instead of inlining toggle state into each (large) Tab
 * component.
 *
 * Default state matches V1: `collapsedGroups = ref({})` / `isCollapsed` returns
 * `!!collapsedGroups[id]`, i.e. all cards start **expanded**. The open state is
 * local component UI state — no shared/global ref, no new i18n key (the title is
 * passed in by the caller using its existing `t()` text).
 */
import { ref } from "vue";

const props = withDefaults(
  defineProps<{
    /** Whether the svc-cfg-card body is initially expanded (V1 default: true). */
    defaultOpen?: boolean;
  }>(),
  { defaultOpen: true },
);

const open = ref(props.defaultOpen);
</script>

<template>
  <section class="svc-cfg-card collapsible-card">
    <h3
      class="svc-cfg-card-header collapsible-card__header"
      role="button"
      tabindex="0"
      :aria-expanded="open"
      @click="open = !open"
      @keydown.enter.prevent="open = !open"
      @keydown.space.prevent="open = !open"
    >
      <span class="svc-cfg-card-title collapsible-card__title-content">
        <slot name="title" />
      </span>
      <span
        class="svc-cfg-collapse-arrow"
        :class="{ collapsed: !open }"
        aria-hidden="true"
      >▼</span>
    </h3>
    <div
      v-show="open"
      class="svc-cfg-card-body"
    >
      <slot />
    </div>
  </section>
</template>

<style scoped src="./service-config.css"></style>
