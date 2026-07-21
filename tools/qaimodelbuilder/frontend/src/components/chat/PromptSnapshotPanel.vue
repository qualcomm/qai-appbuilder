<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * PromptSnapshotPanel — the prompt-snapshot modal overlay (V1
 * `index.html:8403-12700` + `useForgeConfig.js:20-127`).
 *
 * Extracted from `ChatMessageList.vue` (F4 cohesion split). This is a pure
 * presentation component: all state + behaviour live in the
 * `usePromptSnapshot` composable, whose API is threaded in via the `api`
 * prop so the host (ChatMessageList) keeps owning the open trigger (the
 * per-message 📋 button) while this component renders the dialog.
 *
 * §3.9: custom overlay, NOT window.confirm/alert.
 */
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import type { UsePromptSnapshot } from "@/composables/chat/usePromptSnapshot";
import {
  parsePromptSnapshotTools,
  parsePromptSnapshotParams,
} from "@/composables/chat/usePromptSnapshot";
import { useFocusTrap } from "@/composables/useFocusTrap";

const props = defineProps<{ api: UsePromptSnapshot }>();
const { t } = useI18n();

const modal = props.api.promptSnapshotModal;

// Request-options block (tools / tool_choice / sampling / session_id) — the
// REAL non-message wire fields, captured per turn. Collapsed by default like
// system messages (it can be long when many tools are advertised).
const reqOptsCollapsed = ref(true);
const hasRequestOptions = computed(() => {
  const o = modal.value.requestOptions;
  return o !== null && o !== undefined && Object.keys(o).length > 0;
});
// Tools parsed into per-tool cards, preserving the WIRE ORDER the model
// actually receives (read / edit / write / … first — fixed by the backend
// TOOL_ORDER). Each card shows name + description + parameter names; the raw
// schema stays available via a per-card "raw" toggle (no info lost).
const toolViews = computed(() =>
  parsePromptSnapshotTools(modal.value.requestOptions?.tools),
);
const paramRows = computed(() =>
  parsePromptSnapshotParams(modal.value.requestOptions),
);
// Per-tool "show raw schema" toggles, keyed by index.
const toolRawOpen = ref<Record<number, boolean>>({});
function toggleToolRaw(idx: number): void {
  toolRawOpen.value[idx] = !toolRawOpen.value[idx];
}

// V1 parity (utils/focus-trap.js): Tab focus cycles inside the modal +
// opener focus restored on close. Esc-to-close is owned by the upstream
// composable's window-level keydown listener (usePromptSnapshot.ts:172),
// so we only need Tab cycling + opener restore here — no `onEscape`.
const dialogEl = ref<HTMLElement | null>(null);
const trapActive = computed(() => modal.value.visible);
useFocusTrap(dialogEl, { active: trapActive, focusFirst: true });
</script>

<template>
  <div
    v-if="modal.visible"
    class="prompt-modal-overlay"
    data-testid="prompt-snapshot-modal"
    role="dialog"
    aria-modal="true"
    @click.self="props.api.closePromptSnapshot"
  >
    <div
      ref="dialogEl"
      class="prompt-modal"
    >
      <div class="prompt-modal-header">
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          style="flex-shrink:0;color:var(--accent)"
        >
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line
            x1="16"
            y1="13"
            x2="8"
            y2="13"
          />
          <line
            x1="16"
            y1="17"
            x2="8"
            y2="17"
          />
          <polyline points="10 9 9 9 8 9" />
        </svg>
        <div class="prompt-modal-title">
          {{ t("promptSnapshot.title") }}
        </div>
        <div class="prompt-modal-meta">
          {{ modal.modelId || "—" }} · {{ modal.timeStr }}
        </div>
        <button
          class="btn btn-icon"
          style="margin-left:8px"
          :title="t('common.copy')"
          @click="props.api.copyPromptSnapshot"
        >
          <svg
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          ><rect
            x="9"
            y="9"
            width="13"
            height="13"
            rx="2"
            ry="2"
          /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
        </button>
        <button
          class="btn btn-icon"
          style="margin-left:4px"
          :title="t('common.close')"
          @click="props.api.closePromptSnapshot"
        >
          <svg
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2.5"
            stroke-linecap="round"
          ><line
            x1="18"
            y1="6"
            x2="6"
            y2="18"
          /><line
            x1="6"
            y1="6"
            x2="18"
            y2="18"
          /></svg>
        </button>
      </div>
      <div class="prompt-modal-body">
        <div
          v-if="modal.loading"
          style="display:flex;justify-content:center;padding:40px"
        >
          <div
            class="spinner"
            style="width:28px;height:28px;border-width:3px"
          />
        </div>
        <div
          v-else-if="modal.error"
          style="color:var(--error,#f87171);padding:16px;text-align:center"
        >
          {{ modal.error }}
        </div>
        <template v-else>
          <div
            v-if="modal.snapshotError"
            style="background:#f59e0b18;border:1px solid #f59e0b44;border-radius:6px;padding:8px 12px;font-size:var(--text-sm);color:#fbbf24;margin-bottom:4px"
          >
            ⚠️ {{ t("index.promptSnapshotBuildError") }}{{ modal.snapshotError }}
          </div>
          <div
            v-for="(pmsg, idx) in modal.messages"
            :key="idx"
            :class="['prompt-msg-block', { expanded: !modal.collapsed[idx] }]"
          >
            <div
              class="prompt-msg-header"
              @click="props.api.togglePromptMsg(idx)"
            >
              <span :class="['prompt-role-badge', pmsg.role]">{{ pmsg.role }}</span>
              <span style="font-size:var(--text-xs);color:var(--text-muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:0 8px">{{ props.api.promptMsgPreview(pmsg) }}</span>
              <!-- Per-message ordinal (V1 index.html:236): "#1" / "#2" … -->
              <span class="prompt-msg-index">#{{ idx + 1 }}</span>
              <!-- Per-message copy button (V1 useForgeConfig.js:129-139 /
                   index.html:238-242). Stop propagation so clicking copy does
                   not also toggle the collapse state. -->
              <button
                class="btn btn-icon prompt-msg-copy-btn"
                :title="t('index.copyThisMessage')"
                data-testid="prompt-msg-copy-btn"
                @click.stop="props.api.copyPromptMsg(idx)"
              >
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                ><rect
                  x="9"
                  y="9"
                  width="13"
                  height="13"
                  rx="2"
                  ry="2"
                /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
              </button>
              <span :class="['prompt-collapse-arrow', { open: !modal.collapsed[idx] }]">▼</span>
            </div>
            <div
              v-show="!modal.collapsed[idx]"
              class="prompt-msg-content"
            >
              <pre>{{ props.api.promptMsgText(pmsg) }}</pre>
            </div>
          </div>
          <!-- Request options (tools / tool_choice / sampling / session_id):
               the REAL non-message wire fields sent to the model. Rendered as
               a dedicated collapsible block so the dialog shows the COMPLETE
               request, not just the messages. Only shown when captured. -->
          <div
            v-if="hasRequestOptions"
            :class="['prompt-msg-block', { expanded: !reqOptsCollapsed }]"
            data-testid="prompt-request-options"
          >
            <div
              class="prompt-msg-header"
              @click="reqOptsCollapsed = !reqOptsCollapsed"
            >
              <span class="prompt-role-badge request">{{ t("promptSnapshot.requestOptions") }}</span>
              <span style="font-size:var(--text-xs);color:var(--text-muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:0 8px">{{ t("promptSnapshot.requestOptionsHint") }}</span>
              <span :class="['prompt-collapse-arrow', { open: !reqOptsCollapsed }]">▼</span>
            </div>
            <div
              v-show="!reqOptsCollapsed"
              class="prompt-msg-content prompt-reqopts"
            >
              <!-- Tools: one card per tool, in the exact WIRE ORDER sent to
                   the model (read / edit / write / … first). Each card shows
                   name + description + parameter names; "raw" reveals the full
                   OpenAI schema (nothing hidden). -->
              <div
                v-if="toolViews.length > 0"
                class="reqopts-section"
              >
                <div class="reqopts-section-title">
                  {{ t("promptSnapshot.toolsSent", { n: toolViews.length }) }}
                </div>
                <div
                  v-for="(tv, ti) in toolViews"
                  :key="ti"
                  class="tool-card"
                  data-testid="prompt-tool-card"
                >
                  <div class="tool-card-head">
                    <span class="tool-card-ord">#{{ ti + 1 }}</span>
                    <span class="tool-card-name">{{ tv.name }}</span>
                    <button
                      class="btn btn-ghost btn-sm tool-card-raw-btn"
                      type="button"
                      @click="toggleToolRaw(ti)"
                    >
                      {{ toolRawOpen[ti] ? t("promptSnapshot.hideRaw") : t("promptSnapshot.showRaw") }}
                    </button>
                  </div>
                  <div
                    v-if="tv.description"
                    class="tool-card-desc"
                  >
                    {{ tv.description }}
                  </div>
                  <div
                    v-if="tv.params.length > 0"
                    class="tool-card-params"
                  >
                    <code
                      v-for="p in tv.params"
                      :key="p.name"
                      :class="['tool-param', { required: p.required }]"
                      :title="p.required ? t('promptSnapshot.paramRequired') : t('promptSnapshot.paramOptional')"
                    >{{ p.name }}{{ p.required ? "*" : "" }}</code>
                  </div>
                  <pre
                    v-show="toolRawOpen[ti]"
                    class="tool-card-raw"
                  >{{ tv.raw }}</pre>
                </div>
              </div>
              <!-- Sampling params / tool_choice / session_id as a key-value
                   table (compact, readable). -->
              <div
                v-if="paramRows.length > 0"
                class="reqopts-section"
              >
                <div class="reqopts-section-title">
                  {{ t("promptSnapshot.samplingParams") }}
                </div>
                <table class="reqopts-params">
                  <tbody>
                    <tr
                      v-for="row in paramRows"
                      :key="row.key"
                    >
                      <td class="reqopts-param-key">
                        {{ row.key }}
                      </td>
                      <td class="reqopts-param-val">
                        {{ row.value }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </template>
      </div>
      <div class="prompt-modal-footer">
        <span class="msg-count">{{ t("index.msgCountTotal", { n: modal.messages.length }) }}</span>
        <button
          class="btn btn-ghost btn-sm"
          @click="props.api.expandAllPromptMsgs"
        >
          {{ t("promptSnapshot.expandAll") }}
        </button>
        <button
          class="btn btn-ghost btn-sm"
          @click="props.api.collapseAllPromptMsgs"
        >
          {{ t("promptSnapshot.collapseAll") }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Per-message copy button: hidden by default, revealed on card hover
   (V1 parity — index.html message-actions opacity-on-hover). */
.prompt-msg-copy-btn {
  opacity: 0;
  margin: 0 4px;
  transition: opacity 0.12s ease;
}
.prompt-msg-block:hover .prompt-msg-copy-btn,
.prompt-msg-copy-btn:focus-visible {
  opacity: 1;
}

/* Request-options block: structured tool cards + sampling key/value table
   (readable alternative to a single JSON blob). Theme-variable driven. */
.prompt-reqopts {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.reqopts-section-title {
  font-size: var(--text-xs);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.tool-card {
  border: 1px solid var(--border, #ffffff1a);
  border-radius: var(--radius-sm, 6px);
  padding: 8px 10px;
  margin-bottom: 6px;
  background: var(--bg-subtle, #ffffff08);
}
.tool-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tool-card-ord {
  font-size: var(--text-xs);
  color: var(--text-muted);
  flex-shrink: 0;
}
.tool-card-name {
  font-weight: 700;
  font-family: var(--font-mono, monospace);
  color: var(--accent);
  flex: 1;
}
.tool-card-raw-btn {
  flex-shrink: 0;
}
.tool-card-desc {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-top: 4px;
  white-space: pre-wrap;
}
.tool-card-params {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  margin-top: 6px;
}
.tool-param {
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
  padding: 1px 6px;
  border-radius: var(--radius-xs, 4px);
  background: var(--bg-subtle, #ffffff10);
  border: 1px solid var(--border, #ffffff14);
  color: var(--text-secondary, #cbd5e1);
}
.tool-param.required {
  color: var(--accent);
  border-color: var(--accent);
}
.tool-card-raw {
  margin-top: 6px;
  font-size: var(--text-xs);
}
.reqopts-params {
  border-collapse: collapse;
  width: 100%;
}
.reqopts-param-key {
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
  color: var(--text-muted);
  padding: 2px 12px 2px 0;
  white-space: nowrap;
  vertical-align: top;
}
.reqopts-param-val {
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
  color: var(--text-secondary, #cbd5e1);
  word-break: break-all;
}
</style>
