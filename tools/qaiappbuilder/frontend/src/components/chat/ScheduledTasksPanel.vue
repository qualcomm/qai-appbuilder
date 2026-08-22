<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ScheduledTasksPanel — WebUI manager for scheduled tasks (Settings ⏰ tab).
 *
 * Lists every scheduled task with its schedule / next-run / state, and lets
 * the user edit a task's content (prompt / schedule / name / repeat) and its
 * per-task tool + skill permission whitelists, plus pause / resume / run-now /
 * remove. All state is owned by the backend (`/api/scheduled-tasks`); this
 * panel is a thin editor over the `useScheduledTasksStore`.
 *
 * Skill whitelist note: `enabled_skills` is stored + editable here, but the
 * scheduled run does not yet enforce it (the turn exposes only a skill DISABLE
 * set). We surface a hint so the user knows the choice is captured, not
 * silently ignored; tool whitelists ARE enforced.
 */
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import {
  useScheduledTasksStore,
  type ScheduledTaskItem,
  type ScheduledTaskRun,
  type ScheduledTaskUpdate,
} from "@/stores/scheduledTasks";
import { useToast } from "@/composables/useToast";
import { useConfirm } from "@/composables/useConfirm";
import { useChatTabsStore } from "@/stores/chatTabs";
import ScheduleEditor, { type ScheduleDraft } from "./ScheduleEditor.vue";
import { ElTable, ElTableColumn } from "element-plus";
import "element-plus/es/components/table/style/css";
import "element-plus/es/components/table-column/style/css";
import { renderMarkdown } from "@/composables/markdown";

const { t } = useI18n();
const store = useScheduledTasksStore();
const toast = useToast();
const confirm = useConfirm();
const chatTabs = useChatTabsStore();
const router = useRouter();

/** ElTable slot rows come typed as ``DefaultRow`` (its permissive
 * ``Record<PropertyKey, any>``), which under ``strict`` cannot flow into
 * handlers expecting the concrete ``ScheduledTaskItem``. This narrowing seam
 * is used inside every column template — the data source
 * (``store.sortedTasks``) is already ``ScheduledTaskItem[]``, so the runtime
 * shape is guaranteed and the cast is purely a type-level bridge. */
function asTask(row: unknown): ScheduledTaskItem {
  return row as ScheduledTaskItem;
}

/** task_id currently expanded in the editor (only one at a time). */
const editingId = ref<string | null>(null);
/** Per-edit working copy, keyed by task_id. */
const draft = reactive<Record<string, ScheduledTaskUpdate>>({});
/** Saving flag per task_id, so a row's Save button can show progress. */
const saving = reactive<Record<string, boolean>>({});

/** The task currently being edited in the modal (null when closed). */
const editingTask = computed<ScheduledTaskItem | null>(() =>
  editingId.value === null
    ? null
    : store.tasks.find((x) => x.task_id === editingId.value) ?? null,
);

onMounted(async () => {
  await Promise.all([store.loadTasks(), store.loadCatalog()]);
});

/**
 * Effective status key for a task, collapsing the raw backend state + the
 * enabled flag into one of five user-facing buckets:
 *   paused | error | completed | running | scheduled
 * (a disabled task reads as "paused" regardless of stored state).
 */
function stateKey(task: ScheduledTaskItem): string {
  if (!task.enabled || task.state === "paused") return "paused";
  if (task.state === "error") return "error";
  if (task.state === "completed") return "completed";
  if (task.state === "running") return "running";
  return "scheduled";
}

/** Localised status label shown in the badge. */
function stateLabel(task: ScheduledTaskItem): string {
  return t(`scheduledTasks.status.${stateKey(task)}`);
}

/** Badge modifier class (colour) for the status. */
function stateClass(task: ScheduledTaskItem): string {
  return `sched-badge--${stateKey(task)}`;
}

/**
 * User-friendly rendering of a task's ``last_error``. A "tab busy / locked"
 * error is a benign skip (another turn held the conversation), so we show a
 * plain hint instead of the raw ``chat.tab_state_invalid`` / ``…locked``
 * envelope; other errors show verbatim. Empty when there is no error.
 */
function friendlyError(task: ScheduledTaskItem): string {
  const raw = task.last_error;
  if (!raw) return "";
  if (raw.includes("tab_state_invalid") || raw.includes("conversation_locked")) {
    return t("scheduledTasks.errorBusy");
  }
  return raw;
}

function fmtTime(iso: string | null): string {
  if (iso === null || iso === "") return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Table-cell time formatting split into two lines: date on top, HH:mm below.
 * A single-line ``toLocaleString`` ("8/4/2026, 7:04:47 PM") overflowed the
 * column and wrapped mid-string to three ragged lines. Two clean lines with
 * a fixed 24-hour ``HH:mm`` (no seconds, no AM/PM) reads at a glance and lines
 * up visually across rows. */
function fmtDate(iso: string | null): string {
  if (iso === null || iso === "") return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}
function fmtTimeShort(iso: string | null): string {
  if (iso === null || iso === "") return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
function pad2(n: number): string {
  return n.toString().padStart(2, "0");
}

/**
 * Open the task's bound conversation: switch to its already-open tab if any,
 * else open a new tab on it. No-op when the task carries no conversation id
 * (should not happen for a session-scoped task).
 */
async function openConversation(task: ScheduledTaskItem): Promise<void> {
  const cid = task.conversation_id;
  if (cid === "") return;
  // Reuse a MAIN-agent tab already bound to this conversation, else open one
  // (mirrors AppSidebar.selectConversation — a sub-agent tab shares the same
  // conversationId as its parent but is deliberately NOT the main tab, so
  // exclude it here otherwise the "open main conversation" click would just
  // switch to the sub-agent tab and appear as a no-op).
  const existing = chatTabs.tabs.find(
    (x) => x.conversationId === cid && x.kind !== "subagent",
  );
  const tabId =
    existing !== undefined
      ? (chatTabs.switchTab(existing.id), existing.id)
      : chatTabs.openTab({ conversationId: cid, title: task.name }).id;
  // Two-step load: lazy for a fresh tab, then a force-refresh for reused
  // tabs. ``loadHistoryMessages`` short-circuits on ``messages.length > 0``
  // (its once-per-open guard), so it CANNOT re-hydrate a tab whose messages
  // were wiped in memory but still exist in the backend DB — the recurring
  // "opened conversation shows the welcome screen despite N messages
  // persisted" symptom. ``reloadConversationMessages`` bypasses that guard
  // and force-fetches the newest page (with the empty-page write-guard we
  // added earlier so a transient failure never blanks a populated tab), so
  // the panel is authoritative when it opens a task's conversation.
  await chatTabs.loadHistoryMessages(tabId);
  await chatTabs.reloadConversationMessages(cid);
  // The panel lives on the /settings route as a sibling of <RouterView>;
  // updating chatTabs alone doesn't mount ChatView, so the switched/opened
  // tab stays invisible. Push /chat so the user actually SEES the click
  // take effect — the missing step that made this button read as broken.
  if (router.currentRoute.value.name !== "chat") {
    await router.push({ name: "chat" });
  }
}

function beginEdit(task: ScheduledTaskItem): void {
  editingId.value = task.task_id;
  // Seed the draft from the task's current values (copy arrays so the
  // checkbox toggles below don't mutate the store item in place).
  draft[task.task_id] = {
    prompt: task.prompt,
    schedule: task.schedule,
    // `start_at` only applies to interval schedules; carry the stored value in
    // so ScheduleEditor can show the existing first-run instead of blanking it.
    start_at: task.start_at ?? null,
    name: task.name,
    repeat_times: task.repeat_times,
    enabled_tools: [...task.enabled_tools],
    enabled_skills: [...task.enabled_skills],
  };
}

function cancelEdit(taskId: string): void {
  editingId.value = null;
  delete draft[taskId];
}

function toggleTool(taskId: string, name: string): void {
  const d = draft[taskId];
  if (d === undefined) return;
  const list = d.enabled_tools ?? [];
  d.enabled_tools = list.includes(name)
    ? list.filter((n) => n !== name)
    : [...list, name];
}

function toggleSkill(taskId: string, name: string): void {
  const d = draft[taskId];
  if (d === undefined) return;
  const list = d.enabled_skills ?? [];
  d.enabled_skills = list.includes(name)
    ? list.filter((n) => n !== name)
    : [...list, name];
}

function isToolOn(taskId: string, name: string): boolean {
  return (draft[taskId]?.enabled_tools ?? []).includes(name);
}

function isSkillOn(taskId: string, name: string): boolean {
  return (draft[taskId]?.enabled_skills ?? []).includes(name);
}

/**
 * The open task's `{ schedule, start_at }` pair as one v-model target for
 * ScheduleEditor. The draft stays the single source of truth; this is only a
 * lens over the two fields the editor owns.
 */
const scheduleDraft = computed<ScheduleDraft>({
  get: () => {
    const d = editingId.value === null ? undefined : draft[editingId.value];
    return { schedule: d?.schedule ?? "", start_at: d?.start_at ?? null };
  },
  set: (next) => {
    const d = editingId.value === null ? undefined : draft[editingId.value];
    if (d === undefined) return;
    d.schedule = next.schedule;
    d.start_at = next.start_at;
  },
});

async function save(task: ScheduledTaskItem): Promise<void> {
  const d = draft[task.task_id];
  if (d === undefined) return;
  const prompt = (d.prompt ?? "").trim();
  const schedule = (d.schedule ?? "").trim();
  if (prompt === "") {
    toast.info(t("scheduledTasks.errorEmptyPrompt"));
    return;
  }
  if (schedule === "") {
    toast.info(t("scheduledTasks.errorEmptySchedule"));
    return;
  }
  saving[task.task_id] = true;
  try {
    await store.updateTask(task.task_id, {
      prompt,
      schedule,
      start_at: d.start_at ?? null,
      name: (d.name ?? "").trim(),
      repeat_times: d.repeat_times ?? null,
      enabled_tools: d.enabled_tools ?? [],
      enabled_skills: d.enabled_skills ?? [],
    });
    toast.info(t("scheduledTasks.saved"));
    cancelEdit(task.task_id);
  } catch (err) {
    toast.info(
      t("scheduledTasks.saveFailed", {
        error: err instanceof Error ? err.message : String(err),
      }),
    );
  } finally {
    saving[task.task_id] = false;
  }
}

async function pause(task: ScheduledTaskItem): Promise<void> {
  try {
    await store.pauseTask(task.task_id);
  } catch (err) {
    toast.info(t("scheduledTasks.actionFailed", { error: String(err) }));
  }
}

async function resume(task: ScheduledTaskItem): Promise<void> {
  try {
    await store.resumeTask(task.task_id);
  } catch (err) {
    toast.info(t("scheduledTasks.actionFailed", { error: String(err) }));
  }
}

async function runNow(task: ScheduledTaskItem): Promise<void> {
  try {
    await store.runTask(task.task_id);
    toast.info(t("scheduledTasks.queued"));
  } catch (err) {
    toast.info(t("scheduledTasks.actionFailed", { error: String(err) }));
  }
}

async function remove(task: ScheduledTaskItem): Promise<void> {
  const ok = await confirm.confirm({
    title: t("scheduledTasks.removeTitle"),
    message: t("scheduledTasks.removeConfirm", { name: task.name }),
  });
  if (!ok) return;
  try {
    await store.removeTask(task.task_id);
    if (editingId.value === task.task_id) cancelEdit(task.task_id);
    if (runsFor.value?.task_id === task.task_id) closeRuns();
  } catch (err) {
    toast.info(t("scheduledTasks.actionFailed", { error: String(err) }));
  }
}

/**
 * Task whose run history is open in the runs modal (null when closed). A
 * GLOBAL task's result is never folded into a conversation, so this view is
 * the only place its full output can be read after the fact.
 */
const runsFor = ref<ScheduledTaskItem | null>(null);
/** Run records for `runsFor`, newest first. */
const runs = ref<ScheduledTaskRun[]>([]);
/** True while the run-history request is in flight. */
const runsLoading = ref(false);

/**
 * Which run rows are CURRENTLY EXPANDED in the history modal. Keyed by a
 * per-modal stable index (``0..runs.length-1``) — using ``ran_at`` would
 * collide on two fires that landed in the same millisecond, so we key on
 * the ordinal within ``runs`` instead. Cleared on every ``openRuns`` so a
 * task switch starts fresh (no stale "run #3 expanded" leaking across
 * tasks).
 */
const expandedRuns = ref<Set<number>>(new Set());

/**
 * Decide the DEFAULT expansion for one run when the modal opens:
 *   - the newest run (index 0) — the user almost always wants to see it,
 *   - AND every failed run — the error text is usually short and IS the
 *     diagnostic value, so hiding it behind a click is user-hostile.
 * Everything else starts collapsed so a long history reads as a scannable
 * timeline instead of a wall of nested markdown.
 */
function defaultExpanded(run: ScheduledTaskRun, i: number): boolean {
  return i === 0 || !run.ok;
}

function isRunExpanded(i: number): boolean {
  return expandedRuns.value.has(i);
}

function toggleRun(i: number): void {
  const next = new Set(expandedRuns.value);
  if (next.has(i)) next.delete(i);
  else next.add(i);
  expandedRuns.value = next;
}

/** Expand every currently-visible (post-filter) run in one click. */
function expandAllRuns(): void {
  const next = new Set<number>();
  filteredRuns.value.forEach((entry) => next.add(entry.index));
  expandedRuns.value = next;
}

/** Collapse every run — including any the initial-open policy expanded. */
function collapseAllRuns(): void {
  expandedRuns.value = new Set();
}

/**
 * Search / filter query. Case-insensitive substring match against ``result_text``
 * (raw markdown, so a search for "Astra" finds it inside a heading or link URL).
 * Empty string = show all. State is scoped to the currently-open modal — it
 * clears on every ``openRuns`` / ``closeRuns``.
 */
const runsQuery = ref("");

/**
 * Filtered view over ``runs`` with the ORIGINAL index preserved. The
 * accordion identity is that original index (``expandedRuns`` keys), so
 * filtering must NOT re-number rows or a filter-then-toggle sequence
 * would drift.
 */
interface RunEntry {
  run: ScheduledTaskRun;
  /** Stable identity — matches ``expandedRuns`` keys. */
  index: number;
}

const filteredRuns = computed<RunEntry[]>(() => {
  const q = runsQuery.value.trim().toLowerCase();
  const entries: RunEntry[] = runs.value.map((run, index) => ({
    run,
    index,
  }));
  if (q === "") return entries;
  return entries.filter((e) =>
    e.run.result_text.toLowerCase().includes(q),
  );
});

/**
 * Group the filtered runs by day bucket (today / yesterday / earlier).
 * Buckets stay newest-first because the underlying ``runs`` list is
 * already sorted DESC by ``ran_at``; a group is dropped when empty so
 * the modal shows only the buckets that have content.
 */
interface RunGroup {
  key: "today" | "yesterday" | "earlier";
  label: string;
  entries: RunEntry[];
}

const groupedRuns = computed<RunGroup[]>(() => {
  const now = new Date();
  const startOfToday = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000;
  const buckets: Record<RunGroup["key"], RunEntry[]> = {
    today: [],
    yesterday: [],
    earlier: [],
  };
  for (const entry of filteredRuns.value) {
    const ts = Date.parse(entry.run.ran_at);
    const bucket: RunGroup["key"] = Number.isNaN(ts)
      ? "earlier"
      : ts >= startOfToday
        ? "today"
        : ts >= startOfYesterday
          ? "yesterday"
          : "earlier";
    buckets[bucket].push(entry);
  }
  const out: RunGroup[] = [];
  if (buckets.today.length > 0) {
    out.push({ key: "today", label: t("scheduledTasks.groupToday"), entries: buckets.today });
  }
  if (buckets.yesterday.length > 0) {
    out.push({ key: "yesterday", label: t("scheduledTasks.groupYesterday"), entries: buckets.yesterday });
  }
  if (buckets.earlier.length > 0) {
    out.push({ key: "earlier", label: t("scheduledTasks.groupEarlier"), entries: buckets.earlier });
  }
  return out;
});

/**
 * Per-run "just copied" flag — flips true for 1.5 s after a successful copy
 * so the button label swaps to a confirmation. Keyed by run index so two
 * quick copies on different rows don't step on each other.
 */
const copiedIndex = ref<number | null>(null);
let _copyTimer: ReturnType<typeof setTimeout> | null = null;

async function copyRunResult(index: number, text: string): Promise<void> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard !== undefined) {
      await navigator.clipboard.writeText(text);
    } else {
      throw new Error("clipboard-api-unavailable");
    }
    copiedIndex.value = index;
    if (_copyTimer !== null) clearTimeout(_copyTimer);
    _copyTimer = setTimeout(() => {
      copiedIndex.value = null;
      _copyTimer = null;
    }, 1500);
  } catch {
    toast.info(t("scheduledTasks.copyFailed"));
  }
}

/**
 * Collapsed-state one-line preview: strip Markdown syntax noise (``#``,
 * ``*``, ``> ``, backticks, list bullets, link chrome) so the header still
 * hints at content in ~60 chars. This is display-only — the full text is
 * still available when the user expands. Fails to a blank when the input
 * is empty (the template falls back to the empty-state label in that case).
 */
function runSummary(text: string): string {
  if (text === "") return "";
  const stripped = text
    .replace(/```[\s\S]*?```/g, " ")       // fenced code blocks
    .replace(/`([^`]+)`/g, "$1")            // inline code
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")  // images
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")// links → their text
    .replace(/^\s*#+\s*/gm, "")             // heading markers
    .replace(/^\s*[-*+]\s+/gm, "")          // bullet markers
    .replace(/^\s*>\s?/gm, "")              // blockquote markers
    .replace(/[*_]{1,3}([^*_]+)[*_]{1,3}/g, "$1") // bold/italic
    .replace(/\s+/g, " ")
    .trim();
  return stripped.length > 60
    ? stripped.slice(0, 60) + "…"
    : stripped;
}

async function openRuns(task: ScheduledTaskItem): Promise<void> {
  runsFor.value = task;
  runs.value = [];
  expandedRuns.value = new Set();
  runsQuery.value = "";
  copiedIndex.value = null;
  runsLoading.value = true;
  try {
    const list = await store.loadRuns(task.task_id);
    // Drop a late response for a task the user already navigated away from.
    if (runsFor.value?.task_id === task.task_id) {
      runs.value = list;
      // Seed the initial expansion state now that we know the runs. See
      // ``defaultExpanded`` for the policy (newest + every failure).
      const seed = new Set<number>();
      list.forEach((r, i) => {
        if (defaultExpanded(r, i)) seed.add(i);
      });
      expandedRuns.value = seed;
    }
  } catch {
    // A failed fetch reads as "no runs": the modal keeps its empty state
    // instead of dumping a raw transport error over the history list.
    if (runsFor.value?.task_id === task.task_id) runs.value = [];
  } finally {
    if (runsFor.value?.task_id === task.task_id) runsLoading.value = false;
  }
}

function closeRuns(): void {
  runsFor.value = null;
  runs.value = [];
  expandedRuns.value = new Set();
  runsQuery.value = "";
  copiedIndex.value = null;
  runsLoading.value = false;
}

/**
 * Render one run's ``result_text`` to sanitised HTML for the history modal.
 * Scheduled-task results are LLM output in Markdown (headings, fenced code,
 * lists, links); the plain ``<pre>`` render showed them as raw source with
 * ``##`` markers and unwrapped URLs. Route them through the SAME
 * ``renderMarkdown`` pipeline the chat bubbles use (marked + DOMPurify +
 * hljs) so the history entry reads like an assistant turn. ``breaks: true``
 * mirrors the assistant-bubble render option so single newlines survive as
 * ``<br>``. Blank input yields an empty string (the template already shows
 * a localised empty-state row in that case).
 */
function renderRunHtml(text: string): string {
  if (text === "") return "";
  return renderMarkdown(text, { markedOptions: { breaks: true } });
}
</script>

<template>
  <div class="config-section sched-panel">
    <div
      v-if="store.loading && store.tasks.length === 0"
      class="sched-empty"
    >
      {{ t("common.loading") }}
    </div>

    <div
      v-else-if="store.tasks.length === 0"
      class="sched-empty"
      data-testid="scheduled-tasks-empty"
    >
      {{ t("scheduledTasks.empty") }}
    </div>

    <ElTable
      v-else
      :data="store.sortedTasks"
      row-key="task_id"
      size="small"
      stripe
      class="sched-table"
    >
      <ElTableColumn
        prop="state"
        :label="t('scheduledTasks.colStatus')"
        width="90"
      >
        <template #default="{ row }">
          <span
            class="sched-badge"
            :class="stateClass(asTask(row))"
            :title="stateLabel(asTask(row))"
          >{{ stateLabel(asTask(row)) }}</span>
        </template>
      </ElTableColumn>

      <ElTableColumn
        prop="name"
        :label="t('scheduledTasks.colTask')"
        min-width="220"
      >
        <template #default="{ row }">
          <div class="sched-cell-task">
            <div class="sched-cell-task__name">{{ asTask(row).name || asTask(row).task_id }}</div>
            <div class="sched-cell-task__meta">
              <code class="sched-cell-task__schedule">{{ asTask(row).schedule }}</code>
              <span
                v-if="asTask(row).repeat_times !== null"
                class="sched-cell-task__runs"
              >{{ asTask(row).completed_runs }} / {{ asTask(row).repeat_times }}</span>
            </div>
            <div
              v-if="friendlyError(asTask(row)) !== ''"
              class="sched-cell-task__err"
              :title="asTask(row).last_error"
            >⚠ {{ friendlyError(asTask(row)) }}</div>
          </div>
        </template>
      </ElTableColumn>

      <ElTableColumn
        :label="t('scheduledTasks.colConversation')"
        width="160"
      >
        <template #default="{ row }">
          <button
            v-if="asTask(row).conversation_id !== ''"
            type="button"
            class="sched-cell-conv"
            :title="t('scheduledTasks.openConversation')"
            data-testid="scheduled-task-open-conversation"
            @click="openConversation(asTask(row))"
          >💬 {{ asTask(row).conversation_title || asTask(row).conversation_id }}</button>
          <span
            v-else-if="asTask(row).is_global"
            class="sched-badge sched-badge--global"
            :title="t('scheduledTasks.typeGlobalDesc')"
            data-testid="scheduled-task-global-badge"
          >🌐 {{ t('scheduledTasks.typeGlobal') }}</span>
          <span v-else class="sched-cell-muted">—</span>
        </template>
      </ElTableColumn>

      <ElTableColumn
        :label="t('scheduledTasks.colNextRun')"
        width="150"
        align="right"
      >
        <template #default="{ row }">
          <div v-if="asTask(row).next_run_at !== null" class="sched-cell-time">
            <div class="sched-cell-time__date">{{ fmtDate(asTask(row).next_run_at) }}</div>
            <div class="sched-cell-time__hm">{{ fmtTimeShort(asTask(row).next_run_at) }}</div>
          </div>
          <span v-else class="sched-cell-muted">—</span>
        </template>
      </ElTableColumn>

      <ElTableColumn
        :label="t('scheduledTasks.colLastRun')"
        width="150"
        align="right"
      >
        <template #default="{ row }">
          <div v-if="asTask(row).last_run_at !== null" class="sched-cell-time">
            <div class="sched-cell-time__date">{{ fmtDate(asTask(row).last_run_at) }}</div>
            <div class="sched-cell-time__hm">{{ fmtTimeShort(asTask(row).last_run_at) }}</div>
          </div>
          <span v-else class="sched-cell-muted">—</span>
        </template>
      </ElTableColumn>

      <ElTableColumn
        :label="t('scheduledTasks.colActions')"
        width="320"
        fixed="right"
        align="center"
      >
        <template #default="{ row }">
          <div class="sched-cell-actions">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="beginEdit(asTask(row))"
            >{{ t("scheduledTasks.edit") }}</button>
            <button
              v-if="asTask(row).enabled && asTask(row).state !== 'paused'"
              type="button"
              class="btn btn-ghost btn-sm"
              @click="pause(asTask(row))"
            >{{ t("scheduledTasks.pause") }}</button>
            <button
              v-else
              type="button"
              class="btn btn-ghost btn-sm"
              @click="resume(asTask(row))"
            >{{ t("scheduledTasks.resume") }}</button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="runNow(asTask(row))"
            >{{ t("scheduledTasks.runNow") }}</button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :title="t('scheduledTasks.runHistory')"
              data-testid="scheduled-task-view-runs"
              @click="openRuns(asTask(row))"
            >{{ t("scheduledTasks.viewRuns") }}</button>
            <button
              type="button"
              class="btn btn-ghost btn-sm sched-cell-actions__danger"
              @click="remove(asTask(row))"
            >{{ t("scheduledTasks.remove") }}</button>
          </div>
        </template>
      </ElTableColumn>
    </ElTable>

    <!-- Edit modal — opened by a row's 编辑 button. Teleported to body so it
         overlays the whole page instead of squeezing into the table row. -->
    <Teleport to="body">
      <div
        v-if="editingTask !== null && draft[editingTask.task_id]"
        class="sched-modal-overlay"
        data-testid="scheduled-task-edit-modal"
        @click.self="cancelEdit(editingTask.task_id)"
      >
        <div class="sched-modal" role="dialog" aria-modal="true">
          <header class="sched-modal__head">
            <h4 class="sched-modal__title">
              {{ t("scheduledTasks.editTitle") }}
            </h4>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :aria-label="t('common.close')"
              @click="cancelEdit(editingTask.task_id)"
            >
              ✕
            </button>
          </header>

          <div class="sched-modal__body">
            <div class="config-field">
              <label class="config-label">{{ t("scheduledTasks.fieldName") }}</label>
              <input v-model="draft[editingTask.task_id]!.name" type="text" class="config-input" />
            </div>

            <div class="config-field">
              <label class="config-label">{{ t("scheduledTasks.fieldPrompt") }}</label>
              <div class="config-comment">{{ t("scheduledTasks.fieldPromptDesc") }}</div>
              <textarea v-model="draft[editingTask.task_id]!.prompt" rows="5" class="config-input sched-textarea" />
            </div>

            <ScheduleEditor v-model="scheduleDraft" />

            <div class="config-field">
              <label class="config-label">{{ t("scheduledTasks.fieldRepeat") }}</label>
              <div class="config-comment">{{ t("scheduledTasks.fieldRepeatDesc") }}</div>
              <input
                v-model.number="draft[editingTask.task_id]!.repeat_times"
                type="number"
                min="1"
                class="config-input config-number"
              />
            </div>

            <div class="config-field">
              <label class="config-label">{{ t("scheduledTasks.fieldTools") }}</label>
              <div class="config-comment">{{ t("scheduledTasks.fieldToolsDesc") }}</div>
              <div class="sched-chips">
                <label
                  v-for="name in store.tools"
                  :key="name"
                  class="sched-chip"
                  :class="{ 'sched-chip--on': isToolOn(editingTask.task_id, name) }"
                >
                  <input
                    type="checkbox"
                    :checked="isToolOn(editingTask.task_id, name)"
                    @change="toggleTool(editingTask.task_id, name)"
                  />
                  {{ name }}
                </label>
              </div>
            </div>

            <div class="config-field">
              <label class="config-label">{{ t("scheduledTasks.fieldSkills") }}</label>
              <div class="config-comment">{{ t("scheduledTasks.fieldSkillsDesc") }}</div>
              <div v-if="store.skills.length === 0" class="config-comment">
                {{ t("scheduledTasks.noSkills") }}
              </div>
              <div v-else class="sched-chips">
                <label
                  v-for="name in store.skills"
                  :key="name"
                  class="sched-chip"
                  :class="{ 'sched-chip--on': isSkillOn(editingTask.task_id, name) }"
                >
                  <input
                    type="checkbox"
                    :checked="isSkillOn(editingTask.task_id, name)"
                    @change="toggleSkill(editingTask.task_id, name)"
                  />
                  {{ name }}
                </label>
              </div>
            </div>
          </div>

          <footer class="sched-modal__footer">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="cancelEdit(editingTask.task_id)"
            >
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              class="btn btn-primary btn-sm"
              :disabled="saving[editingTask.task_id]"
              @click="save(editingTask)"
            >
              {{ saving[editingTask.task_id] ? t("common.saving") : t("common.save") }}
            </button>
          </footer>
        </div>
      </div>
    </Teleport>

    <!-- Run-history modal — mirrors the edit modal above. A global task's
         result never lands in a chat, so its full text is only readable here. -->
    <Teleport to="body">
      <div
        v-if="runsFor !== null"
        class="sched-modal-overlay"
        data-testid="scheduled-task-runs-modal"
        @click.self="closeRuns()"
      >
        <div class="sched-modal sched-modal--runs" role="dialog" aria-modal="true">
          <header class="sched-modal__head">
            <h4 class="sched-modal__title">
              {{ t("scheduledTasks.runHistory") }}
              <span class="sched-runs__for">{{ runsFor.name || runsFor.task_id }}</span>
            </h4>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              :aria-label="t('common.close')"
              @click="closeRuns()"
            >
              ✕
            </button>
          </header>

          <!-- Toolbar: search + batch expand / collapse. Sits between head
               and body so it stays visible while the body scrolls. Hidden
               entirely while loading or when the history is empty (nothing
               to filter / expand). -->
          <div
            v-if="!runsLoading && runs.length > 0"
            class="sched-runs__toolbar"
          >
            <input
              v-model="runsQuery"
              type="search"
              class="config-input sched-runs__search"
              :placeholder="t('scheduledTasks.searchPlaceholder')"
              data-testid="scheduled-task-runs-search"
            />
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              data-testid="scheduled-task-runs-expand-all"
              @click="expandAllRuns()"
            >
              {{ t("scheduledTasks.expandAll") }}
            </button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              data-testid="scheduled-task-runs-collapse-all"
              @click="collapseAllRuns()"
            >
              {{ t("scheduledTasks.collapseAll") }}
            </button>
          </div>

          <div class="sched-modal__body">
            <div v-if="runsLoading" class="sched-empty">
              {{ t("common.loading") }}
            </div>
            <div
              v-else-if="runs.length === 0"
              class="sched-empty"
              data-testid="scheduled-task-runs-empty"
            >
              {{ t("scheduledTasks.noRuns") }}
            </div>
            <div
              v-else-if="filteredRuns.length === 0"
              class="sched-empty"
              data-testid="scheduled-task-runs-no-match"
            >
              {{ t("scheduledTasks.noSearchMatch", { query: runsQuery }) }}
            </div>
            <template v-else>
              <section
                v-for="group in groupedRuns"
                :key="group.key"
                class="sched-runs__group"
                :data-testid="`scheduled-task-runs-group-${group.key}`"
              >
                <h5 class="sched-runs__group-label">{{ group.label }}</h5>
                <ol class="sched-runs">
                  <li
                    v-for="entry in group.entries"
                    :key="entry.index"
                    class="sched-run"
                    :class="{ 'sched-run--open': isRunExpanded(entry.index) }"
                    data-testid="scheduled-task-run"
                  >
                    <!-- Whole header row is the toggle. ``<button>`` gives us
                         Enter/Space/tab-order for free; ``aria-expanded`` lets
                         screen readers announce the state and swaps the
                         ``▸ / ▾`` glyph accordingly. -->
                    <button
                      type="button"
                      class="sched-run__head"
                      :aria-expanded="isRunExpanded(entry.index)"
                      :aria-controls="`sched-run-body-${entry.index}`"
                      data-testid="scheduled-task-run-toggle"
                      @click="toggleRun(entry.index)"
                    >
                      <span class="sched-run__chevron" aria-hidden="true">
                        {{ isRunExpanded(entry.index) ? "▾" : "▸" }}
                      </span>
                      <span
                        class="sched-badge"
                        :class="entry.run.ok ? 'sched-badge--completed' : 'sched-badge--error'"
                      >
                        <!-- Failure prefix icon so users spot bad runs at a
                             glance without reading the label. -->
                        <span
                          v-if="!entry.run.ok"
                          class="sched-badge__icon"
                          aria-hidden="true"
                        >⚠️</span>
                        {{ entry.run.ok ? t("scheduledTasks.runOk") : t("scheduledTasks.runFailed") }}
                      </span>
                      <span class="sched-run__at">{{ fmtTime(entry.run.ran_at) }}</span>
                      <!-- Collapsed-state one-liner so the user can still tell
                           which run is which without expanding every card. -->
                      <span
                        v-if="!isRunExpanded(entry.index) && entry.run.result_text !== ''"
                        class="sched-run__preview"
                        data-testid="scheduled-task-run-preview"
                      >{{ runSummary(entry.run.result_text) }}</span>
                    </button>

                    <!-- Result text is LLM Markdown output; render it through the
                         same pipeline as the chat bubbles so headings, code
                         fences, lists and links come out formatted instead of
                         as raw source. ``renderRunHtml`` runs the string
                         through DOMPurify (via ``renderMarkdown``), so v-html
                         is safe here. -->
                    <!-- eslint-disable vue/no-v-html -->
                    <div
                      v-if="isRunExpanded(entry.index)"
                      :id="`sched-run-body-${entry.index}`"
                      class="sched-run__body"
                      data-testid="scheduled-task-run-body"
                    >
                      <!-- Copy-raw-markdown affordance: users often want to
                           paste the report into another surface (email,
                           doc). Positioned as an inline toolbar above the
                           rendered markdown; disabled + hidden on empty
                           results (nothing to copy). -->
                      <div
                        v-if="entry.run.result_text !== ''"
                        class="sched-run__toolbar"
                      >
                        <button
                          type="button"
                          class="btn btn-ghost btn-sm sched-run__copy-btn"
                          data-testid="scheduled-task-run-copy"
                          @click.stop="copyRunResult(entry.index, entry.run.result_text)"
                        >
                          {{
                            copiedIndex === entry.index
                              ? t("scheduledTasks.copiedResult")
                              : t("scheduledTasks.copyResult")
                          }}
                        </button>
                      </div>
                      <div
                        v-if="entry.run.result_text !== ''"
                        class="sched-run__markdown"
                        v-html="renderRunHtml(entry.run.result_text)"
                      />
                      <div v-else class="sched-item__muted sched-run__at">
                        {{ t("scheduledTasks.emptyResult") }}
                      </div>
                    </div>
                    <!-- eslint-enable vue/no-v-html -->
                  </li>
                </ol>
              </section>
            </template>
          </div>

          <footer class="sched-modal__footer">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              @click="closeRuns()"
            >
              {{ t("scheduledTasks.closeRuns") }}
            </button>
          </footer>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.sched-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.sched-empty {
  padding: 24px;
  color: var(--text-muted);
}
/* Status badge — colour-coded pill so the state reads at a glance. */
.sched-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: var(--text-xs, 11px);
  font-weight: 600;
  white-space: nowrap;
}
.sched-badge--scheduled {
  color: #1d4ed8;
  background: rgba(59, 130, 246, 0.15);
}
.sched-badge--running {
  color: #0369a1;
  background: rgba(14, 165, 233, 0.18);
}
.sched-badge--completed {
  color: #15803d;
  background: rgba(34, 197, 94, 0.16);
}
.sched-badge--paused {
  color: #a16207;
  background: rgba(234, 179, 8, 0.18);
}
.sched-badge--error {
  color: #b91c1c;
  background: rgba(239, 68, 68, 0.16);
}

/* ── ElTable theming — map the widget's own tokens onto the app's design
   tokens so it reads as part of this app rather than a stock element-plus
   table. ``:deep`` reaches the internal markup (no scope id). */
.sched-panel :deep(.el-table) {
  --el-table-bg-color: transparent;
  --el-table-tr-bg-color: transparent;
  --el-table-row-hover-bg-color: var(--bg-hover);
  --el-table-header-bg-color: transparent;
  --el-table-border-color: var(--border);
  --el-table-header-text-color: var(--text-muted);
  --el-table-text-color: var(--text-primary);
  --el-fill-color-lighter: var(--bg-secondary);
  /* Stripe every OTHER row: use a subtle surface tint from our palette so
     the alternation reads as depth rather than element-plus grey. */
  --el-table-row-hover-bg-color: var(--bg-hover);
  background: transparent;
  color: var(--text-primary);
}
.sched-panel :deep(.el-table th.el-table__cell) {
  background: transparent;
  font-weight: 600;
  font-size: var(--text-xs, 11px);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
/* The fixed-right ``Actions`` column paints itself as an overlay when the
   table can scroll horizontally, so it needs a solid background matching
   the row it hovers over — otherwise the row's schedule text bleeds
   through. */
.sched-panel :deep(.el-table__fixed-right .el-table__cell),
.sched-panel :deep(.el-table__fixed-right-patch) {
  background: var(--bg-primary);
}
.sched-panel :deep(.el-table__row:hover .el-table__fixed-right .el-table__cell) {
  background: var(--bg-hover);
}

/* ── Cell content styles (used inside ElTableColumn #default templates). */
.sched-cell-task {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.sched-cell-task__name {
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sched-cell-task__meta {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  font-size: var(--text-xs, 11px);
  color: var(--text-secondary);
}
.sched-cell-task__schedule {
  font-family: var(--font-mono, monospace);
  color: var(--text-secondary);
}
.sched-cell-task__runs {
  color: var(--text-muted, #9b97c4);
}
.sched-cell-task__err {
  font-size: var(--text-xs, 12px);
  color: var(--danger, #e5484d);
  margin-top: 2px;
}
.sched-cell-conv {
  border: none;
  background: transparent;
  padding: 0;
  color: var(--accent, #8b7dff);
  cursor: pointer;
  font: inherit;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}
.sched-cell-conv:hover {
  text-decoration: underline;
}
.sched-cell-muted {
  color: var(--text-muted, #9b97c4);
}
.sched-cell-time {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
}
.sched-cell-time__date {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #9b97c4);
}
.sched-cell-time__hm {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.sched-cell-actions {
  display: flex;
  gap: 4px;
  justify-content: center;
  flex-wrap: nowrap;
}
.sched-cell-actions__danger {
  color: var(--danger, #e5484d);
}
.sched-textarea {
  resize: vertical;
  font-family: inherit;
}
.sched-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.sched-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  cursor: pointer;
  user-select: none;
}
.sched-chip--on {
  border-color: var(--accent, #3b82f6);
  background: var(--accent-subtle, rgba(59, 130, 246, 0.12));
}
.sched-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: rgba(0, 0, 0, 0.5);
}
.sched-modal {
  width: 100%;
  max-width: 560px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg, 12px);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
}
.sched-modal__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border);
}
.sched-modal__title {
  margin: 0;
  font-size: var(--text-lg, 16px);
  font-weight: 700;
}
.sched-modal__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  overflow-y: auto;
}
.sched-modal__footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--border);
}
/* Type badge — a global task has no conversation, so the conv column carries
   its type instead of a link. Tinted with the same accent as the conv link so
   the column reads as one family, distinct from the status colours. */
.sched-badge--global {
  color: var(--accent, #8b7dff);
  background: var(--accent-subtle, rgba(139, 125, 255, 0.16));
  cursor: help;
}
/* Run history: wider than the edit modal because result text is the content.
   Same viewport-capped sizing as the notification-center result modal so
   markdown reports read comfortably. */
.sched-modal--runs {
  max-width: min(90vw, 960px);
  max-height: 85vh;
  min-height: 320px;
}
.sched-runs__for {
  margin-left: var(--space-2);
  font-size: var(--text-sm);
  font-weight: 400;
  color: var(--text-secondary);
}
/* Toolbar between modal head and body — search box + batch expand/collapse
   controls. Fixed position within the flex column so it stays visible
   while the body scrolls. */
.sched-runs__toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-2, 8px);
  padding: 0 var(--space-4, 16px) var(--space-2, 8px);
  border-bottom: 1px solid var(--border, #2a2750);
}
.sched-runs__search {
  flex: 1 1 0;
  min-width: 0;
}
/* Day-group section: h5 sticky-ish label, then the runs list itself. */
.sched-runs__group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2, 8px);
}
.sched-runs__group + .sched-runs__group {
  margin-top: var(--space-3, 12px);
}
.sched-runs__group-label {
  margin: 0;
  font-size: var(--text-xs, 11px);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-secondary, #9d99c8);
}
/* Failure warning glyph inside the run badge. A hair-thin margin keeps
   the emoji from touching the localised label. */
.sched-badge__icon {
  margin-right: 3px;
}
/* Per-run action bar (currently just Copy) — right-aligned above the
   rendered markdown so it doesn't compete with the reading flow. */
.sched-run__toolbar {
  display: flex;
  justify-content: flex-end;
  padding-top: var(--space-2, 8px);
}
/* Accordion of run cards. Gap between cards + solid card surface gives
   the visual separation the divider-only version was missing (users
   read two consecutive runs as one glob otherwise). */
.sched-runs {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3, 12px);
}
.sched-run {
  background: var(--bg-tertiary, #0f0d24);
  border: 1px solid var(--border, #2a2750);
  border-radius: var(--radius-sm, 6px);
  overflow: hidden;
  transition: border-color 0.15s ease;
}
.sched-run--open {
  /* Subtle emphasis for the currently-open card so a scroll'd list still
     tells the user which body belongs to which header. */
  border-color: var(--accent, #7c6cff);
}
/* Whole header row is a click target — full-width button reset. */
.sched-run__head {
  display: flex;
  align-items: center;
  gap: var(--space-2, 8px);
  width: 100%;
  padding: var(--space-2, 8px) var(--space-3, 12px);
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
  font: inherit;
}
.sched-run__head:hover {
  background: rgba(108, 99, 255, 0.08);
}
.sched-run__head:focus-visible {
  outline: 2px solid var(--accent, #7c6cff);
  outline-offset: -2px;
}
.sched-run__chevron {
  display: inline-flex;
  width: 14px;
  justify-content: center;
  color: var(--text-secondary, #9d99c8);
  font-size: var(--text-xs, 11px);
  flex-shrink: 0;
}
.sched-run__at {
  font-size: var(--text-xs, 11px);
  color: var(--text-secondary);
  flex-shrink: 0;
}
/* Collapsed one-line preview — truncated with ellipsis so it never wraps
   and pushes the header taller than the badge row. */
.sched-run__preview {
  flex: 1 1 0;
  min-width: 0;
  font-size: var(--text-xs, 12px);
  color: var(--text-secondary, #9d99c8);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* Expanded body sits inside the card; the modal itself owns the outer
   scroll (removed the inner ``max-height`` — a nested scrollbar over a
   scrollable modal was double-scroll UX poison). */
.sched-run__body {
  padding: 0 var(--space-3, 12px) var(--space-3, 12px);
  border-top: 1px solid var(--border, #2a2750);
}
/* Markdown container for one run's rendered result — mirrors the
   notification-center result modal. Uses ``:deep()`` because the HTML is
   v-html'd from :func:`renderMarkdown` and would otherwise miss scoped-CSS
   attributes. NO nested scroll: the accordion collapses long runs by
   default and the OUTER modal owns the scrollbar. A nested scroll on top
   of a scrollable modal traps the wheel on the wrong axis. */
.sched-run__markdown {
  padding-top: var(--space-3, 12px);
  color: var(--text-primary, #e9e7ff);
  font-size: var(--text-sm, 14px);
  line-height: 1.7;
  word-wrap: break-word;
  overflow-wrap: anywhere;
}
.sched-run__markdown :deep(h1),
.sched-run__markdown :deep(h2),
.sched-run__markdown :deep(h3),
.sched-run__markdown :deep(h4) {
  margin: var(--space-3, 12px) 0 6px;
  font-weight: 600;
  line-height: 1.35;
}
.sched-run__markdown :deep(h1) { font-size: 1.35em; }
.sched-run__markdown :deep(h2) { font-size: 1.2em; }
.sched-run__markdown :deep(h3) { font-size: 1.08em; }
.sched-run__markdown :deep(h4) { font-size: 1em; }
.sched-run__markdown :deep(p) { margin: 6px 0; }
.sched-run__markdown :deep(ul),
.sched-run__markdown :deep(ol) {
  padding-left: var(--space-5, 20px);
  margin: 6px 0;
}
.sched-run__markdown :deep(li) { margin: 3px 0; }
.sched-run__markdown :deep(a) {
  color: var(--accent, #7c6cff);
  text-decoration: underline;
  word-break: break-all;
}
.sched-run__markdown :deep(a:hover) { opacity: 0.85; }
.sched-run__markdown :deep(blockquote) {
  border-left: 3px solid var(--accent, #7c6cff);
  padding: 2px 0 2px var(--space-3, 12px);
  margin: var(--space-2, 8px) 0;
  color: var(--text-secondary, #9d99c8);
}
.sched-run__markdown :deep(code):not(:deep(pre code)) {
  background: rgba(108, 99, 255, 0.15);
  color: #a78bfa;
  padding: 1px 5px;
  border-radius: 3px;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.9em;
}
.sched-run__markdown :deep(pre) {
  margin: var(--space-3, 12px) 0;
  padding: var(--space-3, 12px) var(--space-4, 16px);
  background: var(--bg-tertiary, #0f0d24);
  border: 1px solid var(--border, #2a2750);
  border-radius: var(--radius-sm, 6px);
  overflow-x: auto;
}
.sched-run__markdown :deep(pre code) {
  background: transparent;
  color: inherit;
  padding: 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.88em;
  line-height: 1.5;
}
.sched-run__markdown :deep(hr) {
  border: 0;
  border-top: 1px solid var(--border, #2a2750);
  margin: var(--space-3, 12px) 0;
}
.sched-run__markdown :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: var(--space-2, 8px) 0;
  font-size: 0.95em;
}
.sched-run__markdown :deep(th),
.sched-run__markdown :deep(td) {
  border: 1px solid var(--border, #2a2750);
  padding: 6px 10px;
  text-align: left;
}
.sched-run__markdown :deep(th) {
  background: var(--bg-tertiary, #0f0d24);
  font-weight: 600;
}
.sched-run__markdown :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 6px;
}
</style>
