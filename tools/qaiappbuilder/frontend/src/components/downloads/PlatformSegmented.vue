<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  PlatformSegmented — V1 multi-platform / multi-variant segmented selector.

  Renders a horizontal pill row where each pill represents one option
  (a `ServicePackage` or a `ModelVariant`). The active pill is highlighted;
  each pill carries an optional status dot derived from the per-task
  download state (idle/downloading/done/error → CSS modifier).

  V1 reference: DownloadCenterPanel.js:334-354 (service) + 624-693 (model).
-->
<script setup lang="ts">
import { computed } from "vue";

import type { DownloadStatus } from "@/types/downloads";

interface Option {
  /** Stable id (`platform_id` for service / `variant_id` for model). */
  id: string;
  /** Display label (`platform` / `platform`+chip). */
  label: string;
  /** Optional status feed for the right-edge dot. */
  status?: DownloadStatus | null;
}

interface Props {
  options: Option[];
  modelValue: string;
  ariaLabel?: string;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();

const activeId = computed(() => props.modelValue);

function pickStatusClass(status: DownloadStatus | null | undefined): string {
  if (status === null || status === undefined || status === "idle") return "";
  return `platform-segmented__dot--${status}`;
}
</script>

<template>
  <div
    class="platform-segmented"
    role="radiogroup"
    :aria-label="ariaLabel ?? 'platform selection'"
  >
    <button
      v-for="opt in options"
      :key="opt.id"
      type="button"
      role="radio"
      class="platform-segmented__btn"
      :class="{ 'platform-segmented__btn--active': opt.id === activeId }"
      :aria-checked="opt.id === activeId"
      @click="emit('update:modelValue', opt.id)"
    >
      <span class="platform-segmented__label">{{ opt.label }}</span>
      <span
        v-if="opt.status !== undefined && opt.status !== null && opt.status !== 'idle'"
        class="platform-segmented__dot"
        :class="pickStatusClass(opt.status)"
        :aria-label="opt.status"
      />
    </button>
  </div>
</template>

<style scoped>
/*
  V1 .platform-segmented (downloads.css:891-927): full-width connected
  segmented control — single bordered frame with overflow:hidden, each
  segment flex:1 to fill the row, a right divider between segments, and an
  accent-light (soft purple) + accent text active state (NOT a solid filled
  pill). Mirrors V1 visuals while reusing V2 CSS tokens.
*/
.platform-segmented {
  display: flex;
  gap: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.platform-segmented__btn {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: var(--space-3) var(--space-4);
  text-align: center;
  background: var(--bg-secondary);
  border: none;
  border-right: 1px solid var(--border);
  cursor: pointer;
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--text-secondary);
  user-select: none;
  transition: background 0.12s, color 0.12s;
}

.platform-segmented__btn:last-child {
  border-right: none;
}

.platform-segmented__btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.platform-segmented__btn--active {
  background: var(--accent-light);
  color: var(--accent);
  font-weight: 600;
}

.platform-segmented__btn--active:hover {
  background: var(--accent-light);
  color: var(--accent);
}

.platform-segmented__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  display: inline-block;
}

.platform-segmented__dot--preparing,
.platform-segmented__dot--downloading {
  background: var(--warning);
  animation: dc-pulse 1.2s ease-in-out infinite;
}

.platform-segmented__dot--done {
  background: var(--success);
}

.platform-segmented__dot--error {
  background: var(--error);
}

.platform-segmented__dot--cancelled {
  background: var(--text-muted);
}

@keyframes dc-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}
</style>
