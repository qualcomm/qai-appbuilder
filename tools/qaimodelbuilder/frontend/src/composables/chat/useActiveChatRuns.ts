// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import {
  computed,
  getCurrentInstance,
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  ref,
  watch,
  type Ref,
} from "vue";
import { useRouter } from "vue-router";
import { apiJson } from "@/api";
import { useChatTabsStore, type TabId } from "@/stores/chatTabs";
import { useChatTransports } from "@/composables/chat/useChatTransports";
import { attachActiveChatRun } from "@/composables/chat/useActiveChatRunAttach";
import type { ApiMethod } from "@/api";

export interface ActiveRunStopSpec {
  method: ApiMethod;
  path: string;
  body: Record<string, unknown>;
}

export interface ActiveRunItemWire {
  kind: "chat" | "subagent";
  id: string;
  tab_id: string | null;
  conversation_id: string | null;
  subagent_id: string | null;
  root_conversation_id: string | null;
  title: string | null;
  status: string | null;
  model_id: string | null;
  model_provider: string | null;
  started_at: string;
  last_active_at: string;
  aborted: boolean;
  reason: string | null;
  openable: boolean;
  attach_path: string | null;
  stop: ActiveRunStopSpec;
}

interface ActiveRunsResponse {
  items: ActiveRunItemWire[];
}

export interface ActiveRunView extends ActiveRunItemWire {
  localTabId: string | null;
  isCurrent: boolean;
  isOpened: boolean;
  displayTitle: string;
}

/**
 * Active-runs popover state + behaviour for the composer's "running sessions"
 * button. Polls `/api/chat/active-runs` at two cadences:
 *
 * - popover OPEN  -> 2s, fast updates while user is looking at the list
 * - popover CLOSED -> 10s, just enough to keep the button BADGE COUNT fresh
 *
 * Background polls always go through silent mode so the popover's empty-state
 * text never flickers between "loading…" and "no running sessions".
 *
 * MUST be called from a component `setup()` (or `<script setup>`) so that
 * `onBeforeUnmount` can clear the polling timer. Calling from a non-component
 * context (e.g. a Pinia store action) leaks the `setInterval`.
 */
export function useActiveChatRuns(open: Ref<boolean>) {
  const store = useChatTabsStore();
  const router = useRouter();
  const { peekTransport } = useChatTransports();
  const items = ref<ActiveRunItemWire[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const stoppingIds = ref(new Set<string>());
  let timer: number | undefined;
  let refreshSeq = 0;
  // Two poll cadences:
  // - popover OPEN: fast 2s so the list reflects new/finished runs promptly.
  // - popover CLOSED: slow 10s, just enough to keep the badge count fresh
  //   (the button shows `runs.length` even when the menu is closed) without
  //   wasting CPU/network on a panel nobody is looking at.
  // Both cadences write through silent refresh — the empty-state text inside
  // the popover never flickers between "loading…" and "no running sessions"
  // because background polls don't flip `loading`.
  const POLL_OPEN_MS = 2000;
  const POLL_CLOSED_MS = 10000;
  let pollMs = POLL_CLOSED_MS;

  const runs = computed<ActiveRunView[]>(() =>
    items.value.map((item) => {
      const localTab =
        item.kind === "chat"
          ? store.tabs.find((t) => t.id === item.tab_id) ??
            store.tabs.find(
              (t) => t.kind !== "subagent" && t.conversationId === item.conversation_id,
            )
          : store.tabs.find(
              (t) => t.kind === "subagent" &&
                t.subagentMeta?.subagentId === item.subagent_id,
            );
      return {
        ...item,
        aborted: item.aborted || stoppingIds.value.has(item.id),
        localTabId: localTab?.id ?? null,
        isCurrent: localTab?.id === store.activeTabId,
        isOpened: localTab !== undefined,
        displayTitle: localTab?.title || item.title || item.id,
      };
    }),
  );

  async function refresh(opts?: { silent?: boolean }): Promise<void> {
    const silent = opts?.silent === true;
    const seq = ++refreshSeq;
    if (!silent) loading.value = true;
    error.value = null;
    try {
      const res = await apiJson<ActiveRunsResponse>("GET", "/api/chat/active-runs");
      if (seq !== refreshSeq) return;
      items.value = Array.isArray(res.items) ? res.items : [];
      const liveIds = new Set(items.value.map((item) => item.id));
      stoppingIds.value = new Set(
        [...stoppingIds.value].filter((id) => liveIds.has(id)),
      );
    } catch (err) {
      if (seq !== refreshSeq) return;
      error.value = err instanceof Error ? err.message : String(err);
    } finally {
      if (seq === refreshSeq && !silent) loading.value = false;
    }
  }

  async function ensureChatRoute(): Promise<void> {
    if (router.currentRoute.value.name !== "chat") {
      await router.push({ name: "chat" });
    }
  }

  // A ``query::*`` model id (CEBot / MB Pro) is a TRANSIENT routing hint the
  // turn ran under, NOT a durable tab model the user picked from the dropdown.
  // When adopting an active run into a new tab we must NOT seed ``tab.modelId``
  // with it — otherwise the model-selector button shows e.g. "mb_pro" and the
  // next ordinary turn gets mis-routed to the query-service adapter. Drop it so
  // the tab falls back to the normal model auto-selection (the run still streams
  // correctly because its transport/model_hint live on the run, not the tab).
  function durableModelId(modelId: string | null): string | undefined {
    if (modelId === null || modelId === "") return undefined;
    return modelId.startsWith("query::") ? undefined : modelId;
  }

  async function openRun(run: ActiveRunView): Promise<void> {
    if (!run.openable) {
      error.value = "open_unavailable";
      return;
    }
    if (run.kind === "subagent" && run.subagent_id) {
      // (β 扁平 tab strip) `openSubAgentTab` 打开该 sub-agent tab —— top
      // strip 的一等公民（任意深度都直接在顶部与主 tab 同行）—— 并内部
      // `switchTab` 到它，让 ChatMessageList pivot 到它的 transcript。
      // 无投影、无隐藏 tab、无 rail 焦点同步。
      await store.openSubAgentTab(run.subagent_id);
      await ensureChatRoute();
      return;
    }
    if (run.kind !== "chat" || run.tab_id === null) {
      return;
    }
    const byTab = store.tabs.find((t) => t.id === run.tab_id);
    if (byTab !== undefined) {
      store.switchTab(byTab.id);
      await ensureChatRoute();
      return;
    }
    const tab = store.openTab({
      id: run.tab_id,
      conversationId: run.conversation_id ?? undefined,
      title: run.title ?? "",
      modelId: durableModelId(run.model_id),
      modelProvider: run.model_provider ?? undefined,
    });
    if (run.attach_path !== null) {
      store.setStreaming(tab.id);
    }
    if (tab.conversationId !== null && tab.conversationId !== "") {
      await store.loadHistoryMessages(tab.id);
    }
    if (run.attach_path !== null) {
      attachActiveChatRun(tab.id, run.attach_path);
    }
    store.switchTab(tab.id);
    await ensureChatRoute();
  }

  async function stopRun(run: ActiveRunView): Promise<void> {
    stoppingIds.value = new Set([...stoppingIds.value, run.id]);
    try {
      if (run.kind === "subagent" && run.subagent_id) {
        await store.interruptSubAgent(run.subagent_id);
        await refresh();
        return;
      }
      if (run.kind === "chat" && run.tab_id !== null) {
        const transport = peekTransport(run.tab_id as TabId);
        if (transport !== undefined) {
          transport.cancel();
        } else {
          await apiJson(run.stop.method, run.stop.path, run.stop.body);
          const tab = store.tabs.find((t) => t.id === run.tab_id);
          if (tab?.status === "streaming") {
            store.requestCancel(tab.id);
          }
        }
        await refresh();
      }
    } catch (err) {
      stoppingIds.value = new Set(
        [...stoppingIds.value].filter((id) => id !== run.id),
      );
      error.value = err instanceof Error ? err.message : String(err);
    }
  }

  function startPolling(): void {
    if (timer !== undefined) return;
    timer = window.setInterval(() => {
      void refresh({ silent: true });
    }, pollMs);
  }

  function stopPolling(): void {
    if (timer !== undefined) {
      window.clearInterval(timer);
      timer = undefined;
    }
  }

  // Switch poll cadence without dropping a beat: when the popover toggles we
  // tear down the timer and start a new one at the new cadence.
  function restartPollingAt(nextMs: number): void {
    pollMs = nextMs;
    stopPolling();
    startPolling();
  }

  // popover OPEN  -> show loading on the first fetch (non-silent), then poll
  //                  fast (2s) silently so the empty-state text never flickers.
  // popover CLOSED -> keep polling slowly (10s) silently so the BADGE COUNT
  //                  (visible on the button even when menu is closed) stays
  //                  fresh. The initial poll on mount is silent so the badge
  //                  appears without any flash of loading text.
  watch(
    open,
    (value) => {
      if (value) {
        void refresh();
        restartPollingAt(POLL_OPEN_MS);
      } else {
        // Initial mount fires this with value=false: kick off a silent fetch
        // immediately so the button badge reflects current runs without
        // waiting a full poll interval (regression-guarded by the
        // "refreshes immediately on mount" test).
        void refresh({ silent: true });
        restartPollingAt(POLL_CLOSED_MS);
      }
    },
    { immediate: true },
  );
  // Tab status changes drive a refresh that feeds the button badge count
  // (runs.length) and must update even while the popover is closed. Use silent
  // mode so it never flips `loading` and thus never flickers the empty state
  // text between "loading…" and "no running sessions".
  watch(
    () =>
      store.tabs
        .map((tab) => `${tab.id}:${tab.status}:${tab.kind ?? "chat"}`)
        .join("|"),
    () => {
      void refresh({ silent: true });
    },
  );
  if (getCurrentInstance() !== null) {
    onBeforeUnmount(stopPolling);
    // KeepAlive-aware: ChatView (and thus the ChatComposer with this
    // composer-button) is cached by AppMain.vue's <KeepAlive>. Stop the badge
    // poller when the chat surface is hidden, restart it on return so the
    // badge count is fresh again. The watch(open, ..., { immediate: true })
    // above already starts the poller in setup, so onActivated only needs to
    // restart it when the timer was torn down by a prior onDeactivated. The
    // `timer === undefined` guard skips the redundant restart on the very
    // first activation (onActivated fires right after onMounted), which would
    // otherwise stop+start the just-created timer + fire an extra silent
    // refresh.
    onDeactivated(stopPolling);
    onActivated(() => {
      if (timer !== undefined) return;
      void refresh({ silent: true });
      restartPollingAt(open.value ? POLL_OPEN_MS : POLL_CLOSED_MS);
    });
  }

  return { runs, loading, error, refresh, openRun, stopRun };
}
