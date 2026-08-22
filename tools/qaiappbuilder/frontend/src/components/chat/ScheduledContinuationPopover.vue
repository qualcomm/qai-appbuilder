<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ScheduledContinuationPopover — unattended "keep working" timer setup + list.
 *
 * Surfaced from a small alarm button in the composer's `.rit-right` row. Lets
 * the user, before walking away, schedule a periodic check that nudges the
 * model to continue a plan ONLY when it has stopped. Three continue strategies
 * (same-session / new-session / auto-by-context) are configured here; a list of
 * ALL timers across every tab is shown for management.
 *
 * UI contract mirrors the sibling composer popovers (SessionToolsPopover /
 * PromptHistoryPopover): `open` prop + `update:open` emit, capture-phase
 * outside-mousedown close, `role="dialog"` / `aria-modal="false"`, all colours
 * from design tokens (styles live in the global chat.css under `.sched-*`). No
 * native confirm/alert/prompt — "stop all" uses the global `useConfirm()`.
 */
import {
  computed,
  onMounted,
  onBeforeUnmount,
  ref,
  useTemplateRef,
  watch,
} from "vue";
import { useI18n } from "vue-i18n";

import { useChatTabsStore } from "@/stores/chatTabs";
import { useConfirm } from "@/composables/useConfirm";
import {
  useScheduledContinuation,
  type ScheduledContinuationMode,
  type ContextThresholdMode,
  type ScheduledContinuationJob,
} from "@/composables/chat/useScheduledContinuation";

interface Props {
  open: boolean;
}
const props = defineProps<Props>();
const emit = defineEmits<{
  "update:open": [value: boolean];
}>();

const { t } = useI18n();
const store = useChatTabsStore();
const { confirm } = useConfirm();
const {
  jobs,
  createForTab,
  updateJob,
  pauseJob,
  resumeJob,
  stopJob,
  stopAll,
  runNow,
  jobsForTab,
  defaultDraft,
} = useScheduledContinuation();

const popoverRef = useTemplateRef<HTMLDivElement>("popover");

// ── Draft form state (bound to the active tab) ──────────────────────────────
const intervalMinutes = ref<number>(10);
const prompt = ref<string>("");
const mode = ref<ScheduledContinuationMode>("same-session");
const thresholdMode = ref<ContextThresholdMode>("context-percent");
const fixedTokens = ref<number>(200000);
const percent = ref<number>(80);

const activeTabId = computed<string | null>(() => store.activeTab?.id ?? null);

// The job (if any) already bound to the active tab — drives "create" vs
// "update" of the form.
const activeTabJob = computed<ScheduledContinuationJob | null>(() => {
  const id = activeTabId.value;
  if (id === null) return null;
  return jobsForTab(id)[0] ?? null;
});

/** Load the form from the active tab's existing job, or reset to defaults. */
function loadForm(): void {
  const job = activeTabJob.value;
  if (job !== null) {
    intervalMinutes.value = job.intervalMinutes;
    prompt.value = job.prompt;
    mode.value = job.mode;
    thresholdMode.value = job.contextThreshold.mode;
    fixedTokens.value = job.contextThreshold.fixedTokens;
    percent.value = job.contextThreshold.percent;
    return;
  }
  const d = defaultDraft();
  intervalMinutes.value = d.intervalMinutes;
  prompt.value = d.prompt;
  mode.value = d.mode;
  thresholdMode.value = d.contextThreshold.mode;
  fixedTokens.value = d.contextThreshold.fixedTokens;
  percent.value = d.contextThreshold.percent;
}

watch(
  () => props.open,
  (next) => {
    if (next) loadForm();
  },
);

function draftFromForm() {
  return {
    intervalMinutes: intervalMinutes.value,
    prompt: prompt.value,
    mode: mode.value,
    contextThreshold: {
      mode: thresholdMode.value,
      fixedTokens: fixedTokens.value,
      percent: percent.value,
    },
  };
}

const canSave = computed(
  () => activeTabId.value !== null && intervalMinutes.value >= 1,
);

function onEnableOrUpdate(): void {
  const id = activeTabId.value;
  if (id === null) return;
  const existing = activeTabJob.value;
  if (existing !== null) {
    updateJob(existing.id, draftFromForm());
    if (!existing.enabled) resumeJob(existing.id);
  } else {
    createForTab(id, draftFromForm());
  }
}

function onStopActive(): void {
  const job = activeTabJob.value;
  if (job !== null) stopJob(job.id);
}

async function onStopAll(): Promise<void> {
  if (jobs.value.length === 0) return;
  const ok = await confirm({
    icon: "⏱️",
    title: t("chat.scheduler.confirmStopAllTitle", "停止所有定时器"),
    message: t(
      "chat.scheduler.confirmStopAllBody",
      "确定要停止并清除所有标签会话中的定时器吗？",
    ),
    confirmText: t("chat.scheduler.stopAll", "全部停止"),
    cancelText: t("common.cancel", "取消"),
    confirmStyle: "danger",
  });
  if (ok) stopAll();
}

function onClose(): void {
  emit("update:open", false);
}

// ── Timer list helpers ──────────────────────────────────────────────────────
function modeLabel(m: ScheduledContinuationMode): string {
  if (m === "same-session") return t("chat.scheduler.modeSame", "本会话继续");
  if (m === "new-session") return t("chat.scheduler.modeNew", "新会话继续");
  return t("chat.scheduler.modeAuto", "上下文超阈值时新会话");
}

function statusLabel(job: ScheduledContinuationJob): string {
  switch (job.status) {
    case "scheduled":
      return t("chat.scheduler.statusScheduled", "已计划");
    case "waiting_idle":
      return t("chat.scheduler.statusWaiting", "等待模型停止");
    case "handoff_pending":
      return t("chat.scheduler.statusHandoff", "生成接力提示词");
    case "creating_session":
      return t("chat.scheduler.statusCreating", "创建新会话");
    case "sent":
      return t("chat.scheduler.statusSent", "已发送");
    case "paused":
      return t("chat.scheduler.statusPaused", "已暂停");
    case "error":
      return t("chat.scheduler.statusError", "错误");
    default:
      return job.status;
  }
}

/** Minutes-from-now until the next check (>= 0). */
function minutesUntilNext(job: ScheduledContinuationJob): number {
  return Math.max(0, Math.round((job.nextRunAt - Date.now()) / 60_000));
}

function tabTitle(job: ScheduledContinuationJob): string {
  const tab = store.tabById(job.tabId);
  const title = (tab?.title ?? job.title).trim();
  return title === "" ? t("chat.tab.untitled", "未命名会话") : title;
}

function switchToTab(job: ScheduledContinuationJob): void {
  const tab = store.tabById(job.tabId);
  if (tab !== null) {
    store.switchTab(job.tabId);
    onClose();
  }
}

// ── Outside-close (capture-phase mousedown; trigger wrapper handles toggle) ──
function onDocMouseDown(ev: MouseEvent): void {
  if (!props.open) return;
  const el = popoverRef.value;
  if (el === null) return;
  const target = ev.target as Node | null;
  if (target === null) return;
  if (el.contains(target)) return;
  const wrap = el.parentElement;
  if (wrap !== null && wrap.contains(target)) return;
  onClose();
}

onMounted(() => {
  document.addEventListener("mousedown", onDocMouseDown, true);
});
onBeforeUnmount(() => {
  document.removeEventListener("mousedown", onDocMouseDown, true);
});
</script>

<template>
  <div
    v-if="open"
    ref="popover"
    class="sched-popover"
    role="dialog"
    aria-modal="false"
    :aria-label="t('chat.scheduler.title', '定时继续检查')"
    data-testid="scheduler-popover"
    @mousedown.stop
    @keydown.escape="onClose"
  >
    <div class="sched-header">
      <span>⏱️ {{ t("chat.scheduler.title", "定时继续检查") }}</span>
    </div>
    <p class="sched-intro">
      {{
        t(
          "chat.scheduler.intro",
          "当模型停止运行时，按设定间隔自动发送提示词，触发模型继续完成计划中的任务。",
        )
      }}
    </p>

    <!-- ── Current-tab settings ─────────────────────────────────────────── -->
    <div class="sched-section">
      <label class="sched-field">
        <span class="sched-label">{{ t("chat.scheduler.interval", "检查间隔") }}</span>
        <span class="sched-interval">
          <input
            v-model.number="intervalMinutes"
            type="number"
            min="1"
            max="1440"
            class="sched-input sched-input--num"
            data-testid="scheduler-interval"
          />
          <span class="sched-unit">{{ t("chat.scheduler.minutes", "分钟") }}</span>
        </span>
      </label>

      <div class="sched-field">
        <span class="sched-label">{{ t("chat.scheduler.mode", "继续方式") }}</span>
        <div class="sched-radios">
          <label class="sched-radio">
            <input
              v-model="mode"
              type="radio"
              value="same-session"
              data-testid="scheduler-mode-same"
            />
            {{ t("chat.scheduler.modeSame", "本会话继续") }}
          </label>
          <label class="sched-radio">
            <input
              v-model="mode"
              type="radio"
              value="new-session"
              data-testid="scheduler-mode-new"
            />
            {{ t("chat.scheduler.modeNew", "新会话继续") }}
          </label>
          <label class="sched-radio">
            <input
              v-model="mode"
              type="radio"
              value="auto-by-context"
              data-testid="scheduler-mode-auto"
            />
            {{ t("chat.scheduler.modeAuto", "上下文超阈值时新会话") }}
          </label>
        </div>
      </div>

      <!-- Context threshold (auto mode only) -->
      <div
        v-if="mode === 'auto-by-context'"
        class="sched-field"
      >
        <span class="sched-label">{{ t("chat.scheduler.threshold", "上下文阈值") }}</span>
        <div class="sched-threshold">
          <label class="sched-radio">
            <input
              v-model="thresholdMode"
              type="radio"
              value="context-percent"
              data-testid="scheduler-threshold-percent"
            />
            <input
              v-model.number="percent"
              type="number"
              min="1"
              max="100"
              class="sched-input sched-input--num"
              :disabled="thresholdMode !== 'context-percent'"
            />
            <span class="sched-unit">%</span>
          </label>
          <label class="sched-radio">
            <input
              v-model="thresholdMode"
              type="radio"
              value="fixed-tokens"
              data-testid="scheduler-threshold-fixed"
            />
            <input
              v-model.number="fixedTokens"
              type="number"
              min="1000"
              step="1000"
              class="sched-input sched-input--num sched-input--wide"
              :disabled="thresholdMode !== 'fixed-tokens'"
            />
            <span class="sched-unit">tokens</span>
          </label>
        </div>
      </div>

      <label class="sched-field">
        <span class="sched-label">{{ t("chat.scheduler.prompt", "提示词") }}</span>
        <textarea
          v-model="prompt"
          class="sched-input sched-textarea"
          rows="4"
          data-testid="scheduler-prompt"
        />
      </label>

      <p
        v-if="mode === 'new-session'"
        class="sched-note"
      >
        💡 {{
          t(
            "chat.scheduler.newSessionNote",
            "新会话继续时，每次触发都会先让当前会话生成接力提示词，再自动打开并切换到新会话，并把本定时器迁移过去。",
          )
        }}
      </p>
      <p
        v-else-if="mode === 'auto-by-context'"
        class="sched-note"
      >
        💡 {{
          t(
            "chat.scheduler.autoNote",
            "未超过阈值时在本会话继续；超过阈值时才生成接力提示词、自动切换到新会话并迁移本定时器。",
          )
        }}
      </p>

      <div class="sched-actions">
        <button
          type="button"
          class="sched-btn sched-btn--primary"
          :disabled="!canSave"
          data-testid="scheduler-save"
          @click="onEnableOrUpdate"
        >
          {{
            activeTabJob !== null
              ? t("chat.scheduler.update", "更新")
              : t("chat.scheduler.enable", "启用")
          }}
        </button>
        <button
          v-if="activeTabJob !== null"
          type="button"
          class="sched-btn"
          data-testid="scheduler-stop-active"
          @click="onStopActive"
        >
          {{ t("chat.scheduler.stop", "停止") }}
        </button>
      </div>
    </div>

    <!-- ── All timers list ──────────────────────────────────────────────── -->
    <div class="sched-list-section">
      <div class="sched-list-head">
        <span>{{ t("chat.scheduler.listTitle", "所有定时器") }}</span>
        <button
          v-if="jobs.length > 0"
          type="button"
          class="sched-stop-all"
          data-testid="scheduler-stop-all"
          @click="onStopAll"
        >
          {{ t("chat.scheduler.stopAll", "全部停止") }}
        </button>
      </div>

      <p
        v-if="jobs.length === 0"
        class="sched-empty"
      >
        {{ t("chat.scheduler.empty", "暂无定时器") }}
      </p>
      <ul
        v-else
        class="sched-list"
      >
        <li
          v-for="job in jobs"
          :key="job.id"
          class="sched-item"
          :class="{ 'sched-item--error': job.status === 'error' }"
          data-testid="scheduler-list-item"
        >
          <div class="sched-item-main">
            <button
              type="button"
              class="sched-item-title"
              :title="t('chat.scheduler.switchTo', '切换到该会话')"
              @click="switchToTab(job)"
            >
              {{ tabTitle(job) }}
            </button>
            <span class="sched-item-meta">
              {{ modeLabel(job.mode) }} ·
              {{ t("chat.scheduler.everyMinutes", { n: job.intervalMinutes }) }}
            </span>
            <span class="sched-item-meta">
              {{ statusLabel(job) }} ·
              {{ t("chat.scheduler.nextIn", { n: minutesUntilNext(job) }) }}
            </span>
            <span
              v-if="job.lastError !== null"
              class="sched-item-error"
            >{{ job.lastError }}</span>
          </div>
          <div class="sched-item-ops">
            <button
              type="button"
              class="sched-op"
              :title="t('chat.scheduler.runNow', '立即检查')"
              data-testid="scheduler-run-now"
              @click="runNow(job.id)"
            >
              ▶
            </button>
            <button
              v-if="job.enabled"
              type="button"
              class="sched-op"
              :title="t('chat.scheduler.pause', '暂停')"
              data-testid="scheduler-pause"
              @click="pauseJob(job.id)"
            >
              ⏸
            </button>
            <button
              v-else
              type="button"
              class="sched-op"
              :title="t('chat.scheduler.resume', '恢复')"
              data-testid="scheduler-resume"
              @click="resumeJob(job.id)"
            >
              ⏵
            </button>
            <button
              type="button"
              class="sched-op sched-op--danger"
              :title="t('chat.scheduler.delete', '删除')"
              data-testid="scheduler-delete"
              @click="stopJob(job.id)"
            >
              ✕
            </button>
          </div>
        </li>
      </ul>
    </div>

    <div class="sched-foot">
      <button
        type="button"
        class="sched-btn"
        data-testid="scheduler-done"
        @click="onClose"
      >
        {{ t("chat.scheduler.done", "完成") }}
      </button>
    </div>
  </div>
</template>
