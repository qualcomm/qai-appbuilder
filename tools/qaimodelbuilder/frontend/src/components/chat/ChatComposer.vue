<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChatComposer — V1-style rich input area with toolbar (PR-054 rewrite).
 *
 * Structure:
 *   .input-area
 *     .input-toolbar  (model selector, params, AI coding mode pills)
 *     .chat-input-wrap
 *       .rich-input-box
 *         textarea.rich-input-textarea
 *         .rit-toolbar
 *           .rit-left  (Attach, Model Builder, App Builder, Coding, Translate)
 *           .rit-right (enhance, voice, send)
 *
 * Emits `submit` with the trimmed prompt text when the user hits Send.
 * The parent (`ChatView`) wires this to the active tab's transport.
 *
 * Keyboard:
 *   Enter           → submit (or enqueue while the tab is streaming,
 *                     V1 useChat.js:2820 `handleEnter`)
 *   Shift+Enter     → newline
 *
 * P4-A (T2): "+" attach button now opens an image picker. Selected
 * images are previewed inline; on submit each is uploaded via
 * `POST /api/images/upload` and a markdown image link is appended
 * to the outgoing prompt.
 */
import { ref, computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, useTemplateRef, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  useChatTabsStore,
  type ToolModeKey,
  type AwayAutoAnswerSettings,
} from "@/stores/chatTabs";
import { useUiStore, type ToolMode } from "@/stores/ui";
import { useVoiceInput, VOICE_ENGINES } from "@/composables/useVoiceInput";
import { useContextUsage } from "@/composables/chat/useContextUsage";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { useSkillsStore } from "@/stores/skills";
import { useClaudeCode } from "@/composables/useClaudeCode";
import { useOpenCode } from "@/composables/useOpenCode";
import { useLightbox } from "@/composables/useLightbox";
import { usePendingImages } from "@/composables/chat/usePendingImages";
import {
  pendingChatSubmit,
  drainChatSubmit,
  decideExternalSubmitAction,
} from "@/composables/chat/usePendingChatSubmit";
import { useOnboardingBubble } from "@/composables/chat/useOnboardingBubble";
import { usePill } from "@/composables/chat/usePill";
import { useToolModeParams } from "@/composables/chat/useToolModeParams";
import { useComposerModelSelection } from "@/composables/chat/useComposerModelSelection";
import { useComposerCtxBadge } from "@/composables/chat/useComposerCtxBadge";
import { useConversationBudget } from "@/composables/chat/useConversationBudget";
import { useToast } from "@/composables/useToast";
import { useComposerSubmit } from "@/composables/chat/useComposerSubmit";
import { usePromptHistory } from "@/composables/chat/usePromptHistory";
import { useScheduledContinuation } from "@/composables/chat/useScheduledContinuation";
import { useDiscussion } from "@/composables/chat/useDiscussion";
import {
  useMentionAutocomplete,
  type MentionCandidate,
} from "@/composables/chat/useMentionAutocomplete";
import { fmtKTokens, fmtKLimit, fmtPct } from "@/utils/contextBadge";
import PromptEnhanceBtn from "./PromptEnhanceBtn.vue";
import VoiceInputBtn from "./VoiceInputBtn.vue";
import ModelDropdown from "./ModelDropdown.vue";
import ModelParamsPopover from "./ModelParamsPopover.vue";
import WhisperEnginePopover from "./WhisperEnginePopover.vue";
import PromptHistoryPopover from "./PromptHistoryPopover.vue";
import SessionToolsPopover from "./SessionToolsPopover.vue";
import BudgetPopover from "./BudgetPopover.vue";
import BudgetDecisionDialog from "./BudgetDecisionDialog.vue";
import ScheduledContinuationPopover from "./ScheduledContinuationPopover.vue";
import AwayQuestionAutoAnswerDialog from "./AwayQuestionAutoAnswerDialog.vue";
import UserMessageJumpPopover from "./UserMessageJumpPopover.vue";
import ModeFrameModelBuilder from "./toolbar-modes/ModeFrameModelBuilder.vue";
import ModeFrameModelHub from "./toolbar-modes/ModeFrameModelHub.vue";
import ModeFrameAppBuilder from "./toolbar-modes/ModeFrameAppBuilder.vue";
import ModeFrameCoding from "./toolbar-modes/ModeFrameCoding.vue";
import ModeFrameTranslate from "./toolbar-modes/ModeFrameTranslate.vue";
import ModeFramePpt from "./toolbar-modes/ModeFramePpt.vue";
// Internal-only sub-toolbars (MB Pro / GoMaster). Referenced lazily and ONLY
// on the internal edition so the external open-source build tree-shakes the
// dynamic import() away — the modules are physically absent from that bundle
// and the source files can be removed without a broken static import.
const ModeFramePro = IS_INTERNAL
  ? defineAsyncComponent(() => import("@/components/chat/toolbar-modes/ModeFramePro.vue"))
  : null;
const ModeFrameGomaster = IS_INTERNAL
  ? defineAsyncComponent(() => import("@/components/chat/toolbar-modes/ModeFrameGomaster.vue"))
  : null;
import ComposerModeButtons from "./composer/ComposerModeButtons.vue";
import ActiveRunsButton from "./composer/ActiveRunsButton.vue";
import ComposerPendingImages from "./composer/ComposerPendingImages.vue";
import ComposerLightbox from "./composer/ComposerLightbox.vue";
import DiscussionPanel from "./DiscussionPanel.vue";
import MentionAutocomplete from "./MentionAutocomplete.vue";
import {
  discussionColorToken,
  type DiscussionParticipant,
} from "@/stores/_chatTabsTypes";
import { IS_INTERNAL } from "@/edition";

const { t } = useI18n();
const store = useChatTabsStore();
const ui = useUiStore();

const emit = defineEmits<{
  submit: [prompt: string];
  cancel: [];
  /**
   * UserMessageJumpPopover row click: scroll the conversation to the user
   * message with this id. Routed through `ChatView`, which calls the scroll
   * helper that `ChatMessageList` exposes via `defineExpose`. Composer stays
   * presentational — it doesn't reach into the message list directly.
   */
  "jump-to-message": [messageId: string];
}>();

// ─── Forge config (toolbar gates) ────────────────────────────────────────────
// Pulls the visible mode list from `useForgeConfig`, which mirrors V1's
// `forge_config.ui.toolbar_modules` nested dict. Settings → App Config
// edits flip the `enabled` flag; the v-for here re-renders immediately.
const {
  visibleToolbarModules,
  load: loadForgeConfig,
  config: forgeConfig,
  ccEnabled,
  ocEnabled,
} = useForgeConfig();

// ─── Claude Code / Open Code pills (T2.7-B / 功能块 7) ───────────────────────
// Mutually exclusive coding modes; declared early so the model-selection,
// submit, and pill composables can all reference them.
const claudeCode = useClaudeCode();
const openCode = useOpenCode();

const text = ref("");
const modelDropdownOpen = ref(false);
const paramsOpen = ref(false);
const whisperOpen = ref(false);
// Prompt history / favorites popover open state (the clock-icon button in
// `.rit-right`). Recording of sent prompts is wired via useComposerSubmit's
// `onSent` callback below; the popover reads/writes the shared store.
const promptHistoryOpen = ref(false);
const activeRunsOpen = ref(false);
// User-message-jump popover open state (the speech-bubble + list-lines button
// in `.rit-right`, placed immediately LEFT of the prompt-history clock). Lets
// the user pick any of their own past messages in the active tab and jump the
// conversation viewport to it. See `UserMessageJumpPopover.vue`.
const userMessageJumpOpen = ref(false);
const { recordSent: recordSentPrompt } = usePromptHistory();
// V1 parity (index.html:1313-1351) — CC effort dropdown open state.
const effortDropdownOpen = ref(false);
// Multi-Agent discussion (block-5) — DiscussionPanel popover open state +
// whether discussion mode is currently ON for the active tab (pill active).
const discussionOpen = ref(false);
const discussionActive = computed(
  () => store.activeTab?.discussion?.isDiscussion === true,
);

// Sub-agent "allow question" toggle. Shown ONLY when the active tab is a
// sub-agent tab (`kind === "subagent"`). Session-scoped + per-tab, default
// off. When on, the transport forwards `allow_question=true` so the backend
// advertises the blocking `question` tool to the taken-over sub-agent.
const isSubAgentTab = computed(
  () => store.activeTab?.kind === "subagent",
);
const isMainAgentTab = computed(
  () => store.activeTab !== null && store.activeTab.kind !== "subagent",
);

const subAgentAllowQuestion = computed(
  () => store.activeTab?.allowSubAgentQuestion === true,
);
function toggleSubAgentAllowQuestion(): void {
  const tab = store.activeTab;
  if (tab === null || tab.kind !== "subagent") return;
  store.setSubAgentAllowQuestion(tab.id, !subAgentAllowQuestion.value);
}

// Sub-agent spawn-permission toggles (V2 enhancement). Two SEPARATE per-tab
// switches with distinct semantics; only one is ever shown at a time because
// they are gated on `tab.kind`:
//   * MAIN agent tab (`kind !== "subagent"`) → `allowChildSpawn`: controls
//     whether the FIRST-LEVEL sub-agents this main agent spawns are themselves
//     allowed to create (second-level / grand) sub-agents. Default off keeps
//     the historical hard recursion guard. Forwarded as `allow_child_spawn`.
//   * SUB-agent take-over tab (`kind === "subagent"`) → `selfAllowSpawn`:
//     controls whether THIS taken-over sub-agent may create its own
//     sub-agents. Independent of the main toggle. Forwarded as
//     `self_allow_spawn`.
const allowChildSpawn = computed(
  () => store.activeTab?.allowChildSpawn === true,
);
function toggleAllowChildSpawn(): void {
  const tab = store.activeTab;
  if (tab === null || tab.kind === "subagent") return;
  store.setAllowChildSpawn(tab.id, !allowChildSpawn.value);
}
const selfAllowSpawn = computed(
  () => store.activeTab?.selfAllowSpawn === true,
);
function toggleSelfAllowSpawn(): void {
  const tab = store.activeTab;
  if (tab === null || tab.kind !== "subagent") return;
  store.setSelfAllowSpawn(tab.id, !selfAllowSpawn.value);
}

// Per-session tool / SKILL switches popover (this conversation only). Open
// state is local; the "active" dot lights up when the active tab has any
// session-level override (a tool/skill switched off for this session).
const sessionToolsOpen = ref(false);
const sessionToolsActive = computed(() => {
  const o = store.activeTab?.sessionToolOverride;
  return (
    o !== undefined &&
    (o.disabledTools.length > 0 || o.disabledSkills.length > 0)
  );
});

// Dedicated per-conversation TOKEN-budget popover (its own toolbar button,
// separate from the tools/skills panel). Only rendered for the MAIN agent tab
// (product spec: the user sets ONE cap for the whole session; sub-/grand-agents
// share the same root_conversation_id budget pool and do NOT expose the
// control). Open state is local.
const budgetPopoverOpen = ref(false);

// Scheduled-continuation ("定时继续检查") popover. Open state is local; the
// "active" dot lights up when the active tab has at least one timer bound to
// it. The timer state machine itself lives in `useScheduledContinuation`.
const schedulerOpen = ref(false);
const scheduler = useScheduledContinuation();
const schedulerActive = computed(() => {
  const id = store.activeTab?.id;
  if (id === undefined) return false;
  return scheduler.jobsForTab(id).some((j) => j.enabled);
});

// Per-tab "away auto-answer" settings dialog (this conversation only). The
// trigger's active dot reflects ONLY the active tab's `awayAutoAnswer.enabled`
// (per-tab, default-off) — switching tabs changes the dot. Editing opens a
// modal seeded from the active tab's settings; Save writes back via the store.
const awayAutoAnswerOpen = ref(false);
const awayAutoAnswerActive = computed(
  () => store.activeTab?.awayAutoAnswer?.enabled === true,
);
const awayAutoAnswerSettings = computed<AwayAutoAnswerSettings | undefined>(
  () => store.activeTab?.awayAutoAnswer,
);
function onAwayAutoAnswerSave(value: AwayAutoAnswerSettings): void {
  const id = store.activeTab?.id;
  if (id === undefined) return;
  store.setAwayAutoAnswer(id, value);
  awayAutoAnswerOpen.value = false;
}

/**
 * V1 parity (index.html:1464) — single textarea ESC handler that closes
 * every popover at once (`modelDropdownOpen / activeSubMenu /
 * effortDropdownOpen / modelParamsOpen / voiceEngineMenuOpen`). V2 keeps
 * each popover self-contained (no global keymap), so this composer-level
 * close fan-out runs only when the textarea is focused, mirroring V1.
 */
function closeAllPopovers(): void {
  modelDropdownOpen.value = false;
  paramsOpen.value = false;
  whisperOpen.value = false;
  effortDropdownOpen.value = false;
  sessionToolsOpen.value = false;
  budgetPopoverOpen.value = false;
  promptHistoryOpen.value = false;
  activeRunsOpen.value = false;
  schedulerOpen.value = false;
  userMessageJumpOpen.value = false;
}

// V1 parity (index.html:1042) — Params button is "active" (accent color)
// when the user has overridden the model defaults, NOT just because the
// popover is open. Mirrors V1's `{ active: !modelParamUseDefaults }`.
const paramsActive = computed<boolean>(
  () => store.activeTab !== null && !store.activeTab.modelParams.useDefaults,
);

// ─── Context usage badge (T2.7-C) — useComposerCtxBadge (ARCH-1 split) ───────
const {
  ctxConversationId,
  ctxInfo,
  ctxLoading,
  refreshCtx,
  ctxBadgeClass,
  ctxBadgeFooterTitle,
  ctxCompacted,
  ctxSavedPct,
  ctxOverLimit,
  onCtxBadgeClick,
} = useComposerCtxBadge();

// ─── Per-conversation TOKEN budget (max_budget_tokens) badge ─────────────────
// The persisted per-conversation cap (Conversation.meta.budget). The badge sits
// next to the context badge and is visible only when a positive cap is set for
// the active conversation. Refreshed on session switch (composable watch) and
// on each turn's END (`budgetExceededSignal` watcher below + the streaming→idle
// ctx refresh path). Reuses the ctx badge's three-tier severity token.
const toast = useToast();
const {
  snapshot: budgetSnapshot,
  enabled: budgetEnabled,
  pct: budgetPct,
  severity: budgetSeverity,
  refresh: refreshBudget,
  save: saveBudget,
} = useConversationBudget(ctxConversationId);

/** Show the budget mini-badge only when a positive cap is configured for the
 *  active conversation (task spec: `v-if` on `max_tokens > 0`). */
const showBudgetBadge = computed<boolean>(
  () =>
    budgetEnabled.value &&
    budgetSnapshot.value !== null &&
    budgetSnapshot.value.max_tokens !== null,
);

/** Severity → the same `ctx-ok / ctx-warn / ctx-danger` tint class the context
 *  badge uses, so the two badges are visually consistent (no hard-coded
 *  colours). */
const budgetBadgeClass = computed(() => `ctx-${budgetSeverity.value}`);

/** Integer percentage for the badge label (`预算 used/max (pct%)`). */
const budgetPctInt = computed<number>(() => Math.round(budgetPct.value * 100));

/** Progress-bar fill width, clamped to 100% (the label still shows the true
 *  percentage, which may exceed 100 on an over-budget turn). */
const budgetBarWidth = computed<string>(
  () => `${Math.min(100, Math.round(budgetPct.value * 100)).toString()}%`,
);

// Budget-decision dialog (max_budget_tokens). `frameHandlers.handleEnd` stamps a
// monotonic `budgetExceededSignal` + `budgetDecision` metadata on the active tab
// when the terminal END frame carries `reason: "budget_exceeded"`. Instead of a
// silent stop we open an interactive dialog letting the user choose: "continue"
// (raise the cap +raisePct% via PATCH .../budget, then resend a continuation
// turn) or "stop" (leave stopped). Watching in setup context is where i18n +
// budget save + submit live.
const budgetDecisionOpen = ref(false);
const budgetDecisionData = ref<{
  used: number;
  max: number;
  nextMax: number;
  raisePct: number;
} | null>(null);

watch(
  () => store.activeTab?.budgetExceededSignal,
  (next, prev) => {
    if (typeof next !== "number" || next === prev) return;
    void refreshBudget();
    const decision = store.activeTab?.budgetDecision;
    if (decision !== undefined) {
      // Show the interactive continue/stop dialog with the exact numbers.
      budgetDecisionData.value = { ...decision };
      budgetDecisionOpen.value = true;
    } else {
      // Legacy END without decision metadata: fall back to the prior toast.
      toast.warning(
        t(
          "chat.budget.exceededToast",
          "已达本会话 Token 预算上限，已停止。可在「本会话设置」调高上限或重置已用量。",
        ),
      );
    }
  },
);

/** Dialog "continue": raise the cap to `nextMax` (persisted via PATCH .../budget)
 *  then resend a continuation turn so the SAME work proceeds under the new cap. */
async function onBudgetContinue(): Promise<void> {
  const data = budgetDecisionData.value;
  budgetDecisionOpen.value = false;
  budgetDecisionData.value = null;
  // Dismiss the pending decision on the tab so a later tab-switch does NOT
  // re-fire the watcher (root-cause fix for the "切走切回弹窗又弹出" bug —
  // see `dismissBudgetDecision` in chatTabs.ts for the full rationale).
  // Must run BEFORE `saveBudget` — if the raise fails and the next END frame
  // fires again with a fresh signal, the fresh `Date.now()` !== undefined and
  // the dialog re-arms correctly.
  const tabId = store.activeTab?.id;
  if (tabId !== undefined) {
    store.dismissBudgetDecision(tabId);
  }
  if (data === null) return;
  // Persist the raised cap (State-Truth-First: the badge reflects the backend
  // result). `saveBudget` PATCHes the conversation's meta.budget.max_tokens.
  await saveBudget(data.nextMax, false);
  await refreshBudget();
  // Resend a lightweight continuation turn (same conversation/context) so the
  // model keeps working. The backend now sees a higher cap and won't re-stop
  // immediately. Enqueue-while-streaming safe: the tab is idle at END time.
  text.value = t("chat.budgetDecision.continuePrompt");
  autoResize();
  onSubmit();
}

/** Dialog "stop": leave the turn stopped; just close + refresh the badge. */
function onBudgetStop(): void {
  budgetDecisionOpen.value = false;
  budgetDecisionData.value = null;
  // Dismiss the pending decision on the tab so a later tab-switch does NOT
  // re-fire the watcher (root-cause fix for the "切走切回弹窗又弹出" bug —
  // see `dismissBudgetDecision` in chatTabs.ts for the full rationale).
  const tabId = store.activeTab?.id;
  if (tabId !== undefined) {
    store.dismissBudgetDecision(tabId);
  }
  void refreshBudget();
}

// Refresh the composer's OWN budget snapshot after each turn completes
// (streaming → idle), so the pill's used/pct grows turn-by-turn even when no
// budget-exceeded signal fires (the `budgetExceededSignal` watcher above only
// covers the cap-hit case). State-Truth-First: re-reads the persisted
// `meta.budget` counter the backend advanced.
watch(
  () => store.activeTab?.status,
  (next, prev) => {
    if (prev === "streaming" && next === "idle") {
      void refreshBudget();
    }
  },
);

/** BudgetPopover `saved` handler — the popover writes the cap through its OWN
 *  `useConversationBudget` instance; refresh THIS composer's instance so the
 *  pill appears / updates immediately (they are separate instances). */
function onBudgetSaved(): void {
  void refreshBudget();
}

/** Budget button "active" dot — lit when a positive cap is configured for the
 *  active conversation (parity with the other `.rit-right` toggles). */
const budgetActive = computed<boolean>(() => showBudgetBadge.value);

// ─── Session-cumulative I/O (↑ new input · ↓ output) ─────────────
// Shows TaskUsage ↑input ↓output next to the context badge. It
// accumulates over the WHOLE session PER-ROUND (Σ over every LLM round),
// monotonic:
//   ↑ = Σ adjustedInput = Σ max(0, input − cache_read − cache_write)
//        ← adjustedInputTokens
//   ↓ = Σ completion_tokens (output)
// cache_read / cache_write come from the DISPLAY-only observed fields
// (``*_cache_read_display`` / ``*_cache_write_display``) the backend
// tail-appends: on a cache-hit turn ``*_cache_read_tokens`` is ZEROED
// (eff-prompt keystone, must not double-add), so we read the observed value
// here — otherwise the badge would show the whole prompt as "new input".
//
// MAIN-AGENT 2-ROUND FIX (was showing 0/1 instead of ~4): a main-agent turn
// persists only ONE assistant message whose ``last_round_*`` bind to the
// FINAL round (Round 2 = cache-read hit → nets ~1), losing Round 1's net-new
// (write turn → nets ~3 = the user's sentence). The full total is ~4 by summing
// BOTH rounds. So per message we compute BOTH the first-round net-new and the
// last-round net-new and add them — but ONLY when first≠last. Sub-agent
// (one message per round) and single-round main-agent turns have first===last
// (the backend helper falls back to last_round when there is no distinct
// first_round_usage → the two display sets are byte-identical), so we count
// them ONCE. Per-round clamp (max(0, …)) is kept to mirror the Σ max(0,step).
// Null when the active tab has no usage-bearing assistant message → badge hides.
const cumulativeInputNew = computed<number | null>(() => {
  const tab = store.activeTab;
  if (tab === null) return null;
  let total = 0;
  let seen = false;
  for (const m of tab.messages) {
    if (m == null || m.role !== "assistant" || m.usage == null) continue;
    const u = m.usage;
    // Last-round net-new (the FINAL LLM round's wire − its cache read/write).
    const lastInp = u.last_round_prompt_tokens ?? u.prompt_tokens ?? 0;
    const lastNew = Math.max(
      0,
      lastInp -
        (u.last_round_cache_read_display ??
          u.last_round_cache_read_tokens ??
          0) -
        (u.last_round_cache_write_display ?? 0),
    );
    // First-round (round-0) net-new (the write-turn's user sentence on a
    // main-agent 2-round turn). Absent first_round_prompt_tokens ⇒ legacy /
    // single-round ⇒ treat as first===last (count once via lastNew below).
    const firstInp =
      u.first_round_prompt_tokens ?? u.last_round_prompt_tokens ?? u.prompt_tokens ?? 0;
    const firstNew = Math.max(
      0,
      firstInp -
        (u.first_round_cache_read_display ?? 0) -
        (u.first_round_cache_write_display ?? 0),
    );
    // first===last when the backend fell back (no distinct first_round_usage)
    // OR the figures coincide (single-round turn / sub-agent per-round stamp):
    // count ONCE. Only a genuine multi-round main-agent message (first≠last)
    // adds both rounds' net-new.
    const firstIsLast =
      u.first_round_prompt_tokens == null ||
      (u.first_round_prompt_tokens === u.last_round_prompt_tokens &&
        (u.first_round_cache_write_display ?? 0) ===
          (u.last_round_cache_write_display ?? 0) &&
        (u.first_round_cache_read_display ?? 0) ===
          (u.last_round_cache_read_display ?? 0));
    total += firstIsLast ? lastNew : firstNew + lastNew;
    seen = true;
  }
  return seen ? total : null;
});
const cumulativeOutput = computed<number | null>(() => {
  const tab = store.activeTab;
  if (tab === null) return null;
  let total = 0;
  let seen = false;
  for (const m of tab.messages) {
    if (m == null || m.role !== "assistant" || m.usage == null) continue;
    total += m.usage.completion_tokens ?? 0;
    seen = true;
  }
  return seen ? total : null;
});

/** Format a token count compactly: <1 000 → raw, ≥1 K → "12.3K", ≥1 M → "1.2M". */
function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

// ─── Model selection — useComposerModelSelection (ARCH-1 split) ──────────────
// Owns the status-dot colour, selected-model label, cloud auto-select,
// persisted-preference restore, local-model auto-load, and the
// streaming→idle `noteInferred` + ctx refresh. See the composable for the
// V1 parity notes (useModels.js).
const {
  hasModels,
  modelsLoading,
  selectedModelIsPlaceholder,
  modelDotStyle,
  selectedModelLabel,
  currentModelIsCloud,
  selectModel,
} = useComposerModelSelection({
  modelDropdownOpen,
  claudeCode,
  openCode,
  forgeConfig,
  refreshCtx,
});

// V1-parity auto-grow textarea (useChat.js:475-486). The textarea starts at
// one row and grows up to ~6 text lines (cap defined by the textarea's
// `max-height` in chat.css `.rich-input-textarea`, expressed as
// `calc((var(--text-md) * 1.6 * 6) + var(--space-4) + var(--space-2))` so the
// cap follows user font scaling / accessibility zoom). Beyond that the
// textarea overflows internally. Clearing the text (after send) resets it
// back to one row so there is no large blank area under the input (the
// "输入框下方大片空白" bug was caused by a fixed rows="3" with no auto-resize).
const textareaEl = useTemplateRef<HTMLTextAreaElement>("textareaEl");

function autoResize(event?: Event): void {
  const el = textareaEl.value;
  if (el === null) return;
  // Skip mid-IME-composition resizes; compositionend fires the final pass.
  const isComposing =
    event !== undefined && (event as InputEvent).isComposing === true;
  if (isComposing) return;
  el.style.height = "auto";
  // Read the LIVE max-height resolved by the browser from the CSS calc()
  // expression in `.rich-input-textarea` so the JS cap follows user font
  // scaling / accessibility zoom (KA-TEXTAREA-EM-1). `getComputedStyle`
  // returns the resolved px value (or "none" when no max-height is set on
  // the element). Fall back to scrollHeight if parsing fails so we never
  // over-shrink the textarea.
  let cap = Number.POSITIVE_INFINITY;
  const cssMaxHeight = window.getComputedStyle(el).maxHeight;
  if (cssMaxHeight !== "" && cssMaxHeight !== "none") {
    const parsed = parseFloat(cssMaxHeight);
    if (Number.isFinite(parsed) && parsed > 0) cap = parsed;
  }
  el.style.height = `${Math.min(el.scrollHeight, cap)}px`;
}

// When `text` is set programmatically (voice transcript append, prompt
// enhance replacement, quick-prompt insert) the native `input` event does
// not fire, so resize on the next tick to keep height in sync.
watch(text, () => {
  void nextTick(() => autoResize());
});

// Voice engine — read for the toolbar pill label. The actual recording
// happens inside <VoiceInputBtn>; this composable instance is a lightweight
// reader sharing the same backend preference.
const {
  engineId: voiceEngineId,
  isCurrentEngineWarm: voiceWarm,
  preloadState: voicePreloadState,
  refreshWorkerStatus: refreshVoiceWorkerStatus,
  // V1 parity (index.html:2220-2229) — footer voice-status line. These
  // surface the live recording / transcribing state so the footer can
  // show "Listening Ns" / "Transcribing…" while the (separate)
  // <VoiceInputBtn> instance drives the actual capture.
  isListening: voiceIsListening,
  isProcessing: voiceIsProcessing,
  isBusy: voiceIsBusy,
  recordSecs: voiceRecordSecs,
} = useVoiceInput();
const whisperLabel = computed(() => {
  const eng = VOICE_ENGINES.find((e) => e.id === voiceEngineId.value)
    ?? VOICE_ENGINES[0]!;
  return t(eng.labelKey, eng.label);
});

// V1 parity (index.html line 1124-1135): pill status dot tooltip.
const whisperDotTitle = computed(() => {
  if (voiceWarm.value) return t("voiceInput.warmTooltip");
  if (voicePreloadState.value === "loading") return t("voiceInput.loadingTooltip");
  return t("voiceInput.coldTooltip");
});

function toggleWhisperPopover(): void {
  whisperOpen.value = !whisperOpen.value;
  // Mirrors V1's `voiceEngineMenuOpen && refreshWorkerStatus()` (line 1113).
  if (whisperOpen.value) {
    void refreshVoiceWorkerStatus();
  }
}

// Prompt-history popover: fill the chosen prompt into the textarea (does NOT
// send — the user can still edit). Focus the textarea end on next tick so the
// caret lands after the inserted text; the `watch(text)` above auto-resizes.
function fillFromHistory(prompt: string): void {
  text.value = prompt;
  promptHistoryOpen.value = false;
  void nextTick(() => {
    autoResize();
    const el = textareaEl.value;
    if (el !== null) {
      el.focus();
      const end = el.value.length;
      el.setSelectionRange(end, end);
    }
  });
}

// UserMessageJumpPopover row click: forward the message id up to ChatView,
// which calls `ChatMessageList.scrollToMessage(id)` via its template ref. We
// also close the popover so the conversation is fully visible after the jump.
function onJumpToMessage(messageId: string): void {
  userMessageJumpOpen.value = false;
  emit("jump-to-message", messageId);
}

// ─── Image attachments (H3) — usePendingImages (F1① cohesion split) ──────────
// The pending-image queue + its file-picker / paste / external-intake /
// upload lifecycle live in `composables/chat/usePendingImages.ts`. The
// composer just binds the hidden <input> ref and forwards the textarea
// value (used as the conversation-title seed on lazy create).
const fileInputRef = useTemplateRef<HTMLInputElement>("fileInput");
const {
  pendingImages,
  openFilePicker,
  onFilesSelected,
  removePendingImage,
  handlePaste,
  uploadPendingImages,
} = usePendingImages(text, fileInputRef);

// Image lightbox for the pending-image thumbnails (V1 index.html:1446
// `@click="lightboxSrc = img.dataUrl"`). Shared logic with `ChatMessageList`
// via `useLightbox`.
const lightbox = useLightbox();

onMounted(() => {
  window.addEventListener("keydown", lightbox.onKeydown);
});
onBeforeUnmount(() => {
  window.removeEventListener("keydown", lightbox.onKeydown);
});

// ─── Submit / stop / keyboard — useComposerSubmit (ARCH-1 split) ─────────────
// Send/stop gating, placeholder + footer-hint text, the submit lifecycle
// (CC/OC routing + image upload + emit), and the Enter-enqueue-while-
// streaming path. See the composable for the V1 parity notes.
const {
  activeTab,
  canSubmit,
  showStop,
  inputPlaceholder,
  onSubmit,
  onStop,
  onKeydown,
  enqueueWithFeedback,
  canInject,
  injectWhileStreaming,
  appendToDraft,
} = useComposerSubmit({
  text,
  autoResize,
  uploadPendingImages,
  claudeCode,
  openCode,
  emit,
  onSent: (prompt: string) => recordSentPrompt(prompt),
});

// ─── @mention autocomplete (V2 enhancement 2026-06-21) ───────────────────────
// Active only when the tab is in discussion mode. Reads the current roster
// from ``useDiscussion()`` and feeds it to ``useMentionAutocomplete`` which
// owns the state machine. The popover renders just above the textarea
// (positioned by ``.rich-input-box``). Single-agent chat (the default)
// short-circuits inside ``refreshMention`` → zero behaviour change for that
// path.
const discussion = useDiscussion();
const mentionRoster = computed<MentionCandidate[]>(() => {
  if (!discussionActive.value) return [];
  return discussion.participants.value.map(
    (p: DiscussionParticipant, idx: number) => {
      const colorIdx =
        typeof p.config.color === "number" ? p.config.color : idx;
      return {
        name: p.display_name,
        color: discussionColorToken(colorIdx),
        modelId: p.model_id ?? null,
      };
    },
  );
});
const mention = useMentionAutocomplete(mentionRoster);

/** Inspect the textarea's current value + caret. Called from `@input`. */
function refreshMention(): void {
  if (!discussionActive.value) {
    if (mention.open.value) mention.close();
    return;
  }
  const ta = textareaEl.value;
  if (ta === null) return;
  mention.update(text.value, ta.selectionStart ?? text.value.length);
}

/**
 * Keyboard wedge — runs BEFORE the existing ``onKeydown`` from
 * :composable:`useComposerSubmit` so the autocomplete can intercept
 * ArrowUp / ArrowDown / Enter / Tab / Escape when its popover is open. When
 * the popover is closed (or there are no candidates), the event falls
 * through to ``onKeydown`` → preserved single-Agent submit semantics.
 *
 * Returns ``true`` when the event was consumed (host should NOT also call
 * the existing submit ``onKeydown``); ``false`` otherwise.
 */
function onMentionKeydown(ev: KeyboardEvent): boolean {
  if (!mention.open.value || !mention.hasCandidates.value) return false;
  switch (ev.key) {
    case "ArrowDown":
      ev.preventDefault();
      mention.move(1);
      return true;
    case "ArrowUp":
      ev.preventDefault();
      mention.move(-1);
      return true;
    case "Enter":
    case "Tab": {
      // Don't shadow Shift+Enter — that should still insert a newline.
      if (ev.shiftKey) return false;
      ev.preventDefault();
      acceptMention();
      return true;
    }
    case "Escape":
      ev.preventDefault();
      mention.close();
      return true;
    default:
      return false;
  }
}

/** Apply the highlighted candidate to the textarea + restore caret. */
function acceptMention(): void {
  const ta = textareaEl.value;
  if (ta === null) return;
  const result = mention.accept(
    text.value,
    ta.selectionStart ?? text.value.length,
  );
  if (result === null) return;
  text.value = result.text;
  void nextTick(() => {
    const el = textareaEl.value;
    if (el !== null) {
      el.focus();
      el.setSelectionRange(result.caret, result.caret);
    }
    autoResize();
  });
}

/** Click handler from the popover row — host-side commit. */
function onMentionPick(idx: number): void {
  // Move highlight to the clicked row, then accept (so a click on a
  // non-active row works identically to arrow+Enter).
  let guard = mentionRoster.value.length + 1;
  while (mention.activeIndex.value !== idx && guard > 0) {
    mention.move(1);
    guard -= 1;
    if (!mention.open.value) return;
  }
  acceptMention();
}

/** Composite keydown handler: mention wedge first, then existing submit. */
function onComposerKeydown(ev: KeyboardEvent): void {
  if (onMentionKeydown(ev)) return;
  onKeydown(ev);
}

/** Composite input handler: refresh mention scan, then auto-resize. */
function onComposerInput(): void {
  refreshMention();
  autoResize();
}

// Expose the mention autocomplete state as TOP-LEVEL computeds so the template
// tracks them reactively. ``mention`` is a plain object returned by the
// composable (NOT a reactive wrapper), so a template that reads
// ``mention.open.value`` does NOT establish a reactive dependency on the inner
// ref — the popover would never re-render when ``open`` flips. Re-exposing the
// inner refs through component-level computeds restores proper tracking.
const mentionOpen = computed(() => mention.open.value);
const mentionCandidates = computed(() => mention.candidates.value);
const mentionActiveIndex = computed(() => mention.activeIndex.value);

// In discussion mode the empty composer explains how to drive the discussion
// (ask everyone, or @-mention specific agents). Falls back to the normal
// single-agent placeholder otherwise. Tri-lingual via i18n (en/zh-CN/zh-TW).
const effectivePlaceholder = computed(() =>
  discussionActive.value
    ? t("chat.discussion.composerPlaceholder")
    : inputPlaceholder.value,
);

// ─── Programmatic submit intake (App Builder → Chat bridge) ──────────────────
// V1 parity (app.js:600-625): the App Builder "Send to Chat" set the shared
// composer input then ran the composer's submit (`sendMessage()`). V2's bridge
// cannot reach this component's local `text` ref / submit lifecycle directly,
// so it enqueues the composed prompt via `usePendingChatSubmit`; we drain it
// here and dispatch it through the SAME path a user-typed message uses.
//
// Tab-state handling mirrors V1 `sendMessage` (useChat.js:1552-1564) +
// `handleEnter` (useChat.js:2810-2835):
//   - error   → V1's `isStreaming` is reset to false after a failure (it never
//               error-gated a send), so a programmatic send must NOT be dropped
//               when the tab is in `error`. We `resetError()` → idle first,
//               matching V1's "a failed turn does not block the next send".
//               (The V2 `pushUserMessage` strictly requires `idle`, so without
//               this reset the send was silently dropped — the regression.)
//   - streaming/aborting → V1 enqueues instead of sending while streaming-here
//               (handleEnter:2820). We enqueue; ChatView's streaming→idle
//               dequeue watcher (ChatView.vue:207) auto-sends it when the
//               in-flight turn finishes.
//   - idle    → submit directly via the composer's normal `onSubmit()`.
//
// Any images the bridge forwarded are already in `pendingImages` (via the
// `pendingFileIntake` queue), so `onSubmit` uploads them and prepends the image
// prefix exactly like V1 `sendMessage()`.
async function drainExternalSubmit(): Promise<void> {
  const prompts = drainChatSubmit();
  for (const prompt of prompts) {
    const tab = store.activeTab;
    if (tab === null) continue;
    const action = decideExternalSubmitAction(tab.status);
    if (action === "enqueue") {
      // Busy here → enqueue WITH user feedback (toast: queued→success /
      // full→warning), identical to the manual Enter-while-streaming path
      // (useComposerSubmit.tryEnqueueWhileStreaming). Auto-sent on next idle by
      // ChatView's dequeue watcher. (Previously this enqueued silently, so the
      // user saw "点了没反应" — this restores the既有入队 UX.)
      enqueueWithFeedback(prompt);
      continue;
    }
    if (action === "reset-and-submit") {
      // A failed turn must not block the next send (V1 resets isStreaming).
      store.resetError(tab.id);
    }
    // Set the input then submit, mirroring V1's inputText + sendMessage().
    text.value = prompt;
    // Wait a tick so any just-enqueued pending images settle into the list
    // (and the resetError → idle transition flushes) before onSubmit() reads
    // `canSubmit` / pendingImages.
    await nextTick();
    await onSubmit();
  }
}
onMounted(() => {
  void drainExternalSubmit();
});
watch(
  () => pendingChatSubmit.value.length,
  (n) => {
    if (n > 0) void drainExternalSubmit();
  },
);

/**
 * Recall a pending message back into the composer for re-editing (the queue
 * bubble's ✎ "edit" affordance, F6). Appends `text` to the current draft
 * (never overwrites — `appendToDraft` from `useComposerSubmit`) and focuses the
 * textarea at its end so the user can keep typing. Exposed for ChatView, which
 * forwards `MessageQueuePanel`'s `edit` event here (the panel stays
 * presentational; the composer owns the textarea + draft).
 */
function recallToDraft(textToEdit: string): void {
  appendToDraft(textToEdit);
  void nextTick(() => {
    const el = textareaEl.value;
    if (el !== null) {
      el.focus();
      const end = el.value.length;
      el.setSelectionRange(end, end);
    }
  });
}

defineExpose({ recallToDraft });


function setToolMode(mode: ToolModeKey): void {
  const tab = store.activeTab;
  if (tab === null) return;
  // Toggle: clicking the active mode again clears it (V1 parity).
  const next: ToolModeKey | null = tab.activeMode === mode ? null : mode;
  store.setActiveMode(tab.id, next);
  // Mirror into the UI-global store so the rest of the layout (which
  // currently keys off `ui.activeToolMode`) stays in sync. Note the
  // type cast — `ToolMode` includes a 'ppt' value not in `ToolModeKey`.
  ui.setActiveToolMode(next as ToolMode);
}

const activeMode = computed<ToolModeKey | null>(
  () => store.activeTab?.activeMode ?? null,
);

// State-Truth-First: ``ChatTab.activeMode`` (per-tab, persisted) is the SINGLE
// source of truth for the active toolbar mode; ``ui.activeToolMode`` is only a
// global mirror the layout reads. They MUST agree, or they drift: when you
// switch to / open another conversation, the new tab's ``activeMode`` is its
// own value (``null`` for a fresh tab), but ``ui.activeToolMode`` would keep the
// PREVIOUS tab's mode — so the toolbar would still show e.g.「增强/Pro」while
// the send path (``useChatTransport`` reads the per-tab ``tab.activeMode``)
// routes to the dropdown's cloud model. That mismatch silently sent「Pro」turns
// to the cloud LLM instead of MB Pro. Re-mirror the global to the active tab's
// mode on every tab switch/creation (keyed on ``activeTabId`` so it does NOT
// fight ``setToolMode`` mutations within the SAME tab) to keep the one truth.
watch(
  () => store.activeTabId,
  () => {
    const mode = store.activeTab?.activeMode ?? null;
    if (ui.activeToolMode !== mode) {
      ui.setActiveToolMode(mode as ToolMode);
    }
    // Close every composer-local popover on a tab switch so a popover opened
    // on the previous tab does not linger / auto-reappear on the new tab (the
    // open-state refs are composer-level, not per-tab). Mirrors `closeAllPopovers`
    // but runs on the tab-switch boundary rather than only textarea ESC.
    closeAllPopovers();
  },
  { immediate: true },
);

// Enabled-skills count for the `⚡ N skills active` indicator (V1 parity).
// Shared store so the composer and the sidebar Skills badge agree.
const skillsStore = useSkillsStore();
const enabledSkillsCount = computed(() => skillsStore.enabledSkillsCount);

// ─── Assistant Onboarding bubble (F1② cohesion split) ───────────────────────
// First-time onboarding bubble for the AI-coding pills. All state +
// position math + ResizeObserver/window-resize lifecycle live in
// `composables/chat/useOnboardingBubble.ts` (V1 app.js:706-904 1:1 port).
// The composer binds the element refs in the template and passes the
// Settings gates + mode states as reactive getters.
const {
  ccPillEl,
  ocPillEl,
  assistantOnboardingBubbleEl,
  showAssistantOnboarding,
  assistantOnboardingPos,
  dismissAssistantOnboarding,
  hideAssistantOnboardingTransient,
} = useOnboardingBubble(
  ccEnabled,
  ocEnabled,
  () => claudeCode.isCCMode.value,
  () => openCode.isOCMode.value,
);

// ─── CC / OC assistant pills (F1③ cohesion split) ───────────────────────────
// Pill labels / tooltips / click + right-click handlers (mode entry/exit,
// panel toggle, CC↔OC mutual exclusion) live in
// `composables/chat/usePill.ts`. The onboarding transient-hide is threaded
// in so a pill click dismisses the bubble for this session (V1 parity).
const {
  ccPillLabel,
  ccPillTitle,
  onClickCCPill,
  onContextMenuCCPill,
  ocPillLabel,
  ocPillTitle,
  onClickOCPill,
  onContextMenuOCPill,
} = usePill(claudeCode, openCode, () => {
  if (showAssistantOnboarding.value) hideAssistantOnboardingTransient();
});

// Load forge-config once on mount; subsequent useForgeConfig() calls
// (e.g. AppConfigPanel) reuse the same module-singleton refs.
onMounted(() => {
  void loadForgeConfig();
});

// V1 parity (index.html:918-937 `bottom: inputAreaHeight+13`): expose the
// composer's measured height as a CSS var on the nearest `.chat-view`
// ancestor so the floating Stop button (`.stop-streaming-float` in
// ChatMessageList) can pin itself JUST ABOVE the composer regardless of
// how tall the toolbar grows (subagent strip, app-builder workbench,
// uploaded image thumbnails, etc.). Without this the float was
// statically anchored at `bottom: var(--space-4)` and visually drifted
// (the reported "时有时无 / 位置不固定" symptom — D / D2 of the bug list).
const inputAreaEl = useTemplateRef<HTMLDivElement>("inputAreaEl");
let _composerResizeObs: ResizeObserver | null = null;
function _findChatView(el: HTMLElement | null): HTMLElement | null {
  let cur: HTMLElement | null = el;
  while (cur !== null) {
    if (cur.classList.contains("chat-view")) return cur;
    cur = cur.parentElement;
  }
  return null;
}
function _publishInputAreaHeight(): void {
  const el = inputAreaEl.value;
  if (el === null) return;
  const host = _findChatView(el);
  if (host === null) return;
  // Round up so the float never overlaps the composer's top border.
  const h = Math.ceil(el.getBoundingClientRect().height);
  host.style.setProperty("--qai-chat-input-h", `${h}px`);
}
onMounted(() => {
  // ResizeObserver lacks a Window typing on older lib targets; gate on
  // the global to keep SSR-style imports safe and avoid throwing in
  // older browsers (graceful degradation = float falls back to the CSS
  // default `bottom: var(--space-4)`).
  if (typeof ResizeObserver === "undefined") return;
  _composerResizeObs = new ResizeObserver(() => _publishInputAreaHeight());
  if (inputAreaEl.value !== null) {
    _composerResizeObs.observe(inputAreaEl.value);
    _publishInputAreaHeight();
  }
});
onBeforeUnmount(() => {
  if (_composerResizeObs !== null) {
    _composerResizeObs.disconnect();
    _composerResizeObs = null;
  }
  // Clean up the CSS var so a Chat-view → other-view transition doesn't
  // leave a stale bottom offset on `.chat-view` if it's reused.
  const el = inputAreaEl.value;
  const host = el === null ? null : _findChatView(el);
  if (host !== null) {
    host.style.removeProperty("--qai-chat-input-h");
  }
});

// Map of v1-style key (e.g. `model_builder`) → ToolModeKey for the
// store. Only model_builder/app_builder/code/translate are typed
// ToolModeKey values; ppt is stored as a generic UI-side ToolMode.
function setToolModeFromKey(modeStr: string): void {
  const tab = store.activeTab;
  if (tab === null) return;
  const known: ReadonlySet<string> = new Set([
    "model-build",
    "model-hub",
    "app-builder",
    "code",
    "translate",
    "ppt",
    "pro",
    "gomaster",
  ]);
  if (known.has(modeStr)) {
    setToolMode(modeStr as ToolModeKey);
    return;
  }
  // Unknown future mode: mirror only to the global UI store.
  const next = ui.activeToolMode === modeStr ? null : (modeStr as ToolMode);
  ui.setActiveToolMode(next);
}

function exitMode(): void {
  const tab = store.activeTab;
  if (tab !== null) {
    store.setActiveMode(tab.id, null);
  }
  ui.setActiveToolMode(null);
}

/** Effective mode for sub-toolbar selection: ChatTab.activeMode is
 *  union-typed and excludes 'ppt'; ui.activeToolMode is the broader
 *  superset used for ppt. */
const effectiveMode = computed<ToolMode>(() =>
  activeMode.value !== null ? (activeMode.value as ToolMode) : ui.activeToolMode,
);

// ─── Tool-mode params + CC effort (F1⑤ cohesion split) ──────────────────────
// Per-tab code/translate/ppt params, translate auto-detect, and the CC
// effort dropdown live in `composables/chat/useToolModeParams.ts`. The
// composer passes `text` (auto-detect input), `effectiveMode`, the shared
// CC composable, and its own `effortDropdownOpen` ref (the ESC fan-out
// also toggles that flag, so it stays owned by the composer).
const {
  codeSpeed,
  codePersona,
  codeFilePath,
  codeRepoUrl,
  translateLang,
  pptLength,
  translateAuto,
  updateCodeSpeed,
  updateCodePersona,
  updateCodeFilePath,
  updateCodeRepoUrl,
  updateTranslateLang,
  updatePptLength,
  ccCurrentEffort,
  ccEffortLabel,
  ccEffortOptions,
  pickCcEffort,
} = useToolModeParams(text, effectiveMode, claudeCode, effortDropdownOpen);
</script>

<template>
  <div
    ref="inputAreaEl"
    class="input-area"
    data-testid="chat-composer"
  >
    <!-- Top toolbar: model selector + params + AI mode pills -->
    <div class="input-toolbar">
      <!-- Model selector trigger + 380px popover (V1 parity GT-2) -->
      <div class="model-selector-wrap">
        <button
          type="button"
          class="model-selector-btn"
          data-testid="model-selector-btn"
          :title="t('chat.modelDropdownHeader')"
          @click="modelDropdownOpen = !modelDropdownOpen"
        >
          <!-- V1 parity (index.html:946-954) — 5-state status dot via
               inline :style. CC mode / OC mode / loading / offline /
               placeholder / normal. Colour encoding: green=ready,
               grey=offline, amber=no selection, brand=CC/OC. -->
          <span
            class="model-dot"
            :style="modelDotStyle"
            :data-state="claudeCode.isCCMode.value
              ? 'cc'
              : openCode.isOCMode.value
                ? 'oc'
                : modelsLoading
                  ? 'loading'
                  : selectedModelIsPlaceholder
                    ? 'placeholder'
                    : 'online'"
          ></span>
          <span class="model-selector-label">{{ selectedModelLabel }}</span>
          <span aria-hidden="true">▾</span>
        </button>
        <ModelDropdown
          v-if="modelDropdownOpen"
          :selected-model-id="activeTab?.modelId ?? null"
          :selected-model-provider="activeTab?.modelProvider ?? ''"
          @select="selectModel"
          @close="modelDropdownOpen = false"
        />
      </div>
      <div class="model-params-wrap">
        <button
          type="button"
          class="toolbar-pill"
          :class="{ active: paramsActive }"
          data-testid="params-btn"
          :title="t('index.modelParamsTitleBtn')"
          @click="paramsOpen = !paramsOpen"
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <path
              d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"
            />
            <circle
              cx="12"
              cy="12"
              r="3"
            />
          </svg>
          <span>{{ t("index.modelParamsBtn") }}</span>
        </button>
        <ModelParamsPopover
          :open="paramsOpen"
          @update:open="paramsOpen = $event"
        />
      </div>

      <!-- Whisper pill — toolbar engine selector -->
      <div class="model-params-wrap">
        <button
          type="button"
          class="toolbar-pill rit-voice-engine-pill"
          :class="{ 'is-active': whisperOpen }"
          data-testid="whisper-pill"
          :title="t('voiceInput.engineMenuTitle')"
          @click="toggleWhisperPopover"
        >
          <!-- V1 parity (index.html lines 1116-1120): mic icon — three
               strokes (head / arc / single stem), NO base line. Round
               line caps & joins for the soft V1 silhouette. -->
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
            <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
            <line
              x1="12"
              y1="19"
              x2="12"
              y2="23"
            />
          </svg>
          <span class="rit-voice-engine-pill-label">{{ whisperLabel }}</span>
          <!-- V1 parity (index.html lines 1124-1135): green/pulse/grey dot -->
          <span
            class="rit-voice-engine-pill-dot"
            :class="{
              'is-warm': voiceWarm,
              'is-loading': !voiceWarm && voicePreloadState === 'loading',
            }"
            :title="whisperDotTitle"
            :data-testid="
              voiceWarm
                ? 'whisper-pill-dot-warm'
                : voicePreloadState === 'loading'
                  ? 'whisper-pill-dot-loading'
                  : 'whisper-pill-dot-cold'
            "
            aria-hidden="true"
          ></span>
          <!-- V1 parity (index.html lines 1136-1138): outline chevron
               caret — viewBox 0 0 10 6, single path "M1 1l4 4 4-4",
               stroke 1.5 / round / opacity 0.6. NOT a filled triangle. -->
          <svg
            width="9"
            height="9"
            viewBox="0 0 10 6"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
            style="opacity: 0.6"
          >
            <path d="M1 1l4 4 4-4" />
          </svg>
        </button>
        <WhisperEnginePopover
          :open="whisperOpen"
          @update:open="whisperOpen = $event"
        />
      </div>

      <!-- Multi-Agent discussion pill (block-5) — toggles the DiscussionPanel
           popover where the user enables discussion mode + configures named
           participants. Active state reflects whether discussion mode is ON for
           the current tab. Pure V2 enhancement (design §7); ordinary chat is
           unaffected when off. -->
      <div class="model-params-wrap">
        <button
          type="button"
          class="toolbar-pill"
          :class="{ active: discussionActive || discussionOpen }"
          data-testid="discussion-pill"
          :title="t('chat.discussion.title')"
          @click="discussionOpen = !discussionOpen"
        >
          <span aria-hidden="true">👥</span>
          <span>{{ t("chat.discussion.title") }}</span>
        </button>
        <div
          v-if="discussionOpen"
          class="discussion-popover"
          data-testid="discussion-popover"
        >
          <DiscussionPanel />
        </div>
      </div>

      <!-- Enabled skills indicator (V1 index.html:1185-1189) —
           `⚡ N skills active`, shown only when N > 0. Count comes from
           the shared skills store (mode !== 'off'), same source as the
           sidebar Skills nav badge. -->
      <div
        v-if="enabledSkillsCount > 0"
        class="rit-skills-active"
        data-testid="skills-active-indicator"
      >
        <span aria-hidden="true">⚡</span>
        <span>{{ t("chat.skillsActive", { n: enabledSkillsCount }) }}</span>
      </div>

      <!-- Claude Code pill (T2.7-B) — V1 parity (index.html:1191-1209).
           Settings gate: hidden when forge_config.ai_coding.cc.enabled
           is false. Click → enterCCMode; right-click → exitCCMode.
           V1 uses `class="btn btn-ghost btn-sm assistant-pill ..."` so the
           `.btn` primitive supplies the 1px solid border that
           `.assistant-pill { border-style: dashed }` then converts to
           dashed. Without `.btn`, the dashed override has no
           border-width to operate on and the pill renders flat. -->
      <button
        v-if="ccEnabled"
        ref="ccPillEl"
        type="button"
        class="btn btn-ghost btn-sm assistant-pill assistant-pill-cc"
        :class="{ active: claudeCode.isCCMode.value }"
        :style="claudeCode.isCCMode.value
          ? 'font-size:var(--text-sm);padding:2px 8px;border-radius:12px;display:flex;align-items:center;gap:4px;color:#7eb8f7;border-color:#7eb8f7;background:rgba(126,184,247,0.12)'
          : 'font-size:var(--text-sm);padding:2px 8px;border-radius:12px;display:flex;align-items:center;gap:4px'"
        data-testid="cc-pill"
        :title="ccPillTitle"
        @click="onClickCCPill"
        @contextmenu="onContextMenuCCPill"
      >
        <span aria-hidden="true">🤖</span>
        <span class="cc-pill-label">{{ ccPillLabel }}</span>
      </button>

      <!-- Open Code pill (功能块 7) — symmetric with CC. Settings gate:
           hidden when forge_config.ai_coding.oc.enabled is false.
           Click → enterOCMode; right-click → exitOCMode. Mutually
           exclusive with CC. -->
      <button
        v-if="ocEnabled"
        ref="ocPillEl"
        type="button"
        class="btn btn-ghost btn-sm assistant-pill assistant-pill-oc"
        :class="{ active: openCode.isOCMode.value }"
        :style="openCode.isOCMode.value
          ? 'font-size:var(--text-sm);padding:2px 8px;border-radius:12px;display:flex;align-items:center;gap:4px;color:#63b3ed;border-color:#63b3ed;background:rgba(99,179,237,0.12)'
          : 'font-size:var(--text-sm);padding:2px 8px;border-radius:12px;display:flex;align-items:center;gap:4px'"
        data-testid="oc-pill"
        :title="ocPillTitle"
        @click="onClickOCPill"
        @contextmenu="onContextMenuOCPill"
      >
        <span aria-hidden="true">🔷</span>
        <span class="oc-pill-label">{{ ocPillLabel }}</span>
      </button>

      <!-- First-time onboarding bubble (V1 index.html:1231-1247) —
           shown when ai_coding.cc/oc is enabled in Settings AND the user
           hasn't enabled either mode yet AND hasn't dismissed the bubble
           permanently AND hasn't transiently hidden it (by clicking
           CC/OC pill in this session). Anchor + position computed from
           the CC pill's bounding rect via ResizeObserver. -->
      <div
        v-if="showAssistantOnboarding"
        ref="assistantOnboardingBubbleEl"
        class="assistant-onboarding-bubble"
        :style="assistantOnboardingPos.bubbleStyle"
      >
        <div
          class="assistant-onboarding-arrow"
          :style="assistantOnboardingPos.arrowStyle"
        ></div>
        <div class="assistant-onboarding-title">
          💡 {{ t("index.assistantOnboardingTitle") }}
        </div>
        <div class="assistant-onboarding-body">
          {{ t("index.assistantOnboardingBody") }}
        </div>
        <button
          type="button"
          class="assistant-onboarding-close"
          @click="dismissAssistantOnboarding"
        >
          {{ t("index.assistantOnboardingGotIt") }}
        </button>
      </div>

      <!-- Toolbar-top context-usage badge (V1 index.html:1253-1310).
           Visible when an active conversation has a context reading;
           sits inline with the model selector / CC / OC pills so the
           usage % is glanceable without scanning to the footer. The
           footer also keeps its own ctx badge (V1 双显示, line 2231).
           Reuses the global `.ctx-badge-toolbar` token (components.css
           L941-978) — severity tint comes from `useContextUsage`. -->
      <span
        v-if="ctxConversationId !== null && ctxInfo !== null"
        class="ctx-badge-toolbar ctx-badge-toolbar--clickable"
        :class="[ctxBadgeClass, { 'ctx-badge-toolbar--compacted': ctxCompacted }]"
        data-testid="ctx-badge-toolbar"
        :title="ctxBadgeFooterTitle"
        @click="onCtxBadgeClick"
      >
        <!-- Compacted state (V2 enhancement): 🗜 原始全量 → 压缩后 · 省 N%.
             The "before" number is the PRE-compaction full usage
             (`estimated_tokens`) — the SAME base `ctxSavedPct` divides by —
             NOT the context-window limit, so `before → after · 省%` is
             internally consistent (saved% = 1 − after/before). -->
        <template v-if="ctxCompacted">
          <svg
            width="11"
            height="11"
            viewBox="0 0 16 16"
            fill="none"
            style="flex-shrink:0"
            aria-hidden="true"
          >
            <!-- compress: two chevrons squeezing toward the centre line -->
            <path
              d="M5 2.5 8 5.5 11 2.5"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
            <path
              d="M5 13.5 8 10.5 11 13.5"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
            <line
              x1="3.5"
              y1="8"
              x2="12.5"
              y2="8"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
            />
          </svg>
          <span class="ctx-badge-orig">~{{ fmtKTokens(ctxInfo.estimated_tokens) }}K</span>
          <span
            class="ctx-badge-arrow"
            aria-hidden="true"
          >→</span>
          <!-- 压缩后实际发给模型的大小：带显式 label，让用户一眼看清
               这才是当前真正发给模型的上下文，而非又一个全量占用数。 -->
          <span
            class="ctx-badge-tomodel-label"
            :title="t('chat.composer.ctxToModelTitle')"
          >{{ t("chat.composer.ctxToModelLabel") }}</span>
          <span
            class="ctx-badge-compacted"
            :title="t('chat.composer.ctxToModelTitle')"
          >~{{ fmtKTokens(ctxInfo.compactedTokens ?? 0) }}K</span>
          <span style="opacity:0.55">/ {{ fmtKLimit(ctxInfo.context_limit) }}K</span>
          <span class="ctx-badge-saved">{{ t("chat.composer.ctxSaved", { pct: ctxSavedPct }) }}</span>
        </template>

        <!-- Uncompacted state (unchanged V1 parity): ~12.3K / 200K · 6% -->
        <template v-else>
          <svg
            width="11"
            height="11"
            viewBox="0 0 16 16"
            fill="none"
            style="flex-shrink:0"
            aria-hidden="true"
          >
            <rect
              x="1"
              y="3"
              width="14"
              height="10"
              rx="2"
              stroke="currentColor"
              stroke-width="1.5"
            />
            <rect
              x="3"
              y="6"
              width="4"
              height="4"
              rx="0.5"
              fill="currentColor"
              opacity="0.7"
            />
            <rect
              x="9"
              y="6"
              width="4"
              height="4"
              rx="0.5"
              fill="currentColor"
              opacity="0.3"
            />
          </svg>
          <span>~{{ fmtKTokens(ctxInfo.estimated_tokens) }}K</span>
          <span style="opacity:0.55">/ {{ fmtKLimit(ctxInfo.context_limit) }}K</span>
          <span class="ctx-badge-pct">{{ fmtPct(ctxInfo.usage_pct) }}%</span>
          <!-- Session-cumulative I/O: ↑ Σ cache-adjusted new input ·
               ↓ Σ output. Shows TaskUsage; hidden when no usage-bearing
               assistant message exists yet or both sides are zero. -->
          <span
            v-if="cumulativeInputNew != null && (cumulativeInputNew > 0 || (cumulativeOutput ?? 0) > 0)"
            class="ctx-badge-io"
            :title="t('chat.composer.ctxIoTitle')"
          >
            <span class="ctx-io-up">↑{{ fmtTokens(cumulativeInputNew) }}</span>
            <span class="ctx-io-down">↓{{ fmtTokens(cumulativeOutput ?? 0) }}</span>
          </span>
          <!-- Over-window marker: the real (un-clamped) history exceeds the
               model window, so the prompt no longer fits and compaction is
               imminent. Only shown when usage ≥ 100% (ctxOverLimit). -->
          <span
            v-if="ctxOverLimit"
            class="ctx-badge-over"
          >{{ t("chat.composer.ctxOver") }}</span>
        </template>
      </span>

      <!-- Per-conversation TOKEN budget mini-badge (max_budget_tokens).
           Visible only when a positive cap is set for the active conversation.
           Sits right of the context badge with the SAME height / radius / font
           tokens (`.ctx-badge-toolbar`) so the two read as a pair; the fill
           severity reuses the ctx `ctx-ok / ctx-warn / ctx-danger` tint
           (70% warn / 90% danger). No hard-coded colours — all theme vars. -->
      <span
        v-if="showBudgetBadge && budgetSnapshot !== null && budgetSnapshot.max_tokens !== null"
        class="ctx-badge-toolbar budget-badge-toolbar"
        :class="budgetBadgeClass"
        data-testid="budget-badge-toolbar"
        :title="
          t('chat.composer.budgetBadgeTitle', {
            used: budgetSnapshot.used_tokens.toLocaleString(),
            max: budgetSnapshot.max_tokens.toLocaleString(),
            pct: budgetPctInt,
          })
        "
      >
        <span class="budget-badge-label">{{ t("chat.composer.budgetBadgeLabel", "预算") }}</span>
        <span>{{ fmtKTokens(budgetSnapshot.used_tokens) }}K / {{ fmtKLimit(budgetSnapshot.max_tokens) }}K</span>
        <span class="ctx-badge-pct">{{ budgetPctInt }}%</span>
        <span
          class="budget-badge-bar"
          aria-hidden="true"
        >
          <span
            class="budget-badge-bar-fill"
            :style="{ width: budgetBarWidth }"
          ></span>
        </span>
      </span>


      <!-- CC effort dropdown (V1 index.html:1313-1351) — only when CC mode
           active AND a session exists. 5 options (default / 低 / 中 / 高 /
           最大), disabled while CC is streaming. Reuses the global
           `.effort-btn` / `.effort-dropdown` / `.effort-item` token
           (components.css L683-773). -->
      <div
        v-if="claudeCode.isCCMode.value && claudeCode.activeSessionId.value !== null"
        class="cc-effort-wrap"
      >
        <button
          type="button"
          class="effort-btn"
          :class="{ 'effort-btn--active': ccCurrentEffort !== null }"
          :disabled="claudeCode.streaming.value"
          data-testid="cc-effort-btn"
          :title="t('index.effortBtnTitle')"
          @mousedown.stop
          @click.stop="effortDropdownOpen = !effortDropdownOpen"
        >
          <span aria-hidden="true">🧠</span>
          <span>{{ ccEffortLabel }}</span>
          <svg
            width="9"
            height="9"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2.5"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <polyline
              v-if="effortDropdownOpen"
              points="6 9 12 15 18 9"
            />
            <polyline
              v-else
              points="18 15 12 9 6 15"
            />
          </svg>
        </button>
        <div
          v-if="effortDropdownOpen"
          class="effort-dropdown"
          data-testid="cc-effort-dropdown"
          @mousedown.stop
        >
          <div class="effort-dropdown-header">
            {{ t("index.effortLabel") }}
          </div>
          <div
            v-for="opt in ccEffortOptions"
            :key="opt.value ?? '__default__'"
            class="effort-item"
            :class="{ 'effort-item--selected': ccCurrentEffort === opt.value }"
            @click="pickCcEffort(opt.value)"
          >
            <span class="effort-item-label">{{ t(opt.labelKey) }}</span>
            <span
              v-if="ccCurrentEffort === opt.value"
              class="effort-item-check"
            >✓</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Rich input box -->
    <div class="chat-input-wrap">
      <div class="rich-input-box">
        <!-- Pending image thumbnails (H3) — V1 index.html:1444-1449.
             ComposerPendingImages renders the strip INSIDE `.rich-input-box`
             at the top so it shares the input frame's border/background
             (V1 parity). Class names + global tokens unchanged. -->
        <ComposerPendingImages
          :pending-images="pendingImages"
          @open="lightbox.open($event)"
          @remove="removePendingImage"
        />

        <!-- Hidden file input bound to the "+" attach button -->
        <input
          ref="fileInput"
          type="file"
          accept="image/*"
          multiple
          class="rit-file-input"
          @change="onFilesSelected"
        />
        <!-- Textarea + @mention popover wrap: the popover anchors to THIS
             container (position: relative) so it floats directly above the
             textarea instead of the entire ``.rich-input-box`` (which would
             place it far above, on top of the pending-images strip / mode
             toolbar). -->
        <div class="rich-input-textarea-wrap">
          <textarea
            ref="textareaEl"
            v-model="text"
            class="rich-input-textarea"
            :placeholder="effectivePlaceholder"
            :aria-label="effectivePlaceholder"
            :disabled="activeTab === null"
            data-testid="chat-input"
            rows="1"
            @keydown="onComposerKeydown"
            @keydown.escape="closeAllPopovers"
            @input="onComposerInput"
            @compositionend="autoResize"
            @paste="handlePaste"
            @blur="mention.close()"
            @click="refreshMention"
          ></textarea>
          <!-- @mention autocomplete popover (discussion mode only).
               Anchored to the textarea wrapper above so it sits directly
               above the textarea. Renders nothing when discussion mode is
               off or the roster filter is empty (see internal v-if). -->
          <MentionAutocomplete
            :open="mentionOpen"
            :candidates="mentionCandidates"
            :active-index="mentionActiveIndex"
            @pick="onMentionPick"
            @hover="(idx) => { while (mention.activeIndex.value !== idx) mention.move(1); }"
          />
        </div>

        <div class="rit-toolbar">
          <!-- Default toolbar (no active mode) — attach + data-driven mode
               buttons (ARCH-1 split into ComposerModeButtons). -->
          <ComposerModeButtons
            v-if="effectiveMode === null"
            :modules="visibleToolbarModules"
            :effective-mode="effectiveMode"
            @attach="openFilePicker"
            @pick-mode="setToolModeFromKey"
          />

          <!-- Active-mode sub-toolbars — only one renders at a time. -->
          <ModeFrameModelBuilder
            v-else-if="effectiveMode === 'model-build'"
            @exit="exitMode"
          />
          <ModeFrameAppBuilder
            v-else-if="effectiveMode === 'app-builder'"
            @exit="exitMode"
          />
          <ModeFrameModelHub
            v-else-if="effectiveMode === 'model-hub'"
            @exit="exitMode"
            @fill-prompt="appendToDraft"
          />
          <ModeFrameCoding
            v-else-if="effectiveMode === 'code'"
            :speed="codeSpeed"
            :persona="codePersona"
            :file-path="codeFilePath"
            :repo-url="codeRepoUrl"
            :current-model-is-cloud="currentModelIsCloud"
            @exit="exitMode"
            @update:speed="updateCodeSpeed"
            @update:persona="updateCodePersona"
            @update:file-path="updateCodeFilePath"
            @update:repo-url="updateCodeRepoUrl"
          />
          <ModeFrameTranslate
            v-else-if="effectiveMode === 'translate'"
            :lang="translateLang"
            :auto-detect="translateAuto"
            @exit="exitMode"
            @update:lang="updateTranslateLang"
            @update:auto-detect="translateAuto = $event"
          />
          <ModeFramePpt
            v-else-if="effectiveMode === 'ppt'"
            :length="pptLength"
            @exit="exitMode"
            @update:length="updatePptLength"
          />
          <component
            :is="ModeFramePro"
            v-else-if="ModeFramePro && effectiveMode === 'pro'"
            @exit="exitMode"
          />
          <component
            :is="ModeFrameGomaster"
            v-else-if="ModeFrameGomaster && effectiveMode === 'gomaster'"
            @exit="exitMode"
          />

          <div class="rit-right">
            <button
              v-if="isSubAgentTab"
              type="button"
              class="rit-btn rit-question-toggle"
              :class="{ 'rit-question-toggle--active': subAgentAllowQuestion }"
              data-testid="subagent-question-toggle"
              :title="subAgentAllowQuestion
                ? t('chat.subAgent.allowQuestionOn', '允许子 Agent 提问（已开启）')
                : t('chat.subAgent.allowQuestion', '允许子 Agent 提问')"
              :aria-label="t('chat.subAgent.allowQuestion', '允许子 Agent 提问')"
              :aria-pressed="subAgentAllowQuestion"
              @click="toggleSubAgentAllowQuestion"
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
                aria-hidden="true"
              >
                <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
                <path d="M9.6 9a2.4 2.4 0 0 1 4.7.6c0 1.6-2.4 2.4-2.4 2.4" />
                <line
                  x1="12"
                  y1="15.5"
                  x2="12"
                  y2="15.5"
                />
              </svg>
            </button>
            <!-- Dedicated per-conversation TOKEN-budget button (wallet /
                 gauge). MAIN agent tab only (product spec: one cap for the
                 whole session; sub-/grand-agents share the same budget pool and
                 do not expose the control). Active dot lights when a positive
                 cap is set. Sits to the LEFT of the session-tools button. -->
            <span
              v-if="isMainAgentTab"
              class="rit-budget-wrap"
            >
              <button
                type="button"
                class="rit-btn rit-budget-btn"
                :class="{ 'rit-budget-btn--active': budgetActive }"
                data-testid="budget-toggle"
                :title="t('chat.budgetPopover.buttonTitle', '设置本会话 Token 用量预算')"
                :aria-label="t('chat.budgetPopover.buttonTitle', '设置本会话 Token 用量预算')"
                :aria-pressed="budgetPopoverOpen"
                @click="budgetPopoverOpen = !budgetPopoverOpen"
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
                  aria-hidden="true"
                >
                  <!-- Wallet: rounded body + flap + clasp -->
                  <path d="M3 7a2 2 0 0 1 2-2h13a1 1 0 0 1 1 1v2" />
                  <rect
                    x="3"
                    y="7"
                    width="18"
                    height="12"
                    rx="2"
                  />
                  <path d="M16 12h2" />
                </svg>
                <span
                  v-if="budgetActive"
                  class="rit-budget-dot"
                  aria-hidden="true"
                />
              </button>
              <BudgetPopover
                :open="budgetPopoverOpen"
                @update:open="budgetPopoverOpen = $event"
                @saved="onBudgetSaved"
              />
            </span>
            <span class="rit-session-tools-wrap">
              <button
                type="button"
                class="rit-btn rit-session-tools-btn"
                :class="{ 'rit-session-tools-btn--active': sessionToolsActive }"
                data-testid="session-tools-toggle"
                :title="t('chat.sessionTools.title', '本会话工具 / 技能')"
                :aria-label="t('chat.sessionTools.title', '本会话工具 / 技能')"
                :aria-pressed="sessionToolsOpen"
                @click="sessionToolsOpen = !sessionToolsOpen"
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
                  aria-hidden="true"
                >
                  <line
                    x1="4"
                    y1="21"
                    x2="4"
                    y2="14"
                  />
                  <line
                    x1="4"
                    y1="10"
                    x2="4"
                    y2="3"
                  />
                  <line
                    x1="12"
                    y1="21"
                    x2="12"
                    y2="12"
                  />
                  <line
                    x1="12"
                    y1="8"
                    x2="12"
                    y2="3"
                  />
                  <line
                    x1="20"
                    y1="21"
                    x2="20"
                    y2="16"
                  />
                  <line
                    x1="20"
                    y1="12"
                    x2="20"
                    y2="3"
                  />
                  <line
                    x1="1"
                    y1="14"
                    x2="7"
                    y2="14"
                  />
                  <line
                    x1="9"
                    y1="8"
                    x2="15"
                    y2="8"
                  />
                  <line
                    x1="17"
                    y1="16"
                    x2="23"
                    y2="16"
                  />
                </svg>
                <span
                  v-if="sessionToolsActive"
                  class="rit-session-tools-dot"
                  aria-hidden="true"
                />
              </button>
              <SessionToolsPopover
                :open="sessionToolsOpen"
                @update:open="sessionToolsOpen = $event"
              />
            </span>
            <!-- Active runs ("运行中会话"): second position (user spec
                 2026-06-28 — moved here from after the scheduler). Opens the
                 running-sessions popover; self-contained component owning its
                 own waveform icon + active state. -->
            <ActiveRunsButton v-model:open="activeRunsOpen" />
            <!-- Sub-agent spawn-permission toggle (V2 enhancement). Exactly
                 ONE of the two buttons below renders,
                 gated on the active tab `kind`:
                   * MAIN agent tab → "allow first-level sub-agents to spawn
                     their OWN sub-agents" (`allowChildSpawn`). The icon shows a
                     root node branching to a child that itself branches to a
                     grandchild — conveying "children may create grandchildren".
                   * SUB-agent take-over tab → "allow THIS sub-agent to create
                     sub-agents" (`selfAllowSpawn`). The icon shows a single
                     node branching to two children — conveying "self creates
                     children". Both use the shared `rit-btn` idiom + the
                     accent-coloured active dot (parity with the other toggles
                     in this row). -->
            <button
              v-if="isMainAgentTab"
              type="button"
              class="rit-btn rit-spawn-toggle"
              :class="{ 'rit-spawn-toggle--active': allowChildSpawn }"
              data-testid="allow-child-spawn-toggle"
              :title="allowChildSpawn
                ? t('chat.subAgent.allowChildSpawnOn', '允许一级子 Agent 创建子 Agent（已开启）')
                : t('chat.subAgent.allowChildSpawn', '允许一级子 Agent 创建子 Agent')"
              :aria-label="t('chat.subAgent.allowChildSpawn', '允许一级子 Agent 创建子 Agent')"
              :aria-pressed="allowChildSpawn"
              @click="toggleAllowChildSpawn"
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
                aria-hidden="true"
              >
                <circle cx="5" cy="4" r="2.1" />
                <circle cx="12" cy="12" r="2.1" />
                <circle cx="12" cy="20" r="2.1" />
                <circle cx="19" cy="20" r="2.1" />
                <path d="M6.5 5.5 10.6 10.6" />
                <path d="M12 14.1V17.9" />
                <path d="M13.5 13.6 17.5 18.4" />
              </svg>
              <span
                v-if="allowChildSpawn"
                class="rit-spawn-dot"
                aria-hidden="true"
              />
            </button>
            <button
              v-else-if="isSubAgentTab"
              type="button"
              class="rit-btn rit-spawn-toggle rit-spawn-toggle--self"
              :class="{ 'rit-spawn-toggle--active': selfAllowSpawn }"
              data-testid="self-allow-spawn-toggle"
              :title="selfAllowSpawn
                ? t('chat.subAgent.selfAllowSpawnOn', '允许此子 Agent 创建子 Agent（已开启）')
                : t('chat.subAgent.selfAllowSpawn', '允许此子 Agent 创建子 Agent')"
              :aria-label="t('chat.subAgent.selfAllowSpawn', '允许此子 Agent 创建子 Agent')"
              :aria-pressed="selfAllowSpawn"
              @click="toggleSelfAllowSpawn"
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
                aria-hidden="true"
              >
                <circle cx="12" cy="5" r="2.3" />
                <circle cx="6" cy="19" r="2.3" />
                <circle cx="18" cy="19" r="2.3" />
                <path d="M11 7 7 16.6" />
                <path d="M13 7 17 16.6" />
              </svg>
              <span
                v-if="selfAllowSpawn"
                class="rit-spawn-dot"
                aria-hidden="true"
              />
            </button>
            <!-- Scheduled continuation ("定时继续检查"): periodically nudge the
                 model to keep working on a plan when it has stopped. Active dot
                 lights up when the active tab has an enabled timer. -->
            <span class="rit-scheduler-wrap">
              <button
                type="button"
                class="rit-btn rit-scheduler"
                :class="{ 'rit-scheduler--active': schedulerActive }"
                data-testid="scheduler-toggle"
                :title="t('chat.scheduler.title', '定时继续检查')"
                :aria-label="t('chat.scheduler.title', '定时继续检查')"
                :aria-pressed="schedulerOpen"
                @click="schedulerOpen = !schedulerOpen"
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
                  aria-hidden="true"
                >
                  <path d="M4 12a8 8 0 0 1 13.7-5.7" />
                  <path d="M18 3v5h-5" />
                  <path d="M20 12a8 8 0 0 1-13.7 5.7" />
                  <path d="M6 21v-5h5" />
                  <path d="M11 9.5v5l3-2.5-3-2.5Z" />
                </svg>
                <span
                  v-if="schedulerActive"
                  class="rit-scheduler-dot"
                  aria-hidden="true"
                />
              </button>
              <ScheduledContinuationPopover
                :open="schedulerOpen"
                @update:open="schedulerOpen = $event"
              />
            </span>
            <!-- Per-tab "away auto-answer" trigger (this conversation only).
                 Opens a settings dialog; the active dot reflects whether the
                 ACTIVE tab has it enabled (per-tab, default-off). -->
            <span class="rit-away-reply-wrap">
              <button
                type="button"
                class="rit-btn rit-away-reply-btn"
                :class="{ 'rit-away-reply-btn--active': awayAutoAnswerActive }"
                data-testid="away-auto-answer-toggle"
                :disabled="activeTab === null"
                :title="t('chat.awayQuestionAutoAnswer.iconTitle')"
                :aria-label="t('chat.awayQuestionAutoAnswer.iconTitle')"
                :aria-pressed="awayAutoAnswerOpen"
                @click="awayAutoAnswerOpen = true"
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
                  aria-hidden="true"
                >
                  <path d="M5 7.5h9.5a4.5 4.5 0 0 1 0 9H10l-4 3v-3H5a4.5 4.5 0 0 1 0-9Z" />
                  <path d="M9 11h4" />
                  <path d="M9 14h2" />
                  <path d="M18 5v4h4" />
                  <path d="M21 9a5 5 0 0 0-7-4.6" />
                </svg>
                <span
                  v-if="awayAutoAnswerActive"
                  class="rit-away-reply-dot"
                  aria-hidden="true"
                />
              </button>
            </span>
            <span
              v-if="!claudeCode.isCCMode.value && !openCode.isOCMode.value"
              class="rit-user-jump-wrap"
            >
              <button
                type="button"
                class="rit-btn rit-user-jump"
                :class="{ 'rit-history--active': userMessageJumpOpen }"
                data-testid="user-message-jump-toggle"
                :disabled="activeTab === null"
                :title="t('userMessageJump.buttonTitle', '跳转到我发过的消息')"
                :aria-label="t('userMessageJump.buttonTitle', '跳转到我发过的消息')"
                :aria-pressed="userMessageJumpOpen"
                @click="userMessageJumpOpen = !userMessageJumpOpen"
              >
                <!-- Speech bubble + three horizontal list lines: visualises
                     "list of my past chat messages". Inline SVG, currentColor,
                     matches the 15x15 / stroke-width=2 conventions used by
                     every other rit-btn in this toolbar. -->
                <svg
                  width="15"
                  height="15"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z" />
                  <line x1="7" y1="8" x2="17" y2="8" />
                  <line x1="7" y1="11" x2="17" y2="11" />
                  <line x1="7" y1="14" x2="13" y2="14" />
                </svg>
              </button>
              <UserMessageJumpPopover
                :open="userMessageJumpOpen"
                @update:open="userMessageJumpOpen = $event"
                @jump="onJumpToMessage"
              />
            </span>
            <span class="rit-prompt-history-wrap">
              <button
                type="button"
                class="rit-btn rit-history"
                :class="{ 'rit-history--active': promptHistoryOpen }"
                data-testid="prompt-history-toggle"
                :title="t('promptHistory.buttonTitle', '历史 prompt 与收藏')"
                :aria-label="t('promptHistory.buttonTitle', '历史 prompt 与收藏')"
                :aria-pressed="promptHistoryOpen"
                @click="promptHistoryOpen = !promptHistoryOpen"
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
                  aria-hidden="true"
                >
                  <path d="M3 3v5h5" />
                  <path d="M3.05 13A9 9 0 1 0 6 5.3L3 8" />
                  <polyline points="12 7 12 12 15 14" />
                </svg>
              </button>
              <PromptHistoryPopover
                :open="promptHistoryOpen"
                @update:open="promptHistoryOpen = $event"
                @fill="fillFromHistory"
              />
            </span>
            <PromptEnhanceBtn
              class="rit-btn rit-prompt-enhance"
              :text="text"
              :has-models="hasModels"
              @enhanced="text = $event"
            />
            <VoiceInputBtn
              class="rit-btn rit-voice"
              @transcribed="text += $event"
            />
            <button
              v-if="!showStop"
              type="button"
              class="rit-send"
              :disabled="!canSubmit"
              data-testid="chat-send"
              :title="t('chat.composer.sendTitle', 'Send')"
              @click="() => { void onSubmit(); }"
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <line
                  x1="12"
                  y1="19"
                  x2="12"
                  y2="5"
                />
                <polyline points="5,12 12,5 19,12" />
              </svg>
            </button>
            <!-- Mid-turn user injection (V2 enhancement). Visible only while
                 streaming with non-empty input; folds the text into the SAME
                 run at the next inter-round (tool) seam instead of sending a
                 fresh turn. Distinct from the Enter queue (which sends after
                 the turn ends). Sits to the LEFT of the Stop button (user
                 spec 2026-06-24): the down-arrow inject precedes the ⏹ stop. -->
            <button
              v-if="showStop && canInject"
              type="button"
              class="rit-send rit-send--inject"
              data-testid="chat-inject"
              :title="
                t('chat.composer.injectTitle', 'Inject into current run')
              "
              :aria-label="
                t('chat.composer.injectTitle', 'Inject into current run')
              "
              @click="() => { void injectWhileStreaming(); }"
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2.5"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <line
                  x1="12"
                  y1="5"
                  x2="12"
                  y2="19"
                />
                <polyline points="5,12 12,19 19,12" />
              </svg>
            </button>
            <button
              v-if="showStop"
              type="button"
              class="rit-send rit-send--stop"
              data-testid="chat-stop"
              :title="t('chat.composer.stopTitle', 'Stop')"
              :aria-label="t('chat.composer.stopTitle', 'Stop')"
              @click="onStop"
            >
              <span
                class="rit-send-stop-glyph"
                aria-hidden="true"
              >⏹</span>
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Image lightbox overlay (V1 index.html:137-148 + app.js:168-213).
         Opened by clicking a pending-image thumbnail; ARCH-1 split into
         ComposerLightbox, still driven by the shared `useLightbox`. -->
    <ComposerLightbox :lightbox="lightbox" />
    <!-- Per-tab away auto-answer settings (Teleported modal). Seeded from the
         active tab's settings; Save writes back through the store. -->
    <AwayQuestionAutoAnswerDialog
      :visible="awayAutoAnswerOpen"
      :settings="awayAutoAnswerSettings"
      @save="onAwayAutoAnswerSave"
      @cancel="awayAutoAnswerOpen = false"
    />

    <!-- Interactive per-conversation TOKEN budget decision (Teleported modal).
         Opened when a turn ends with reason="budget_exceeded"; "continue" raises
         the cap +raisePct% and resends a continuation turn, "stop" leaves it. -->
    <BudgetDecisionDialog
      v-if="budgetDecisionData !== null"
      :visible="budgetDecisionOpen"
      :used="budgetDecisionData.used"
      :max="budgetDecisionData.max"
      :next-max="budgetDecisionData.nextMax"
      :raise-pct="budgetDecisionData.raisePct"
      @continue="onBudgetContinue"
      @stop="onBudgetStop"
    />
  </div>
</template>

<style scoped>
/* Per-conversation TOKEN budget mini-badge (max_budget_tokens). Layers a thin
   progress bar on top of the shared `.ctx-badge-toolbar` chip so it reads as a
   sibling of the context badge. Colours come from the ctx severity tint
   (`ctx-ok / ctx-warn / ctx-danger`, set on the same element via
   `budgetBadgeClass`) + theme tokens — no hard-coded hex. */
.budget-badge-toolbar {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.budget-badge-label {
  opacity: 0.7;
}
.budget-badge-bar {
  display: inline-block;
  width: 34px;
  height: 4px;
  border-radius: 999px;
  background: var(--border-light, rgba(255, 255, 255, 0.12));
  overflow: hidden;
  flex-shrink: 0;
}
.budget-badge-bar-fill {
  display: block;
  height: 100%;
  border-radius: inherit;
  /* Inherit the badge's severity text colour so the fill matches the tint
     (ok/warn/danger) applied by `budgetBadgeClass` — single source of truth. */
  background: currentColor;
  transition: width 0.2s ease;
}

/* Enabled-skills indicator — V1 parity (index.html:1186 inline style:
   flex row, 4px gap, small muted text). */
.rit-skills-active {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--text-sm, 0.8125rem);
  color: var(--text-muted, #9aa0aa);
  white-space: nowrap;
}

/* Sub-agent "allow question" toggle (收尾 ①). Inherits the icon-button
   `.rit-btn` look (transparent → hover bg); when active it tints to
   `--accent` and shows a small accent dot at the bottom-right corner —
   the SAME "active = accent + dot" idiom the toolbar pills use. All colours
   come from design tokens (no hard-coded hex). */
.rit-question-toggle {
  position: relative;
  justify-content: center;
}
.rit-question-toggle--active {
  color: var(--accent, #a594ff);
}
.rit-question-toggle--active::after {
  content: "";
  position: absolute;
  right: 3px;
  bottom: 3px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #a594ff);
}

/* Sub-agent spawn-permission toggle (second-position button). Shares the
   `.rit-btn` icon-button look + the "active = accent + dot" idiom used across
   this toolbar row. The `--self` variant (sub-agent's own switch) is visually
   distinguished from the main-agent variant by a slightly stronger idle tint
   so the two icons don't look identical at a glance — colours from tokens
   only (no hard-coded theme hex). */
.rit-spawn-toggle {
  position: relative;
  justify-content: center;
}
.rit-spawn-toggle--self {
  opacity: 0.92;
}
.rit-spawn-toggle--active {
  color: var(--accent, #a594ff);
  opacity: 1;
}
.rit-spawn-dot {
  position: absolute;
  right: 3px;
  bottom: 3px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #a594ff);
}

/* Per-session tool / SKILL switches button (this conversation only). Same
   `.rit-btn` icon-button look + "active = accent + dot" idiom as the sub-agent
   question toggle above. The wrapper is `position: relative` so the popover
   (absolute, bottom: 100%) anchors to this button. All colours from tokens. */
.rit-session-tools-wrap {
  position: relative;
  display: inline-flex;
}
.rit-session-tools-btn {
  position: relative;
  justify-content: center;
}
.rit-session-tools-btn--active {
  color: var(--accent, #a594ff);
}
.rit-session-tools-dot {
  position: absolute;
  right: 3px;
  bottom: 3px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #a594ff);
}

/* Dedicated Token-budget button — same `.rit-btn` icon-button + active-dot
   idiom as the session-tools / away-reply toggles. Wrapper is
   `position: relative` so the BudgetPopover (absolute, bottom: 100%) anchors to
   this button. All colours from design tokens. */
.rit-budget-wrap {
  position: relative;
  display: inline-flex;
}
.rit-budget-btn {
  position: relative;
  justify-content: center;
}
.rit-budget-btn--active {
  color: var(--accent, #a594ff);
}
.rit-budget-dot {
  position: absolute;
  right: 3px;
  bottom: 3px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #a594ff);
}

/* Away auto-answer trigger — same icon-button + active-dot pattern as the
   session-tools toggle above. All colours from design tokens. */
.rit-away-reply-wrap {
  position: relative;
  display: inline-flex;
}
.rit-away-reply-btn {
  position: relative;
  justify-content: center;
}
.rit-away-reply-btn--active {
  color: var(--accent, #a594ff);
}
.rit-away-reply-dot {
  position: absolute;
  right: 3px;
  bottom: 3px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #a594ff);
}

/* Stop button — V1 parity (frontend/index.html lines 1505-1510 +
 * css/chat.css `.rit-send`). V1's `.rit-send` button stays the same
 * purple `--accent` colour while streaming; only the inner glyph
 * swaps from the upward send-arrow SVG to the ⏹ stop glyph
 * (Unicode U+23F9). All colours come from design tokens —
 * --accent / --accent-hover already defined in variables.css. */
.rit-send-stop-glyph {
  font-size: var(--text-base, 14px);
  line-height: 1;
  display: inline-block;
}

/* Wrapper for the params + whisper pill so the popovers can anchor
 * bottom-up off the trigger button. */
.model-params-wrap {
  position: relative;
  display: inline-block;
}

/* Multi-Agent discussion panel popover (block-5) — anchored bottom-up off the
 * discussion pill trigger (parity with the params / whisper popovers). */
.discussion-popover {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  z-index: 50;
  width: 570px;
  max-width: calc(100vw - 32px);
}

/* Model selector trigger — uses global .model-selector-btn / .model-dot
 * from chat.css; this wrapper is positioned so the 570px popover can
 * align bottom-up over it. */
.model-selector-wrap {
  position: relative;
  display: inline-block;
}
.model-selector-label {
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: inline-block;
  vertical-align: middle;
}
/* V1 parity (index.html:946-954): the 5-state status colour is supplied
 * by the inline `:style` binding on `.model-dot` — no CSS-level state
 * classes (V1 also uses inline :style for this). The base geometry
 * (8x8 / 50% radius / flex-shrink:0) lives in chat.css `.model-dot`. */

.chat-input-hint {
  font-size: var(--text-xs, 11px);
  color: var(--text-muted, #6b7280);
  text-align: left;
  padding: 4px 0;
}

/* Hidden file input bound to the "+" attach button. The pending-image
 * thumbnails (incl. the failed-upload visual) live in ComposerPendingImages
 * (ARCH-1 split); the rest of the thumbnail visuals come from the global
 * `chat.css` tokens. */
.rit-file-input {
  display: none;
}

/* CC effort dropdown wrapper — relative anchor so the global
 * `.effort-dropdown` (position:absolute, components.css L720) lifts
 * upward off the trigger. */
.cc-effort-wrap {
  position: relative;
  display: inline-block;
}
</style>
