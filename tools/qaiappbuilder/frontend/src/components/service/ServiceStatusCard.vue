<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ServiceStatusCard — Region 3 (status card) + Region 4 (not-installed
 * guidance card) of the Service page, extracted from ServiceView.vue to keep
 * that view within the cohesion budget.
 *
 * Presentational: renders the running dot / PID / uptime / exe path, the
 * ⚙️ config gear, Start / Stop buttons, the inline guidance lines, and the
 * separate "GenieAPIService not installed" notice card (Region 4 — same
 * !remote && !running && !installed condition, so it lives here for cohesion).
 * All state stays in the page-level `useServiceControl`; this panel only emits
 * intents (open-config / start / stop) and navigation requests back up.
 */
import { useI18n } from "vue-i18n";
import type { ServiceStatusResponse } from "@/types/service";

defineProps<{
  serviceStatus: ServiceStatusResponse;
  isRunning: boolean;
  isRemoteMode: boolean;
  serviceStarting: boolean;
  serviceStopping: boolean;
  serviceModelsLoading: boolean;
  serviceModelsCount: number;
  canStartService: boolean;
  canConfigure: boolean;
  /** formatted uptime string from svc.formatUptime(uptime_seconds) */
  uptimeText: string;
}>();

const emit = defineEmits<{
  (e: "open-config"): void;
  (e: "start"): void;
  (e: "stop"): void;
  (e: "download-service"): void;
  (e: "download-models"): void;
}>();

const { t } = useI18n();
</script>

<template>
  <div>
    <!-- ── Region 3. Status card ─────────────────────────────────────── -->
    <div
      class="service-status-card"
      :class="{ running: isRunning }"
    >
      <div
        class="service-status-indicator"
        :class="{ running: isRunning }"
      />
      <div class="service-status-main">
        <div class="service-status-title">
          GenieAPIService&nbsp;
          <span :class="isRunning ? 'status-on' : 'status-off'">
            {{ isRunning ? t("service.running") : t("service.stopped") }}
          </span>
        </div>
        <div
          v-if="isRunning"
          class="service-status-meta"
        >
          {{ t("service.pid") }}: {{ serviceStatus.pid }} ·
          {{ t("service.uptime") }}: {{ uptimeText }}
        </div>
        <div
          v-if="isRemoteMode && !isRunning"
          class="service-status-remote-hint"
        >
          {{ t("service.remoteModeStartHint") }}
        </div>
        <div
          v-if="serviceStatus.exe_path"
          class="service-status-exe"
          :title="serviceStatus.exe_path"
        >
          {{ serviceStatus.exe_path }}
        </div>
      </div>
      <div class="service-status-actions">
        <div class="service-status-buttons">
          <button
            type="button"
            class="btn btn-ghost btn-sm svc-cfg-gear-btn"
            :title="
              canConfigure
                ? t('service.config')
                : t('service.configRequiresInstall')
            "
            :disabled="!canConfigure"
            @click="emit('open-config')"
          >
            ⚙️
          </button>
          <button
            type="button"
            class="btn btn-success"
            :disabled="isRunning || serviceStarting || !canStartService"
            @click="emit('start')"
          >
            <span
              v-if="serviceStarting"
              class="spinner svc-btn-spinner"
              aria-hidden="true"
            ></span>
            <span v-else>▶</span>
            {{ serviceStarting ? t("service.starting") : t("service.start") }}
          </button>
          <button
            type="button"
            class="btn btn-danger"
            :disabled="!isRunning || serviceStopping || isRemoteMode"
            @click="emit('stop')"
          >
            <span
              v-if="serviceStopping"
              class="spinner svc-btn-spinner"
              aria-hidden="true"
            ></span>
            <span v-else>■</span>
            {{ serviceStopping ? t("service.stopping") : t("service.stop") }}
          </button>
        </div>
        <div
          v-if="!isRemoteMode && !isRunning && !serviceStatus.exe_path && !serviceModelsLoading"
          class="service-status-guidance"
        >
          {{ t("service.serviceNotFound") }}<br />
          <a
            href="#"
            @click.prevent="emit('download-service')"
          >{{ t("service.downloadArrow") }}</a>
        </div>
        <div
          v-else-if="!isRemoteMode && !isRunning && serviceModelsCount === 0 && !serviceModelsLoading"
          class="service-status-guidance error"
        >
          <!-- V1 parity (index.html:2829-2832): static prefix text, only the
               trailing "go download →" segment is a link (NOT the entire
               line). Previously the whole sentence was wrapped in <a>, which
               made the prefix text clickable too. -->
          {{ t("service.noModelsAvailablePrefix") }}<a
            href="#"
            @click.prevent="emit('download-models')"
          >{{ t("service.goDownloadArrow") }}</a>
        </div>
      </div>
    </div>

    <!-- ── Region 4. GenieAPIService-not-installed guidance card ───────── -->
    <div
      v-if="!isRemoteMode && !isRunning && !serviceStatus.exe_path && !serviceModelsLoading"
      class="svc-notice warn"
    >
      <div class="svc-notice-title">
        <span>⚠️</span>
        {{ t("service.geniesvcNotFoundTitle") }}
      </div>
      <div class="svc-notice-body">
        {{ t("service.geniesvcNotFoundBody") }}
      </div>
      <div class="svc-notice-actions">
        <a
          href="#"
          class="svc-notice-action-btn primary"
          @click.prevent="emit('download-service')"
        >
          {{ t("service.gotoDownloadGeniesvc") }} →
        </a>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── Status card ── */
.service-status-card {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  /* V1 service.css:21 — wider horizontal padding. */
  padding: var(--space-4) var(--space-5);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  /* V1 service.css:25-26 — animate border/shadow on running transition. */
  transition: border-color var(--duration-fast) var(--ease-out),
    box-shadow var(--duration-fast) var(--ease-out);
}
.service-status-card.running {
  /* V1 service.css:28-31 — whole card turns green when running. */
  border-color: var(--success);
  background: var(--banner-success-bg);
}
.service-status-indicator {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
  transition: background var(--transition);
}
.service-status-indicator.running {
  background: var(--success);
  /* V1 service.css:41-44 — running state pulses a green ring. */
  animation: pulse-green 2s infinite;
}
@keyframes pulse-green {
  0%,
  100% {
    box-shadow: 0 0 0 0 rgba(76, 175, 80, 0.4);
  }
  50% {
    box-shadow: 0 0 0 6px rgba(76, 175, 80, 0);
  }
}
.service-status-main {
  flex: 1;
  min-width: 0;
}
.service-status-title {
  font-size: var(--text-md);
  font-weight: 700;
  color: var(--text-primary);
}
.status-on {
  color: var(--success);
}
.status-off {
  color: var(--text-muted);
}
.service-status-meta {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-top: 2px;
}
.service-status-remote-hint {
  font-size: var(--text-xs);
  color: var(--banner-warn-text);
  margin-top: 3px;
}
.service-status-exe {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-top: 2px;
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.service-status-actions {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
  flex-shrink: 0;
}
.service-status-buttons {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}
/* V1 parity (index.html:2803-2805): ⚙️ gear button has opacity:0.7 default,
   transitions to 1.0 on hover. */
.svc-cfg-gear-btn {
  opacity: 0.7;
  transition: opacity 0.15s ease;
}
.svc-cfg-gear-btn:hover {
  opacity: 1;
}
/* V1 parity (index.html:2810/2816/2732/2748/2869): start/stop/test/save
   buttons show a rotating spinner while their action is in flight. Reuses
   the global `.spinner` chrome at a button-inline size. */
.svc-btn-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-width: 2px;
  vertical-align: -1px;
}
.service-status-guidance {
  font-size: var(--text-xs);
  color: var(--warning);
  text-align: right;
  max-width: 220px;
  line-height: 1.6;
}
.service-status-guidance.error {
  color: var(--error);
}
.service-status-guidance a {
  color: var(--accent);
  text-decoration: none;
}

/* ── Notice cards (Region 4) ── */
.svc-notice {
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.svc-notice.warn {
  background: var(--banner-warn-bg);
  border: 1px solid var(--banner-warn-border);
}
.svc-notice-title {
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 6px;
}
.svc-notice-body {
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.svc-notice-actions {
  /* V1 service.css:391-395 — stacked action buttons. */
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
}
.svc-notice-action-btn {
  /* V1 service.css:397-410 — filled/outlined pill buttons. */
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  text-decoration: none;
  border: 1px solid transparent;
  transition: background var(--transition), border-color var(--transition),
    color var(--transition);
  width: fit-content;
}
.svc-notice-action-btn.primary {
  /* V1 service.css:411-419 — solid accent button. */
  background: var(--accent);
  border-color: var(--accent);
  color: #ffffff;
}
.svc-notice-action-btn.primary:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
</style>
