<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CodingPermissionDialog — real-time tool permission approval card
 * (功能块 6 阶段C, V1 `AiCodingPanel.js` Phase-2 approval parity).
 *
 * V1 reference (frontend/js/ai-coding/AiCodingPanel.js:1408-1476):
 *   - Modal overlay covers the cc-panel (position:absolute;inset:0 +
 *     rgba(10,18,28,0.92) + backdrop-filter:blur(2px))
 *   - Dialog card: rounded 10px, max-width 340px, yellow border tint
 *   - Header: 🔐 + "工具呼叫审批 / Tool Call Approval"
 *           + subtitle "Claude Code 请求执行以下操作"
 *   - Tool block: yellow bg, tool icon (mapped) + tool name (bold, large)
 *   - Params block: grey bg, key/value list (NOT raw JSON), max-height 120px
 *   - Timeout hint: "⏱️ 120秒后自动拒绝"
 *   - Two buttons row: ❌ Deny (red) | ✅ Allow (green)
 *   - Below: "🔓 Allow & Remember (this session)" full-width purple button
 *
 * V2 mounts this dialog inside the unified chat message stream (rendered by
 * ChatViewClaudeCode/OpenCode below the active message). The card uses V1
 * visual language with V1 i18n keys (aiCoding.panel.permissionTitle /
 * permissionSubtitle / toolLabel / paramsLabel / noParams /
 * permissionTimeoutHint / deny / allow / allowAndRemember).
 *
 * Emits:
 *   - approve         → decide("approved")
 *   - reject          → decide("rejected")
 *   - rememberApprove → approve + create persistent auto-approve rule
 *     (V2's useAutoApprove store; see ChatViewClaudeCode.onRememberApprove).
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { PermissionRequest } from "@/composables/useCodingSession";
import { iconForTool } from "@/utils/codingToolIcons";

const props = defineProps<{ request: PermissionRequest }>();
const emit = defineEmits<{
  (e: "approve"): void;
  (e: "reject"): void;
  (e: "rememberApprove"): void;
}>();

const { t } = useI18n();

/**
 * V1-parity tool-icon mapper (AiCodingPanel.js getToolIcon helper).
 * Uses the shared {@link iconForTool} registry so the icon presented in
 * the approval card matches the tool-call card and V1 1:1 (see
 * `utils/codingToolIcons.ts` — 13 named entries vs. V2's previous 5
 * substring heuristics).
 */
const toolIcon = computed<string>(() => iconForTool(props.request.tool));

/** Tool input args as a key/value list (V1 row-by-row rendering). */
const argEntries = computed<Array<{ key: string; display: string }>>(() => {
  const args = props.request.args;
  if (args === null || args === undefined || typeof args !== "object") {
    return [];
  }
  return Object.entries(args).map(([key, val]) => {
    let display: string;
    if (typeof val === "string") {
      display = val.length > 120 ? `${val.slice(0, 120)}...` : val;
    } else {
      const json = JSON.stringify(val);
      display = json.length > 80 ? `${json.slice(0, 80)}...` : json;
    }
    return { key, display };
  });
});

const timeoutSec = computed<number>(
  () => (props.request as { timeout_seconds?: number }).timeout_seconds ?? 120,
);
</script>

<template>
  <!-- V1-style modal overlay (semi-opaque + blur backdrop). The host view
       overlays this on top of the chat message area; pointer events go
       through to the dialog only. -->
  <div
    class="cc-perm-overlay"
    data-testid="cc-permission-dialog"
    role="alertdialog"
    aria-modal="true"
  >
    <div class="cc-perm-card">
      <!-- Header -->
      <div class="cc-perm-card__head">
        <span
          aria-hidden="true"
          style="font-size:var(--text-lg,18px)"
        >🔐</span>
        <div>
          <div class="cc-perm-card__title">
            {{ t("aiCoding.panel.permissionTitle", "Tool Call Approval") }}
          </div>
          <div class="cc-perm-card__subtitle">
            {{ t("aiCoding.panel.permissionSubtitle", "Claude Code requests to perform the following operation") }}
          </div>
        </div>
      </div>

      <!-- Tool name block (yellow tinted) -->
      <div class="cc-perm-card__tool">
        <div class="cc-perm-card__label">
          {{ t("aiCoding.panel.toolLabel", "Tool") }}
        </div>
        <div class="cc-perm-card__tool-name">
          <span aria-hidden="true">{{ toolIcon }}</span>
          {{ request.tool }}
        </div>
      </div>

      <!-- Tool input parameters block (grey tinted, key/value list) -->
      <div
        class="cc-perm-card__args"
        data-testid="cc-permission-args"
      >
        <div class="cc-perm-card__label">
          {{ t("aiCoding.panel.paramsLabel", "Parameters") }}
        </div>
        <div
          v-for="entry in argEntries"
          :key="entry.key"
          class="cc-perm-card__arg-row"
        >
          <span class="cc-perm-card__arg-key">{{ entry.key }}：</span>
          <span class="cc-perm-card__arg-val">{{ entry.display }}</span>
        </div>
        <div
          v-if="argEntries.length === 0"
          class="cc-perm-card__no-args"
        >
          {{ t("aiCoding.panel.noParams", "(no parameters)") }}
        </div>
      </div>

      <!-- Timeout hint -->
      <div class="cc-perm-card__timeout">
        ⏱️ {{ t("aiCoding.panel.permissionTimeoutHint", { n: timeoutSec }, `No response will auto-deny in ${timeoutSec} seconds`) }}
      </div>

      <!-- Two-button row: Deny / Allow -->
      <div class="cc-perm-card__row2">
        <button
          type="button"
          class="cc-perm-card__btn cc-perm-card__btn--deny"
          data-testid="cc-permission-reject"
          :title="t('aiCoding.panel.permissionDenyTitle', 'Deny this tool call, Claude Code will receive a denial notification')"
          @click="emit('reject')"
        >
          ❌ {{ t("aiCoding.panel.deny", "Deny") }}
        </button>
        <button
          type="button"
          class="cc-perm-card__btn cc-perm-card__btn--allow"
          data-testid="cc-permission-approve"
          :title="t('aiCoding.panel.permissionAllowTitle', 'Allow this tool call, Claude Code will continue execution')"
          @click="emit('approve')"
        >
          ✅ {{ t("aiCoding.panel.allow", "Allow") }}
        </button>
      </div>

      <!-- Allow & remember (full-width purple) -->
      <button
        type="button"
        class="cc-perm-card__btn cc-perm-card__btn--remember"
        data-testid="cc-permission-remember"
        :title="t('aiCoding.panel.permissionAllowRememberTitle', 'Allow this tool call and auto-allow subsequent calls of the same tool in this session')"
        @click="emit('rememberApprove')"
      >
        🔓 {{ t("aiCoding.panel.allowAndRemember", "Allow & Remember (this session)") }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* V1 parity: modal overlay covering the cc-panel surface (AiCodingPanel.js:1410-1413).
   In V2 we render this inside ChatViewClaudeCode/OpenCode message area; the
   parent already constrains height, so absolute fill with blur works the
   same as V1. */
.cc-perm-overlay {
  position: absolute;
  inset: 0;
  background: rgba(10, 18, 28, 0.92);
  z-index: 100;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 16px;
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
}
.cc-perm-card {
  background: var(--bg-secondary, #0f1923);
  border: 1px solid rgba(251, 191, 36, 0.4);
  border-radius: 10px;
  padding: 16px;
  width: 100%;
  max-width: 340px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.cc-perm-card__head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.cc-perm-card__title {
  font-size: var(--text-base, 14px);
  font-weight: 700;
  color: #fbbf24;
}
.cc-perm-card__subtitle {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
}
.cc-perm-card__tool {
  background: rgba(251, 191, 36, 0.08);
  border: 1px solid rgba(251, 191, 36, 0.2);
  border-radius: 6px;
  padding: 8px 10px;
}
.cc-perm-card__label {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
  margin-bottom: 3px;
}
.cc-perm-card__tool-name {
  font-size: var(--text-base, 14px);
  font-weight: 600;
  color: #fbbf24;
  display: flex;
  align-items: center;
  gap: 6px;
}
.cc-perm-card__args {
  background: var(--bg-code);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  max-height: 120px;
  overflow-y: auto;
}
.cc-perm-card__arg-row {
  font-size: var(--text-xs, 11px);
  margin-bottom: 3px;
  word-break: break-all;
}
.cc-perm-card__arg-key {
  color: var(--text-muted, #9b97c4);
}
.cc-perm-card__arg-val {
  color: var(--text-secondary, #b8b4e0);
}
.cc-perm-card__no-args {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
}
.cc-perm-card__timeout {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
  text-align: center;
}
.cc-perm-card__row2 {
  display: flex;
  gap: 8px;
}
.cc-perm-card__btn {
  flex: 1;
  font-size: var(--text-sm, 13px);
  padding: 6px 0;
  border-radius: 6px;
  cursor: pointer;
  font-weight: 600;
  border: 1px solid transparent;
  text-align: center;
}
.cc-perm-card__btn--deny {
  background: #f87171;
  border-color: #f87171;
  color: #fff;
}
.cc-perm-card__btn--allow {
  background: #4ade80;
  border-color: #4ade80;
  color: #0f1923;
}
.cc-perm-card__btn--remember {
  flex: none;
  width: 100%;
  background: rgba(167, 139, 250, 0.15);
  border: 1px solid rgba(167, 139, 250, 0.4);
  color: #a78bfa;
  font-size: var(--text-xs, 11px);
  font-weight: 500;
  padding: 5px 0;
}
</style>
