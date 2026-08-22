<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ServiceLogPanel — the Service page's log viewer (extracted from
 * ServiceView.vue to keep that view within the cohesion budget).
 *
 * Behaviour is intentionally identical to the previous inline block:
 * all log state + streaming + auto-scroll live in the page-level
 * `useServiceControl` composable. This panel is presentational — it
 * renders the lines (ANSI → HTML), the header (title / streaming dot /
 * line count / fullscreen toggle) and the scroll-nav capsule, and
 * forwards scroll interactions back to the parent via emits. The real
 * scroll container DOM node is exposed via `defineExpose({ logPanel })`
 * so the parent can hand it to `useServiceControl` (which reads
 * `logPanel.value` for auto-scroll), exactly as before.
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import { ansiToHtml } from "@/utils/ansi";

defineProps<{
  /** svc.serviceLogs */
  logs: string[];
  /** svc.serviceLogsStreaming */
  streaming: boolean;
  /** svc.logsExpanded (two-way) */
  expanded: boolean;
  /** svc.logUserScrolledUp — drives the "scroll to bottom" active state */
  userScrolledUp: boolean;
  /** left offset applied to the fullscreen overlay */
  sidebarOffset: string;
}>();

const emit = defineEmits<{
  (e: "update:expanded", v: boolean): void;
  (e: "scroll"): void;
  (e: "scroll-top"): void;
  (e: "scroll-bottom"): void;
}>();

const { t } = useI18n();

// Real scroll container — exposed so the parent's useServiceControl can
// drive auto-scroll against it (V1 parity: same DOM node as before).
const logPanel = ref<HTMLElement | null>(null);
defineExpose({ logPanel });

/** V1 parity: classify a log line for colour by severity keyword. */
function logLineClass(line: string): string {
  const l = line.toLowerCase();
  if (l.includes("[error]") || l.includes("error:") || l.includes("failed") || l.includes("exception")) {
    return "service-log-line log-error";
  }
  if (l.includes("[warn") || l.includes("warning")) return "service-log-line log-warn";
  if (l.includes("[info]") || l.includes("info:")) return "service-log-line log-info";
  if (l.includes("[debug]") || l.includes("[verbose]")) return "service-log-line log-debug";
  return "service-log-line";
}
</script>

<template>
  <div
    class="service-log-section"
    :class="{ 'service-log-expanded': expanded }"
    :style="expanded ? { left: sidebarOffset } : {}"
  >
    <div class="service-log-header">
      <span class="svc-logs-title">
        📋 {{ t("service.logs") }}
        <span
          v-if="streaming"
          class="service-log-streaming-dot"
          :title="t('service.streamingLive')"
        />
      </span>
      <div class="svc-logs-actions">
        <span class="svc-logs-count">{{ logs.length }} {{ t("service.linesUnit") }}</span>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :title="expanded ? t('service.collapseLogArea') : t('service.expandLogArea')"
          @click="emit('update:expanded', !expanded)"
        >
          <span v-if="expanded">⊡</span>
          <span v-else>⊞</span>
        </button>
      </div>
    </div>
    <div class="service-log-body">
      <div
        ref="logPanel"
        class="service-log-panel"
        @scroll="emit('scroll')"
      >
        <div
          v-if="logs.length === 0"
          class="service-log-empty"
        >
          {{ t("service.noLogs") }}
        </div>
        <!-- eslint-disable vue/no-v-html -->
        <!-- Log text is HTML-escaped inside ansiToHtml before any span is
             inserted; the only markup emitted is machine-built inline-style
             spans. Mirrors V1 `v-html="ansiToHtml(entry)"`. -->
        <div
          v-for="(line, i) in logs"
          :key="i"
          :class="logLineClass(line)"
          v-html="ansiToHtml(line)"
        />
        <!-- eslint-enable vue/no-v-html -->
      </div>
      <!-- V1 parity (index.html:3031): scroll nav wrapped in <transition> for
           fade-in/out when logs appear/disappear. -->
      <transition name="scroll-btn-fade">
        <div
          v-if="logs.length > 0"
          class="svc-log-scroll-nav"
        >
          <button
            type="button"
            class="scroll-nav-btn"
            :title="t('service.scrollTop')"
            @click="emit('scroll-top')"
          >
            ↑
          </button>
          <button
            type="button"
            class="scroll-nav-btn"
            :class="{ 'scroll-nav-btn--active': userScrolledUp }"
            :title="t('service.scrollBottomLog')"
            @click="emit('scroll-bottom')"
          >
            ↓
          </button>
        </div>
      </transition>
    </div>
  </div>
</template>

<style scoped>
/* ── Logs (moved verbatim from ServiceView.vue) ── */
.service-log-section {
  /* V1 parity (service.css:226-228): the log section must grow + have a
     min-height so it has real height when NOT fullscreen. Vue scoped CSS
     adds [data-v-*] and outranks the global `service.css .service-log-section`
     that DOES set these — so omitting them here let the section collapse,
     `overflow:hidden` clipped the absolutely-positioned scroll-nav, and the
     ↑/↓ button was invisible in non-fullscreen. Restore flex:1 + min-height. */
  flex: 1;
  min-height: 200px;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.service-log-expanded {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  /* V1 service.css:243 — fullscreen log overlay sits ABOVE page content but
     BELOW modal overlays (`.svc-cfg-modal-overlay` at z-index 1000), so a
     Service Config dialog opened while the log is fullscreened still appears
     on top. Previously this was 1500 which would cover the modal. */
  z-index: 200;
  border-radius: 0;
  /* When fullscreen, `position:fixed` provides height; the non-fullscreen
     min-height:200px above is harmless here (overridden by top/bottom:0). */
}
.service-log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--border);
}
.svc-logs-title {
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 6px;
}
.service-log-streaming-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--success);
  /* V1 service.css:321-324 — opacity 1↔0.2 at 1s cadence (was 1↔0.3 at 1.4s,
     visibly slower than V1). */
  animation: svc-pulse 1s ease-in-out infinite;
}
@keyframes svc-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.2; }
}
.svc-logs-actions {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}
.svc-logs-count {
  font-size: var(--text-xs);
  color: var(--text-muted);
}
.service-log-body {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.service-log-panel {
  /* V1 service.css:277-282 — log panel uses dedicated code bg token, with
     `padding: var(--space-3) var(--space-4)` and `min-height: 180px`; the
     height is driven by flex (parent container decides). Previously a hard
     `max-height: 300px` clamped the panel below the available space, and a
     tighter `var(--space-2)` padding made the lines feel cramped. */
  background: var(--bg-code);
  padding: var(--space-3) var(--space-4);
  min-height: 180px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  flex: 1;
}
.service-log-expanded .service-log-panel {
  /* fullscreen overlay: let panel grow to fill the viewport */
  min-height: 0;
}
.service-log-empty {
  color: var(--text-muted);
  text-align: center;
  padding: var(--space-4);
}
.service-log-line {
  padding: 1px 4px;
  line-height: 1.5;
  /* V1 service.css:305 — default log line uses code text color. */
  color: var(--code-text);
  white-space: pre-wrap;
  word-break: break-all;
}
.service-log-line.log-error {
  color: var(--error);
}
.service-log-line.log-warn {
  color: var(--warning);
}
.service-log-line.log-info {
  color: var(--info);
}
.service-log-line.log-debug {
  color: var(--text-muted);
}
.svc-log-scroll-nav {
  /* V1 service.css:286-301 — capsule container: secondary bg, pill radius,
     shadow, stacked buttons with no gap, overflow hidden. */
  position: absolute;
  right: var(--space-4);
  bottom: var(--space-4);
  z-index: 50;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.22);
  overflow: hidden;
  pointer-events: auto;
}
.scroll-nav-btn {
  /* V1 parity (chat.css:1289-1319): flex-centered arrows, 32x36 cells, a
     divider between the two buttons, and a tinted (not solid) active state. */
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 36px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--text-lg);
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
  user-select: none;
}
.scroll-nav-btn:first-child {
  /* divider line between ↑ and ↓ (V1 chat.css:1305-1307) */
  border-bottom: 1px solid var(--border);
}
.scroll-nav-btn:hover {
  background: var(--bg-tertiary);
  color: var(--text-primary);
}
.scroll-nav-btn--active {
  /* V1: tinted highlight + accent text, NOT a solid fill (was bg:accent/#fff) */
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
.scroll-nav-btn--active:hover {
  background: color-mix(in srgb, var(--accent) 20%, transparent);
}
/* V1 parity (index.html:3031 <transition name="scroll-btn-fade">):
   fade-in/out for the scroll nav capsule. */
.scroll-btn-fade-enter-active,
.scroll-btn-fade-leave-active {
  transition: opacity 0.2s ease;
}
.scroll-btn-fade-enter-from,
.scroll-btn-fade-leave-to {
  opacity: 0;
}
</style>
