// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useImplementation` — implementation-run control composable (DISC-1 二期
 * §22.9). Mirrors `useDiscussion`: binds the conversation-scoped implementation
 * store (`stores/implementation.ts`) to a chat tab's reactive `implementation`
 * state (`chatTabs.tab.implementation`). The ImplementationPanel uses this to:
 *   - show the live plan (phase + items + current item),
 *   - edit items (re-assign role / rename / skip / add / delete),
 *   - drive execution control (start / pause / resume / stop) by SENDING a
 *     localized control message through the ordinary chat send path (control
 *     router) — NOT a dedicated route.
 *
 * Plan edits PATCH the WHOLE item array (the store merges by id); the panel
 * keeps the authoritative copy on the tab (single source of truth) and reverts
 * the optimistic local copy on a backend failure so the UI never lies
 * (State-Truth-First). Backend 400 (illegal assigned_role) / 409 (cannot edit
 * the in-progress current item) errors surface as a localized toast keyed off
 * the error `code`.
 *
 * The composable owns NO module-level mutable state — it is instantiated per
 * panel and reads the active tab through the store getter each call, so there
 * is no stale-closure / global-ref anti-pattern.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { ApiError } from "@/api";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useChatTransports } from "@/composables/chat/useChatTransports";
import {
  useImplementationStore,
  wireToState,
  type ItemPatchWire,
} from "@/stores/implementation";
import {
  DEFAULT_IMPLEMENTATION_STATE,
  type ImplementationItemVM,
  type TabImplementationState,
  type TabId,
} from "@/stores/_chatTabsTypes";

/** Fields the panel may change on an existing item (all optional). */
export interface ItemEdit {
  assignedRole?: string | null;
  title?: string;
  description?: string;
  acceptanceCriteria?: string[];
  /** DISC-1 完成判定 B — per-item verification command. */
  verifyCommand?: string;
}

/** Fields needed to append a brand-new item. */
export interface NewItem {
  title: string;
  description?: string;
  assignedRole?: string | null;
}

export interface UseImplementation {
  /** Reactive implementation state of the bound tab (idle shell when no tab). */
  readonly state: ComputedRef<TabImplementationState>;
  /** Reactive item list of the bound tab. */
  readonly items: ComputedRef<ImplementationItemVM[]>;
  /** Run phase (`none` / `planned` / `implementing` / `paused` / `completed` /
   *  `failed` / `planning` / `planning_failed`). */
  readonly phase: ComputedRef<string>;
  /** Id of the item currently being implemented (null when idle/terminal). */
  readonly currentItem: ComputedRef<string | null>;
  /** True while a backend sync is in flight. */
  readonly busy: Ref<boolean>;
  /** Last error message (null when none). */
  readonly error: Ref<string | null>;
  /** Fetch the plan from the backend into the tab. */
  reload(): Promise<void>;
  /** Edit an existing item (re-assign role / rename / …); persists. */
  updateItem(id: string, edit: ItemEdit): Promise<void>;
  /** Delete an item; persists. */
  deleteItem(id: string): Promise<void>;
  /** Append a new pending item; persists. */
  addItem(item: NewItem): Promise<void>;
  /** Mark a pending item as skipped; persists. */
  skipItem(id: string): Promise<void>;
  /** Trigger the run (control message → control router). */
  start(): Promise<void>;
  /** Pause the run. */
  pause(): Promise<void>;
  /** Resume a paused run. */
  resume(): Promise<void>;
  /** Stop the run. */
  stop(): Promise<void>;
  /** Retry the failed items (三期-step2): reset failed→pending + re-run. */
  retry(): Promise<void>;
}

/** Bind the implementation composable to a SPECIFIC tab id, or (default) the
 *  currently active tab resolved lazily on each call. */
export function useImplementation(tabIdRef?: Ref<TabId | null>): UseImplementation {
  const tabs = useChatTabsStore();
  const store = useImplementationStore();
  const transports = useChatTransports();
  const { t } = useI18n();

  const busy = ref(false);
  const error = ref<string | null>(null);

  function currentTabId(): TabId | null {
    if (tabIdRef !== undefined) return tabIdRef.value;
    return tabs.activeTab?.id ?? null;
  }

  function currentTab() {
    const id = currentTabId();
    if (id === null) return null;
    return tabs.tabs.find((tab) => tab.id === id) ?? null;
  }

  function conversationId(): string | null {
    return currentTab()?.conversationId ?? null;
  }

  const state = computed<TabImplementationState>(
    () => currentTab()?.implementation ?? { ...DEFAULT_IMPLEMENTATION_STATE },
  );
  const items = computed<ImplementationItemVM[]>(() => state.value.items);
  const phase = computed<string>(() => state.value.phase);
  const currentItem = computed<string | null>(() => state.value.currentItem);

  /** Translate a thrown error into a localized, user-facing message. Backend
   *  contract codes map to friendly copy; everything else falls back to the raw
   *  message. */
  function describeError(e: unknown): string {
    if (e instanceof ApiError) {
      if (e.code === "chat.implementation_plan.invalid_assigned_role") {
        return t("chat.implementation.errors.invalidRole");
      }
      if (e.code === "chat.implementation_plan.item_in_progress") {
        return t("chat.implementation.errors.itemInProgress");
      }
      return e.message;
    }
    return e instanceof Error ? e.message : String(e);
  }

  function applyLocal(next: TabImplementationState): TabImplementationState | null {
    const tab = currentTab();
    if (tab === null) return null;
    const prev = tab.implementation;
    tabs.setImplementation(tab.id, next);
    return prev;
  }

  function revertLocal(prev: TabImplementationState | null): void {
    const tab = currentTab();
    if (tab !== null && prev !== null) tabs.setImplementation(tab.id, prev);
  }

  async function reload(): Promise<void> {
    const convId = conversationId();
    const tab = currentTab();
    if (convId === null || tab === null) return;
    busy.value = true;
    error.value = null;
    try {
      const wire = await store.fetchImplementationPlan(convId);
      tabs.setImplementation(tab.id, wireToState(wire));
    } catch (e) {
      error.value = describeError(e);
    } finally {
      busy.value = false;
    }
  }

  /** Build the full item-patch array from the CURRENT tab items, applying a
   *  transform (edit / delete / add / skip). The backend merges by id, so we
   *  must always send every id we want to keep. */
  function buildPatch(
    transform: (items: ImplementationItemVM[]) => ItemPatchWire[],
  ): ItemPatchWire[] {
    return transform([...items.value]);
  }

  /** Map a VM to a FULL editable-field patch entry. The backend `_merge_item`
   *  rebuilds each item from the incoming editable fields, so echoing the full
   *  current values for every item we keep prevents a bare `{id}` from RESETTING
   *  that item's title/description/acceptanceCriteria/assignedRole/verifyCommand
   *  (the multi-item wipe trap — AGENTS.md 方法4 / 🟡🟡). */
  function fullPatchOf(it: ImplementationItemVM): ItemPatchWire {
    return {
      id: it.id,
      title: it.title,
      description: it.description,
      acceptance_criteria: [...it.acceptanceCriteria],
      assigned_role: it.assignedRole,
      verify_command: it.verifyCommand,
    };
  }

  /** Shared "optimistic local + persisted PATCH (with revert on failure)" for an
   *  item-array mutation. `nextItems` is the optimistic VM list to show; `wire`
   *  is the full patch array to send. */
  async function persistItems(
    nextItems: ImplementationItemVM[],
    wire: ItemPatchWire[],
  ): Promise<void> {
    const convId = conversationId();
    const tab = currentTab();
    if (convId === null || tab === null) return;
    const prev = applyLocal({ ...state.value, items: nextItems });
    busy.value = true;
    error.value = null;
    try {
      const updated = await store.updateImplementationPlan(convId, wire);
      const cur = currentTab();
      if (cur !== null) tabs.setImplementation(cur.id, wireToState(updated));
    } catch (e) {
      error.value = describeError(e);
      revertLocal(prev);
    } finally {
      busy.value = false;
    }
  }

  async function updateItem(id: string, edit: ItemEdit): Promise<void> {
    const nextItems = items.value.map((it) =>
      it.id === id
        ? {
            ...it,
            ...(edit.assignedRole !== undefined
              ? { assignedRole: edit.assignedRole }
              : {}),
            ...(edit.title !== undefined ? { title: edit.title } : {}),
            ...(edit.description !== undefined
              ? { description: edit.description }
              : {}),
            ...(edit.acceptanceCriteria !== undefined
              ? { acceptanceCriteria: [...edit.acceptanceCriteria] }
              : {}),
            ...(edit.verifyCommand !== undefined
              ? { verifyCommand: edit.verifyCommand }
              : {}),
          }
        : it,
    );
    const wire = buildPatch((all) =>
      all.map((it) => {
        const base = fullPatchOf(it);
        if (it.id !== id) return base;
        if (edit.assignedRole !== undefined) base.assigned_role = edit.assignedRole;
        if (edit.title !== undefined) base.title = edit.title;
        if (edit.description !== undefined) base.description = edit.description;
        if (edit.acceptanceCriteria !== undefined)
          base.acceptance_criteria = [...edit.acceptanceCriteria];
        if (edit.verifyCommand !== undefined)
          base.verify_command = edit.verifyCommand;
        return base;
      }),
    );
    await persistItems(nextItems, wire);
  }

  async function deleteItem(id: string): Promise<void> {
    const nextItems = items.value.filter((it) => it.id !== id);
    // Omit the deleted id from the patch → backend deletes it (merge semantics);
    // echo every kept item's full editable fields so none is wiped.
    const wire = buildPatch((all) =>
      all.filter((it) => it.id !== id).map(fullPatchOf),
    );
    await persistItems(nextItems, wire);
  }

  async function addItem(item: NewItem): Promise<void> {
    const title = item.title.trim();
    if (title === "") return;
    // A fresh client id — the backend creates a new pending item for any id it
    // has not seen (merge-by-id "new id ⇒ insert"). Monotonic + random suffix
    // keeps it unique within the session.
    const newId = `impl-new-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const newVM: ImplementationItemVM = {
      id: newId,
      title,
      status: "pending",
      assignedRole: item.assignedRole ?? null,
      suggestedRole: null,
      resultSummary: null,
      lastError: null,
      description: item.description?.trim() ?? "",
      acceptanceCriteria: [],
      verifyCommand: "",
      dependsOn: [],
      attemptCount: 0,
    };
    const nextItems = [...items.value, newVM];
    const wire = buildPatch((all) => {
      const patch: ItemPatchWire[] = all.map(fullPatchOf);
      const fresh: ItemPatchWire = { id: newId, title };
      if (item.description !== undefined && item.description.trim() !== "")
        fresh.description = item.description.trim();
      if (item.assignedRole !== undefined && item.assignedRole !== null)
        fresh.assigned_role = item.assignedRole;
      patch.push(fresh);
      return patch;
    });
    await persistItems(nextItems, wire);
  }

  async function skipItem(id: string): Promise<void> {
    const nextItems = items.value.map((it) =>
      it.id === id ? { ...it, status: "skipped" } : it,
    );
    const wire = buildPatch((all) =>
      all.map((it) => {
        const base = fullPatchOf(it);
        if (it.id === id) base.status = "skipped";
        return base;
      }),
    );
    await persistItems(nextItems, wire);
  }

  /** Send a localized control message through the ordinary chat send path. This
   *  is how execution control reaches the control router (no dedicated route) —
   *  the backend recognizes the localized trigger text. */
  async function sendControl(text: string): Promise<void> {
    const tab = currentTab();
    if (tab === null) return;
    const messageId = tabs.pushUserMessage(tab.id, text);
    if (messageId === null) return; // tab not idle → no-op (defensive)
    const transport = transports.getTransport(tab.id);
    try {
      await transport.send(text, messageId);
    } catch {
      // The transport surfaces the failure via store.recordError + the
      // per-message send-error marker (ChatMessageList renders a retry banner).
    }
  }

  async function start(): Promise<void> {
    await sendControl(t("chat.implementation.controlMsg.start"));
  }
  async function pause(): Promise<void> {
    await sendControl(t("chat.implementation.controlMsg.pause"));
  }
  async function resume(): Promise<void> {
    await sendControl(t("chat.implementation.controlMsg.resume"));
  }
  async function stop(): Promise<void> {
    await sendControl(t("chat.implementation.controlMsg.stop"));
  }
  async function retry(): Promise<void> {
    await sendControl(t("chat.implementation.controlMsg.retry"));
  }

  return {
    state,
    items,
    phase,
    currentItem,
    busy,
    error,
    reload,
    updateItem,
    deleteItem,
    addItem,
    skipItem,
    start,
    pause,
    resume,
    stop,
    retry,
  };
}
