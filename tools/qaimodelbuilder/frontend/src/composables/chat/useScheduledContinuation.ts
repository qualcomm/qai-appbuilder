// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useScheduledContinuation` — unattended "keep the model working" timers.
 *
 * Scenario (user spec): the user hands the model a plan document and walks away.
 * The model sometimes finishes only part of the work and stops. A scheduled
 * timer periodically checks whether the target conversation's model has STOPPED
 * and, only when stopped, nudges it to continue.
 *
 * Hard rules baked in (see plan doc + AGENTS State-Truth-First):
 *   - Trigger ONLY when the target tab is genuinely idle/errored. While the tab
 *     is `streaming` / `aborting` we do NOT send, NOT enqueue, NOT inject — we
 *     just reschedule the next check.
 *   - The target is bound by tab id (NOT the active tab), so a background tab is
 *     nudged correctly even after the user switches tabs.
 *   - Three continue strategies:
 *       same-session     — send the continue prompt in the same tab.
 *       new-session       — ask the CURRENT model to author a handoff prompt,
 *                           then open a NEW tab, send the handoff prompt there,
 *                           and MIGRATE this timer to the new tab (clone + close
 *                           the old one). The UI switches to the new tab (user
 *                           decision 2026-06-25).
 *       auto-by-context   — read the conversation's context usage; below the
 *                           threshold → same-session, at/over → new-session.
 *
 * State ownership:
 *   - Job records (serialisable) live in a module-level reactive ref and are
 *     mirrored into `sessionStorage` so a reload within the SAME browser tab
 *     restores them. NOT persisted to localStorage / backend (this is a
 *     "this window" helper, not a server cron).
 *   - `setTimeout` handles live in a module-level Map (never serialised),
 *     mirroring the toast / transient-handles pattern.
 *
 * The composable is a process-wide singleton: every call returns the same job
 * list + API, and the status watcher driving new-session handoff completion is
 * installed exactly once.
 */
import { ref, watch, effectScope, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useChatTurnSubmit } from "@/composables/chat/useChatTurnSubmit";
import { fetchContextUsage } from "@/composables/chat/useContextUsage";
import { useToast } from "@/composables/useToast";

export type ScheduledContinuationMode =
  | "same-session"
  | "new-session"
  | "auto-by-context";

export type ContextThresholdMode = "fixed-tokens" | "context-percent";

export type ScheduledContinuationStatus =
  | "scheduled"
  | "waiting_idle"
  | "handoff_pending"
  | "creating_session"
  | "sent"
  | "paused"
  | "error";

export interface ContextThresholdConfig {
  mode: ContextThresholdMode;
  /** Fixed token ceiling (used when mode === "fixed-tokens"). */
  fixedTokens: number;
  /** Percent 0..100 of the model's context window (mode === "context-percent"). */
  percent: number;
}

export interface ScheduledContinuationJob {
  id: string;
  tabId: string;
  conversationId: string | null;
  /** Snapshot of the bound tab's title for the list (refreshed on each check). */
  title: string;
  intervalMinutes: number;
  prompt: string;
  mode: ScheduledContinuationMode;
  contextThreshold: ContextThresholdConfig;
  enabled: boolean;
  createdAt: number;
  nextRunAt: number;
  lastCheckedAt: number | null;
  lastTriggeredAt: number | null;
  lastContextUsedTokens: number | null;
  lastContextLimitTokens: number | null;
  lastContextRatio: number | null;
  status: ScheduledContinuationStatus;
  /** When new-session handoff is in flight, the messages length at request time
   *  so we only read the assistant reply produced AFTER our request. */
  handoffBaselineMsgCount: number | null;
  handoffPromptPreview: string | null;
  lastError: string | null;
}

/** Input for creating / updating a job's user-editable fields. */
export interface ScheduledContinuationDraft {
  intervalMinutes: number;
  prompt: string;
  mode: ScheduledContinuationMode;
  contextThreshold: ContextThresholdConfig;
}

const SESSION_STORAGE_KEY = "qai.chat.scheduledContinuation.v1";
const MIN_INTERVAL_MINUTES = 1;
const MAX_INTERVAL_MINUTES = 1440;
/**
 * Max time to wait for the new-session handoff-authoring turn to finish before
 * giving up. Guards against a job stuck in `handoff_pending` forever when the
 * source tab never returns to idle (e.g. a silently-dropped WS that leaves the
 * tab "streaming" indefinitely, so the status watcher never fires). On timeout
 * the job fails gracefully (error + toast) and reschedules its normal interval.
 */
const HANDOFF_TIMEOUT_MS = 10 * 60_000;

export const DEFAULT_CONTINUE_PROMPT =
  "请检查计划文档中的任务是否已经全部完成。若仍有未完成任务，请继续执行下一步，并在完成后更新计划文档/任务状态。不要重复已经完成的工作；先根据当前会话上下文和计划文档判断进展，再继续推进。";

export const DEFAULT_HANDOFF_REQUEST_PROMPT =
  "请为一个新的会话生成接力提示词，用于让新会话中的模型继续完成当前计划文档中的未完成工作。\n\n" +
  "要求：\n" +
  "1. 用清晰结构总结当前目标、计划文档路径、关键约束和完成标准。\n" +
  "2. 列出已经完成的任务、已修改/需要关注的文件、已验证的结果。\n" +
  "3. 列出未完成任务、当前阻塞点、下一步最优执行顺序。\n" +
  "4. 明确告诉新会话：先读取计划文档和相关源码，再继续执行，不要重复已完成工作。\n" +
  "5. 输出只包含可直接复制给新会话的完整提示词，不要包含额外解释。";

export const DEFAULT_THRESHOLD: ContextThresholdConfig = {
  mode: "context-percent",
  fixedTokens: 200000,
  percent: 80,
};

// ── Module-level singleton state ────────────────────────────────────────────
// Job records are reactive + serialisable. Timer handles are transient and kept
// OUT of the reactive state (mirrors toast.ts / transientHandles.ts).
const jobs = ref<ScheduledContinuationJob[]>([]);
const timers = new Map<string, ReturnType<typeof setTimeout>>();
// Per-job watchdog timers for the new-session handoff phase (separate from the
// interval `timers` so a handoff timeout can fire independently).
const handoffWatchdogs = new Map<string, ReturnType<typeof setTimeout>>();
// Job ids whose async due-check is currently in flight — guards against a
// re-entrant run (e.g. the user clicks "check now" while an auto-by-context
// context query is still awaiting) double-dispatching the same cycle.
const inFlight = new Set<string>();
let initialised = false;
let watcherInstalled = false;
// Detached effect scope so the handoff-completion watcher lives for the whole
// app session — NOT tied to the ChatComposer/ChatView component that first
// calls the composable (those unmount on route change while timers keep
// running in the background, like the app-level transport singleton).
const schedulerScope = effectScope(true);

function genId(): string {
  if (
    typeof globalThis.crypto !== "undefined" &&
    typeof globalThis.crypto.randomUUID === "function"
  ) {
    return globalThis.crypto.randomUUID();
  }
  return `sched-${Date.now().toString()}-${Math.random().toString(36).slice(2, 8)}`;
}

function clampInterval(minutes: number): number {
  if (!Number.isFinite(minutes)) return 10;
  const m = Math.round(minutes);
  if (m < MIN_INTERVAL_MINUTES) return MIN_INTERVAL_MINUTES;
  if (m > MAX_INTERVAL_MINUTES) return MAX_INTERVAL_MINUTES;
  return m;
}

function intervalMs(minutes: number): number {
  return clampInterval(minutes) * 60_000;
}

// ── sessionStorage persistence (best-effort, graceful) ──────────────────────
function safeSession(): Storage | null {
  try {
    const s = globalThis.sessionStorage;
    if (s === undefined || s === null) return null;
    // probe
    const probe = "__qai_sched_probe__";
    s.setItem(probe, "1");
    s.removeItem(probe);
    return s;
  } catch {
    return null;
  }
}

function persist(): void {
  const s = safeSession();
  if (s === null) return;
  try {
    // Only the serialisable user-facing skeleton; transient runtime status
    // values that have no meaning after reload are normalised on load.
    s.setItem(SESSION_STORAGE_KEY, JSON.stringify({ version: 1, jobs: jobs.value }));
  } catch {
    // Quota / serialisation failure — keep the in-memory timers working.
  }
}

function loadFromSession(): ScheduledContinuationJob[] {
  const s = safeSession();
  if (s === null) return [];
  try {
    const raw = s.getItem(SESSION_STORAGE_KEY);
    if (raw === null) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !Array.isArray((parsed as { jobs?: unknown }).jobs)
    ) {
      return [];
    }
    const list = (parsed as { jobs: unknown[] }).jobs;
    const restored: ScheduledContinuationJob[] = [];
    for (const item of list) {
      if (typeof item !== "object" || item === null) continue;
      const j = item as Partial<ScheduledContinuationJob>;
      if (typeof j.id !== "string" || typeof j.tabId !== "string") continue;
      restored.push(normaliseLoadedJob(j));
    }
    return restored;
  } catch {
    return [];
  }
}

/** Normalise a loaded job: clamp interval, coerce transient runtime status back
 *  to a stable resting state, and fill any missing fields with defaults. */
function normaliseLoadedJob(j: Partial<ScheduledContinuationJob>): ScheduledContinuationJob {
  const interval = clampInterval(j.intervalMinutes ?? 10);
  const enabled = j.enabled === true;
  // A transient mid-flight status (handoff_pending / creating_session /
  // waiting_idle) is meaningless after reload — its in-flight turn is gone.
  // Reset enabled jobs to "scheduled" and disabled to "paused".
  const status: ScheduledContinuationStatus = enabled ? "scheduled" : "paused";
  return {
    id: j.id as string,
    tabId: j.tabId as string,
    conversationId: j.conversationId ?? null,
    title: typeof j.title === "string" ? j.title : "",
    intervalMinutes: interval,
    prompt: typeof j.prompt === "string" && j.prompt !== "" ? j.prompt : DEFAULT_CONTINUE_PROMPT,
    mode: isMode(j.mode) ? j.mode : "same-session",
    contextThreshold: normaliseThreshold(j.contextThreshold),
    enabled,
    createdAt: typeof j.createdAt === "number" ? j.createdAt : Date.now(),
    nextRunAt: Date.now() + interval * 60_000,
    lastCheckedAt: typeof j.lastCheckedAt === "number" ? j.lastCheckedAt : null,
    lastTriggeredAt: typeof j.lastTriggeredAt === "number" ? j.lastTriggeredAt : null,
    lastContextUsedTokens: typeof j.lastContextUsedTokens === "number" ? j.lastContextUsedTokens : null,
    lastContextLimitTokens: typeof j.lastContextLimitTokens === "number" ? j.lastContextLimitTokens : null,
    lastContextRatio: typeof j.lastContextRatio === "number" ? j.lastContextRatio : null,
    status,
    handoffBaselineMsgCount: null,
    handoffPromptPreview: typeof j.handoffPromptPreview === "string" ? j.handoffPromptPreview : null,
    lastError: typeof j.lastError === "string" ? j.lastError : null,
  };
}

function isMode(v: unknown): v is ScheduledContinuationMode {
  return v === "same-session" || v === "new-session" || v === "auto-by-context";
}

function normaliseThreshold(t: ContextThresholdConfig | undefined): ContextThresholdConfig {
  if (t === undefined || t === null) return { ...DEFAULT_THRESHOLD };
  const mode: ContextThresholdMode =
    t.mode === "fixed-tokens" || t.mode === "context-percent" ? t.mode : DEFAULT_THRESHOLD.mode;
  const fixedTokens =
    typeof t.fixedTokens === "number" && t.fixedTokens > 0 ? Math.round(t.fixedTokens) : DEFAULT_THRESHOLD.fixedTokens;
  let percent = typeof t.percent === "number" ? t.percent : DEFAULT_THRESHOLD.percent;
  if (percent < 1) percent = 1;
  if (percent > 100) percent = 100;
  return { mode, fixedTokens, percent: Math.round(percent) };
}

// ── Pure helpers (exported for unit tests) ──────────────────────────────────

/** Decide whether a tab in this status is safe to nudge (model stopped). */
export function isTabContinuable(status: string | undefined): boolean {
  return status === "idle" || status === "error";
}

/** Decide whether the auto-by-context strategy should spill into a new session. */
export function shouldSpillToNewSession(
  threshold: ContextThresholdConfig,
  usedTokens: number,
  limitTokens: number,
  ratio: number,
): boolean {
  if (threshold.mode === "fixed-tokens") {
    return usedTokens >= threshold.fixedTokens;
  }
  // context-percent: compare ratio (0..1) against percent/100. Fall back to
  // used/limit if ratio looks unset.
  const r = Number.isFinite(ratio) && ratio > 0 ? ratio : limitTokens > 0 ? usedTokens / limitTokens : 0;
  return r >= threshold.percent / 100;
}

// ── Core scheduler wiring (set up once via the composable) ───────────────────

function scheduleJob(job: ScheduledContinuationJob, runDueCheck: (jobId: string) => void): void {
  clearJobTimer(job.id);
  if (!job.enabled) return;
  const delay = Math.max(0, job.nextRunAt - Date.now());
  const handle = setTimeout(() => {
    runDueCheck(job.id);
  }, delay);
  timers.set(job.id, handle);
}

function clearJobTimer(jobId: string): void {
  const h = timers.get(jobId);
  if (h !== undefined) {
    clearTimeout(h);
    timers.delete(jobId);
  }
}

function clearHandoffWatchdog(jobId: string): void {
  const h = handoffWatchdogs.get(jobId);
  if (h !== undefined) {
    clearTimeout(h);
    handoffWatchdogs.delete(jobId);
  }
}

function findJob(jobId: string): ScheduledContinuationJob | null {
  return jobs.value.find((j) => j.id === jobId) ?? null;
}

function patchJob(jobId: string, patch: Partial<ScheduledContinuationJob>): void {
  jobs.value = jobs.value.map((j) => (j.id === jobId ? { ...j, ...patch } : j));
  persist();
}

export function useScheduledContinuation(): {
  jobs: Ref<ScheduledContinuationJob[]>;
  createForTab: (tabId: string, draft: ScheduledContinuationDraft) => ScheduledContinuationJob | null;
  updateJob: (jobId: string, draft: ScheduledContinuationDraft) => void;
  pauseJob: (jobId: string) => void;
  resumeJob: (jobId: string) => void;
  stopJob: (jobId: string) => void;
  stopAll: () => void;
  runNow: (jobId: string) => void;
  jobsForTab: (tabId: string) => ScheduledContinuationJob[];
  defaultDraft: () => ScheduledContinuationDraft;
} {
  const store = useChatTabsStore();
  const { submitToTab } = useChatTurnSubmit();
  const toast = useToast();
  const { t } = useI18n();

  if (!initialised) {
    initialised = true;
    jobs.value = loadFromSession();
    // Re-arm timers for restored, enabled jobs.
    for (const job of jobs.value) {
      if (job.enabled) scheduleJob(job, runDueCheck);
    }
  }

  if (!watcherInstalled) {
    watcherInstalled = true;
    // Host the watcher in the detached scope so it survives component unmount.
    schedulerScope.run(() => {
      installHandoffWatcher();
    });
  }

  // ── Public API ────────────────────────────────────────────────────────────

  function defaultDraft(): ScheduledContinuationDraft {
    return {
      intervalMinutes: 10,
      prompt: DEFAULT_CONTINUE_PROMPT,
      mode: "same-session",
      contextThreshold: { ...DEFAULT_THRESHOLD },
    };
  }

  function createForTab(
    tabId: string,
    draft: ScheduledContinuationDraft,
  ): ScheduledContinuationJob | null {
    const tab = store.tabById(tabId);
    if (tab === null) return null;
    const interval = clampInterval(draft.intervalMinutes);
    const job: ScheduledContinuationJob = {
      id: genId(),
      tabId,
      conversationId: tab.conversationId,
      title: tab.title,
      intervalMinutes: interval,
      prompt: draft.prompt.trim() === "" ? DEFAULT_CONTINUE_PROMPT : draft.prompt,
      mode: draft.mode,
      contextThreshold: normaliseThreshold(draft.contextThreshold),
      enabled: true,
      createdAt: Date.now(),
      nextRunAt: Date.now() + interval * 60_000,
      lastCheckedAt: null,
      lastTriggeredAt: null,
      lastContextUsedTokens: null,
      lastContextLimitTokens: null,
      lastContextRatio: null,
      status: "scheduled",
      handoffBaselineMsgCount: null,
      handoffPromptPreview: null,
      lastError: null,
    };
    jobs.value = [...jobs.value, job];
    persist();
    scheduleJob(job, runDueCheck);
    return job;
  }

  function updateJob(jobId: string, draft: ScheduledContinuationDraft): void {
    const job = findJob(jobId);
    if (job === null) return;
    // Editing a job cancels any in-flight handoff for it (the user is
    // reconfiguring): drop its watchdog + baseline so a late-arriving handoff
    // turn is ignored and the job restarts cleanly on its new interval.
    clearHandoffWatchdog(jobId);
    const interval = clampInterval(draft.intervalMinutes);
    patchJob(jobId, {
      intervalMinutes: interval,
      prompt: draft.prompt.trim() === "" ? DEFAULT_CONTINUE_PROMPT : draft.prompt,
      mode: draft.mode,
      contextThreshold: normaliseThreshold(draft.contextThreshold),
      nextRunAt: Date.now() + interval * 60_000,
      status: job.enabled ? "scheduled" : "paused",
      handoffBaselineMsgCount: null,
      lastError: null,
    });
    const updated = findJob(jobId);
    if (updated !== null && updated.enabled) scheduleJob(updated, runDueCheck);
  }

  function pauseJob(jobId: string): void {
    clearJobTimer(jobId);
    clearHandoffWatchdog(jobId);
    patchJob(jobId, { enabled: false, status: "paused", handoffBaselineMsgCount: null });
  }

  function resumeJob(jobId: string): void {
    const job = findJob(jobId);
    if (job === null) return;
    const nextRunAt = Date.now() + intervalMs(job.intervalMinutes);
    patchJob(jobId, { enabled: true, status: "scheduled", nextRunAt, lastError: null });
    const updated = findJob(jobId);
    if (updated !== null) scheduleJob(updated, runDueCheck);
  }

  function stopJob(jobId: string): void {
    clearJobTimer(jobId);
    clearHandoffWatchdog(jobId);
    jobs.value = jobs.value.filter((j) => j.id !== jobId);
    persist();
  }

  function stopAll(): void {
    for (const id of [...timers.keys()]) clearJobTimer(id);
    for (const id of [...handoffWatchdogs.keys()]) clearHandoffWatchdog(id);
    jobs.value = [];
    persist();
  }

  function runNow(jobId: string): void {
    runDueCheck(jobId);
  }

  function jobsForTab(tabId: string): ScheduledContinuationJob[] {
    return jobs.value.filter((j) => j.tabId === tabId);
  }

  // ── Due check + strategy dispatch ───────────────────────────────────────────

  function runDueCheck(jobId: string): void {
    // Re-entrancy guard: ignore a fresh trigger while this job's previous
    // async cycle is still resolving (e.g. "check now" during an auto-mode
    // context-query await). The in-flight cycle will reschedule on completion.
    if (inFlight.has(jobId)) return;
    void runDueCheckAsync(jobId);
  }

  async function runDueCheckAsync(jobId: string): Promise<void> {
    inFlight.add(jobId);
    try {
      await runDueCheckBody(jobId);
    } finally {
      inFlight.delete(jobId);
    }
  }

  async function runDueCheckBody(jobId: string): Promise<void> {
    const job = findJob(jobId);
    if (job === null) return;
    if (!job.enabled) return;
    // A handoff is already in flight (waiting for the source tab to finish
    // authoring / creating the new session). Do NOT start a fresh cycle — it
    // would clobber `handoff_pending` and orphan the in-flight handoff. The
    // handoff watcher (or its watchdog) will resolve it.
    if (job.status === "handoff_pending" || job.status === "creating_session") {
      return;
    }

    const tab = store.tabById(job.tabId);
    // Target tab gone (closed) → stop the job (no event to subscribe to;
    // detected here on the due check, per plan).
    if (tab === null) {
      clearJobTimer(jobId);
      clearHandoffWatchdog(jobId);
      jobs.value = jobs.value.filter((j) => j.id !== jobId);
      persist();
      toast.warning(t("chat.scheduler.toast.tabClosed", "定时器已停止：绑定的会话已关闭"));
      return;
    }

    // Refresh the snapshot title + conversation binding for the list.
    patchJob(jobId, {
      title: tab.title,
      conversationId: tab.conversationId,
      lastCheckedAt: Date.now(),
    });

    // HARD RULE: only act when the model has stopped. While busy, just
    // reschedule the next check — never send / enqueue / inject.
    if (!isTabContinuable(tab.status)) {
      patchJob(jobId, { status: "waiting_idle" });
      reschedule(jobId);
      return;
    }

    // Fresh actionable cycle — clear any stale note from a prior cycle; each
    // branch below re-sets it if something noteworthy happens.
    patchJob(jobId, { lastError: null });

    // Decide effective strategy.
    let effective: "same-session" | "new-session" = "same-session";
    if (job.mode === "same-session") {
      effective = "same-session";
    } else if (job.mode === "new-session") {
      effective = "new-session";
    } else {
      // auto-by-context: need a persisted conversation to query usage.
      effective = await decideAutoStrategy(job, tab.modelId, tab.modelProvider);
    }

    // Re-validate AFTER any await (the auto-by-context context query awaits a
    // network round-trip during which the user could resume the tab or close
    // it). State-Truth-First: never act on a stale snapshot — re-read live tab.
    const liveTab = store.tabById(job.tabId);
    if (liveTab === null) {
      clearJobTimer(jobId);
      jobs.value = jobs.value.filter((j) => j.id !== jobId);
      persist();
      toast.warning(t("chat.scheduler.toast.tabClosed", "定时器已停止：绑定的会话已关闭"));
      return;
    }
    if (!isTabContinuable(liveTab.status)) {
      // Became busy mid-decision — skip this cycle, re-check next interval.
      patchJob(jobId, { status: "waiting_idle" });
      reschedule(jobId);
      return;
    }

    if (effective === "same-session") {
      await doSameSession(jobId);
    } else {
      beginNewSessionHandoff(jobId);
    }
  }

  async function decideAutoStrategy(
    job: ScheduledContinuationJob,
    modelId: string,
    modelProvider: string,
  ): Promise<"same-session" | "new-session"> {
    if (job.conversationId === null || job.conversationId === "") {
      // No backend conversation yet → nothing to spill; stay same-session.
      return "same-session";
    }
    const mid = modelId !== "" && modelId !== "qai-default" ? modelId : null;
    const provider = modelProvider !== "" ? modelProvider : null;
    const usage = await fetchContextUsage(job.conversationId, mid, provider);
    if (usage === null) {
      // Query failed → conservative: stay in the same session, record note.
      patchJob(job.id, {
        lastError: t("chat.scheduler.note.ctxQueryFailed", "上下文占用查询失败，已按本会话继续"),
      });
      return "same-session";
    }
    patchJob(job.id, {
      lastContextUsedTokens: usage.estimated_tokens,
      lastContextLimitTokens: usage.context_limit,
      lastContextRatio: usage.usage_pct,
      lastError: null,
    });
    return shouldSpillToNewSession(
      job.contextThreshold,
      usage.estimated_tokens,
      usage.context_limit,
      usage.usage_pct,
    )
      ? "new-session"
      : "same-session";
  }

  async function doSameSession(jobId: string): Promise<void> {
    const job = findJob(jobId);
    if (job === null) return;
    // NOTE: lastError is cleared at the start of each actionable cycle; a note
    // set THIS cycle (e.g. auto-mode ctx-query failure that fell back to
    // same-session) is intentionally preserved here so the list explains it.
    patchJob(jobId, { status: "sent", lastTriggeredAt: Date.now() });
    await submitToTab(job.tabId, job.prompt);
    reschedule(jobId);
  }

  /**
   * New-session step 1: ask the CURRENT session's model to author a handoff
   * prompt. We record the messages length so the completion watcher can read
   * ONLY the assistant reply produced after this request, then flip to
   * `handoff_pending`. The actual new-tab creation + migration happens in the
   * status watcher once this tab returns to idle.
   */
  function beginNewSessionHandoff(jobId: string): void {
    const job = findJob(jobId);
    if (job === null) return;
    const tab = store.tabById(job.tabId);
    if (tab === null) {
      stopJob(jobId);
      return;
    }
    const baseline = tab.messages.length;
    patchJob(jobId, {
      status: "handoff_pending",
      handoffBaselineMsgCount: baseline,
      lastTriggeredAt: Date.now(),
      lastError: null,
    });
    // Fire the handoff-authoring request WITHOUT awaiting turn completion;
    // `installHandoffWatcher()` drives the rest when the tab returns to idle.
    // (Awaiting here would just hold this async frame for the whole turn.)
    void submitToTab(job.tabId, DEFAULT_HANDOFF_REQUEST_PROMPT);
    // Watchdog: if the source tab never returns to idle (e.g. a silently
    // dropped WS), the status watcher never fires — fail the job gracefully
    // after a timeout instead of leaving it stuck in `handoff_pending` forever.
    armHandoffWatchdog(jobId);
  }

  function armHandoffWatchdog(jobId: string): void {
    clearHandoffWatchdog(jobId);
    const handle = setTimeout(() => {
      handoffWatchdogs.delete(jobId);
      const job = findJob(jobId);
      if (job === null || job.status !== "handoff_pending") return;
      patchJob(jobId, {
        status: "error",
        lastError: t("chat.scheduler.note.handoffTimeout", "接力提示词生成超时，已保留本定时器"),
        handoffBaselineMsgCount: null,
      });
      toast.error(
        t("chat.scheduler.toast.handoffFailed", "生成新会话接力提示词失败，已保留本定时器"),
      );
      reschedule(jobId);
    }, HANDOFF_TIMEOUT_MS);
    handoffWatchdogs.set(jobId, handle);
  }

  function reschedule(jobId: string): void {
    const job = findJob(jobId);
    if (job === null || !job.enabled) return;
    const nextRunAt = Date.now() + intervalMs(job.intervalMinutes);
    // Preserve the informational resting statuses (`waiting_idle` after a
    // busy check, `error` after a failed handoff) so the list still tells the
    // user WHY nothing happened; only the transient in-flight statuses fold
    // back to `scheduled`.
    const keep =
      job.status === "waiting_idle" || job.status === "error" ? job.status : "scheduled";
    patchJob(jobId, { nextRunAt, status: keep });
    const updated = findJob(jobId);
    if (updated !== null) scheduleJob(updated, runDueCheck);
  }

  /**
   * Watch tab status transitions so a `handoff_pending` job, whose source tab
   * has returned to idle, can extract the freshly-authored handoff prompt and
   * complete the new-session migration.
   */
  function installHandoffWatcher(): void {
    watch(
      () => store.tabs.map((t) => ({ id: t.id, status: t.status })),
      (next, prev) => {
        const prevById = new Map((prev ?? []).map((p) => [p.id, p.status]));
        for (const { id, status } of next) {
          const before = prevById.get(id);
          // The handoff-authoring turn has settled when the source tab leaves
          // a busy state. `idle` → extract + migrate; `error` → fail the job
          // gracefully so it can't get stuck in `handoff_pending` forever.
          const wasBusy = before === "streaming" || before === "aborting";
          if (!wasBusy) continue;
          const pending = jobs.value.find(
            (j) => j.tabId === id && j.status === "handoff_pending",
          );
          if (pending === undefined) continue;
          if (status === "idle") {
            // Defer to a microtask so the store has fully settled (the settled
            // assistant message is committed) before we read it. `flush:sync`
            // can otherwise fire mid-mutation and see a not-yet-committed reply.
            const pendingId = pending.id;
            queueMicrotask(() => {
              completeNewSessionHandoff(pendingId);
            });
          } else if (status === "error") {
            clearHandoffWatchdog(pending.id);
            patchJob(pending.id, {
              status: "error",
              lastError: t(
                "chat.scheduler.note.handoffEmpty",
                "接力提示词生成失败（回复为空）",
              ),
              handoffBaselineMsgCount: null,
            });
            toast.error(
              t(
                "chat.scheduler.toast.handoffFailed",
                "生成新会话接力提示词失败，已保留本定时器",
              ),
            );
            reschedule(pending.id);
          }
        }
      },
      // The handoff-completion watcher lives in a DETACHED scope (no host
      // component to batch against) so it survives ChatView/ChatComposer
      // unmount while background timers keep running. It uses `flush: "sync"`
      // so it reacts immediately to the source tab's status transition without
      // depending on a component render/flush cycle (a post-flush watcher in a
      // detached scope is not reliably driven when no app is rendering). The
      // source tab's settled assistant message is committed atomically with its
      // idle status by `confirmDone`, so the synchronous read sees final state.
      { deep: true, flush: "sync" },
    );
  }

  function completeNewSessionHandoff(jobId: string): void {
    const job = findJob(jobId);
    if (job === null || job.status !== "handoff_pending") return;
    // The handoff turn has resolved → cancel its watchdog.
    clearHandoffWatchdog(jobId);
    const sourceTab = store.tabById(job.tabId);
    if (sourceTab === null) {
      stopJob(jobId);
      return;
    }

    // Extract the last assistant reply produced AFTER the handoff request.
    const baseline = job.handoffBaselineMsgCount ?? 0;
    const handoffPrompt = extractLastAssistantText(sourceTab.messages, baseline);
    if (handoffPrompt === null || handoffPrompt.trim() === "") {
      patchJob(jobId, { status: "error", lastError: t("chat.scheduler.note.handoffEmpty", "接力提示词生成失败（回复为空）"), handoffBaselineMsgCount: null });
      toast.error(t("chat.scheduler.toast.handoffFailed", "生成新会话接力提示词失败，已保留本定时器"));
      reschedule(jobId);
      return;
    }

    patchJob(jobId, { status: "creating_session" });

    // Open a NEW tab carrying the SAME model (explicit — never rely on
    // active-tab inheritance, the active tab may have changed).
    const preIds = new Set(store.tabs.map((t) => t.id));
    const newTab = store.openTab({
      title: sourceTab.title,
      modelId: sourceTab.modelId,
      modelProvider: sourceTab.modelProvider,
    });
    // Cap-rejection guard: at MAX_OPEN_TABS, openTab returns an EXISTING tab
    // (its id was already present). Do NOT migrate / close in that case.
    if (preIds.has(newTab.id)) {
      patchJob(jobId, {
        status: "error",
        lastError: t("chat.scheduler.note.tabLimit", "标签页数量已达上限，无法创建新会话"),
        handoffBaselineMsgCount: null,
      });
      toast.error(t("chat.scheduler.toast.tabLimit", "标签页数量已达上限，无法在新会话继续"));
      reschedule(jobId);
      return;
    }

    // Send the handoff prompt as the new session's first user message. Fire it
    // WITHOUT awaiting turn completion (the turn can run for minutes); the
    // migration below must land immediately so the timer isn't stuck in
    // `creating_session` for the whole turn. submitToTab swallows its own
    // errors (per-message marker), so a floating promise is safe here.
    void submitToTab(newTab.id, handoffPrompt);

    // Migrate THIS timer to the new tab (clone config + close the old job).
    const interval = clampInterval(job.intervalMinutes);
    const migrated: ScheduledContinuationJob = {
      ...job,
      id: genId(),
      tabId: newTab.id,
      conversationId: newTab.conversationId,
      title: newTab.title,
      intervalMinutes: interval,
      nextRunAt: Date.now() + interval * 60_000,
      status: "scheduled",
      enabled: true,
      lastCheckedAt: null,
      lastTriggeredAt: Date.now(),
      lastContextUsedTokens: null,
      lastContextLimitTokens: null,
      lastContextRatio: null,
      handoffBaselineMsgCount: null,
      handoffPromptPreview: handoffPrompt.slice(0, 200),
      lastError: null,
    };
    // Remove old job + its timer, add migrated job.
    clearJobTimer(jobId);
    jobs.value = [...jobs.value.filter((j) => j.id !== jobId), migrated];
    persist();
    scheduleJob(migrated, runDueCheck);
    toast.info(t("chat.scheduler.toast.migrated", "已在新会话继续，定时器已迁移到新会话"));
  }

  return {
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
  };
}

/**
 * Extract the last assistant message text produced at or after `baseline`
 * (the messages length captured when the handoff request was sent), skipping
 * slash-command echoes/replies (display-only). Returns null when none found.
 */
export function extractLastAssistantText(
  messages: readonly { role: string; content: string; isCommandReply?: boolean }[],
  baseline: number,
): string | null {
  for (let i = messages.length - 1; i >= Math.max(0, baseline); i--) {
    const m = messages[i];
    if (m === undefined) continue;
    if (m.role === "assistant" && m.isCommandReply !== true && m.content.trim() !== "") {
      return m.content;
    }
  }
  return null;
}
