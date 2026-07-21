// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useDiscussion` — Multi-Agent discussion composable (block-5).
 *
 * Binds the conversation-scoped discussion store (`stores/discussion.ts`) to a
 * chat tab's reactive `discussion` config (`chatTabs.tab.discussion`). The
 * DiscussionPanel uses this for:
 *   - toggling discussion mode on/off,
 *   - switching the speaker selector (manager ↔ round_robin),
 *   - editing the round cap + judge flag,
 *   - CRUD over the named-participant registry,
 *   - "calling on" a participant (pinned speaker) for the next turn.
 *
 * It keeps the authoritative copy on the tab (single source of truth) and the
 * backend in sync: every mutation PATCHes / POSTs / DELETEs through the store
 * AND updates the tab's `discussion` so the SSE qs builder + frame handlers see
 * the change immediately. Network failures surface via the returned `error`
 * ref (the panel shows a toast / inline message); the optimistic local copy is
 * reverted on failure so the UI never lies (State-Truth-First).
 *
 * The composable owns NO module-level mutable state — it is instantiated per
 * panel and reads the active tab through the store getter each call, so there
 * is no stale-closure / global-ref anti-pattern.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useConversationsStore } from "@/stores/conversations";
import { useDiscussionStore, type ParticipantInput } from "@/stores/discussion";
import { useCloudModelStatus } from "@/composables/useCloudModelStatus";
import { DEFAULT_DISCUSSION_CONFIG } from "@/stores/_chatTabsTypes";
import type {
  DiscussionConfig,
  DiscussionParticipant,
  SelectorMode,
  TabId,
} from "@/stores/_chatTabsTypes";

export interface UseDiscussion {
  /** Reactive discussion config of the bound tab (or null when no tab). */
  readonly config: ComputedRef<DiscussionConfig | null>;
  /** Reactive participant registry of the bound tab. */
  readonly participants: ComputedRef<DiscussionParticipant[]>;
  /** Whether discussion mode is currently ON. */
  readonly isDiscussion: ComputedRef<boolean>;
  /** Id of the participant pinned to speak next (call-on), or null. */
  readonly pinnedSpeaker: ComputedRef<string | null>;
  /** True while a backend sync is in flight. */
  readonly busy: Ref<boolean>;
  /** Last error message (null when none). */
  readonly error: Ref<string | null>;
  /** Fetch the config + registry from the backend into the tab. */
  reload(): Promise<void>;
  /** Resolve the conversation id, lazily creating an empty conversation when
   *  the tab has none yet (so roster CRUD / template import can target it).
   *  Returns null only when there is no tab or the create call fails. */
  ensureConversation(): Promise<string | null>;
  /** Toggle discussion mode on/off (persists). */
  setDiscussionEnabled(enabled: boolean): Promise<void>;
  /** Switch the speaker selector strategy (persists). */
  setSelectorMode(mode: SelectorMode): Promise<void>;
  /** Set the hard round cap (persists). */
  setMaxRounds(rounds: number): Promise<void>;
  /** Toggle the final judge round (persists). */
  setEnableJudge(enabled: boolean): Promise<void>;
  /** Toggle the discussion convergence-control master switch (DISC-2 §22A.8;
   *  persists). When OFF the three sub-flags below have no effect. */
  setConvergenceControlEnabled(enabled: boolean): Promise<void>;
  /** Allow the manager to end the discussion early once converged (persists). */
  setManagerEarlyEndEnabled(enabled: boolean): Promise<void>;
  /** Soft-stop repeated / low-information turns (persists). */
  setSoftStopEnabled(enabled: boolean): Promise<void>;
  /** Set the soft-stop strategy id (DISC-2 §22A.8; persists). */
  setSoftStopMode(mode: string): Promise<void>;
  /** Set the social/lightweight-path response policy (DISC-2 §22A.7; persists). */
  setSocialResponsePolicy(policy: string): Promise<void>;
  /** Set the Manager scheduling-preference append text (DISC-2 §22A.7 P4-step2;
   *  persists). Manager-mode only; empty ⇒ no append (phase-1 prompt). */
  setManagerPromptAppend(text: string): Promise<void>;
  /** Toggle the "discussion → implementation" master switch (DISC-1 §22.7;
   *  persists). When ON an @mention + implementation verb routes the addressed
   *  role into implementation mode (tools opened up). */
  setImplementationEnabled(enabled: boolean): Promise<void>;
  /** Toggle the LLM grey-zone intent classifier (DISC-2 §22A.5; persists). */
  setIntentClassifierEnabled(enabled: boolean): Promise<void>;
  /** Toggle the OPTIONAL implementation item validator (DISC-1 三期-step5;
   *  persists). When ON each item gets an independent LLM acceptance review. */
  setImplementationValidatorEnabled(enabled: boolean): Promise<void>;
  /** Persist one DISC-1 TODO-2 tunable knob (run budget / soft-stop / classifier
   *  / planner). The key is the camelCase ``DiscussionConfig`` field; the value
   *  is sent verbatim (the backend DTO + resolver clamp out-of-range values). */
  setTunable(
    key:
      | "implMaxTotalFileEdits"
      | "implMaxTotalExecCalls"
      | "implMaxTotalRuntimeSeconds"
      | "implMaxTotalChangedFiles"
      | "softStopSimilarity"
      | "softStopMinRounds"
      | "softStopConsecutiveTurns"
      | "intentClassifierTimeoutMs"
      | "implementationPlannerTimeoutMs"
      | "implementationValidatorTimeoutMs"
      | "implementationVerifyCommandTimeoutMs",
    value: number,
  ): Promise<void>;
  /** Persist a model-id tunable (classifier / planner). Empty ⇒ "let the ladder
   *  decide" on the backend. */
  setModelTunable(
    key: "intentClassifierModel" | "implementationPlannerModel",
    value: string,
  ): Promise<void>;
  /** Set the discussion FRAMING prompt (persists). Empty ⇒ backend default. */
  setDiscussionPrompt(text: string): Promise<void>;
  /** Select the collaboration mode (design §26/§27 V1; persists). */
  setSelectedMode(modeId: string, policy?: string): Promise<void>;
  /** Create a new named participant (persists). */
  addParticipant(input: ParticipantInput): Promise<void>;
  /** Update an existing participant (persists). */
  editParticipant(id: string, input: ParticipantInput): Promise<void>;
  /** Remove a participant (persists). */
  removeParticipant(id: string): Promise<void>;
  /** Pin a participant to speak on the next turn (local — consumed by the SSE
   *  qs builder). Pass null to clear. */
  callOn(id: string | null): void;
}

/** Bind the discussion composable to a SPECIFIC tab id, or (default) the
 *  currently active tab resolved lazily on each call. */
export function useDiscussion(tabIdRef?: Ref<TabId | null>): UseDiscussion {
  const tabs = useChatTabsStore();
  const store = useDiscussionStore();
  const conversationsStore = useConversationsStore();
  const { t } = useI18n();
  const cloudStatus = useCloudModelStatus();

  const busy = ref(false);
  const error = ref<string | null>(null);

  /** Resolve the bound tab (explicit ref → that tab; else the active tab). */
  function currentTabId(): TabId | null {
    if (tabIdRef !== undefined) return tabIdRef.value;
    return tabs.activeTab?.id ?? null;
  }

  function currentTab() {
    const id = currentTabId();
    if (id === null) return null;
    return tabs.tabs.find((t) => t.id === id) ?? null;
  }

  const config = computed<DiscussionConfig | null>(
    () => currentTab()?.discussion ?? null,
  );
  const participants = computed<DiscussionParticipant[]>(
    () => config.value?.participants ?? [],
  );
  const isDiscussion = computed<boolean>(
    () => config.value?.isDiscussion === true,
  );
  const pinnedSpeaker = computed<string | null>(
    () => currentTab()?.pinnedSpeaker ?? null,
  );

  /** Resolve the conversation id needed for backend calls. Returns null when
   *  the tab has not yet created its conversation (use ``ensureConversation``
   *  to lazily create one for discussion CRUD). */
  function conversationId(): string | null {
    return currentTab()?.conversationId ?? null;
  }

  /** Resolve the conversation id, lazily CREATING an empty conversation when
   *  the tab has none yet. The discussion panel can be configured (toggle,
   *  participants) in a brand-new tab before the user sends the first message;
   *  the participant/discussion CRUD routes are conversation-scoped, so we
   *  materialise a real conversation on demand, bind it to the tab, and seed
   *  the sidebar — instead of erroring with ``no_conversation``. Returns null
   *  only when there is no tab at all or the create call fails. */
  async function ensureConversation(): Promise<string | null> {
    const tab = currentTab();
    if (tab === null) return null;
    const existing = tab.conversationId ?? null;
    if (existing !== null) return existing;
    // Create a real (empty) conversation for this tab. Title mirrors the
    // tab's default "新对话" label; the backend auto-titles on first send.
    const summary = await store.createConversation(tab.title || "新对话");
    const newId = typeof summary.id === "string" ? summary.id : null;
    if (newId === null) return null;
    tabs.setConversationId(tab.id, newId);
    // Seed the sidebar so the just-created conversation shows up immediately
    // (mirrors the transport's upsert-on-create, State-Truth-First).
    conversationsStore.upsert(summary as never);
    return newId;
  }

  /** Apply a local patch to the tab's discussion config (optimistic). Returns
   *  the previous config so the caller can revert on a backend failure. */
  function applyLocal(
    patch: Partial<DiscussionConfig>,
  ): DiscussionConfig | null {
    const tab = currentTab();
    if (tab === null) return null;
    const prev = tab.discussion;
    tabs.setDiscussion(tab.id, { ...prev, ...patch });
    return prev;
  }

  function revertLocal(prev: DiscussionConfig | null): void {
    const tab = currentTab();
    if (tab !== null && prev !== null) tabs.setDiscussion(tab.id, prev);
  }

  async function reload(): Promise<void> {
    const convId = conversationId();
    const tab = currentTab();
    if (convId === null || tab === null) return;
    busy.value = true;
    error.value = null;
    try {
      const cfg = await store.fetchConfig(convId);
      tabs.setDiscussion(tab.id, cfg);
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      busy.value = false;
    }
  }

  /** Shared "optimistic local + persisted PATCH (with revert on failure)" for a
   *  conversation-level setting. When the tab has no conversation yet the change
   *  stays LOCAL (no backend call) — it is persisted on the first send once the
   *  conversation row exists (reload picks it up). */
  async function patchSetting(
    patch: Partial<
      Pick<
        DiscussionConfig,
        | "isDiscussion"
        | "selectorMode"
        | "maxRounds"
        | "enableJudge"
        | "discussionPrompt"
        | "selectedModeId"
        | "modeSelectionPolicy"
        | "convergenceControlEnabled"
        | "managerEarlyEndEnabled"
        | "softStopEnabled"
        | "softStopMode"
        | "socialResponsePolicy"
        | "managerPromptAppend"
        | "implementationEnabled"
        | "intentClassifierEnabled"
        | "implMaxTotalFileEdits"
        | "implMaxTotalExecCalls"
        | "implMaxTotalRuntimeSeconds"
        | "implMaxTotalChangedFiles"
        | "softStopSimilarity"
        | "softStopMinRounds"
        | "softStopConsecutiveTurns"
        | "intentClassifierModel"
        | "intentClassifierTimeoutMs"
        | "implementationPlannerModel"
        | "implementationPlannerTimeoutMs"
        | "implementationValidatorEnabled"
        | "implementationValidatorTimeoutMs"
        | "implementationVerifyCommandTimeoutMs"
      >
    >,
  ): Promise<void> {
    const prev = applyLocal(patch);
    busy.value = true;
    error.value = null;
    try {
      const convId = await ensureConversation();
      if (convId === null) return; // no tab — keep the optimistic local copy
      await store.patchConfig(convId, patch);
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
      revertLocal(prev);
    } finally {
      busy.value = false;
    }
  }

  async function setDiscussionEnabled(enabled: boolean): Promise<void> {
    // When ENABLING discussion we seed the full toggle set (not just
    // `isDiscussion`) so the conversation's `meta["discussion"]` persists the
    // tab's current flag values — including the convergence flags and the
    // DISC-1/DISC-2 feature flags whose defaults otherwise stay only in the
    // front-end view-model (用户 2026-06-24 拍板「新建会话默认开」). A brand-new
    // tab carries DEFAULT_DISCUSSION_CONFIG (these flags = true), so enabling
    // its discussion truly persists them ON; an EXISTING conversation has had
    // its local config hydrated from the backend by `reload()`, so re-enabling
    // re-persists its own values (absent backend key ⇒ false ⇒ stays OFF) —
    // existing conversations are never silently flipped ON.
    if (!enabled) {
      await patchSetting({ isDiscussion: false });
      return;
    }
    // Guard (任务 3 兜底): the multi-Agent discussion runs exclusively on cloud
    // models; with none configured, turning it ON would dead-end. Block here so
    // every entry point that auto-enables discussion (import roster/agent
    // template, select a mode) is covered, and surface the same guidance as the
    // panel banner instead of silently failing.
    await cloudStatus.ensureChecked();
    if (!cloudStatus.hasCloudModels.value) {
      error.value = t("chat.discussion.noCloudModels.blocked");
      return;
    }
    const cfg = config.value ?? DEFAULT_DISCUSSION_CONFIG;
    await patchSetting({
      isDiscussion: true,
      selectorMode: cfg.selectorMode,
      maxRounds: cfg.maxRounds,
      enableJudge: cfg.enableJudge,
      convergenceControlEnabled: cfg.convergenceControlEnabled,
      managerEarlyEndEnabled: cfg.managerEarlyEndEnabled,
      softStopEnabled: cfg.softStopEnabled,
      softStopMode: cfg.softStopMode,
      socialResponsePolicy: cfg.socialResponsePolicy,
      implementationEnabled: cfg.implementationEnabled,
      intentClassifierEnabled: cfg.intentClassifierEnabled,
      // TODO-2 tunables: seed the new conversation with the tab's defaults so
      // the panel's shown numbers are actually persisted (mirrors the flag
      // seeding above). An existing conversation re-persists its own values.
      implMaxTotalFileEdits: cfg.implMaxTotalFileEdits,
      implMaxTotalExecCalls: cfg.implMaxTotalExecCalls,
      implMaxTotalRuntimeSeconds: cfg.implMaxTotalRuntimeSeconds,
      implMaxTotalChangedFiles: cfg.implMaxTotalChangedFiles,
      softStopSimilarity: cfg.softStopSimilarity,
      softStopMinRounds: cfg.softStopMinRounds,
      softStopConsecutiveTurns: cfg.softStopConsecutiveTurns,
      intentClassifierTimeoutMs: cfg.intentClassifierTimeoutMs,
      implementationPlannerTimeoutMs: cfg.implementationPlannerTimeoutMs,
      implementationValidatorEnabled: cfg.implementationValidatorEnabled,
      implementationValidatorTimeoutMs: cfg.implementationValidatorTimeoutMs,
      implementationVerifyCommandTimeoutMs:
        cfg.implementationVerifyCommandTimeoutMs,
      // Model ids: only seed when non-empty (empty ⇒ "let the ladder decide",
      // which we leave as an absent key rather than persisting a blank string).
      ...(cfg.intentClassifierModel
        ? { intentClassifierModel: cfg.intentClassifierModel }
        : {}),
      ...(cfg.implementationPlannerModel
        ? { implementationPlannerModel: cfg.implementationPlannerModel }
        : {}),
    });
  }
  async function setSelectorMode(mode: SelectorMode): Promise<void> {
    await patchSetting({ selectorMode: mode });
  }
  async function setMaxRounds(rounds: number): Promise<void> {
    const clamped = Math.max(1, Math.min(50, Math.floor(rounds)));
    await patchSetting({ maxRounds: clamped });
  }
  async function setEnableJudge(enabled: boolean): Promise<void> {
    await patchSetting({ enableJudge: enabled });
  }
  async function setConvergenceControlEnabled(enabled: boolean): Promise<void> {
    await patchSetting({ convergenceControlEnabled: enabled });
  }
  async function setManagerEarlyEndEnabled(enabled: boolean): Promise<void> {
    await patchSetting({ managerEarlyEndEnabled: enabled });
  }
  async function setSoftStopEnabled(enabled: boolean): Promise<void> {
    await patchSetting({ softStopEnabled: enabled });
  }
  async function setSoftStopMode(mode: string): Promise<void> {
    await patchSetting({ softStopMode: mode });
  }
  async function setSocialResponsePolicy(policy: string): Promise<void> {
    await patchSetting({ socialResponsePolicy: policy });
  }
  async function setManagerPromptAppend(text: string): Promise<void> {
    await patchSetting({ managerPromptAppend: text });
  }
  async function setImplementationEnabled(enabled: boolean): Promise<void> {
    await patchSetting({ implementationEnabled: enabled });
  }
  async function setIntentClassifierEnabled(enabled: boolean): Promise<void> {
    await patchSetting({ intentClassifierEnabled: enabled });
  }
  async function setImplementationValidatorEnabled(
    enabled: boolean,
  ): Promise<void> {
    await patchSetting({ implementationValidatorEnabled: enabled });
  }
  async function setTunable(
    key:
      | "implMaxTotalFileEdits"
      | "implMaxTotalExecCalls"
      | "implMaxTotalRuntimeSeconds"
      | "implMaxTotalChangedFiles"
      | "softStopSimilarity"
      | "softStopMinRounds"
      | "softStopConsecutiveTurns"
      | "intentClassifierTimeoutMs"
      | "implementationPlannerTimeoutMs"
      | "implementationValidatorTimeoutMs"
      | "implementationVerifyCommandTimeoutMs",
    value: number,
  ): Promise<void> {
    if (!Number.isFinite(value)) return;
    await patchSetting({ [key]: value } as Partial<DiscussionConfig>);
  }
  async function setModelTunable(
    key: "intentClassifierModel" | "implementationPlannerModel",
    value: string,
  ): Promise<void> {
    await patchSetting({ [key]: value } as Partial<DiscussionConfig>);
  }
  async function setDiscussionPrompt(text: string): Promise<void> {
    await patchSetting({ discussionPrompt: text });
  }
  async function setSelectedMode(
    modeId: string,
    policy = "manual",
  ): Promise<void> {
    await patchSetting({
      selectedModeId: modeId,
      modeSelectionPolicy: policy,
    });
  }

  async function addParticipant(input: ParticipantInput): Promise<void> {
    const tab = currentTab();
    if (tab === null) {
      error.value = "no_conversation";
      return;
    }
    busy.value = true;
    error.value = null;
    try {
      const convId = await ensureConversation();
      if (convId === null) {
        error.value = "no_conversation";
        return;
      }
      const created = await store.createParticipant(convId, input);
      const cur = currentTab();
      if (cur !== null) {
        tabs.setDiscussion(cur.id, {
          ...cur.discussion,
          participants: [...cur.discussion.participants, created],
        });
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      busy.value = false;
    }
  }

  async function editParticipant(
    id: string,
    input: ParticipantInput,
  ): Promise<void> {
    const convId = conversationId();
    const tab = currentTab();
    if (convId === null || tab === null) return;
    busy.value = true;
    error.value = null;
    try {
      const updated = await store.updateParticipant(convId, id, input);
      tabs.setDiscussion(tab.id, {
        ...tab.discussion,
        participants: tab.discussion.participants.map((p) =>
          p.id === id ? updated : p,
        ),
      });
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      busy.value = false;
    }
  }

  async function removeParticipant(id: string): Promise<void> {
    const convId = conversationId();
    const tab = currentTab();
    if (convId === null || tab === null) return;
    busy.value = true;
    error.value = null;
    try {
      await store.deleteParticipant(convId, id);
      tabs.setDiscussion(tab.id, {
        ...tab.discussion,
        participants: tab.discussion.participants.filter((p) => p.id !== id),
      });
      if (currentTab()?.pinnedSpeaker === id) {
        tabs.setPinnedSpeaker(tab.id, null);
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      busy.value = false;
    }
  }

  function callOn(id: string | null): void {
    const tab = currentTab();
    if (tab !== null) tabs.setPinnedSpeaker(tab.id, id);
  }

  return {
    config,
    participants,
    isDiscussion,
    pinnedSpeaker,
    busy,
    error,
    reload,
    ensureConversation,
    setDiscussionEnabled,
    setSelectorMode,
    setMaxRounds,
    setEnableJudge,
    setConvergenceControlEnabled,
    setManagerEarlyEndEnabled,
    setSoftStopEnabled,
    setSoftStopMode,
    setSocialResponsePolicy,
    setManagerPromptAppend,
    setImplementationEnabled,
    setIntentClassifierEnabled,
    setImplementationValidatorEnabled,
    setTunable,
    setModelTunable,
    setDiscussionPrompt,
    setSelectedMode,
    addParticipant,
    editParticipant,
    removeParticipant,
    callOn,
  };
}
