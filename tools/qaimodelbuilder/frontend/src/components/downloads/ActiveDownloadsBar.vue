<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  ActiveDownloadsBar — V1 floating summary strip above the tabs.

  Shown only while at least one task is `preparing` / `downloading`. Lists
  the most-recent downloads with task id, percent, speed and ETA.

  V1 reference: DownloadCenterPanel.js:236-246.
-->
<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import type { DownloadStateEntry } from "@/types/downloads";
import {
  formatEta,
  formatSpeed,
} from "@/composables/downloads/format";

interface Props {
  active: DownloadStateEntry[];
}

const props = defineProps<Props>();
const { t } = useI18n();

const visibleEntries = computed<DownloadStateEntry[]>(() =>
  // V1 caps the summary line at 4 entries to keep the strip on one row.
  props.active.slice(0, 4),
);
</script>

<template>
  <div
    v-if="active.length > 0"
    class="dc-active-bar"
    role="status"
    aria-live="polite"
  >
    <span
      class="dc-active-bar-dot"
      aria-hidden="true"
    />
    <span class="dc-active-bar__count">
      {{ active.length }} {{ t("downloads.tasksDownloading") }}
    </span>
    <span
      v-for="entry in visibleEntries"
      :key="entry.task_id"
      class="dc-active-bar__entry"
    >
      <span
        class="dc-active-bar__id"
        :title="entry.task_id"
      >{{
        entry.task_id
      }}</span>
      <span class="dc-active-bar__sep">·</span>
      <span class="dc-active-bar__percent">{{ Math.round(entry.percent) }}%</span>
      <template v-if="entry.speed_bps > 0">
        <span class="dc-active-bar__sep">·</span>
        <span class="dc-active-bar__speed">{{
          formatSpeed(entry.speed_bps)
        }}</span>
      </template>
      <template v-if="entry.eta_seconds > 0">
        <span class="dc-active-bar__sep">·</span>
        <span class="dc-active-bar__eta">
          {{ t("downloads.eta") }} {{ formatEta(entry.eta_seconds) }}
        </span>
      </template>
    </span>
    <span
      v-if="active.length > visibleEntries.length"
      class="dc-active-bar__more"
    >
      +{{ active.length - visibleEntries.length }}
    </span>
  </div>
</template>

<style scoped>
/*
  Layout / chrome (`.dc-active-bar` + `.dc-active-bar-dot`) come from the
  global `styles/downloads/downloads.css:502-524` rules — V1-aligned
  warning-orange solid border + pulsing dot. We previously duplicated
  the chrome here with a (drifted) purple-dashed look; now we only keep
  the BEM children that are panel-specific.

  V1 reference: downloads.css:478-500 (`.dc-active-bar`) + 503-518
  (`.dc-active-bar-dot` + `pulse-green` keyframe).
*/
.dc-active-bar__count {
  font-weight: 600;
  /* inherits color from `.dc-active-bar` (var(--warning)) for V1 parity */
}

.dc-active-bar__entry {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-variant-numeric: tabular-nums;
}

.dc-active-bar__id {
  font-family: var(--font-mono);
  max-width: 14em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dc-active-bar__sep {
  color: var(--text-muted);
}

.dc-active-bar__more {
  color: var(--text-muted);
}
</style>
