<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChatViewOpenCode — Open Code working surface, V1-faithful layout
 * (功能块 7 + 会话5 视觉对齐).
 *
 * Mirrors ChatViewClaudeCode: unified chat界面 (welcome screen + avatar
 * bubbles + input row) plus a floating session-management panel
 * (CodingSessionPanel kind="oc") at the lower-right — NOT a整面替换.
 *
 * OC differences kept here (spec block7 §差异点):
 *   - Model selection dropdown in the composer toolbar (`/api/oc/health`
 *     models → `PUT /api/oc/config`).
 *   - Per-user-message "⤺ Revert to here" button.
 *   - No approval frames (permission dialog kept for symmetry, auto-hides).
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useOpenCode } from "@/composables/useOpenCode";
import { renderMarkdown } from "@/composables/markdown";
import CodingToolCallCard from "@/components/ai-coding/CodingToolCallCard.vue";
import CodingPermissionDialog from "@/components/ai-coding/CodingPermissionDialog.vue";
import CodingSessionPanel from "@/components/ai-coding/CodingSessionPanel.vue";
import CodingProgressIndicator from "@/components/ai-coding/CodingProgressIndicator.vue";
import CodingQueueList from "@/components/ai-coding/CodingQueueList.vue";
import ChatComposer from "@/components/chat/ChatComposer.vue";

const { t } = useI18n();
const oc = useOpenCode();

const messages = computed(() => oc.messages.value);
const pendingPermission = computed(() => oc.pendingPermission.value);
const panelOpen = computed(() => oc.panelOpen.value);
const activeProgress = computed(() => oc.activeProgress.value);
const queueItems = computed(() =>
  oc.queue.value.filter((q) => q.sessionId === oc.activeSessionId.value),
);

const showWelcome = computed(
  () => messages.value.length === 0 && !oc.streaming.value,
);

// V1 parity: welcome chips reuse the shared `chat.welcomeChip{1,2,3}{Label,Prompt}`
// i18n keys (3 model-comparison flows). Mirrors ChatViewClaudeCode.
const welcomeChips = computed(() => [
  { icon: "🖼️", label: t("chat.welcomeChip1Label"), prompt: t("chat.welcomeChip1Prompt") },
  { icon: "🎯", label: t("chat.welcomeChip2Label"), prompt: t("chat.welcomeChip2Prompt") },
  { icon: "🧠", label: t("chat.welcomeChip3Label"), prompt: t("chat.welcomeChip3Prompt") },
  { icon: "🎙️", label: t("chat.welcomeChip4Label"), prompt: t("chat.welcomeChip4Prompt") },
  { icon: "🤖", label: t("chat.welcomeChip5Label"), prompt: t("chat.welcomeChip5Prompt") },
  { icon: "🔊", label: t("chat.welcomeChip6Label"), prompt: t("chat.welcomeChip6Prompt") },
]);

onMounted(() => {
  void oc.fetchSessions();
  void oc.fetchCurrentModel();
  void oc.fetchHealth();
});

const messagesContainer = ref<HTMLElement | null>(null);
watch(
  messages,
  () => {
    queueMicrotask(() => {
      const el = messagesContainer.value;
      if (el !== null) el.scrollTop = el.scrollHeight;
    });
  },
  { deep: true },
);

function onChipClick(prompt: string): void {
  if (oc.activeSessionId.value === null) {
    oc.togglePanel();
    return;
  }
  void oc.sendMessage(prompt);
}

async function onInterrupt(): Promise<void> {
  await oc.interrupt();
}

function onRemoveQueued(id: string): void {
  oc.removeFromQueue(id);
}

async function onRevert(messageId: string): Promise<void> {
  const id = oc.activeSessionId.value;
  if (id === null) return;
  await oc.revert(id, messageId);
}

// Per-message copy button (V1 index.html:544 `copyMessage`). OC messages
// render in this view's own bucket (context isolation), so replicate the main
// chat's copy affordance here. Transient ✓ feedback mirrors the main chat.
const copiedMsgId = ref<string | null>(null);
async function onCopyMessage(id: string, content: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(content);
    copiedMsgId.value = id;
    window.setTimeout(() => {
      if (copiedMsgId.value === id) copiedMsgId.value = null;
    }, 1500);
  } catch {
    /* clipboard denied — silent best-effort copy */
  }
}

// ── Permission approval (auto-hidden for OC; kept for symmetry) ────────────────
async function onApprove(): Promise<void> {
  const p = pendingPermission.value;
  if (p === null) return;
  await oc.decidePermission(p.request_id, p.sessionId, "approved");
}
async function onReject(): Promise<void> {
  const p = pendingPermission.value;
  if (p === null) return;
  await oc.decidePermission(p.request_id, p.sessionId, "rejected");
}
async function onRememberApprove(): Promise<void> {
  const p = pendingPermission.value;
  if (p === null) return;
  // V1 parity (AiCodingPanel.js:583-598). OC currently never emits
  // permission_request frames, so this path is exercised mainly in symmetry
  // tests, but the wire shape is forwarded for forward-compat.
  const updatedPermissions = [
    {
      type: "addRules",
      rules: [{ tool_name: p.tool, rule_content: "allow" }],
      behavior: "allow",
      destination: "session",
    },
  ];
  await oc.decidePermission(p.request_id, p.sessionId, "approved", updatedPermissions);
}
</script>

<template>
  <div
    class="coding-surface"
    data-testid="chat-view-oc"
  >
    <div
      ref="messagesContainer"
      class="messages-container"
      data-testid="oc-messages"
    >
      <div
        v-if="showWelcome"
        class="welcome-screen"
        data-testid="oc-welcome"
      >
        <div class="welcome-icon">
          <svg
            class="welcome-logo-glyph"
            viewBox="0 0 112 112"
            fill="none"
            aria-hidden="true"
          >
            <defs>
              <linearGradient id="wl-brand-grad" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stop-color="#7c6cff"/>
                <stop offset="100%" stop-color="#60a5fa"/>
              </linearGradient>
            </defs>
            <rect
              class="welcome-logo-tile"
              x="4" y="4" width="104" height="104" rx="24"
            />
            <g class="welcome-logo-frame">
              <line x1="56" y1="22" x2="34" y2="38"/>
              <line x1="56" y1="22" x2="78" y2="38"/>
              <line x1="34" y1="38" x2="22" y2="56"/>
              <line x1="78" y1="38" x2="90" y2="56"/>
              <line x1="22" y1="56" x2="34" y2="74"/>
              <line x1="90" y1="56" x2="78" y2="74"/>
              <line x1="34" y1="74" x2="56" y2="90"/>
              <line x1="78" y1="74" x2="56" y2="90"/>
              <line x1="34" y1="38" x2="56" y2="56"/>
              <line x1="78" y1="38" x2="56" y2="56"/>
              <line x1="22" y1="56" x2="56" y2="56"/>
              <line x1="90" y1="56" x2="56" y2="56"/>
              <line x1="34" y1="74" x2="56" y2="56"/>
              <line x1="78" y1="74" x2="56" y2="56"/>
            </g>
            <g class="welcome-logo-pulse">
              <rect x="44" y="44" width="24" height="24" rx="4" stroke-linecap="round"/>
              <line x1="50" y1="44" x2="50" y2="39"/>
              <line x1="56" y1="44" x2="56" y2="39"/>
              <line x1="62" y1="44" x2="62" y2="39"/>
              <line x1="50" y1="68" x2="50" y2="73"/>
              <line x1="56" y1="68" x2="56" y2="73"/>
              <line x1="62" y1="68" x2="62" y2="73"/>
              <line x1="44" y1="50" x2="39" y2="50"/>
              <line x1="44" y1="56" x2="39" y2="56"/>
              <line x1="44" y1="62" x2="39" y2="62"/>
              <line x1="68" y1="50" x2="73" y2="50"/>
              <line x1="68" y1="56" x2="73" y2="56"/>
              <line x1="68" y1="62" x2="73" y2="62"/>
              <line x1="49" y1="52" x2="56" y2="52"/>
              <line x1="56" y1="52" x2="56" y2="60"/>
              <line x1="56" y1="60" x2="63" y2="60"/>
            </g>
            <g class="welcome-logo-nodes">
              <circle cx="56" cy="22" r="4.5"/>
              <circle cx="34" cy="38" r="3.5"/>
              <circle cx="78" cy="38" r="3.5"/>
              <circle cx="22" cy="56" r="3"/>
              <circle cx="90" cy="56" r="3"/>
              <circle cx="34" cy="74" r="3.5"/>
              <circle cx="78" cy="74" r="3.5"/>
              <circle cx="56" cy="90" r="4.5"/>
            </g>
          </svg>
        </div>
        <div class="welcome-title">
          {{ t("chat.welcomeTitle") }}
        </div>
        <div class="welcome-subtitle">
          {{ t("chat.welcomeSubtitle") }}
        </div>
        <div class="welcome-chips">
          <button
            v-for="chip in welcomeChips"
            :key="chip.label"
            type="button"
            class="welcome-chip"
            @click="onChipClick(chip.prompt)"
          >
            {{ chip.icon }} {{ chip.label }}
          </button>
        </div>
      </div>

      <template v-else>
        <div
          v-for="msg in messages"
          :key="msg.id"
          :class="['message-row', msg.role === 'user' ? 'user' : 'ai']"
          :data-testid="`oc-message-${msg.role}`"
        >
          <div class="message-avatar">
            {{ msg.role === "user" ? "👤" : "🔷" }}
          </div>
          <div class="message-content-wrap">
            <div class="message-meta">
              <span class="message-role">{{ msg.role === "user" ? t("chat.you", "You") : "Open Code" }}</span>
              <!-- Per-message copy button (V1 index.html:544 `copyMessage`).
                   OC messages render in this view's own bucket, so replicate
                   the main chat's copy affordance here. -->
              <button
                v-if="msg.content !== ''"
                type="button"
                class="btn btn-icon oc-copy-btn"
                :title="copiedMsgId === msg.id ? t('openCode.copied') : t('openCode.copy')"
                :data-testid="`oc-copy-${msg.id}`"
                @click="onCopyMessage(msg.id, msg.content)"
              >
                {{ copiedMsgId === msg.id ? "✓" : "⧉" }}
              </button>
            </div>
            <div
              v-if="msg.role === 'user'"
              class="message-bubble"
            >
              {{ msg.content }}
            </div>
            <!-- eslint-disable vue/no-v-html -->
            <div
              v-else-if="msg.content !== ''"
              class="message-bubble cc-markdown"
              v-html="renderMarkdown(msg.content)"
            ></div>
            <!-- eslint-enable vue/no-v-html -->

            <div
              v-if="msg.toolCalls && msg.toolCalls.length > 0"
              class="cc-message__tools"
            >
              <CodingToolCallCard
                v-for="tc in msg.toolCalls"
                :key="tc.id"
                :call="tc"
              />
            </div>

            <!-- V1 token-badge (index.html:728-732, OC parity): assistant turn
                 footer with `📊 input↑ output↓ {duration}s`. OC's
                 context_size REST surfaces per-turn deltas (last_input_tokens
                 / total_output_tokens), so this badge is real on OC even
                 before a CC TokenCounterPort lands. Suppressed while
                 streaming so it appears as a single final summary. -->
            <div
              v-if="msg.role === 'assistant' && !msg.isStreaming && (msg.usage !== undefined || msg.durationS !== undefined)"
              class="token-badge oc-token-badge"
              data-testid="oc-token-badge"
            >
              <span class="token-icon">📊</span>
              <span v-if="msg.usage?.inputTokens !== undefined">{{ msg.usage.inputTokens }}↑</span>
              <span v-if="msg.usage?.outputTokens !== undefined">{{ msg.usage.outputTokens }}↓</span>
              <span v-if="msg.durationS !== undefined">⏱ {{ msg.durationS }}s</span>
            </div>

            <!-- OC-specific: revert anchor on user messages -->
            <button
              v-if="msg.role === 'user'"
              type="button"
              class="oc-revert-btn"
              :data-testid="`oc-revert-${msg.id}`"
              :title="t('openCode.revertToHere', 'Revert to here')"
              @click="onRevert(msg.id)"
            >
              ⤺ {{ t("openCode.revertToHere", "Revert to here") }}
            </button>

            <span
              v-if="msg.isStreaming"
              class="cc-message__cursor"
              data-testid="oc-streaming-cursor"
            >▋</span>
          </div>
        </div>

        <CodingPermissionDialog
          v-if="pendingPermission !== null"
          :request="pendingPermission"
          @approve="onApprove"
          @reject="onReject"
          @remember-approve="onRememberApprove"
        />
      </template>
    </div>

    <CodingSessionPanel
      v-if="panelOpen"
      kind="oc"
      class="coding-surface__panel"
    />

    <!-- Pending-message queue (shared core ccQueue) -->
    <CodingQueueList
      :items="queueItems"
      @remove="onRemoveQueued"
    />

    <!-- Live progress indicator (V1 AiCodingPanel.js:1095-1121).
         V1 parity (AiCodingPanel.js:144-157): only show when *this* session
         is the one currently streaming. -->
    <CodingProgressIndicator
      v-if="activeProgress !== null && oc.streaming.value && oc.streamingSessionId.value === oc.activeSessionId.value"
      :progress="activeProgress"
      @interrupt="onInterrupt"
    />

    <!-- Bottom composer: reuse the main chat composer (V1 叠加式 parity —
         OC mode keeps the full input-toolbar + OC pill; the OC model is
         selected through the shared model selector / per-session config).
         The composer routes Send/Stop to the active OC session. -->
    <ChatComposer />
  </div>
</template>

<style scoped>
.coding-surface {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 100%;
  min-height: 0;
  gap: var(--space-3, 12px);
  padding: var(--space-4, 16px);
}
.messages-container {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-4, 16px);
  padding: var(--space-5, 20px);
}
.message-role {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
  font-weight: 600;
}
.cc-message__tools {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}
.cc-message__cursor {
  display: inline-block;
  animation: cc-blink 1s step-start infinite;
  color: var(--oc-accent, #63b3ed);
}
@keyframes cc-blink {
  50% { opacity: 0; }
}
.oc-revert-btn {
  align-self: flex-end;
  margin-top: 4px;
  border: none;
  background: transparent;
  color: var(--text-muted, #9b97c4);
  cursor: pointer;
  font-size: var(--text-xs, 11px);
}
.oc-revert-btn:hover {
  color: var(--oc-accent, #63b3ed);
}
.oc-token-badge {
  margin-top: 4px;
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
  display: inline-flex;
  gap: 8px;
  align-items: center;
}
.coding-surface__panel {
  bottom: 152px;
  right: var(--space-4, 16px);
}
.cc-markdown :deep(pre) {
  background: var(--bg-primary, #0f0d24);
  padding: 8px;
  border-radius: 4px;
  overflow-x: auto;
}
.cc-markdown :deep(code) {
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs, 12px);
}
.cc-markdown :deep(p) { margin: 4px 0; }
.cc-markdown :deep(a) { color: var(--accent, #a594ff); }
</style>
