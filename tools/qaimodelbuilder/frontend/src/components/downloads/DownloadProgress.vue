<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  DownloadProgress — V1 per-task progress detail row.

  Shown inside a card while `entry.status` is non-`idle`. Renders:
    - status icon (spinner for preparing/downloading, ✓ for done, ✗ for error/cancelled)
    - status label (i18n)
    - percent
    - downloaded/total bytes
    - speed (when > 0)
    - ETA (when > 0)
    - engine pill (aria2c / httpx)
    - progress bar (full-width, 0..100%)
    - on `done`: save_path footer with copy button

  V1 reference: DownloadCenterPanel.js:357-398 (service) + same template
  for model cards.
-->
<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import type { DownloadStateEntry, DownloadStatus } from "@/types/downloads";
import {
  formatBytes,
  formatEta,
  formatSpeed,
  isTerminalStatus,
} from "@/composables/downloads/format";
import { useToastStore } from "@/stores/toast";

interface Props {
  entry: DownloadStateEntry;
  /** Optional file display name override (V1 falls back to entry.filename). */
  displayName?: string;
}

const props = defineProps<Props>();
const { t } = useI18n();
const toast = useToastStore();

const statusIcon = computed<string>(() => {
  switch (props.entry.status) {
    case "preparing":
    case "downloading":
      return "⟳";
    case "done":
      return "✓";
    case "error":
    case "cancelled":
      return "✗";
    default:
      return "—";
  }
});

const statusLabelKey = computed<string>(() => {
  const map: Record<DownloadStatus, string> = {
    idle: "downloads.statusIdle",
    preparing: "downloads.statusPreparing",
    downloading: "downloads.statusDownloading",
    done: "downloads.statusDone",
    error: "downloads.statusError",
    cancelled: "downloads.statusCancelled",
  };
  return map[props.entry.status];
});

const percentDisplay = computed<string>(() => {
  if (props.entry.status === "done") return "100%";
  const p = Math.min(100, Math.max(0, props.entry.percent));
  return `${Math.round(p)}%`;
});

const bytesDisplay = computed<string>(() => {
  const d = formatBytes(props.entry.downloaded_bytes);
  if (props.entry.total_bytes > 0) {
    return `${d} / ${formatBytes(props.entry.total_bytes)}`;
  }
  return d;
});

const speedDisplay = computed<string>(() => formatSpeed(props.entry.speed_bps));
const etaDisplay = computed<string>(() => formatEta(props.entry.eta_seconds));

const barFill = computed<number>(() => {
  if (props.entry.status === "done") return 100;
  return Math.min(100, Math.max(0, props.entry.percent));
});

const isDone = computed<boolean>(() => props.entry.status === "done");
const isTerminal = computed<boolean>(() => isTerminalStatus(props.entry.status));

/**
 * V1 parity (DownloadCenterPanel.js:380): the progress bar track only renders
 * while `downloading` or `done`. For `preparing`/`error`/`cancelled` V1 shows
 * no bar (a red cancelled bar would be misleading).
 */
const showBar = computed<boolean>(
  () => props.entry.status === "downloading" || props.entry.status === "done",
);

async function copySavePath(): Promise<void> {
  if (!props.entry.save_path) return;
  try {
    await navigator.clipboard.writeText(props.entry.save_path);
    toast.push({
      id: crypto.randomUUID(),
      kind: "success",
      message: t("downloads.pathCopiedSimpleToast"),
      timeoutMs: 2000,
    });
  } catch {
    toast.push({
      id: crypto.randomUUID(),
      kind: "error",
      message: t("downloads.copyFailedToast"),
      timeoutMs: 3000,
    });
  }
}
</script>

<template>
  <div
    class="dc-progress"
    :class="`dc-progress--${entry.status}`"
    role="status"
    :aria-live="isTerminal ? 'polite' : 'off'"
  >
    <div class="dc-progress__row">
      <span
        class="dc-progress__icon"
        aria-hidden="true"
      >{{ statusIcon }}</span>
      <span class="dc-progress__label">{{ t(statusLabelKey) }}</span>
      <span class="dc-progress__percent">{{ percentDisplay }}</span>
      <span class="dc-progress__bytes">{{ bytesDisplay }}</span>
      <span
        v-if="speedDisplay"
        class="dc-progress__speed"
      >{{
        speedDisplay
      }}</span>
      <span
        v-if="etaDisplay"
        class="dc-progress__eta"
      >
        {{ t("downloads.eta") }} {{ etaDisplay }}
      </span>
      <span
        v-if="entry.engine"
        class="dc-progress__engine"
        :class="`dc-progress__engine--${entry.engine}`"
        :title="entry.engine"
      >{{ entry.engine }}</span>
    </div>
    <div
      v-if="showBar"
      class="dc-progress__bar"
      role="progressbar"
      :aria-valuenow="barFill"
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <div
        class="dc-progress__bar-fill"
        :style="{ width: `${barFill}%` }"
      />
    </div>
    <div
      v-if="isDone && entry.save_path"
      class="dc-progress__save-path"
    >
      <span style="color:var(--success);flex-shrink:0">📁</span>
      <code>{{ entry.save_path }}</code>
      <button
        type="button"
        class="dc-progress__copy"
        :title="t('downloads.copySavePath')"
        :aria-label="t('downloads.copySavePath')"
        @click="copySavePath"
      >
        ⧉
      </button>
    </div>
    <div
      v-if="entry.status === 'error' && entry.error"
      class="dc-progress__error"
    >
      {{ entry.error }}
    </div>
    <!-- V1 parity (DownloadCenterPanel.js:659): while `preparing`, show the
         task error OR the "Installing aria2c…" hint so the user knows the
         multi-thread downloader is being auto-installed before bytes flow. -->
    <div
      v-if="entry.status === 'preparing'"
      class="dc-progress__preparing"
    >
      {{ entry.error || t("downloads.aria2cAutoInstalling") }}
    </div>
  </div>
</template>

<style scoped>
.dc-progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: var(--space-2);
  padding: var(--space-2) 0;
}

.dc-progress__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
}

.dc-progress__icon {
  display: inline-flex;
  width: 1.2em;
  justify-content: center;
}

.dc-progress--preparing .dc-progress__icon,
.dc-progress--downloading .dc-progress__icon {
  animation: dc-spin 1.4s linear infinite;
  color: var(--accent);
}

.dc-progress--done .dc-progress__icon {
  color: var(--success);
}

.dc-progress--error .dc-progress__icon,
.dc-progress--cancelled .dc-progress__icon {
  color: var(--error);
}

.dc-progress__label {
  font-weight: 600;
}

.dc-progress__percent {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.dc-progress__bytes,
.dc-progress__speed,
.dc-progress__eta {
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.dc-progress__engine {
  margin-left: auto;
  padding: 2px 7px;
  border-radius: var(--radius-xs);
  font-size: var(--text-xs);
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  color: var(--text-muted);
}

.dc-progress__engine--aria2c {
  background: rgba(108, 99, 255, 0.12);
  border-color: rgba(108, 99, 255, 0.2);
  color: var(--accent);
}

.dc-progress__bar {
  width: 100%;
  height: 6px;
  border-radius: 3px;
  background: var(--bg-tertiary);
  overflow: hidden;
}

.dc-progress__bar-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
  border-radius: 3px;
}

/* V1 parity (downloads.css:313-322): the in-flight fill is a moving
   accent→warning gradient pulsing in brightness, giving the "flowing /
   glowing" download bar. Previously V2 rendered a flat accent fill. */
.dc-progress--preparing .dc-progress__bar-fill,
.dc-progress--downloading .dc-progress__bar-fill {
  background: linear-gradient(90deg, var(--accent), var(--warning));
  animation: dc-progress-shimmer 2s infinite;
}

.dc-progress--done .dc-progress__bar-fill {
  background: var(--success);
}

/* V1 downloads.css:313-316 — downloading state pulses a brightness shimmer
   on the progress fill so the bar reads as actively in-flight. */
.dc-progress--downloading .dc-progress__bar-fill {
  animation: dc-progress-shimmer 1.5s ease-in-out infinite;
}
@keyframes dc-progress-shimmer {
  0%,
  100% {
    filter: brightness(1);
  }
  50% {
    filter: brightness(1.25);
  }
}

.dc-progress--error .dc-progress__bar-fill,
.dc-progress--cancelled .dc-progress__bar-fill {
  background: var(--error);
}

@keyframes dc-progress-shimmer {
  0% {
    filter: brightness(1);
  }
  50% {
    filter: brightness(1.2);
  }
  100% {
    filter: brightness(1);
  }
}

.dc-progress__save-path {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-xs);
}

.dc-progress__save-path code {
  font-family: var(--font-mono);
  background: var(--bg-code);
  padding: 1px 6px;
  border-radius: 3px;
  word-break: break-all;
  flex: 1 1 auto;
  min-width: 0;
}

.dc-progress__copy {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0 6px;
  cursor: pointer;
  font-size: 1rem;
  line-height: 1.4;
}

.dc-progress__copy:hover {
  background: var(--bg-hover);
}

.dc-progress__error {
  color: var(--error);
  font-size: var(--text-xs);
  word-break: break-word;
}

/* V1 preparing hint uses muted text + a subtle bordered box
   (DownloadCenterPanel.js:659 `.dc-error-msg` with muted overrides). */
.dc-progress__preparing {
  color: var(--text-muted);
  font-size: var(--text-xs);
  word-break: break-word;
  padding: 4px 8px;
  border: 1px solid var(--border-secondary, var(--border));
  border-radius: var(--radius-sm);
}

@keyframes dc-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
