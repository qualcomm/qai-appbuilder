<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChatViewClaudeCode — Claude Code working surface, V1-faithful layout
 * (功能块 6 + 会话5 视觉对齐).
 *
 * V1 parity (frontend/index.html + js/ai-coding/{useClaudeCode,AiCodingPanel}.js):
 * CC mode does NOT replace the whole chat surface with a bespoke sidebar
 * layout. Instead it keeps the **unified chat界面** — the same
 * `.messages-container` welcome screen + `.message-row` avatar bubbles + a
 * familiar input row — and overlays a floating session-management panel
 * (`.cc-panel-float`, CodingSessionPanel) at the lower-right.
 *
 * Messages still come from the per-session `useCodingSession` bucket (V2
 * keeps CC streams isolated from the normal chatTabs store), but they are
 * now rendered with the **shared V1 visual language** (avatar + bubble +
 * meta) so the look matches the rest of Chat exactly.
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useClaudeCode } from "@/composables/useClaudeCode";
import { renderMarkdown } from "@/composables/markdown";
import { useConfirm } from "@/composables/useConfirm";
import CodingToolCallCard from "@/components/ai-coding/CodingToolCallCard.vue";
import CodingSubTaskList from "@/components/ai-coding/CodingSubTaskList.vue";
import CodingPermissionDialog from "@/components/ai-coding/CodingPermissionDialog.vue";
import CodingSessionPanel from "@/components/ai-coding/CodingSessionPanel.vue";
import CodingProgressIndicator from "@/components/ai-coding/CodingProgressIndicator.vue";
import CodingQueueList from "@/components/ai-coding/CodingQueueList.vue";
import ChatComposer from "@/components/chat/ChatComposer.vue";

const { t } = useI18n();
const cc = useClaudeCode();
const { confirm } = useConfirm();

// V1 parity (AiCodingPanel.js:639-645): effort options are handled inside
// ChatComposer's input-toolbar dropdown — no separate effortOptions array needed here.

const messages = computed(() => cc.messages.value);
const activeSession = computed(() => cc.activeSession.value);
const pendingPermission = computed(() => cc.pendingPermission.value);
const panelOpen = computed(() => cc.panelOpen.value);
const activeProgress = computed(() => cc.activeProgress.value);
const queueItems = computed(() =>
  cc.queue.value.filter((q) => q.sessionId === cc.activeSessionId.value),
);

const showWelcome = computed(
  () => messages.value.length === 0 && !cc.streaming.value,
);

// Per-message copy button (V1 index.html:544 `copyMessage`). CC messages are
// rendered in this view's own bucket (context isolation), so the shared
// ChatMessageList copy affordance does not apply automatically — replicate it
// here so a CC assistant/user reply can be copied just like in the main chat.
// Transient ✓ feedback mirrors the main chat's `copiedId` pattern.
const copiedMsgId = ref<string | null>(null);
async function onCopyMessage(id: string, content: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(content);
    copiedMsgId.value = id;
    window.setTimeout(() => {
      if (copiedMsgId.value === id) copiedMsgId.value = null;
    }, 1500);
  } catch {
    /* clipboard denied — silent, matches main chat best-effort copy */
  }
}

// V1 parity: welcome chips reuse the shared `chat.welcomeChip{1,2,3}{Label,Prompt}`
// i18n keys (3 model-comparison flows). The visible label is i18n-driven; the
// click action sends the localized prompt to the active session.
const welcomeChips = computed(() => [
  { icon: "🖼️", label: t("chat.welcomeChip1Label"), prompt: t("chat.welcomeChip1Prompt") },
  { icon: "🎯", label: t("chat.welcomeChip2Label"), prompt: t("chat.welcomeChip2Prompt") },
  { icon: "🧠", label: t("chat.welcomeChip3Label"), prompt: t("chat.welcomeChip3Prompt") },
  { icon: "🎙️", label: t("chat.welcomeChip4Label"), prompt: t("chat.welcomeChip4Prompt") },
  { icon: "🤖", label: t("chat.welcomeChip5Label"), prompt: t("chat.welcomeChip5Prompt") },
  { icon: "🔊", label: t("chat.welcomeChip6Label"), prompt: t("chat.welcomeChip6Prompt") },
]);

onMounted(() => {
  void cc.fetchSessions();
  void cc.fetchCurrentModel();
});

// Auto-scroll on new messages.
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
  if (cc.activeSessionId.value === null) {
    cc.togglePanel();
    return;
  }
  void cc.sendMessage(prompt);
}

async function onInterrupt(): Promise<void> {
  await cc.interrupt();
}

/**
 * V1 parity (AiCodingPanel.js:684-698 handleQuickNewSession): inherit the
 * current session's workspace + auto-increment a `#N` suffix on the name.
 * The composable handles the full state machine; this view just routes the
 * click and ensures the panel is visible afterwards (V1 also opens the panel).
 */
async function onQuickNewSession(): Promise<void> {
  const created = await cc.quickNewSession();
  if (created !== null && !cc.panelOpen.value) {
    cc.togglePanel();
  }
}

function onRemoveQueued(id: string): void {
  cc.removeFromQueue(id);
}

// ── Permission approval handlers ──────────────────────────────────────────────
async function onApprove(): Promise<void> {
  const p = pendingPermission.value;
  if (p === null) return;
  await cc.decidePermission(p.request_id, p.sessionId, "approved");
}
async function onReject(): Promise<void> {
  const p = pendingPermission.value;
  if (p === null) return;
  await cc.decidePermission(p.request_id, p.sessionId, "rejected");
}
async function onRememberApprove(): Promise<void> {
  const p = pendingPermission.value;
  if (p === null) return;
  // V1 parity (AiCodingPanel.js:583-598): build `updated_permissions` with
  // an `addRules` mutation so subsequent calls of the same tool inside this
  // session are auto-approved. `destination: "session"` matches V1; backend
  // wire support depends on SDK availability — when unsupported the call
  // still records the standard approve (待 SDK 验证 wire format).
  const updatedPermissions = [
    {
      type: "addRules",
      rules: [{ tool_name: p.tool, rule_content: "allow" }],
      behavior: "allow",
      destination: "session",
    },
  ];
  await cc.decidePermission(p.request_id, p.sessionId, "approved", updatedPermissions);
}

// ── Per-row rewind (V1 index.html:532-543: ⏪ button on user rows) ─────────────
//
// V2 keys rewind off `checkpointId` instead of V1's SDK `sdkUuid`. The
// checkpoint is created in `useCodingSession.sendMessage` step 1b after
// the user message is posted; rows where the create failed (e.g. file
// checkpointing disabled on the session) keep `checkpointId === undefined`
// and the button stays hidden.
const rewindingMsgId = ref<string | null>(null);

async function onRewind(msg: { id: string; checkpointId?: string }): Promise<void> {
  const sid = cc.activeSessionId.value;
  if (sid === null || msg.checkpointId === undefined) return;
  const ok = await confirm({
    icon: "⏪",
    title: t("aiCoding.panel.rewindLabel"),
    // CC SDK file checkpoint/rewind (2-H3): a `checkpointId` only exists when
    // file checkpointing was enabled for the session, so the SDK backend can
    // restore the on-disk files (V1 `session_manager.py:2604-2706`). The
    // confirm text reflects that files will be rolled back; the post-action
    // toast then reports the actual outcome (files restored vs messages only)
    // from the response `files_rewound` flag.
    message:
      t("aiCoding.panel.rewindWithUuidTitle", { uuid: msg.checkpointId }) +
      "\n\n" +
      t("aiCoding.panel.rewindFilesNotice"),
    confirmText: t("aiCoding.panel.rewindLabel"),
    cancelText: t("chat.cancel", "Cancel"),
    confirmStyle: "primary",
  });
  if (!ok) return;
  rewindingMsgId.value = msg.id;
  try {
    await cc.rewindFiles(sid, msg.checkpointId);
  } finally {
    rewindingMsgId.value = null;
  }
}

// Friendly short session title (V1 cc-session-badge: name || sessionId.slice(0,8)).
const sessionShortTitle = computed<string>(() => {
  const s = activeSession.value;
  if (s === null) return "";
  if (s.title !== null && s.title !== "") return s.title;
  return s.session_id.slice(0, 8);
});
</script>

<template>
  <div
    class="coding-surface"
    data-testid="chat-view-cc"
  >
    <!-- Quick-new same-directory session (V1 AiCodingPanel.js:847-854 "+" button).
         Only shown when an active CC session exists; clicking inherits the
         workspace and auto-increments a `#N` suffix on the session name. -->
    <div
      v-if="activeSession !== null"
      class="cc-quick-actions"
    >
      <button
        type="button"
        class="cc-quick-new"
        :title="t('aiCoding.panel.quickNewTitle')"
        data-testid="cc-quick-new"
        @click="onQuickNewSession"
      >
        ＋
      </button>
    </div>
    <!-- Unified chat message area (V1 .messages-container) -->
    <div
      ref="messagesContainer"
      class="messages-container"
      data-testid="cc-messages"
    >
      <!-- Welcome screen (shared with normal chat) -->
      <div
        v-if="showWelcome"
        class="welcome-screen"
        data-testid="cc-welcome"
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

      <!-- Message rows (V1 avatar + bubble) -->
      <template v-else>
        <div
          v-for="msg in messages"
          :key="msg.id"
          :class="['message-row', msg.role === 'user' ? 'user' : 'ai']"
          :data-testid="`cc-message-${msg.role}`"
        >
          <div class="message-avatar">
            {{ msg.role === "user" ? "👤" : "🤖" }}
          </div>
          <div class="message-content-wrap">
            <div class="message-meta">
              <!-- V1 cc-session-badge: assistant rows show "🤖 Claude Code"
                   + a session-name pill so the user can tell which CC
                   session a turn belongs to. User rows reuse the V1 "You"
                   label (see index.html:505-515). -->
              <span
                v-if="msg.role === 'assistant'"
                class="message-role"
              >
                🤖 Claude Code
                <span
                  v-if="sessionShortTitle !== ''"
                  class="cc-session-badge"
                  data-testid="cc-session-badge"
                >{{ sessionShortTitle }}</span>
              </span>
              <span
                v-else
                class="message-role"
              >{{ t("chat.you", "You") }}</span>
              <!-- Per-row ⏪ rewind button (V1 index.html:532-543).
                   Visible only on user rows whose POST checkpoint
                   succeeded — i.e. file checkpointing is enabled and
                   the row carries a checkpoint_id. -->
              <button
                v-if="msg.role === 'user' && msg.checkpointId !== undefined"
                type="button"
                class="cc-rewind-btn"
                :disabled="rewindingMsgId === msg.id"
                :title="t('aiCoding.panel.rewindWithUuidTitle', { uuid: msg.checkpointId })"
                :data-testid="`cc-rewind-${msg.id}`"
                @click="onRewind(msg)"
              >
                <span class="cc-rewind-icon">⏪</span>
                <span>{{ t("aiCoding.panel.rewindLabel") }}</span>
              </button>
              <!-- Per-message copy button (V1 index.html:544 `copyMessage`).
                   Reuses the global `.btn .btn-icon` sizing like the main
                   chat's copy affordance; transient ✓ feedback. Shown for any
                   message that carries text content. -->
              <button
                v-if="msg.content !== ''"
                type="button"
                class="btn btn-icon cc-copy-btn"
                :title="copiedMsgId === msg.id ? t('aiCoding.copied') : t('aiCoding.copy')"
                :data-testid="`cc-copy-${msg.id}`"
                @click="onCopyMessage(msg.id, msg.content)"
              >
                {{ copiedMsgId === msg.id ? "✓" : "⧉" }}
              </button>
            </div>
            <!-- User: plain text; assistant/system: markdown -->
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

            <!-- Tool-call cards -->
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

            <!-- Task/Agent sub-task cards (V1 index.html:588-628). Rendered
                 from task_started/progress/notification frames; appears when
                 an upstream / harness injects Task Agent events. -->
            <CodingSubTaskList
              v-if="msg.subTasks && msg.subTasks.length > 0"
              :sub-tasks="msg.subTasks"
            />

            <!-- V1 token-badge (index.html:728-732): assistant turn
                 footer with `📊 input↑ output↓ {duration}s`. Token
                 counters stay 0 until a TokenCounterPort adapter is
                 wired (see useCodingSession.refreshContextUsage docstring,
                 待 SDK); duration_s reflects the wall-clock turn time
                 captured in onDone. The badge is suppressed while
                 streaming so it appears as a single final summary. -->
            <div
              v-if="msg.role === 'assistant' && !msg.isStreaming && (msg.usage !== undefined || msg.durationS !== undefined)"
              class="token-badge cc-token-badge"
              data-testid="cc-token-badge"
            >
              <span class="token-icon">🔢</span>
              <span v-if="msg.usage?.inputTokens !== undefined">{{ msg.usage.inputTokens }}↑</span>
              <span v-if="msg.usage?.outputTokens !== undefined">{{ msg.usage.outputTokens }}↓</span>
              <span v-if="msg.durationS !== undefined">⏱ {{ msg.durationS }}s</span>
            </div>

            <span
              v-if="msg.isStreaming"
              class="cc-message__cursor"
              data-testid="cc-streaming-cursor"
            >▋</span>
          </div>
        </div>

        <!-- Real-time permission approval -->
        <CodingPermissionDialog
          v-if="pendingPermission !== null"
          :request="pendingPermission"
          @approve="onApprove"
          @reject="onReject"
          @remember-approve="onRememberApprove"
        />
      </template>
    </div>

    <!-- Floating session-management panel (V1 .cc-panel-float) -->
    <CodingSessionPanel
      v-if="panelOpen"
      kind="cc"
      class="coding-surface__panel"
    />

    <!-- Pending-message queue (V1 ccQueue) -->
    <CodingQueueList
      :items="queueItems"
      @remove="onRemoveQueued"
    />

    <!-- Live progress indicator (V1 AiCodingPanel.js:1095-1121).
         V1 parity (AiCodingPanel.js:144-157): only show when *this* session
         is the one currently streaming — otherwise another session's stream
         would render its progress on the active session's surface. -->
    <CodingProgressIndicator
      v-if="activeProgress !== null && cc.streaming.value && cc.streamingSessionId.value === cc.activeSessionId.value"
      :progress="activeProgress"
      @interrupt="onInterrupt"
    />

    <!-- Bottom composer: reuse the main chat composer so CC mode keeps the
         full V1 input-toolbar (model selector / Params / voice / mode pills /
         Send) and the CC pill — V1 叠加式 parity (app.js: CC mode does NOT
         replace the composer). The composer routes Send/Stop to the active
         coding session (see ChatComposer onSubmit/onStop CC branch). -->
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

/* `.messages-container` / `.welcome-*` / `.message-*` come from chat.css —
 * we reuse them verbatim so CC matches the normal chat look exactly. */
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
  color: var(--accent, #a594ff);
}
@keyframes cc-blink {
  50% { opacity: 0; }
}

/* Per-row ⏪ rewind button (V1 index.html:532-543).
   Hover-revealed on user rows; uses the existing CC accent so it
   visually matches the cc-session-badge. */
.cc-rewind-btn {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 20px;
  margin-left: 6px;
  padding: 1px 5px;
  font-size: var(--text-xs, 11px);
  line-height: 1;
  color: var(--cc-accent, #a78bfa);
  background: transparent;
  border: 1px solid var(--cc-accent-border, rgba(167, 139, 250, 0.35));
  border-radius: 4px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s ease;
}
.message-row.user:hover .cc-rewind-btn,
.cc-rewind-btn:focus-visible {
  opacity: 1;
}
.cc-rewind-btn:disabled {
  cursor: wait;
  opacity: 0.5;
}
.cc-rewind-icon {
  font-size: var(--text-xs, 11px);
}

/* Per-turn token badge for the assistant row (V1 index.html:728-732). */
.cc-token-badge {
  margin-top: 4px;
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
  display: inline-flex;
  gap: 8px;
  align-items: center;
}

/* Floating panel sits above the composer at the lower-right. */
.coding-surface__panel {
  bottom: 132px;
  right: var(--space-4, 16px);
}

/* Effort selector bar (CC-only) is handled inside ChatComposer (input-toolbar).
 * No separate bar needed here. */

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

/* Quick-new same-directory session button (V1 AiCodingPanel.js:847-854).
   Sits at the top-right of the CC chat surface, visible only when an
   active CC session exists. */
.cc-quick-actions {
  position: absolute;
  top: var(--space-3, 12px);
  right: var(--space-4, 16px);
  z-index: 5;
  display: flex;
  gap: 4px;
}
.cc-quick-new {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-md, 16px);
  font-weight: 700;
  color: #4ade80;
  background: rgba(74, 222, 128, 0.08);
  border: 1px solid rgba(74, 222, 128, 0.4);
  border-radius: 4px;
  cursor: pointer;
}
.cc-quick-new:hover {
  background: rgba(74, 222, 128, 0.16);
}
</style>
