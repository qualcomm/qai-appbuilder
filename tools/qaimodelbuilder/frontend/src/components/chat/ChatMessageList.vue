<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChatMessageList — V1-style message renderer with welcome screen.
 *
 * Structure:
 *   .messages-container  (deep purple bg, border, rounded)
 *     .welcome-screen    (when no messages)
 *     .message-row.user / .message-row.ai  (when messages exist)
 *
 * Renders the active tab's `messages` array plus the in-flight
 * `streamingContent` (if any). Reactive on the tab's status — shows a
 * typing indicator while streaming, an error banner in `error`, and
 * the welcome screen when the buffer is empty.
 *
 * The component intentionally stays read-only: all mutations route
 * through the store actions, which are dispatched from the transport
 * composables.
 */
import {
  computed,
  ref,
  defineAsyncComponent,
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  onMounted,
  nextTick,
  watch,
} from "vue";
import { useI18n } from "vue-i18n";
import {
  useChatTabsStore,
  type ChatMessage,
  type ChatMessageUsage,
  type ChatMessagePerf,
} from "@/stores/chatTabs";
import { useUiStore } from "@/stores/ui";
import { renderMarkdown } from "@/composables/markdown";
import { useMermaidRender } from "@/composables/useMermaidRender";
import {
  runMermaidAction,
  mermaidSvgOf,
  svgToDataUrl,
  type MermaidAction,
} from "@/composables/markdown-mermaid";
import { useLightbox } from "@/composables/useLightbox";
import { useAssistantLabel } from "@/composables/chat/useAssistantLabel";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { useChatTransports } from "@/composables/chat/useChatTransports";
import { usePromptSnapshot } from "@/composables/chat/usePromptSnapshot";
import {
  useChatScrollMemory,
  chatScrollKey,
} from "@/composables/chat/useChatScrollMemory";
import { useToast } from "@/composables/useToast";
import ToolCallList from "@/components/chat/ToolCallList.vue";
import type { ToolCallView } from "@/stores/_chatTabsTypes";
import type { TabId } from "@/stores/_chatTabsTypes";
import SubAgentBlock from "@/components/chat/SubAgentBlock.vue";
import ReasoningBlock from "@/components/chat/ReasoningBlock.vue";
import PromptSnapshotPanel from "@/components/chat/PromptSnapshotPanel.vue";
import CloudModelOnboarding from "@/components/chat/CloudModelOnboarding.vue";
import CloudModelApiKeyOnboarding from "@/components/chat/CloudModelApiKeyOnboarding.vue";
import SetApiKeyDialog from "@/components/chat/SetApiKeyDialog.vue";
import AppBuilderEmptyState from "@/components/chat/AppBuilderEmptyState.vue";
import ProEmptyState from "@/components/chat/ProEmptyState.vue";
import CodeEmptyState from "@/components/chat/CodeEmptyState.vue";
import ModelBuilderEmptyState from "@/components/chat/ModelBuilderEmptyState.vue";
import ModelHubEmptyState from "@/components/chat/ModelHubEmptyState.vue";
// ModeIntroCard has been relocated to `ChatView.vue` (sticky above the
// composer) so it stays visible in conversations with pre-existing history.
// See `ChatView.vue::introMode` for the new mount.
import { IS_INTERNAL } from "@/edition";
// Internal-only GoMaster empty-state. Lazily referenced ONLY on the internal
// edition so the external open-source build tree-shakes it away (module
// physically absent; source file removable without a broken static import).
const GomasterEmptyState = IS_INTERNAL
  ? defineAsyncComponent(() => import("@/components/gomaster/GomasterEmptyState.vue"))
  : null;
import { useCloudModelStatus } from "@/composables/useCloudModelStatus";
import { useServiceStore } from "@/stores/service";
import { putProviderApiKey } from "@/api/cloudModels";
import {
  resolveChatErrorSpec,
  type ChatErrorAction,
  type ChatErrorActionId,
} from "@/composables/chat/chatErrorActions";
import {
  useChatErrorActions,
  buildDiagnostics,
} from "@/composables/chat/useChatErrorActions";

const emit = defineEmits<{
  "quick-prompt": [prompt: string];
  // App Builder empty-state example chip → FILL the composer (no submit).
  // Distinct from `quick-prompt` (which submits immediately) because the
  // App Builder authoring flow lets the user edit the starter prompt first.
  "fill-prompt": [prompt: string];
  retry: [payload: { id: string; content: string }];
  // Floating stop button (V1 index.html:922-937). ChatMessageList does
  // not own the stream lifecycle, so it emits `stop` for the parent
  // (ChatView) to route into its existing stop entry point
  // (useChatStream cancel / abort). See manifest D2.
  stop: [];
  // Pending mid-turn injection bubble actions (V2 enhancement). The bubble
  // lives in the CONVERSATION (this component) but the cancel/edit logic needs
  // the control channel + composer ref that ChatView owns, so ChatMessageList
  // stays presentational and EMITS these for the parent (mirrors
  // MessageQueuePanel's `@edit`). `inject-cancel` → withdraw + remove the
  // bubble; `inject-edit` → withdraw + remove + refill the composer draft.
  "inject-cancel": [payload: { id: string; text: string }];
  "inject-edit": [payload: { id: string; text: string }];
  // NOTE: `mode-intro-action` used to be emitted here (when ModeIntroCard
  // was mounted inside the message list). ModeIntroCard has moved to
  // ChatView.vue (sticky above the composer), which listens directly, so
  // this emit is no longer needed and has been removed. Removing it also
  // avoids a stale contract entry that would drift as ChatView evolves.
}>();

const { t } = useI18n();
const store = useChatTabsStore();
const ui = useUiStore();
const { peekTransport } = useChatTransports();
const toast = useToast();
const { ensureLoaded: ensureModelsLoaded, resolveAssistantLabel } =
  useAssistantLabel();
const { config: forgeConfig } = useForgeConfig();

/** V2 enhancement: a SubAgentBlock asked to be opened in a new tab.
 *  `openSubAgentTab` 会 fetch sub-agent detail、创建（或复用）一个
 *  `kind:"subagent"` 的 tab、`switchTab` 到它。ChatMessageList 挂在
 *  `store.activeTab` 上，自然渲染新 tab 的 `messages[]`。
 *
 *  两级栏语义（2026-07-02 恢复）：一级栏 `ChatTabStrip` 只显示 main tab
 *  （kind !== "subagent"）；二级栏 `SubAgentRail` 平铺当前 main tab 下所有
 *  sub-agent（任意深度，用 `subagentMeta.rootConversationId` 过滤 `subAgentIndex`）。
 *  **这段 handler 与调用者所在 tab 的 kind / depth 无关**——同一 code path
 *  支持任意深度钻入（主 tab → sub tab → grand sub tab → ...）；`activeMainTabId`
 *  getter 会把一级栏高亮回落到 sub-agent 的父 main tab。
 *
 *  失败（session 无法加载 / 后端 404）→ store 抛错，这里显示 toast，避免
 *  静默的 dead click。 */
async function handleOpenSubAgent(subagentId: string): Promise<void> {
  try {
    await store.openSubAgentTab(subagentId);
  } catch (err) {
    console.error("[ChatMessageList] openSubAgentTab failed", err);
    toast.error(t("chat.subAgent.openFailed"));
  }
}

/** Interrupt a RUNNING sub-agent (block 3) — stops ONLY that sub-agent via
 *  its independent cancellation flag, not the parent tab / main agent. */
function handleStopSubAgent(subagentId: string): void {
  void store.interruptSubAgent(subagentId);
}

/** Per-call cancel (tool card stop button). Goal: WHATEVER the context, a
 *  tool-card stop cancels ONLY that one tool and lets the model continue (the
 *  backend synthesizes a `[cancelled]` result and keeps the turn running).
 *
 *  A per-call `cancel_tool(tab_id, call_id)` marks the call on the abort handle
 *  registered under `tab_id`. So we must send the tab_id whose IN-FLIGHT turn
 *  owns that handle:
 *   - This tab has an actively in-flight transport (ordinary chat, OR a
 *     sub-agent TAKE-OVER typed into the sub-agent tab): the turn is under this
 *     tab id → `cancelToolCall(thisTab, callId)`.
 *   - This is a PARENT-spawned sub-agent tab (read-only WS, no own transport):
 *     the backend threads the per-call cancel through the PARENT turn's handle
 *     (agent_tool.py `parent_consume_cancel_tool`), which is registered under
 *     the parent turn's tab id — i.e. the tab whose `conversationId` equals this
 *     sub-agent's `rootConversationId` and that is itself streaming. Target THAT
 *     tab so the sub-agent's tool loop honours the cancel and continues (same
 *     single-tool semantics as a main-agent card — fixes "主 Agent 派生的子 Agent
 *     按停止就整个停了、不给模型结果").
 *   - Only if no such parent turn is found (e.g. the parent already ended) do we
 *     fall back to interrupting the whole sub-agent — the closest action still
 *     possible. */
function onCancelTool(callId: string): void {
  const tab = activeTab.value;
  if (tab === undefined || tab === null) return;
  const transport = peekTransport(tab.id);
  if (transport !== undefined && transport.isInFlight()) {
    store.cancelToolCall(tab.id, callId);
    return;
  }
  if (tab.kind === "subagent") {
    // Route the per-call cancel to the PARENT turn's tab (whose handle the
    // backend marks). The parent turn runs on the tab whose conversation is
    // this sub-agent's root AND that is currently streaming.
    const rootConvId = tab.subagentMeta?.rootConversationId;
    if (rootConvId !== undefined && rootConvId !== "") {
      const parentTab = store.tabs.find(
        (t) =>
          t.conversationId === rootConvId &&
          (t.status === "streaming" ||
            (peekTransport(t.id)?.isInFlight() ?? false)),
      );
      if (parentTab !== undefined) {
        store.cancelToolCall(parentTab.id, callId);
        return;
      }
    }
    // No live parent turn to route through → whole-sub-agent interrupt.
    const sid = tab.subagentMeta?.subagentId;
    if (sid !== undefined && sid !== "") {
      void store.interruptSubAgent(sid);
      return;
    }
  }
  // Ordinary chat tab with no in-flight transport (should not happen while a
  // running tool card is visible) → best-effort per-call cancel.
  store.cancelToolCall(tab.id, callId);
}

// ─── D3 showPromptInUi (V1 useForgeConfig.js:17) ─────────────────────────────
// Reads forge_config.service_launch.show_prompt_in_ui to gate the prompt
// snapshot button on each assistant message.
const showPromptInUi = computed<boolean>(() => {
  const cfg = forgeConfig.value;
  if (cfg === null || typeof cfg !== "object") return false;
  const sl = (cfg as Record<string, unknown>)["service_launch"];
  if (sl === null || typeof sl !== "object") return false;
  return !!(sl as Record<string, unknown>)["show_prompt_in_ui"];
});

// ─── D3 Prompt Snapshot Modal (F4 cohesion split) ───────────────────────────
// All modal state + lifecycle (open / close / collapse / copy / Escape
// handling) live in `composables/chat/usePromptSnapshot.ts`; the markup
// lives in `components/chat/PromptSnapshotPanel.vue`. ChatMessageList keeps
// the open trigger (the per-message 📋 button) and threads the composable
// API into the panel.
const promptSnapshot = usePromptSnapshot((requestId: string) => {
  // V1 parity (useForgeConfig.js:64-76): a 404 means the snapshot expired
  // (in-memory store cleared on backend restart). Drop the dead request_id
  // from any message that carried it so the 📄 button stops showing — both
  // on the assistant message header and on its tool cards. Otherwise the
  // button lingers and every click flashes the dialog open→404→closed.
  store.clearRequestId(requestId);
});
const openPromptSnapshot = promptSnapshot.openPromptSnapshot;

const activeTab = computed(() => store.activeTab);

// Streaming repaint-gap fix (running tool card not shown until tab-switch,
// reproduced on standalone sub-agent tabs):
//
// `store.activeTab` is a Pinia getter (a `computed`) whose dependencies are
// only `activeTabId` + the `tabs` array identity. `_patchTab` mutates the
// matched tab object IN PLACE (`Object.assign(target, patch)` — a deliberate
// tab-strip perf optimization that keeps the `tabs` array reference stable),
// so a `handleToolCall` write that only replaces `tab.messages` changes
// NEITHER of the getter's dependencies → the getter stays cached and does not
// re-run. On the MAIN agent this was masked: a running exec streams
// `partial=true` tool_result frames every frame, and each `patchTab` for
// those incidentally kicks the render effect. But a SUB-AGENT "never streams
// partials" (`agent_tool.py` `_tool_executor`) — during a 20s exec ZERO
// frames arrive after the single `subagent_tool` write, so nothing re-triggers
// the render and the running card only appears on a tab-switch (which changes
// `activeTabId` and forces re-evaluation).
//
// Reading the messages array by INDEXING the raw reactive `store.tabs` state
// here (rather than only through the cached `activeTab` getter object)
// establishes a direct render dependency on BOTH the `tabs`/`activeTabId`
// state AND the matched tab's `messages` property, so an in-place `messages`
// replacement re-runs this computed and repaints the list immediately.
const activeMessages = computed(() => {
  const id = store.activeTabId;
  if (id === null) return [];
  const tab = store.tabs.find((t) => t.id === id);
  return tab?.messages ?? [];
});


// --- Network-retry banner: live countdown + "立即重试" ---------------------
// The backend NETWORK_RETRY frame carries `deadlineMs` (when the next
// automatic attempt is due). A 1s ticking clock drives a countdown so the
// banner shows "…将在 Ns 后自动重试" without any per-tick backend frame. The
// interval only runs while a retry is active (started/stopped by the watcher
// below) so it costs nothing on the common path.
const nowTick = ref<number>(Date.now());
let retryTickTimer: ReturnType<typeof setInterval> | null = null;

function stopRetryTick(): void {
  if (retryTickTimer !== null) {
    clearInterval(retryTickTimer);
    retryTickTimer = null;
  }
}

watch(
  () => activeTab.value?.networkRetry?.deadlineMs ?? null,
  (deadlineMs) => {
    if (deadlineMs === null || deadlineMs === undefined) {
      stopRetryTick();
      return;
    }
    nowTick.value = Date.now();
    if (retryTickTimer === null) {
      retryTickTimer = setInterval(() => {
        nowTick.value = Date.now();
      }, 500);
    }
  },
  { immediate: true },
);

onBeforeUnmount(stopRetryTick);

// Seconds remaining until the next automatic attempt (>= 0, rounded up); null
// when no deadline is known (legacy SSE path) so the banner omits the count.
const retryCountdownSeconds = computed<number | null>(() => {
  const deadline = activeTab.value?.networkRetry?.deadlineMs;
  if (typeof deadline !== "number") return null;
  return Math.max(0, Math.ceil((deadline - nowTick.value) / 1000));
});

// Banner text: infinite backend retry has no fixed ceiling, so show only the
// attempt number (fixes the "1/1, 2/2, 3/3" oddity). The legacy SSE path still
// carries `max`, so keep the "N/M" form there for back-compat.
const networkRetryText = computed<string>(() => {
  const nr = activeTab.value?.networkRetry;
  if (nr === null || nr === undefined) return "";
  const countdown = retryCountdownSeconds.value;
  if (typeof nr.max === "number") {
    // Legacy SSE client-retry loop (bounded): keep the "N/M" form.
    return t("chat.networkInterrupted", { retry: nr.current, max: nr.max });
  }
  if (countdown !== null && countdown > 0) {
    return t("chat.networkRetryingCountdown", {
      attempt: nr.current,
      seconds: countdown,
    });
  }
  // Countdown elapsed → the backend is (re)connecting right now.
  return t("chat.networkRetryingNow", { attempt: nr.current });
});

async function onRetryNowClick(): Promise<void> {
  const tab = activeTab.value;
  if (tab === null) return;
  // Optimistic UX: zero the local countdown immediately so the banner flips to
  // "正在重连…" without waiting for the next tick; the backend re-opens on the
  // retry_now signal (State-Truth-First: the real next frame corrects the UI).
  nowTick.value = tab.networkRetry?.deadlineMs ?? nowTick.value;
  // Ensure the control channel is connected, then send (mirrors the inject
  // flow, useComposerSubmit.ts). The channel is normally pre-opened at turn
  // start (useChatTransport.send → ensureOpen), so `whenReady` resolves
  // immediately; the short timeout covers a reconnect / initial-load race.
  // There is NO REST fallback (retry_now only makes sense for a live mid-
  // backoff stream), so surface a toast if the control plane is unavailable —
  // the backend still auto-retries on its schedule, the user just could not
  // fast-forward it.
  const { useChatControlChannel } = await import(
    "@/composables/chat/useChatControlChannel"
  );
  const channel = useChatControlChannel();
  const ready = await channel.whenReady(2000);
  const sent = ready && channel.sendRetryNow(String(tab.id));
  if (!sent) {
    toast.error(
      t(
        "chat.retryNowFailed",
        "控制连接不可用，将按计划自动重连",
      ),
    );
  }
}


// When a turn fails, the failure is surfaced in two places that would
// otherwise duplicate the same text: (a) the per-user-message banner
// (`msg-error-banner`, with a Retry button) shown under the user message
// whose turn failed, and (b) this tab-level `chat-error-banner` ("!" bubble).
// To avoid the double prompt we hide the tab-level banner whenever the
// per-message banner already carries the error — i.e. the latest user message
// has a `sendError`. Errors with no originating user message (no per-message
// banner) still fall through to the tab-level banner so they are never
// swallowed.
// True when the per-message banner is actually SHOWING the error (so the
// bottom tab-level banner should stay hidden to avoid a double prompt). The
// per-message banner shows only when the failed user message carries a
// `sendError` AND the failure is NOT followed by later assistant activity
// (see `failedTurnHasLaterActivity`). When the failure DID have later activity
// the per-message banner is suppressed, so this returns false and the bottom
// tab-level banner takes over — surfacing the error at the error location.
const tabErrorShownOnMessage = computed<boolean>(() => {
  const tab = activeTab.value;
  if (tab === null) return false;
  for (let i = tab.messages.length - 1; i >= 0; i -= 1) {
    const m = tab.messages[i];
    if (m !== undefined && m.role === "user") {
      return Boolean(m.sendError) && !failedTurnHasLaterActivity.value;
    }
  }
  return false;
});

// The failed user message whose turn errored — anchor for the retry action.
// Used by the bottom (tab-level) error banner so its ↻ retry re-sends the same
// prompt via the SAME `onRetry` path the per-message banner uses.
const failedUserMessage = computed(() => {
  const tab = activeTab.value;
  if (tab === null) return null;
  for (let i = tab.messages.length - 1; i >= 0; i -= 1) {
    const m = tab.messages[i];
    if (m !== undefined && m.role === "user") {
      return m.sendError ? m : null;
    }
  }
  return null;
});

// True when the failed user message is NOT the last message in the tab — i.e.
// the turn produced later assistant activity (tool rounds) before erroring, so
// the failure happened at the BOTTOM of the conversation (e.g. a stall while
// generating a `write` tool's long args). In that case the per-message banner
// anchored to the (far-up) originating user prompt is confusing — the error
// belongs at the error LOCATION. We then suppress the top per-message banner
// and surface the bottom tab-level banner (with a retry button) instead.
// When the failed user message IS the last message (an ordinary last-turn
// failure with no assistant output), the per-message banner stays as-is.
const failedTurnHasLaterActivity = computed<boolean>(() => {
  const tab = activeTab.value;
  const failed = failedUserMessage.value;
  if (tab === null || failed === null) return false;
  const idx = tab.messages.findIndex((m) => m.id === failed.id);
  return idx >= 0 && idx < tab.messages.length - 1;
});

const showWelcome = computed(() => {
  if (activeTab.value === null) return true;
  // Don't show welcome while history is loading (skeleton takes priority).
  if (activeTab.value.loadingHistory) return false;
  return (
    activeMessages.value.length === 0 &&
    activeTab.value.streamingContent === "" &&
    activeTab.value.status !== "streaming"
  );
});

// ─── Welcome chips (V1 index.html:435-443 — `v-for="chip in welcomeChips"`) ──
// Data-driven chip list so the welcome screen adapts to a variable number of
// quick-prompt suggestions without hardcoding individual chips in the template.
// Default set matches the 3 V1 launch prompts; an external config feed can
// replace or extend this array in the future.
interface WelcomeChip {
  iconSvg: string;
  labelKey: string;
  promptKey: string;
}

const CHIP_ICON_IMAGE =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="14" height="14" rx="2"/><circle cx="6.5" cy="6.5" r="1.5"/><path d="M16 11.5l-3.5-3.5L5 16"/></svg>';
const CHIP_ICON_TARGET =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="9" r="7"/><circle cx="9" cy="9" r="4"/><circle cx="9" cy="9" r="1"/></svg>';
const CHIP_ICON_BRAIN =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2a4 4 0 013.5 5.9A3.5 3.5 0 0114 14.5c0 .8-.7 1.5-1.5 1.5H5.5A1.5 1.5 0 014 14.5a3.5 3.5 0 011.5-6.6A4 4 0 019 2z"/><path d="M7 10h4M8 12.5h2"/></svg>';
const CHIP_ICON_WAVE =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 9h2l1.5-5 3 12 3-9 1.5 4h2"/></svg>';
const CHIP_ICON_AGENT =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="12" height="8" rx="2"/><path d="M9 6V3M6.5 9.5h.01M11.5 9.5h.01"/><path d="M1.5 9.5v1.5M16.5 9.5v1.5"/></svg>';
const CHIP_ICON_SPEAKER =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6.5h3L10 3v12L6 11.5H3z"/><path d="M12.5 6.5a3.5 3.5 0 010 5M14.5 4.5a6 6 0 010 9"/></svg>';
const CHIP_ICON_DIAGRAM =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="1.5" width="6" height="4" rx="1"/><rect x="1.5" y="12.5" width="5" height="4" rx="1"/><rect x="11.5" y="12.5" width="5" height="4" rx="1"/><path d="M9 5.5v3M9 8.5H4v4M9 8.5h5v4"/></svg>';
const CHIP_ICON_SEARCH =
  '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="7.5" r="5"/><path d="M11.5 11.5L16 16"/></svg>';

const DEFAULT_WELCOME_CHIPS: WelcomeChip[] = [
  { iconSvg: CHIP_ICON_IMAGE, labelKey: "chat.welcomeChip1Label", promptKey: "chat.welcomeChip1Prompt" },
  { iconSvg: CHIP_ICON_TARGET, labelKey: "chat.welcomeChip2Label", promptKey: "chat.welcomeChip2Prompt" },
  { iconSvg: CHIP_ICON_BRAIN, labelKey: "chat.welcomeChip3Label", promptKey: "chat.welcomeChip3Prompt" },
  { iconSvg: CHIP_ICON_WAVE, labelKey: "chat.welcomeChip4Label", promptKey: "chat.welcomeChip4Prompt" },
  { iconSvg: CHIP_ICON_AGENT, labelKey: "chat.welcomeChip5Label", promptKey: "chat.welcomeChip5Prompt" },
  { iconSvg: CHIP_ICON_SPEAKER, labelKey: "chat.welcomeChip6Label", promptKey: "chat.welcomeChip6Prompt" },
  { iconSvg: CHIP_ICON_DIAGRAM, labelKey: "chat.welcomeChip7Label", promptKey: "chat.welcomeChip7Prompt" },
  { iconSvg: CHIP_ICON_SEARCH, labelKey: "chat.welcomeChip8Label", promptKey: "chat.welcomeChip8Prompt" },
];

const welcomeChips = computed(() => DEFAULT_WELCOME_CHIPS);

// ─── Cloud-model onboarding hint (V2 enhancement, edition-dual-form §6.4) ────
// When the welcome screen is shown and NO cloud model is configured, surface
// a non-blocking hint guiding the user to Settings → Cloud Models. Edition-
// agnostic: `useCloudModelStatus` only checks "is the cloud list empty?" by
// reusing the existing /api/model-catalog/entries source. The hint is gated
// on `showWelcome` so it only appears on the initial (empty) chat surface.
const cloudModelStatus = useCloudModelStatus();
// Edition flag drives the missing-API-key action button label (internal →
// "Set API Key"; external → "Cloud Model Settings"). The action itself is
// edition-aware inside `openApiKeyFlow()`.
const service = useServiceStore();
const showCloudOnboarding = computed(
  () => showWelcome.value && cloudModelStatus.showOnboarding.value,
);

// ─── Cloud-model "set API key" prompt (internal-edition enhancement) ─────────
// When cloud models are PRE-CONFIGURED but a provider is still missing its
// API key (`useCloudModelStatus.showApiKeyPrompt`), surface a second gentle
// card on the welcome screen. Clicking its CTA opens an in-place dialog to
// set the key directly (no navigation to Settings). Gated on `showWelcome`
// like the no-models onboarding so it only appears on the initial surface.
const showCloudApiKeyPrompt = computed(
  () => showWelcome.value && cloudModelStatus.showApiKeyPrompt.value,
);
// The dialog's open/close state is the SHARED singleton on
// `useCloudModelStatus` (not a component-local ref) so all three entry
// points — the composer pre-send interception, the chat error bubble, and
// this welcome-screen card — open the SAME dialog instance hosted below.
const apiKeyDialogVisible = cloudModelStatus.dialogVisible;
const apiKeySaving = ref(false);

// Single edition-aware entry point (internal → open dialog; external/unknown
// → navigate to Settings → Cloud Models). Shared with the composer + error
// bubble via `useCloudModelStatus.openApiKeyFlow`.
function openApiKeyFlow(): void {
  cloudModelStatus.openApiKeyFlow();
}

async function onSaveApiKey(key: string): Promise<void> {
  const providerId = cloudModelStatus.providerNeedingKey.value;
  const existingConfig = cloudModelStatus.providerNeedingKeyConfig.value;
  if (providerId === null || existingConfig === null) {
    // State drifted (e.g. already saved elsewhere) — close silently.
    apiKeyDialogVisible.value = false;
    return;
  }
  apiKeySaving.value = true;
  try {
    await putProviderApiKey(providerId, existingConfig, key);
    toast.success(t("cloudModels.apiKeyDialog.saveSuccess"));
    apiKeyDialogVisible.value = false;
    // Re-fetch providers so `showApiKeyPrompt` recomputes to false and the
    // prompt disappears without a full reload.
    void cloudModelStatus.refresh();
  } catch (e) {
    toast.error(
      `${t("cloudModels.apiKeyDialog.saveError")}: ${e instanceof Error ? e.message : String(e)}`,
    );
    // Keep the dialog open so the user can retry / correct the key.
  } finally {
    apiKeySaving.value = false;
  }
}

// App Builder mode: when the active tab is in `app-builder` mode, the welcome
// screen shows a dedicated authoring empty-state (3-step guide + model-aware
// example chips) instead of the generic chat chips (Phase 2/3).
const isAppBuilderMode = computed(
  () => activeTab.value?.activeMode === "app-builder",
);

// GoMaster (external one-click optimize) mode: show the GoMaster intro empty
// state instead of the generic chat chips. GoMaster external is not a chat, so
// the intro explains it + offers a「开始优化」button (opens the optimize panel).
const isGomasterMode = computed(
  () => activeTab.value?.activeMode === "gomaster",
);

// Pro (增强 / Model Builder Pro) mode: show the Pro-specific 3-step intro +
// "Open settings / Connect GPU Agent" chips instead of the generic chat
// chips. Parity with the App Builder / GoMaster empty-state pattern.
const isProMode = computed(() => activeTab.value?.activeMode === "pro");

// Code (Claude Code) mode: show the Code-specific 3-step intro + "Pick
// persona / Upload code" chips instead of the generic chat chips. Parity
// with the App Builder / GoMaster / Pro empty-state pattern.
const isCodeMode = computed(() => activeTab.value?.activeMode === "code");
const isModelBuildMode = computed(
  () => activeTab.value?.activeMode === "model-build",
);
const isModelHubMode = computed(
  () => activeTab.value?.activeMode === "model-hub",
);

// Mode-intro card (Plan §7 decision 5 — C+D combo): when the current tab has
// messages AND is in a mode-with-onboarding, render a collapsible intro
// NOTE: `introMode` computed used to live here and drive an in-list
// ModeIntroCard render at the top of the message list. The card has been
// relocated to `ChatView.vue` as a sticky helper strip above the composer,
// because the top-of-list placement was invisible in conversations with
// pre-existing history (default scroll landed on the newest message and
// no user scrolls up to hunt for onboarding). See `ChatView.vue::introMode`.

/** Map a message's `toolCalls` (`ChatToolCall[]`) onto the shared
 *  `ToolCallView[]` consumed by ToolCallList — the SAME render path the
 *  sub-agent block uses (one render logic, two call sites). Field renames
 *  only (`tool`→`toolName`, `output`→`result`); no main-agent capability is
 *  dropped. `timestamp` falls back to the message's `createdAt` (V1 history
 *  mode parity), `key` uses the stable frame id. */
function toToolCallViews(msg: ChatMessage): ToolCallView[] {
  const calls = msg.toolCalls;
  if (!calls) return [];
  return calls.map((call) => ({
    key: call.id,
    callId: call.callId,
    toolName: call.tool,
    args: call.args,
    result: call.output,
    status: call.status,
    outputSize: call.outputSize,
    truncated: call.truncated,
    timestamp: call.ts ?? msg.createdAt,
    durationMs: call.durationMs,
    argsStreaming: call.argsStreaming,
    argsCharCount: call.argsCharCount,
    timedFromGeneration: call.timedFromGeneration,
    generationStartedAt: call.generationStartedAt,
    totalMs: call.totalMs,
  }));
}

/** Frame id of THIS tab's currently-active (awaiting-answer) `question` tool
 *  call, or "" when none. Drives the visibility override below. */
const activeQuestionFrameId = computed(
  () => activeTab.value?.pendingQuestion?.frameId ?? "",
);

/** Whether `msg` carries the tab's ACTIVE (awaiting-answer) question card.
 *  An active question = a `question` tool call whose frame id matches the
 *  tab's `pendingQuestion.frameId` (the store clears that pointer the moment
 *  the user answers, so an answered/read-only card never qualifies). Such a
 *  message's tool-call group must render even when the "show tool messages"
 *  toggle is OFF — otherwise the blocking question card would be hidden and
 *  the agentic loop would wedge with no way to answer (State-Truth-First). */
function hasActiveQuestion(msg: ChatMessage): boolean {
  const fid = activeQuestionFrameId.value;
  if (fid === "") return false;
  return (msg.toolCalls ?? []).some(
    (call) => call.tool === "question" && call.id === fid,
  );
}

/** Resolve the request_id stamped on an assistant message's meta (drives the
 *  per-tool prompt-snapshot button). Empty string when absent. */
function messageRequestId(msg: ChatMessage): string {
  return String(
    (msg.meta as Record<string, unknown> | undefined)?.["request_id"] ?? "",
  );
}

// ─── A1 avatar (V1 index.html:498-500) ────────────────────────────────────────
// User & assistant avatars: rendered as inline SVG for a clean, professional look.

// User avatar SVG — centered person silhouette (white on accent background)
const USER_AVATAR_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M5 21a7 7 0 0 1 14 0"/></svg>`;

// AI avatar SVG — sparkle/star icon (brand purple via currentColor)
const AI_AVATAR_SVG = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L14.5 9.5 22 12 14.5 14.5 12 22 9.5 14.5 2 12 9.5 9.5z"/></svg>`;

// ─── Query-service (self-contained agent) branding ──────────────────────────
// CEBot and MB Pro are query-service backends with a stable per-message source
// key: `msg.modelId === "query::<id>"` (CEBot is selected directly in the
// dropdown; MB Pro is stamped query::mb_pro at commit + persisted as such by
// the backend — see messageCommit.ts / streaming.py _finalize). We give each a
// fixed display name + a distinct avatar so the bubble header/icon reflect the
// actual responder rather than a stripped hint or the user's other selection.
const QUERY_SERVICE_LABELS: Readonly<Record<string, string>> = {
  "query::cebot": "CEBot",
  "query::mb_pro": "QAI ModelBuilder Pro",
  "query::gomaster": "GoMaster",
};

// CEBot avatar — a chat/knowledge "speech bubble with spark" mark.
const CEBOT_AVATAR_SVG = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8z"/><path d="M12 8.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z"/></svg>`;

// MB Pro avatar — a processor/chip mark (centre core + pins on all four sides),
// the classic "compute / GPU" glyph, evoking the remote model-builder agent.
// Crisp at 20px: a rounded outer chip, an inner core square, and short pin
// stubs top/bottom/left/right. Cleaner + more recognizable than the prior
// server-box mark.
const MB_PRO_AVATAR_SVG = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/></svg>`;

// GoMaster avatar — a "neural network / graph" mark (three connected nodes),
// evoking the neural-network inference Agent. Distinct from CEBot's speech
// bubble and MB Pro's chip so the three query-service brands are visually
// separable at 20px.
const GOMASTER_AVATAR_SVG = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7.7 7.7l2.6 8.1M16.3 7.7l-2.6 8.1M8 6h8"/></svg>`;

/** Avatar SVG for an assistant message, branding CEBot / MB Pro / GoMaster by
 *  their stable `msg.modelId` query-service key; everything else uses the
 *  default sparkle. */
function assistantAvatarSvg(msg: ChatMessage): string {
  if (msg.modelId === "query::cebot") return CEBOT_AVATAR_SVG;
  if (msg.modelId === "query::mb_pro") return MB_PRO_AVATAR_SVG;
  if (msg.modelId === "query::gomaster") return GOMASTER_AVATAR_SVG;
  return AI_AVATAR_SVG;
}

// ─── A2 role label (V1 index.html:505-516) ──────────────────────────────────
// V1: user → "You", assistant → msgModelName(msg) (model display name).
// V2 stamps each committed assistant message with `modelId`/`modelProvider`
// (chatTabs.ts) and history rows carry them from the backend, so we resolve
// the real model display name via `useAssistantLabel` (V1 msgModelName
// parity). When a message lacks its own model id (legacy rows / mid-stream)
// we fall back to the active tab's current selection, then to the generic
// "Assistant" i18n label. The user label still goes through chat.you.
function roleLabel(msg: ChatMessage): string {
  if (msg.role === "user") return t("chat.you");
  // Query-service backends (CEBot / MB Pro) show a fixed brand name keyed off
  // the stable per-message `modelId` (e.g. MB Pro must show "QAI ModelBuilder
  // Pro", not the user's other-selected model). Reliable across realtime/
  // reload because the modelId is stamped/persisted as query::<id>.
  // `hasOwnProperty` (not `in`) so a modelId that happens to collide with an
  // Object.prototype key can never spuriously match.
  if (
    msg.modelId !== undefined &&
    Object.prototype.hasOwnProperty.call(QUERY_SERVICE_LABELS, msg.modelId)
  ) {
    return QUERY_SERVICE_LABELS[msg.modelId]!;
  }
  const tab = activeTab.value;
  return resolveAssistantLabel(
    msg.modelId,
    msg.modelProvider,
    tab?.modelId,
    tab?.modelProvider,
  );
}

// Live streaming bubble header (V1 parity): mid-stream the message has not
// been committed yet, so resolve the active tab's current selection.
const streamingLabel = computed(() => {
  const tab = activeTab.value;
  // Multi-Agent discussion (block-5): while a named participant is speaking the
  // live bubble shows ITS display name, not the model name.
  if (tab?.streamingSenderName != null && tab.streamingSenderName !== "") {
    return tab.streamingSenderName;
  }
  // Query-service brand name while streaming (mirror of roleLabel for the
  // not-yet-committed bubble): MB Pro is identified by the active mode (the
  // outgoing hint is query::mb_pro), CEBot by the selected tab model.
  if (tab?.activeMode === "pro") {
    return QUERY_SERVICE_LABELS["query::mb_pro"]!;
  }
  if (tab?.activeMode === "gomaster") {
    return QUERY_SERVICE_LABELS["query::gomaster"]!;
  }
  if (
    tab?.modelId !== undefined &&
    Object.prototype.hasOwnProperty.call(QUERY_SERVICE_LABELS, tab.modelId)
  ) {
    return QUERY_SERVICE_LABELS[tab.modelId]!;
  }
  return resolveAssistantLabel(
    undefined,
    undefined,
    tab?.modelId,
    tab?.modelProvider,
  );
});

// Avatar for the live streaming bubble (no committed `msg` yet): brand CEBot /
// MB Pro the same way as committed messages, keyed off the active tab (MB Pro
// by activeMode, CEBot by selected modelId).
const streamingAvatarSvg = computed(() => {
  const tab = activeTab.value;
  if (tab?.activeMode === "pro") return MB_PRO_AVATAR_SVG;
  if (tab?.activeMode === "gomaster") return GOMASTER_AVATAR_SVG;
  if (tab?.modelId === "query::cebot") return CEBOT_AVATAR_SVG;
  return AI_AVATAR_SVG;
});

// ─── Multi-Agent discussion speaker rendering (block-5) ──────────────────────
// In discussion mode each assistant bubble is attributed to a named
// participant via `senderId`/`senderName`/`senderColor` (set by the frame
// handlers / commit builders). The avatar is a generated "initial on a
// theme-aware colour background" (design §5.3 — colour is a CSS token, NEVER
// hardcoded). Ordinary single-agent bubbles (no `senderId`) render exactly as
// before (the sparkle AI avatar + model-name label).

/** Whether a committed message carries discussion-speaker attribution. */
function hasSender(msg: ChatMessage): boolean {
  return typeof msg.senderId === "string" && msg.senderId !== "";
}

/** First grapheme (uppercased) of a display name for the generated avatar.
 *  Falls back to a generic glyph when the name is empty / non-letter. */
function senderInitial(name: string | undefined): string {
  const trimmed = (name ?? "").trim();
  if (trimmed === "") return "#";
  // Use the first code point so CJK / emoji names render their first glyph.
  return [...trimmed][0]!.toUpperCase();
}

/** Avatar background style for a discussion speaker — uses the participant's
 *  theme-aware palette token (`senderColor`, a `var(--discussion-speaker-N)`
 *  reference). Returns undefined for ordinary bubbles so the default avatar
 *  styling applies. */
function senderAvatarStyle(
  color: string | undefined,
): Record<string, string> | undefined {
  if (color === undefined || color === "") return undefined;
  return { background: color, color: "#fff" };
}

/** Bubble label for an assistant message: the participant name in discussion
 *  mode, else the V1 model-name label. */
function bubbleLabel(msg: ChatMessage): string {
  if (msg.role === "assistant" && hasSender(msg)) {
    return msg.senderName ?? msg.senderId ?? roleLabel(msg);
  }
  return roleLabel(msg);
}

/**
 * Discussion-mode model-name suffix (e.g. "claude-4-6-sonnet") shown right
 * after the speaker name (V2 enhancement 2026-06-21 — user requested
 * "show the model each role is using").
 *
 * Returns the empty string when:
 *   * the message is NOT an assistant turn in a multi-Agent discussion
 *     (single-agent assistant messages already render the model id via
 *     ``roleLabel`` — adding a second pill would be redundant);
 *   * no model id is available (defensive — discussion frames always
 *     carry one, but we fail closed).
 */
function bubbleModelBadge(msg: ChatMessage): string {
  if (msg.role !== "assistant" || !hasSender(msg)) return "";
  const modelId = (msg.modelId ?? "").trim();
  return modelId;
}

// ─── A3 timestamp (V1 index.html:517 + utils.js:79-82) ───────────────────────
// formatTime(ts) = new Date(ts).toLocaleTimeString([], {hour:'2-digit',
// minute:'2-digit'}). V2 messages use `createdAt` (ms epoch).
function formatTime(ts: number | undefined): string {
  if (ts === undefined || ts === null || Number.isNaN(ts)) return "";
  return new Date(ts).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ─── C1 metrics row (V1 index.html:738-746) ──────────────────────────────────
// V1 renders, for a normal (non-CC) assistant message that carries perf:
//   [ I ] {input_tokens} tokens @ {input_tps} tok/sec
//     • [ O ] {output_tokens} tokens @ {output_tps} tok/sec
//     • {ttft_ms}ms first token latency
//     • total: {(total_ms/1000).toFixed(2)}s
//     • 🔧{tool_rounds} tool rounds
// V1 reads the token counts off `perf.input_tokens` / `perf.output_tokens`.
// Per the task mapping we fall back to the `end`-frame usage
// (prompt_tokens → input, completion_tokens → output) when perf has not
// populated those yet. The tok/sec segments depend on `perf.input_tps` /
// `perf.output_tps`, which the transport (useChatTransport — out of this
// agent's scope) has not wired yet; each segment is v-if-guarded so a
// missing rate simply omits "@ N tok/sec" without ever producing NaN.
// See manifest C1 (tps data fill needs useChatTransport — main agent).
interface ChatMetrics {
  inputTokens: number | null;
  inputTps: number | null;
  outputTokens: number | null;
  outputTps: number | null;
  ttftMs: number | null;
  totalSeconds: string | null;
  toolRounds: number | null;
}

function buildMetrics(
  usage: ChatMessageUsage | undefined,
  perf: ChatMessagePerf | undefined,
): ChatMetrics | null {
  if (usage === undefined && perf === undefined) return null;
  // Multi-round agentic turn correctness (per AGENTS.md 🟡🟡 fix):
  // ``usage.prompt_tokens`` is the cross-round SUM (streaming.py
  // ``_accumulate_usage``). For a multi-round turn that SUM inflates the
  // "input size" badge (e.g. 3 rounds of 10K each → SUM 30K vs the true
  // last-round wire 10K). The backend tail-appends
  // ``last_round_prompt_tokens`` (streaming.py:3404) — the LAST round's
  // _extract_usage-corrected true wire — which is what the user expects
  // to see ("this message's real input size"). Single-round turn:
  // ``last_round_prompt_tokens == prompt_tokens`` → no display change.
  // ``perf.input_tokens`` is computed by useChatTransport.flushTurnPerf
  // off the same usage with the same preference, so the perf-first read
  // here remains the live-streaming source of truth.
  const inputTokens =
    perf?.input_tokens ??
    usage?.last_round_prompt_tokens ??
    usage?.prompt_tokens ??
    null;
  const outputTokens =
    perf?.output_tokens ?? usage?.completion_tokens ?? null;
  const totalSeconds =
    perf?.total_ms !== undefined
      ? (perf.total_ms / 1000).toFixed(2)
      : usage?.elapsed_seconds !== undefined
        ? Number(usage.elapsed_seconds).toFixed(2)
        : null;
  return {
    inputTokens,
    inputTps: perf?.input_tps ?? null,
    outputTokens,
    outputTps: perf?.output_tps ?? null,
    ttftMs: perf?.ttft_ms ?? null,
    totalSeconds,
    toolRounds: perf?.tool_rounds ?? null,
  };
}

/** Whether the assistant metrics row has anything worth rendering. */
function hasMetrics(
  usage: ChatMessageUsage | undefined,
  perf: ChatMessagePerf | undefined,
): boolean {
  const m = buildMetrics(usage, perf);
  if (m === null) return false;
  return (
    m.inputTokens !== null ||
    m.outputTokens !== null ||
    m.ttftMs !== null ||
    m.totalSeconds !== null ||
    m.toolRounds !== null
  );
}

/** Per-message copy state — shows a transient "Copied" tick. */
const copiedId = ref<string | null>(null);

async function copyMessage(id: string, content: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(content);
    copiedId.value = id;
    window.setTimeout(() => {
      if (copiedId.value === id) copiedId.value = null;
    }, 1500);
  } catch {
    // clipboard may be unavailable (insecure context); fail silently.
  }
}

// ─── Pending mid-turn injection bubble actions (V2 enhancement) ──────────────
// A mid-turn injection renders as a grey `meta.injected + meta.pending` user
// bubble while the backend folds it into the live run (see
// `insertPendingInjection`). WHILE still pending the user may Cancel (withdraw
// it so the run won't fold it in), Edit (withdraw + put the text back in the
// composer to re-edit) or Copy it — mirroring MessageQueuePanel's ✕/✎/⧉.
// Cancel/Edit need the control channel + composer ref ChatView owns, so they
// are emitted upward; Copy is self-contained here (clipboard + toast).
//
// The actions are gated on `meta.pending === true`: once the
// `injected_message` frame commits the bubble (`pending` cleared) the buttons
// vanish, so a cancel-after-committed click cannot happen — and even a stale
// click would be a no-op because `removePendingInjection` only removes still-
// pending messages (State-Truth-First; the committed bubble is conversation
// truth and stays).
function isPendingInjection(msg: ChatMessage): boolean {
  const meta = msg.meta as Record<string, unknown> | undefined;
  return meta?.["injected"] === true && meta?.["pending"] === true;
}

function onInjectCancel(msg: ChatMessage): void {
  emit("inject-cancel", { id: msg.id, text: msg.content });
}

function onInjectEdit(msg: ChatMessage): void {
  emit("inject-edit", { id: msg.id, text: msg.content });
}

/** Copy a pending injection bubble's text to the clipboard (the ⧉ affordance);
 *  the bubble is preserved (copy does not cancel). Mirrors MessageQueuePanel's
 *  `copyItem` — async Clipboard API + a success/failure toast, never a silent
 *  no-op. */
async function copyInjection(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(t("chat.queueCopied", "Copied"));
  } catch {
    toast.warning(t("chat.queueCopyFailed", "Copy failed"));
  }
}

// ─── B1 code-block copy via event delegation (V1 utils.js:48-51) ─────────────
// markdown.ts renders each code block with a header button carrying
// `data-code-copy` + the raw source in `data-code` (URI-encoded). Rather
// than the V1 inline `onclick` (forbidden by the V2 DOMPurify policy),
// we listen on the messages container and resolve the nearest button.
// A transient "✓" replaces the icon for 1.5s as feedback.
function onContainerClick(ev: MouseEvent): void {
  const target = ev.target as HTMLElement | null;
  if (target === null) return;

  // ── Mermaid toolbar action (copy/download via event delegation) ──────────
  const actionBtn = target.closest<HTMLElement>("[data-mermaid-action]");
  if (actionBtn !== null) {
    onMermaidAction(actionBtn);
    return;
  }
  // Mermaid menu trigger (copy/download dropdown) — toggle open state.
  const menuTrigger = target.closest<HTMLElement>("[data-mermaid-menu-trigger]");
  if (menuTrigger !== null) {
    toggleMermaidMenu(menuTrigger);
    return;
  }
  // Click on the rendered diagram (not on the toolbar) → open the lightbox.
  const zoomTarget = target.closest<HTMLElement>("[data-mermaid-zoom]");
  if (zoomTarget !== null) {
    onMermaidZoom(zoomTarget);
    return;
  }
  // Any other click dismisses open mermaid menus.
  closeMermaidMenus();

  const btn = target.closest<HTMLElement>("[data-code-copy]");
  if (btn === null) return;
  const encoded = btn.getAttribute("data-code");
  if (encoded === null) return;
  let code: string;
  try {
    code = decodeURIComponent(encoded);
  } catch {
    code = encoded;
  }
  void navigator.clipboard
    ?.writeText(code)
    .then(() => {
      const prev = btn.textContent ?? "⧉ Copy";
      btn.textContent = "✓ Copied";
      window.setTimeout(() => {
        btn.textContent = prev;
      }, 1500);
    })
    .catch(() => {
      // clipboard unavailable (insecure context) — fail silently.
    });
}

// ─── Mermaid toolbar actions (copy/download) + click-to-zoom ─────────────────
// Mirrors the `data-code-copy` event-delegation model: the diagram toolbar
// buttons carry `data-mermaid-action`; the pure copy/download logic lives in
// `markdown-mermaid.ts` (browser-only — XMLSerializer + canvas + Clipboard).
function closeMermaidMenus(except?: HTMLElement): void {
  const root = containerRef.value;
  if (root === null) return;
  for (const menu of root.querySelectorAll<HTMLElement>(
    ".mermaid-menu[data-open]",
  )) {
    if (except !== undefined && menu === except) continue;
    menu.removeAttribute("data-open");
    const trigger = menu.querySelector<HTMLElement>(
      "[data-mermaid-menu-trigger]",
    );
    trigger?.setAttribute("aria-expanded", "false");
  }
}

function toggleMermaidMenu(trigger: HTMLElement): void {
  const menu = trigger.closest<HTMLElement>(".mermaid-menu[data-mermaid-menu]");
  if (menu === null) return;
  const open = menu.hasAttribute("data-open");
  closeMermaidMenus();
  if (!open) {
    menu.setAttribute("data-open", "");
    trigger.setAttribute("aria-expanded", "true");
  }
}

function onMermaidAction(btn: HTMLElement): void {
  const action = btn.getAttribute("data-mermaid-action") as MermaidAction | null;
  if (action === null) return;
  const wrapper = btn.closest<HTMLElement>('[data-kind="mermaid"]');
  if (wrapper === null) return;
  // The "copied" feedback must land on the VISIBLE dropdown trigger button, not
  // on the menu item `btn` — `closeMermaidMenus()` hides the menu, so changing
  // the item's text would be invisible to the user.
  const trigger = btn
    .closest<HTMLElement>(".mermaid-menu")
    ?.querySelector<HTMLElement>("[data-mermaid-menu-trigger]");
  closeMermaidMenus();
  void runMermaidAction(action, wrapper)
    .then((copied) => {
      if (!copied || !trigger) return;
      const prev = trigger.textContent ?? "";
      trigger.textContent = t("chat.mermaid.copied", "Copied");
      window.setTimeout(() => {
        trigger.textContent = prev;
      }, 1500);
    })
    .catch(() => {
      // clipboard / canvas unavailable — fail silently (no broken UI).
    });
}

function onMermaidZoom(zoomTarget: HTMLElement): void {
  const wrapper = zoomTarget.closest<HTMLElement>('[data-kind="mermaid"]');
  if (wrapper === null) return;
  const svg = mermaidSvgOf(wrapper);
  if (svg === null) return;
  // Reuse the shared lightbox (accepts a data URL — same as composer images).
  lightbox.open(svgToDataUrl(svg));
}

// ─── Send-failure retry (V1 index.html:685-689) ──────────────────────────────
// A user message whose turn failed carries `msg.sendError`. We surface
// an inline error banner + ↻ Retry button under that message; clicking
// it emits `retry` so the parent (ChatView) can clear the marker,
// remove the failed message and re-send its content (V1
// retryLastMessage parity).

function onRetry(id: string, content: string): void {
  emit("retry", { id, content });
}

// ─── Registry-driven error rendering (single source of truth) ────────────────
// ALL terminal error rendering (tab-level bubble + per-message banner) goes
// through the declarative registry (`chatErrorActions.ts`) + its executor
// (`useChatErrorActions.ts`). The former apiKey / unsupported-param special
// cases are folded into the registry so there is exactly ONE rendering path
// (AGENTS.md §2 判据 1 — no parallel mechanisms). The registry maps each error
// code → concise localized message + up to two action buttons; the executor
// runs the behaviour keyed by the stable action id.
const { runChatErrorAction } = useChatErrorActions();

// Edition-aware label for the missing-API-key action button: internal edition
// can set the key in place ("Set API Key"); external edition must add a
// provider first, so the label points at Cloud Model Settings. The registry's
// generic `setApiKey` label is overridden with this edition-aware wording so it
// matches what actually happens.
const apiKeyActionLabel = computed(() =>
  service.isInternal === true
    ? t("cloudModels.apiKeyError.setKeyCta")
    : t("cloudModels.apiKeyError.goToSettingsCta"),
);

// Resolve the label for an action button. `open_api_key_flow` uses the
// edition-aware label; everything else uses the registry `labelKey`.
function chatErrorActionLabel(action: ChatErrorAction): string {
  if (action.id === "open_api_key_flow") {
    return apiKeyActionLabel.value;
  }
  return t(action.labelKey);
}

// Map an action style → the button emoji prefix (keeps the established visual
// cues: 🔑 set key, ⚙ settings, ⚠ danger). Icon is cosmetic; the id drives
// behaviour.
function chatErrorActionIcon(action: ChatErrorAction): string {
  switch (action.id) {
    case "open_api_key_flow":
      return "🔑";
    case "open_provider_settings":
    case "select_model":
      return "⚙";
    case "configure_tls_and_retry":
      return "⚠";
    case "compress_context":
      return "🗜";
    default:
      return "";
  }
}

// The build-time app version (vite `define`) — included in diagnostics.
const appVersion =
  typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : null;

/**
 * Build the per-invocation executor context for the CURRENT failed turn. The
 * `isCurrent` guard compares the anchor message id captured at click time with
 * the live failed message, so a stale/duplicate click (after the user retried /
 * cleared) is a no-op. `diagnostics` assembles the SANITIZED block from the
 * tab's `lastError` + selected model (never secrets/prompt).
 */
function buildErrorCtx(failedMessageId: string | null, failedContent: string) {
  return {
    failedMessageId,
    failedContent,
    isCurrent: () => {
      const live = failedUserMessage.value;
      // Still current iff the same message still carries a send error (or, for
      // the tab-level bubble with no anchor, the tab is still in error state).
      if (failedMessageId === null) {
        return activeTab.value?.status === "error";
      }
      return live !== null && live.id === failedMessageId;
    },
    retry: (id: string, content: string) => {
      onRetry(id, content);
    },
    diagnostics: () => {
      const tab = activeTab.value;
      const le = tab?.lastError ?? null;
      return buildDiagnostics({
        code: le?.code ?? null,
        retryDisposition: le?.retryDisposition ?? null,
        httpStatus: le?.httpStatus ?? null,
        requestId: le?.requestId ?? null,
        model: tab?.modelId ?? null,
        // No base_url is available client-side without leaking config; omit it
        // (sanitizeHost handles null). Kept in the shape for completeness.
        baseUrl: null,
        attempt: null,
        messageFirstLine: le?.message ?? null,
        appVersion,
      });
    },
  };
}

// Dispatch a registry action button click to the executor. `void` because the
// executor is async (TLS confirm→save→retry); errors surface via toast inside.
function onChatErrorAction(
  actionId: ChatErrorActionId,
  failedMessageId: string | null,
  failedContent: string,
): void {
  void runChatErrorAction(actionId, buildErrorCtx(failedMessageId, failedContent));
}


// Map a backend error `code` to a localized, concise message via the registry
// (the SINGLE source of truth). The registry's `messageKey` is resolved with
// `t`, falling back to the raw backend `message` when the key is missing in a
// locale (so a half-translated locale never shows a bare key, and codes with no
// dedicated spec show the generic message). Replaces the old per-code
// `SEND_ERROR_CODE_KEYS` table — the registry now owns code→message.
//
// One legacy code (`no_discussion_participants`) is NOT an LLM error and keeps
// its dedicated `chat.sendErrors.*` message; it has no registry spec, so we
// special-case just its message lookup here (it never renders action buttons).
function localizedSendError(
  message: string | null | undefined,
  code: string | null | undefined,
): string {
  if (code === "no_discussion_participants") {
    return t("chat.sendErrors.noDiscussionParticipants", message ?? "");
  }
  if (code != null && code !== "") {
    const spec = resolveChatErrorSpec(code);
    // `t(key, fallback)` → localized message, or the raw backend message if the
    // key is somehow absent. For the generic spec this yields a friendly
    // "request failed" line rather than a bare code.
    return t(spec.messageKey, message ?? spec.messageKey);
  }
  return message ?? "";
}

// The registry spec for the CURRENT failed turn's code — drives both the
// per-message banner and the tab-level bubble action buttons + retry.
const failedErrorSpec = computed(() => {
  const tab = activeTab.value;
  const code = tab?.lastError?.code ?? failedUserMessage.value?.sendErrorCode ?? null;
  if (code === null || code === undefined || code === "") {
    return null;
  }
  return resolveChatErrorSpec(code);
});


// ─── B (turn_warning) — V1 useChat.js:1422-1432 parity ───────────────────────
// `chatTabs.applyFrame` commits a turn_warning notice as an assistant
// message with `isCommandReply=true` + `meta.kind="turn_warning"`. When
// the server pre-rendered the message, `content` carries the final
// text; otherwise the store stashes the raw `turn_count` as
// `__turn_warning__:N` so the renderer can localise via i18n here
// (the store has no `t` instance — see comment in chatTabs.ts).
function isTurnWarningPlaceholder(content: string): boolean {
  return content.startsWith("__turn_warning__:");
}

function renderTurnWarning(content: string): string {
  // `__turn_warning__:N` → localized text via chat.turnLimitWarn.
  const raw = content.slice("__turn_warning__:".length);
  const n = Number.parseInt(raw, 10);
  return t("chat.turnLimitWarn", { n: Number.isFinite(n) ? n : raw });
}

function commandReplyText(msg: ChatMessage): string {
  if (
    msg.meta !== undefined &&
    (msg.meta as { kind?: unknown }).kind === "turn_warning" &&
    isTurnWarningPlaceholder(msg.content)
  ) {
    return renderTurnWarning(msg.content);
  }
  return msg.content;
}

// ─── C (abort) — V1 useChat.js:2685-2712 parity ──────────────────────────────
// `confirmAbort` commits any partial streamingContent as an assistant
// message tagged `meta.interrupted=true`. We surface the V1
// `chat.interruptedMark` (localized "*[Interrupted]*") inline at the
// end of the bubble so the user sees what was generated so far plus a
// clear truncation indicator.
function isInterruptedMessage(msg: ChatMessage): boolean {
  return (
    msg.meta !== undefined &&
    (msg.meta as { interrupted?: unknown }).interrupted === true
  );
}

/** Return the message's `meta.kind` when set (a string). Used to detect
 *  independent ``subagent_summary`` messages emitted by the backend
 *  ``_build_subagent_summary_message`` helper (SUBAGENT-RELOAD-PERSIST-
 *  INDEPENDENT-MSG, 2026-07-02). Old inline-shape messages return `undefined`
 *  and continue to render the separator unchanged. */
function messageKind(msg: ChatMessage): string | undefined {
  if (!msg.meta) return undefined;
  const k = (msg.meta as { kind?: unknown }).kind;
  return typeof k === "string" ? k : undefined;
}

/** True iff a subsequent assistant message with visible summary text follows
 *  ``msg`` in the active tab's message list. Used to decide whether to render
 *  the ``subagent-summary-separator`` ("↩ 主 Agent 总结" hint) — showing that
 *  hint below a sub-agent block only makes sense when the main agent actually
 *  spoke after the sub-agents finished. When ``msg`` is an INDEPENDENT
 *  ``subagent_summary`` message (new persistence shape) with no trailing
 *  main-agent text, the separator must be suppressed so the UI does not
 *  falsely promise "the main agent summarised below".
 *
 *  Traversal skips messages that carry no visible bubble (empty content,
 *  ``[tool_calls]`` sentinel, or another ``subagent_summary`` message), so
 *  neighbour tool-call messages don't count as a "summary". Stops at the
 *  next USER message (a new turn begins) — anything past that is a different
 *  turn's summary, not this turn's. */
function hasFollowingMainSummary(msg: ChatMessage): boolean {
  const tab = activeTab.value;
  if (!tab) return false;
  const list = tab.messages;
  const idx = list.findIndex((m) => m.id === msg.id);
  if (idx < 0) return false;
  for (let i = idx + 1; i < list.length; i++) {
    const m = list[i];
    if (!m) continue;
    if (m.role === "user") return false; // next turn started
    if (m.role !== "assistant") continue;
    if (messageKind(m) === "subagent_summary") continue; // another sub-agent card
    // Visible summary text bubble: non-empty content that isn't a sentinel.
    if (m.content && m.content !== "[tool_calls]") return true;
  }
  return false;
}

/** Assistant content with the localized interruption marker appended
 *  when the message represents an aborted turn. The marker already
 *  starts with two newlines (V1: `"\n\n*[Interrupted]*"`) so it
 *  renders as a separate italicized block beneath the partial text. */
function assistantContent(msg: ChatMessage): string {
  if (msg.role !== "assistant") return msg.content;
  if (isInterruptedMessage(msg)) {
    return msg.content + t("chat.interruptedMark");
  }
  return msg.content;
}

// PERF: memoized markdown for COMMITTED (immutable) assistant messages.
//
// The message list template calls `renderMarkdown(...)` inline for every
// assistant message (line ~846). Vue re-evaluates ALL inline template
// expressions on ANY reactive change in this component — including each
// streaming flush that mutates `streamingContent`. Without memoization, every
// streaming tick re-parses (marked + highlight.js `highlightAuto` + DOMPurify)
// EVERY already-committed message, so the per-tick cost grows with the number
// of messages (and got worse once per-round `tool_lead_in` messages were
// committed). Committed message content never changes, so cache the rendered
// HTML keyed by message id + content; a content change (rare) busts the entry.
// The LIVE streaming bubble (template line ~1010) intentionally stays
// un-memoized — it changes every flush, but those flushes are already
// rAF-throttled to ≤~60/s.
const _committedMarkdownCache = new Map<
  string,
  { content: string; html: string }
>();

function renderCommittedMarkdown(msg: ChatMessage): string {
  const content = assistantContent(msg);
  const cached = _committedMarkdownCache.get(msg.id);
  if (cached !== undefined && cached.content === content) {
    return cached.html;
  }
  const html = renderMarkdown(content, { markedOptions: { breaks: true } });
  _committedMarkdownCache.set(msg.id, { content, html });
  // Bound the cache so a very long session can't leak unboundedly.
  if (_committedMarkdownCache.size > 500) {
    const oldest = _committedMarkdownCache.keys().next().value;
    if (oldest !== undefined) _committedMarkdownCache.delete(oldest);
  }
  return html;
}

// ─── Image messages: parse `![alt](url)` markdown + lightbox (V1:690-726) ────
// The composer embeds uploaded-image references as markdown image links
// inside the outgoing prompt text (ChatComposer.uploadPendingImages →
// `![name](/api/images/files/…)`). That text is what gets persisted as
// the message content and what history reload returns verbatim, so a
// single parser handles both live-send and reloaded-history images with
// no extra store field or backend change.

interface ParsedImage {
  alt: string;
  url: string;
}

// Matches a markdown image: ![alt](url). `alt` may be empty; `url`
// captures everything up to the closing paren (no nested parens in our
// own emitted URLs — they are `/api/images/files/<date>/<conv>/<id>.<ext>`).
const IMAGE_MD_RE = /!\[([^\]]*)\]\(([^)\s]+)\)/g;

function extractImages(content: string): ParsedImage[] {
  const out: ParsedImage[] = [];
  IMAGE_MD_RE.lastIndex = 0;
  let m: RegExpExecArray | null = IMAGE_MD_RE.exec(content);
  while (m !== null) {
    const url = m[2];
    if (url !== undefined && url !== "") {
      out.push({ alt: m[1] ?? "", url });
    }
    m = IMAGE_MD_RE.exec(content);
  }
  return out;
}

/** Strip image markdown out of the displayed text bubble so the raw
 *  `![…](…)` link does not show up beside the rendered thumbnail. Used
 *  for the plain-text (user) bubble; assistant bubbles already render
 *  their own markdown images inline via `renderMarkdown`. */
function textWithoutImages(content: string): string {
  return content.replace(IMAGE_MD_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

// Lightbox overlay (V1 index.html:137-148 + app.js:168-213). Clicking a
// thumbnail opens the full image; wheel zooms, drag pans, dblclick resets,
// click-overlay / Esc closes. All state + interactions live in `useLightbox`,
// shared with `ChatComposer.vue` (pending-image thumbnails).
const lightbox = useLightbox();

function openLightbox(url: string): void {
  lightbox.open(url);
}

function onImageError(ev: Event): void {
  const img = ev.target as HTMLImageElement | null;
  if (img === null) return;
  img.style.display = "none";
  const next = img.nextElementSibling as HTMLElement | null;
  if (next !== null) next.style.display = "flex";
}

// ─── D1 scroll-to-top / bottom + userScrolledUp (V1 useChat.js:402-473) ──────
// The messages container is this component's root element. We track
// `userScrolledUp` so the ↓ button highlights when the user is reading
// history (V1 scroll-nav-btn--active). Threshold + behaviour copied from
// V1 (_isAtBottom 80px tolerance; scrollToTop marks userScrolledUp).
const containerRef = ref<HTMLElement | null>(null);
const userScrolledUp = ref(false);
const SCROLL_BOTTOM_THRESHOLD = 80;

// Per-tab scroll-position memory (see useChatScrollMemory): records where the
// user was reading in each tab so switching away and back (or remount under
// <KeepAlive>) restores the position instead of jumping to the newest message.
// Keyed by conversationId (with tabId fallback) — see chatScrollKey().
const scrollMemory = useChatScrollMemory();

/**
 * Build the scroll-memory key for a given tabId. Reads the live tab list to
 * find its conversationId, so close+reopen of the same conversation (which
 * mints a new tabId) keeps hitting the same entry.
 */
function keyForTab(tabId: TabId | null): string | null {
  if (tabId === null) return null;
  const tab = store.tabs.find((t) => t.id === tabId);
  // SCROLL LEAK FIX (2026-07-11): a sub-agent tab's `conversationId` is the
  // PARENT/ROOT conversation id, so keying purely on `conversationId` made the
  // main tab and all its sub-agent tabs share ONE scroll entry and clobber each
  // other on switch. Pass the sub-agent's own id so `chatScrollKey` derives a
  // distinct `sub:<subagentId>` key (mirroring the store's `_subOverrideKey`),
  // letting each agent view remember + restore its OWN scroll position.
  return chatScrollKey(
    tabId,
    tab?.conversationId ?? null,
    tab?.subagentMeta?.subagentId ?? null,
  );
}

/**
 * Snapshot the current container scrollTop AND "at-bottom" flag for the given
 * tab. We record `wasAtBottom` (within the 80px SCROLL_BOTTOM_THRESHOLD) so a
 * tab that was being followed at the bottom keeps following on return —
 * restoring a stale numeric scrollTop would otherwise strand the view in the
 * middle of newly-grown content.
 */
function saveScrollFor(tabId: TabId | null): void {
  const el = containerRef.value;
  if (el === null) return;
  const key = keyForTab(tabId);
  if (key === null) return;
  const wasAtBottom =
    el.scrollHeight - el.scrollTop - el.clientHeight <= SCROLL_BOTTOM_THRESHOLD;
  scrollMemory.save(key, { scrollTop: el.scrollTop, wasAtBottom });
}

/**
 * Restore a tab's saved scroll position. Three cases:
 *  1. No entry recorded → fall back to bottom (first visit / new conversation
 *     — preserves the original "land on newest message" behaviour).
 *  2. Entry says `wasAtBottom = true` → scrollToBottomInstant() and clear
 *     `userScrolledUp` so the follow-streaming watcher continues tracking.
 *  3. Entry says `wasAtBottom = false` → clamp the saved scrollTop to the
 *     current scroll range and restore it (the user was reading history).
 *
 * Async-content correction (KA-MERMAID-RESTORE-1)
 * -----------------------------------------------
 * Some children render asynchronously after the initial `nextTick`:
 *  - `useMermaidRender` schedules its SVG insertion on its own microtask.
 *  - `ReasoningBlock` / `SubAgentBlock` may expand on a follow-up tick.
 *  - Code highlighter / markdown image loads keep extending scrollHeight.
 * The first clamp/`userScrolledUp` calculation runs against the not-yet-grown
 * scrollHeight, so a saved mid-conversation position could land slightly too
 * far down (relative to the eventual content) and `userScrolledUp` could be
 * miscomputed. Re-clamp + re-evaluate one animation frame later: that single
 * rAF catches the common cases (Mermaid SVG, code blocks, lazy image natural
 * size) without spinning into an open-ended ResizeObserver.
 */
function restoreScrollFor(tabId: TabId | null): void {
  const el = containerRef.value;
  if (el === null) {
    return;
  }
  const key = keyForTab(tabId);
  const saved = key === null ? null : scrollMemory.get(key);
  if (saved === null) {
    scrollToBottomInstant();
    // Even bottom-anchored restores need a rAF tail-correction: async
    // children inserted after this tick will grow scrollHeight, and the
    // current `scrollTop = scrollHeight` snapshot will no longer be at the
    // bottom afterwards. Re-pin to the new bottom in the next frame.
    schedulePostRestoreCorrection(null);
    return;
  }
  if (saved.wasAtBottom) {
    scrollToBottomInstant();
    schedulePostRestoreCorrection(null);
    return;
  }
  // Clamp to the current scrollable range (content height may have changed
  // since the position was recorded).
  const max = el.scrollHeight - el.clientHeight;
  el.scrollTop = Math.min(saved.scrollTop, Math.max(0, max));
  userScrolledUp.value =
    el.scrollHeight - el.scrollTop - el.clientHeight > SCROLL_BOTTOM_THRESHOLD;
  // Re-clamp + re-evaluate `userScrolledUp` on the next animation frame to
  // absorb async content (Mermaid SVG / lazy image natural sizes) that
  // landed between this synchronous restore and the first paint.
  schedulePostRestoreCorrection(saved.scrollTop);
}

// rAF handle used by `restoreScrollFor` to defer a "one more pass" clamp
// after async children render. Coalesced so back-to-back restores collapse
// into one correction (the latest call's intent wins).
let postRestoreFrame: number | null = null;
function schedulePostRestoreCorrection(targetScrollTop: number | null): void {
  if (postRestoreFrame !== null) {
    window.cancelAnimationFrame(postRestoreFrame);
  }
  postRestoreFrame = window.requestAnimationFrame(() => {
    postRestoreFrame = null;
    const el = containerRef.value;
    if (el === null) return;
    if (targetScrollTop === null) {
      // Bottom-anchored path: re-pin to the (now possibly taller) bottom.
      el.scrollTop = el.scrollHeight;
      userScrolledUp.value = false;
    } else {
      // Mid-conversation path: re-clamp the originally requested scrollTop
      // against the (now possibly taller) content range, then recompute
      // `userScrolledUp` for the follow-streaming gate.
      const max = el.scrollHeight - el.clientHeight;
      el.scrollTop = Math.min(targetScrollTop, Math.max(0, max));
      userScrolledUp.value =
        el.scrollHeight - el.scrollTop - el.clientHeight > SCROLL_BOTTOM_THRESHOLD;
    }
  });
}
function cancelPostRestoreCorrection(): void {
  if (postRestoreFrame !== null) {
    window.cancelAnimationFrame(postRestoreFrame);
    postRestoreFrame = null;
  }
}

// ─── Native Mermaid rendering (committed messages only) ──────────────────────
// Scan the whole scroll container after each commit; `renderMermaid` walks all
// committed ```mermaid``` marker blocks idempotently (source+theme hash) and
// SKIPS the live streaming bubble (marked `data-mermaid-skip`), so diagrams
// render only once a turn commits — the raw code shows while streaming. The
// content key is a signature over committed assistant message ids + content
// lengths (changes when a message commits / edits, NOT on every streaming
// flush), plus the message count. Theme switches re-render via the hook's
// internal resolvedTheme watch.
const mermaidCommittedKey = computed(() => {
  const msgs = activeTab.value?.messages ?? [];
  let sig = `${msgs.length}`;
  for (const m of msgs) {
    if (m.role === "assistant") {
      sig += `|${m.id}:${(m.content ?? "").length}`;
    }
  }
  return sig;
});
// Render Mermaid ONLY in a TERMINAL tab state (idle = completed, error =
// stream cut off — content will not change further). During a streaming turn,
// agentic-loop "round" messages grow token-by-token INSIDE the committed
// container (they are NOT the bottom `data-mermaid-skip` bubble), so a Mermaid
// block in such a message would be re-rendered on every token: when the source
// is momentarily syntactically complete it renders to an SVG, the next token
// breaks it so it falls back to source text, the next completes it again — the
// reported "flips between text and image" flicker. We also exclude `aborting`:
// it is a transient handshake state where `confirmAbort` has NOT yet committed
// the final content, so rendering then would draw a half-finished diagram that
// the subsequent idle re-render replaces. Gating on terminal states means the
// diagram renders exactly once, after the full source has arrived. The hook
// watches this ref and fires a single render when it flips true at turn end.
const mermaidEnabled = computed(() => {
  const s = activeTab.value?.status;
  return s === "idle" || s === "error";
});
useMermaidRender(containerRef, {
  content: mermaidCommittedKey,
  enabled: mermaidEnabled,
  labels: () => ({
    rendering: t("chat.mermaid.rendering"),
    renderError: (message: string) => t("chat.mermaid.renderError", { message }),
    errorDefault: t("chat.mermaid.errorDefault"),
    errorEmpty: t("chat.mermaid.errorEmpty"),
    copy: t("chat.mermaid.copy"),
    download: t("chat.mermaid.download"),
    copySource: t("chat.mermaid.copySource"),
    copySvg: t("chat.mermaid.copySvg"),
    copyPng: t("chat.mermaid.copyPng"),
    downloadSvg: t("chat.mermaid.downloadSvg"),
    downloadPng: t("chat.mermaid.downloadPng"),
    copied: t("chat.mermaid.copied"),
    zoomHint: t("chat.mermaid.zoomHint"),
  }),
});

function isAtBottom(): boolean {
  const el = containerRef.value;
  if (el === null) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= SCROLL_BOTTOM_THRESHOLD;
}

function onContainerScroll(): void {
  if (isAtBottom()) {
    if (userScrolledUp.value) userScrolledUp.value = false;
  } else if (!userScrolledUp.value) {
    userScrolledUp.value = true;
  }
  // Persist the live reading position for the active tab so it survives a
  // remount / page reload (sessionStorage tier). Throttled via rAF so we
  // coalesce the 30-60Hz scroll burst into at most one `setItem` per frame.
  scheduleScrollSave();
}

// rAF-coalesced scroll persistence — `onContainerScroll` fires per scroll
// event (passive listener), but writing to sessionStorage on every fire is
// wasteful when the browser is throttling rendering to 16ms anyway. Coalesce
// to one write per animation frame.
let scrollSaveFrame: number | null = null;
function scheduleScrollSave(): void {
  if (scrollSaveFrame !== null) return;
  scrollSaveFrame = window.requestAnimationFrame(() => {
    scrollSaveFrame = null;
    saveScrollFor(store.activeTabId);
  });
}
function cancelScrollSave(): void {
  if (scrollSaveFrame !== null) {
    window.cancelAnimationFrame(scrollSaveFrame);
    scrollSaveFrame = null;
  }
}

function scrollToTop(): void {
  const el = containerRef.value;
  if (el === null) return;
  userScrolledUp.value = true;
  el.scrollTo({ top: 0, behavior: "smooth" });
}

function scrollToBottom(): void {
  const el = containerRef.value;
  if (el === null) return;
  el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
}

// Show the scroll-nav group when there is at least one message rendered.
const hasMessages = computed(
  () => activeMessages.value.length > 0,
);

// ─── D3 load-more (IntersectionObserver + scroll preservation) ───────────────
// V1 parity: useChat.js:879-916. The sentinel at the top of the list is
// observed; when it becomes visible AND older messages remain, we load the
// previous page. Before the prepend lands we snapshot scrollHeight so we can
// restore the user's reading position afterwards (V1 useChat.js:879-890).
const loadMoreSentinel = ref<HTMLElement | null>(null);
let loadMoreObserver: IntersectionObserver | null = null;

async function onLoadMore(): Promise<void> {
  const tab = activeTab.value;
  const el = containerRef.value;
  if (tab === null || el === null) return;
  if (!tab.hasMoreMessages || tab.loadingMore) return;
  const prevScrollHeight = el.scrollHeight;
  const prevScrollTop = el.scrollTop;
  await store.loadMoreMessages(tab.id);
  // Restore reading position: the newly prepended page grows scrollHeight;
  // keep the same content under the viewport (V1 useChat.js:885-889).
  await nextTick();
  const delta = el.scrollHeight - prevScrollHeight;
  if (delta > 0) {
    el.scrollTop = prevScrollTop + delta;
  }
}

function setupLoadMoreObserver(): void {
  teardownLoadMoreObserver();
  const root = containerRef.value;
  const sentinel = loadMoreSentinel.value;
  if (root === null || sentinel === null) return;
  loadMoreObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          void onLoadMore();
        }
      }
    },
    { root, rootMargin: "120px 0px 0px 0px", threshold: 0 },
  );
  loadMoreObserver.observe(sentinel);
}

function teardownLoadMoreObserver(): void {
  if (loadMoreObserver !== null) {
    loadMoreObserver.disconnect();
    loadMoreObserver = null;
  }
}

/** Jump to bottom WITHOUT smooth animation (initial load / tab switch). */
function scrollToBottomInstant(): void {
  const el = containerRef.value;
  if (el === null) return;
  el.scrollTop = el.scrollHeight;
  userScrolledUp.value = false;
}

/**
 * Scroll a specific message into view. Used by the composer-toolbar
 * "user-message jump" popover (UserMessageJumpPopover). The message row
 * carries `data-message-id` (the stable `msg.id` UUID) so we can locate it
 * within `containerRef` without coupling the parent to internal layout.
 *
 * After scrolling we set `userScrolledUp = true` so any in-flight streaming
 * tokens do NOT immediately yank the view back to the bottom (matches the
 * 80px threshold semantics used elsewhere in this component).
 */
function scrollToMessage(messageId: string): void {
  const root = containerRef.value;
  if (root === null) return;
  // CSS.escape is widely supported; fall back to literal interpolation if
  // unavailable (msg.id is a UUID — only [a-f0-9-] — so quoting is safe).
  const sel =
    typeof CSS !== "undefined" && typeof CSS.escape === "function"
      ? `[data-message-id="${CSS.escape(messageId)}"]`
      : `[data-message-id="${messageId}"]`;
  const el = root.querySelector(sel) as HTMLElement | null;
  if (el === null) return;
  userScrolledUp.value = true;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
}

defineExpose({ scrollToMessage });

// ─── D2 floating stop button (V1 index.html:922-937) ─────────────────────────
// The floating Stop button itself now lives in ChatView (anchored to the
// composer); ChatMessageList no longer renders any stop affordance of its own
// (the old live tool-card `@stop` is gone with the single-track refactor — the
// in-flight round's tool cards render through the committed-message loop). The
// `stop` emit is retained on the component contract for backward compatibility.

// Tab switch → restore that tab's last reading position (or land on the newest
// message if it has none recorded). Previously this always forced
// `scrollToBottomInstant()`, discarding the user's position. We snapshot the
// LEAVING tab's scrollTop (oldId) before the DOM reflects the new tab, then
// restore the ENTERING tab's saved position on the next tick. Re-arming the
// load-more observer is handled by the sentinel-render-condition watch below
// (which also fires on tab switch because the tab id is part of its key), so we
// do NOT set it up here — that would double-arm it on every tab switch.
watch(
  () => store.activeTabId,
  (newId, oldId) => {
    // Snapshot the tab we are leaving BEFORE Vue swaps in the new tab's
    // messages (the container still shows the old tab's scrollTop here).
    saveScrollFor(oldId ?? null);
    void nextTick(() => {
      restoreScrollFor(newId);
    });
  },
);

// Re-arm the load-more observer when the sentinel's RENDER condition flips.
//
// BUG (reported: "向上滚动到顶端应动态加载更多旧消息，但没加载"): the
// load-more sentinel is rendered with `v-if="activeTab.hasMoreMessages"`, but
// `setupLoadMoreObserver()` only ran on mount / tab-switch. At those moments
// `loadHistoryMessages` (async: dynamic import + two network round-trips) has
// NOT resolved yet, so `hasMoreMessages` is still its initial `false` → the
// sentinel element is absent (`loadMoreSentinel.value === null`) →
// `setupLoadMoreObserver` returns early and observes NOTHING. When history
// later lands and `hasMoreMessages` becomes true, the sentinel finally renders
// — but no watcher re-armed the observer, so scrolling to the top never fired
// the IntersectionObserver and older pages were never loaded.
//
// Watch the sentinel's render condition (per active tab) and (re-)setup or
// teardown the observer to match, after the DOM updates. V1 started its
// observer right after `loadConversation` rendered the first page
// (useChat.js:904-916); this watch restores that "observe once the sentinel
// exists" timing in the component-split V2.
watch(
  () => {
    const tab = activeTab.value;
    return tab ? `${tab.id}|${tab.hasMoreMessages}` : "";
  },
  () => {
    void nextTick(() => {
      setupLoadMoreObserver();
    });
  },
);

// Keep pinned to the bottom while streaming / new messages arrive, unless the
// user has scrolled up to read history (V1 _isAtBottom gate, useChat.js:440).
//
// V1 PARITY (useChat.js:1341-1343): V1 called scrollToBottom() on EVERY
// streamed delta chunk. V2 accumulates streamed text into
// `tab.streamingContent` (rAF-throttled flush, chatTabs.ts) and only commits a
// new `messages` entry when a round settles — so watching `messages.length`
// alone missed the live typewriter growth and the page did NOT follow the
// output. We therefore also watch `streamingContent.length` (and the last
// message's content length, which grows in place when a round flushes) so the
// view follows streaming output the way V1 did. The store-side rAF flush
// already throttles the churn (V1's per-chunk rAF equivalent), and the
// `userScrolledUp` 80px gate still pauses following while the user reads
// history and resumes when they return to the bottom.
watch(
  () => {
    const tab = activeTab.value;
    if (!tab) return "0|0|0|0";
    // Read messages via the raw-state-backed `activeMessages` (NOT the cached
    // `activeTab` getter object) so an in-place `messages` replacement is
    // observed here too — otherwise streaming scroll-follow misses the
    // sub-agent existing-round case (a running tool card appended to an
    // already-open message changes neither messages.length nor the last
    // message's content.length). Include the last message's tool-call count so
    // appending a card still advances the follow key.
    const msgs = activeMessages.value;
    const last = msgs.length > 0 ? msgs[msgs.length - 1]! : undefined;
    const lastLen = last ? last.content.length : 0;
    const lastTools = last?.toolCalls?.length ?? 0;
    return `${msgs.length}|${tab.streamingContent.length}|${lastLen}|${lastTools}`;
  },
  () => {
    if (!userScrolledUp.value) {
      void nextTick(scrollToBottomInstant);
    }
  },
);

onMounted(() => {
  window.addEventListener("keydown", lightbox.onKeydown);
  // Warm the model lists so assistant headers can resolve display names
  // (V1 msgModelName parity). Fire-and-forget; errors are swallowed inside.
  void ensureModelsLoaded();
  // Check whether any cloud model is configured so the welcome screen can
  // show the onboarding hint when none is (V2 enhancement). Uses refresh()
  // (not ensureChecked) so that navigating back from Settings after
  // configuring a cloud model immediately hides the hint without a full
  // page reload. Fire-and-forget; failures are non-fatal.
  void cloudModelStatus.refresh();
  void nextTick(() => {
    containerRef.value?.addEventListener("scroll", onContainerScroll, {
      passive: true,
    });
    // Restore the active tab's last reading position (falls back to bottom on
    // first visit / when nothing was recorded). This makes a remount — e.g.
    // when KeepAlive eventually evicts ChatView under a `:max` limit, or a
    // page reload — land where the user left off rather than always jumping
    // to the newest message.
    restoreScrollFor(store.activeTabId);
    setupLoadMoreObserver();
  });
});

// KeepAlive-aware lifecycle (KA-CHATVIEW-ONACT-1)
// -----------------------------------------------
// `AppMain.vue` wraps `<RouterView>` in `<KeepAlive>`, so /chat is cached on
// navigate-away. We need two extra hooks beyond the existing onMounted/
// onBeforeUnmount pair:
//
//  - onActivated: when the user navigates back to /chat with the same
//    activeTabId (which means `watch(activeTabId, …)` does NOT fire), the
//    DOM scrollTop usually survives KeepAlive's hide/show cycle, but
//    additional async content (Mermaid SVG, lazy image natural sizes) may
//    have rendered while hidden — re-running restoreScrollFor uses the
//    saved entry to land back exactly where the user was. Also makes the
//    restore behaviour robust against a future `<KeepAlive :max="N">` cap
//    that could LRU-evict and re-create the chat surface mid-session.
//    onActivated also fires right after the first onMounted on initial
//    mount; that second restore is idempotent (same saved entry → same
//    scrollTop, with `schedulePostRestoreCorrection` cancelling any
//    superseded rAF).
//
//  - onDeactivated: the existing `watch(activeTabId, …)` snapshot only
//    fires when the tab id changes. Navigating away from /chat to
//    /settings does NOT change activeTabId, so without this hook we'd lose
//    "where the user was reading on the active tab at the moment they
//    left." Save it explicitly here.
onActivated(() => {
  void nextTick(() => {
    restoreScrollFor(store.activeTabId);
  });
});

onDeactivated(() => {
  // Flush any pending rAF before snapshotting so the saved entry reflects
  // the most recent scroll state, not a stale half-frame.
  cancelScrollSave();
  saveScrollFor(store.activeTabId);
});

onBeforeUnmount(() => {
  // Persist the final reading position before tear-down so a remount restores
  // it (the scroll listener may not have fired for the very last position).
  cancelScrollSave();
  cancelPostRestoreCorrection();
  saveScrollFor(store.activeTabId);
  window.removeEventListener("keydown", lightbox.onKeydown);
  containerRef.value?.removeEventListener("scroll", onContainerScroll);
  teardownLoadMoreObserver();
});
</script>

<template>
  <!-- Non-scrolling pane wrapper (V1 `.chat-view` parity, index.html:401).
       The floating scroll-nav group is anchored to THIS box, not to the
       scrollable `.messages-container`. Placing the absolute float inside
       the scroller made `bottom`/`right` resolve against the scrolled
       content height, so the buttons rode to the bottom of the (tall)
       content and scrolled out of view once the conversation overflowed
       ("内容多了就看不到这两个按钮"). Mirrors V1, where `.scroll-nav-group`
       is a sibling of `.messages-container` under the non-scrolling
       `.chat-view` (index.html:894-916). -->
  <div class="chat-message-pane">
    <div
      ref="containerRef"
      class="messages-container"
      role="log"
      aria-live="polite"
      aria-relevant="additions text"
      data-testid="chat-message-list"
      @click="onContainerClick"
    >
    <!-- Welcome screen -->
    <div
      v-if="showWelcome"
      class="welcome-screen"
    >
      <!-- GoMaster (external one-click optimize) mode has its own self-contained
           intro (logo + features + steps + CTA), so it replaces the shared
           welcome header + generic chips entirely. -->
      <component :is="GomasterEmptyState" v-if="GomasterEmptyState && isGomasterMode" />
      <template v-else>
      <div class="welcome-icon">
        <!-- V2 brand mark — AI neural-network + NPU chip motif.
             viewBox 0 0 112 112; CSS in chat.css drives sizing, colors,
             and the floating + frame-flow animations. Brand colors:
             #7c6cff (violet) → #60a5fa (sky-blue) gradient. -->
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
          <!-- Background tile -->
          <rect
            class="welcome-logo-tile"
            x="4" y="4" width="104" height="104" rx="24"
          />
          <!-- Neural network connections (animated frame layer) -->
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
          <!-- Center NPU chip (pulse layer) -->
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
          <!-- Neural network nodes -->
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
      <!-- Mode-specific welcome screens: each replaces the generic
           welcome-title + subtitle + chips + cloud onboarding with a
           mode-tailored 3-step guide and CTA chips. Falls through to the
           generic welcome only when the active mode has no dedicated
           empty state (basic chat / ppt / translate / …).

           The five mode-specific screens (App Builder / GoMaster / Pro /
           Code / Model Builder) share the same visual family (see each
           component's <style>) so switching modes on an empty tab feels
           consistent, not like a genre change. -->
      <AppBuilderEmptyState
        v-if="isAppBuilderMode"
        @fill-prompt="emit('fill-prompt', $event)"
      />
      <ProEmptyState v-else-if="isProMode" />
      <CodeEmptyState v-else-if="isCodeMode" />
      <ModelBuilderEmptyState
        v-else-if="isModelBuildMode"
        @fill-prompt="emit('fill-prompt', $event)"
      />
      <ModelHubEmptyState
        v-else-if="isModelHubMode"
        @fill-prompt="emit('fill-prompt', $event)"
      />
      <template v-else>
        <!-- Generic welcome copy (basic chat + not-yet-mode-tailored
             modes): the app logo above is followed by a generic title +
             subtitle + starter chips. -->
        <div class="welcome-title">
          {{ t("chat.welcomeTitle") }}
        </div>
        <div class="welcome-subtitle">
          {{ t("chat.welcomeSubtitle") }}
        </div>
        <div class="welcome-chips">
          <div
            v-for="(chip, idx) in welcomeChips"
            :key="idx"
            class="welcome-chip"
            :title="t(chip.promptKey)"
            @click="emit('quick-prompt', t(chip.promptKey))"
          >
            <span class="welcome-chip-icon" v-html="chip.iconSvg"></span>
            <span>{{ t(chip.labelKey) }}</span>
          </div>
        </div>
        <!-- Cloud-model onboarding hint (V2 enhancement, edition-dual-form
             §6.4): shown only when no cloud model is configured. Non-blocking
             — the user can still chat with a local model. -->
        <CloudModelOnboarding v-if="showCloudOnboarding" />
        <!-- Cloud-model "set API key" prompt (internal-edition enhancement):
             shown when cloud models are pre-configured but a provider is
             missing its API key. Clicking the CTA opens an in-place dialog. -->
        <CloudModelApiKeyOnboarding
          v-if="showCloudApiKeyPrompt"
          @configure="openApiKeyFlow"
        />
      </template>
      </template>
    </div>

    <!-- Skeleton loading (V1 index.html:407-419): shown when switching to
         an existing conversation while history is being fetched. 3 skeleton
         cards with avatar circle + text lines, matching V1's loading UX. -->
    <div
      v-if="activeTab?.loadingHistory"
      class="messages-skeleton"
      aria-busy="true"
      :aria-label="t('chat.loadingHistory')"
    >
      <div
        v-for="n in 3"
        :key="n"
        class="skeleton-card"
      >
        <div class="skeleton-card-header">
          <div class="skeleton-card-avatar skeleton-circle skeleton" />
          <div class="skeleton-card-body">
            <div class="skeleton-line skeleton-line-medium skeleton" />
            <div class="skeleton-line skeleton-line-short skeleton" />
          </div>
        </div>
        <div class="skeleton-line skeleton-line-long skeleton" />
        <div class="skeleton-line skeleton-line-medium skeleton" />
      </div>
    </div>

    <!-- Messages -->
    <template v-if="activeTab && !showWelcome && !activeTab.loadingHistory">
      <!-- NOTE: ModeIntroCard used to render HERE (top of the message
           list, before the load-more sentinel). That placement failed the
           product intent — with `activeMessages.length > 0` the default
           scroll landed on the newest message and the card sat above ALL
           historical messages, invisible until the user scrolled to the
           very top (which nobody does to hunt for onboarding). The card
           has been moved to `ChatView.vue` as a sticky helper strip
           above the composer, so it lives in the user's natural viewport
           regardless of conversation length. See `ChatView.vue` for the
           new mount. -->
      <!-- D3 load-more sentinel (V1 useChat.js:904-916 _startLoadMoreObserver):
           an IntersectionObserver watches this element; when it scrolls
           into view AND the tab still has older messages, the store loads
           the previous page and prepends it (scroll position preserved). -->
      <div
        v-if="activeTab.hasMoreMessages"
        ref="loadMoreSentinel"
        class="messages-load-more-sentinel"
        data-testid="load-more-sentinel"
        aria-hidden="true"
      >
        <span
          v-if="activeTab.loadingMore"
          class="messages-load-more-spinner"
        />
      </div>
      <div
        v-for="msg in activeMessages"
        :key="msg.id"
        :data-message-id="msg.id"
        class="message-row"
        :class="[
          msg.role === 'user' ? 'user' : 'ai',
          {
            'msg-pending-injection':
              (msg.meta as Record<string, unknown> | undefined)?.['injected'] === true &&
              (msg.meta as Record<string, unknown> | undefined)?.['pending'] === true,
          },
        ]"
      >
        <div
          class="message-avatar"
          :class="{ 'message-avatar--speaker': msg.role !== 'user' && hasSender(msg) }"
          :style="msg.role !== 'user' && hasSender(msg) ? senderAvatarStyle(msg.senderColor) : undefined"
          :data-testid="msg.role !== 'user' && hasSender(msg) ? 'discussion-speaker-avatar' : undefined"
        >
          <!-- Multi-Agent discussion speaker: generated initial avatar
               (block-5). Ordinary bubbles keep the V1 user / AI SVG. -->
          <span v-if="msg.role !== 'user' && hasSender(msg)">{{ senderInitial(msg.senderName) }}</span>
          <!-- eslint-disable-next-line vue/no-v-html -->
          <span v-else-if="msg.role === 'user'" v-html="USER_AVATAR_SVG"></span>
          <!-- eslint-disable-next-line vue/no-v-html -->
          <span v-else v-html="assistantAvatarSvg(msg)"></span>
        </div>
        <div class="message-content-wrap">
          <div class="message-meta">
            <!-- A2 role label: "You" / model name (V1 index.html:516); in
                 discussion mode the participant's display name (block-5). -->
            <span
              :style="msg.role !== 'user' && hasSender(msg) && msg.senderColor ? { color: msg.senderColor } : undefined"
              :data-testid="msg.role !== 'user' && hasSender(msg) ? 'discussion-speaker-name' : undefined"
            >{{ bubbleLabel(msg) }}</span>
            <!-- Discussion-mode model badge (V2 enhancement 2026-06-21):
                 "· claude-4-6-sonnet" next to the speaker name so the user
                 knows which model each role is using. Hidden for single-
                 agent chat (the model is already in the V1 role label). -->
            <span
              v-if="bubbleModelBadge(msg) !== ''"
              class="discussion-speaker-model"
              data-testid="discussion-speaker-model"
            >· {{ bubbleModelBadge(msg) }}</span>
            <!-- A3 timestamp (V1 index.html:517 + utils.js:79-82) -->
            <span
              v-if="formatTime(msg.createdAt) !== ''"
              class="message-time"
            >{{ formatTime(msg.createdAt) }}</span>
            <!-- Per-message action group (V1 index.html:520-553).
                 Wrapped in `.message-actions` so the global hover rule
                 (`.message-row:hover .message-actions { opacity: 1 }`)
                 reveals these buttons only on row hover, matching V1.
                 Buttons reuse the global `.btn .btn-icon` 36×36 sizing
                 instead of the previous tiny custom 14px icon — the
                 dead `.message-action-btn` / `.message-copy-icon`
                 rules in chat.css have been removed. -->
            <div class="message-actions">
              <!-- D3 Prompt snapshot button (V1 index.html:521-528).
                   Shown for assistant messages that carry a request_id
                   (stored in msg.meta.request_id by the transport when
                   the backend end-frame includes it) AND when the user
                   has enabled "Show Prompt in UI" in Service Config.
                   Clicking opens the inline prompt snapshot modal. -->
              <button
                v-if="msg.role === 'assistant' && (msg.meta as Record<string, unknown> | undefined)?.['request_id'] && showPromptInUi"
                type="button"
                class="prompt-snapshot-btn"
                data-testid="message-prompt-snapshot-btn"
                :title="t('index.viewFullPrompt')"
                @click="openPromptSnapshot(String((msg.meta as Record<string, unknown>)['request_id']))"
              >
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                ><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line
                  x1="16"
                  y1="13"
                  x2="8"
                  y2="13"
                /><line
                  x1="16"
                  y1="17"
                  x2="8"
                  y2="17"
                /><polyline points="10 9 9 9 8 9" /></svg>
              </button>
              <!-- A4 copy: ⧉ icon button (V1 index.html:520-545).
                   Transient ✓ feedback retained from V2. -->
              <button
                type="button"
                class="btn btn-icon"
                data-testid="message-copy-btn"
                :title="copiedId === msg.id ? t('common.copied') : t('common.copy')"
                @click="copyMessage(msg.id, msg.content)"
              >
                {{ copiedId === msg.id ? "✓" : "⧉" }}
              </button>
            </div>
          </div>
          <!-- Message body — tool cards inside use ui.toolCardsCollapsed
               (bulk-collapse from the topbar) for their own collapse
               state; the per-message ▼ toggle that used to hide the
               entire body was retired 2026-07-20 (see stores/ui.ts
               header comment). -->
          <!-- Sub-agent blocks (V1 index.html:629-669) — rendered BEFORE the
                 parent agent's text bubble + the mainAgentSummaryHeader
                 separator, so the user sees each sub-agent's progress + tools
                 first and the parent summary below. Persisted on the message
                 via ``subAgentBlocks`` (V1 useChat.js:62 / 2404 parity).

                 Architecture note (post-INDEPENDENT-SUBAGENT-MESSAGE fix,
                 2026-07-02): sub-agent blocks now live on THEIR OWN dedicated
                 assistant message (opened by ``handleSubagentStart`` via the
                 independent ``roundSubAgentMessageIds[ri]`` map — a separate
                 track from the main-agent ``roundMessageIds[ri]``). So a
                 message that has ``subAgentBlocks`` NEVER also carries the
                 parent round's ``content`` / ``toolCalls`` (those live on the
                 sibling main-agent round message that precedes this one in
                 ``tab.messages``). The visual order — parent text → parent
                 tool cards (incl. the ``agent`` dispatch card) → sub-agent
                 blocks — is therefore driven by the natural ``messages``
                 array order (parent-round message first, sub-agent message
                 second), NOT by the template's field order within one
                 message. Keeping ``subAgentBlocks`` above the text bubble
                 here is safe (the field is empty on parent-round messages)
                 and preserves the original ``mainAgentSummaryHeader``
                 separator semantics for legacy single-buffer captures
                 (``activeSubAgentMessageId`` fallback path when
                 ``round_index`` is absent — old backend / non-agentic). -->
            <template
              v-if="msg.role === 'assistant' && msg.subAgentBlocks && msg.subAgentBlocks.length > 0"
            >
              <SubAgentBlock
                v-for="block in msg.subAgentBlocks"
                :key="block.index"
                :block="block"
                @open-subagent="handleOpenSubAgent"
                @stop-subagent="handleStopSubAgent"
                @cancel-tool="onCancelTool"
              />
              <!-- ``subagent-summary-separator`` (↩ "主 Agent 总结") — only makes
                   sense when the parent agent actually said something AFTER the
                   sub-agents finished. For the INDEPENDENT ``subagent_summary``
                   message shape (SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG,
                   2026-07-02) that means checking whether a subsequent assistant
                   text bubble exists in ``activeTab.messages``. For the legacy
                   inline shape (blocks folded onto a message that ALSO carries
                   summary text or tool cards), the separator historically
                   showed unconditionally — we preserve that by keeping it on
                   whenever the CURRENT message itself has a visible text bubble
                   (``content`` non-empty && not the ``[tool_calls]`` sentinel)
                   OR when a following main-agent summary message follows. -->
              <div
                v-if="(msg.content && msg.content !== '[tool_calls]') || hasFollowingMainSummary(msg)"
                class="subagent-summary-separator"
                data-testid="subagent-summary-separator"
              >
                <span>↩ {{ t("index.mainAgentSummaryHeader") }}</span>
              </div>
            </template>
            <!-- Reasoning ("思考过程") — collapsible thinking block rendered
                 ABOVE the answer bubble (separate from `content`). Shown when
                 this assistant turn accumulated `reasoning` frames (cloud
                 reasoning models' delta.reasoning_content + the internal
                 query-service adapter's filtered thinking). Absent ⇒ ordinary
                 turn unchanged. -->
            <ReasoningBlock
              v-if="msg.role === 'assistant' && typeof msg.reasoning === 'string' && msg.reasoning !== ''"
              :text="msg.reasoning"
            />
            <!-- Slash-command reply (V1 index.html:682-683, is_command_reply):
                 /status, /help, etc. render verbatim — escaped plain text +
                 white-space:pre-wrap — preserving emoji, indentation and
                 newlines. Such replies must NOT go through markdown. Vue text
                 interpolation escapes the content (escapeHtml equivalent),
                 keeping it safe without v-html. -->
            <div
              v-if="msg.role === 'assistant' && msg.isCommandReply && msg.content !== '[tool_calls]'"
              class="message-bubble message-command-reply"
              data-testid="message-command-reply"
            >
              {{ commandReplyText(msg) }}
            </div>
            <!-- eslint-disable vue/no-v-html -->
            <!-- Assistant text bubble — rendered ABOVE the tool cards (below)
                 so a tool-call round shows its lead-in text (the "thinking" the
                 model said before invoking tools) FIRST, then the tool cards —
                 V1 parity (useChat.js:2460-2470: lead-in + tool_calls on the
                 same assistant message, text above cards; matches the live
                 streaming layout where the lead-in bubble precedes the cards).
                 Independent of the tool-cards block (not v-else) so BOTH show.
                 Previously V2 made these mutually exclusive via v-else-if, so a
                 tool-call message's lead-in disappeared the instant the tool
                 started — the long-standing "工具调用前的文本消失" bug.
                 Skip the "[tool_calls]" sentinel (no real text) and empty
                 content (pure tool-call round). Aborted turns get the
                 chat.interruptedMark appended (V1 useChat.js:2696). -->
            <div
              v-else-if="msg.role === 'assistant' && msg.content !== '[tool_calls]' && msg.content !== ''"
              class="message-bubble markdown-body"
              :class="{ 'message-bubble--interrupted': isInterruptedMessage(msg) }"
              :data-testid="isInterruptedMessage(msg) ? 'message-interrupted' : undefined"
              v-html="renderCommittedMarkdown(msg)"
            />
            <!-- eslint-enable vue/no-v-html -->
            <div
              v-else-if="msg.content !== '[tool_calls]' && textWithoutImages(msg.content) !== ''"
              class="message-bubble"
            >
              {{ textWithoutImages(msg.content) }}
            </div>
            <!-- Pending mid-turn injection STATUS strip (V2 enhancement):
                 three breathing dots + a "待注入" label, shown only while the
                 bubble is still pending (`meta.injected + meta.pending`). The
                 moment the backend's `injected_message` frame commits it the
                 `pending` flag clears and this strip (with the action buttons
                 below) disappears, so the bubble reads as an ordinary user
                 message. Pure CSS animation (no JS), theme-token colours. -->
            <div
              v-if="isPendingInjection(msg)"
              class="inject-pending-status"
              data-testid="inject-pending-status"
              :aria-label="t('chat.injectPending', 'Pending injection')"
            >
              <span
                class="inject-pending-dots"
                aria-hidden="true"
              >
                <span class="inject-pending-dot" />
                <span class="inject-pending-dot" />
                <span class="inject-pending-dot" />
              </span>
              <span class="inject-pending-label">{{
                t('chat.injectPending', 'Pending injection')
              }}</span>
            </div>
            <!-- Pending mid-turn injection actions (V2 enhancement). Shown ONLY
                 while the bubble is still pending (`meta.injected + meta.pending`);
                 the moment the backend's `injected_message` frame commits it the
                 `pending` flag is cleared and these buttons disappear (so a
                 cancel-after-committed click cannot occur). Icons/semantics mirror
                 MessageQueuePanel (⧉ copy / ✎ edit / ✕ cancel): Cancel withdraws
                 the injection from the run + removes the bubble; Edit does the same
                 then refills the composer draft; Copy copies the text (bubble
                 stays). Cancel/Edit are emitted to ChatView (which owns the control
                 channel + composer ref); Copy is handled here. -->
            <div
              v-if="isPendingInjection(msg)"
              class="inject-pending-actions"
              data-testid="inject-pending-actions"
            >
              <button
                type="button"
                class="queue-panel-item-act"
                data-testid="inject-copy-btn"
                :title="t('index.copyShort', 'Copy')"
                :aria-label="t('index.copyShort', 'Copy')"
                @click="() => { void copyInjection(msg.content); }"
              >
                ⧉
              </button>
              <button
                type="button"
                class="queue-panel-item-act"
                data-testid="inject-edit-btn"
                :title="t('index.editShort', 'Edit')"
                :aria-label="t('index.editShort', 'Edit')"
                @click="onInjectEdit(msg)"
              >
                ✎
              </button>
              <button
                type="button"
                class="queue-panel-item-del"
                data-testid="inject-cancel-btn"
                :title="t('chat.injectCancelTitle', 'Cancel injection')"
                :aria-label="t('chat.injectCancelTitle', 'Cancel injection')"
                @click="onInjectCancel(msg)"
              >
                ✕
              </button>
            </div>
            <!-- Tool call/result cards (V1 index.html:455-494). Hidden when
                 the AppHeader "Tool Calls" toggle is off (ui.showToolMessages),
                 EXCEPT when this message carries the ACTIVE (awaiting-answer)
                 question card: that must stay visible regardless of the toggle
                 so the user can answer and unblock the agentic loop. In that
                 toggle-off case `active-question-only` tells ToolCallList to
                 render ONLY the active question card (other tool cards stay
                 hidden, so the toggle still works for everything else).
                 Rendered AFTER the assistant text bubble above so the round's
                 lead-in text appears above its tool cards (V1 layout). -->
            <div
              v-if="(ui.showToolMessages || hasActiveQuestion(msg)) && msg.toolCalls && msg.toolCalls.length > 0"
              class="message-tool-calls"
              data-testid="message-tool-calls"
            >
              <ToolCallList
                :calls="toToolCallViews(msg)"
                :request-id="messageRequestId(msg)"
                :show-prompt-button="showPromptInUi"
                :tab-id="activeTab.id"
                :active-question-only="!ui.showToolMessages && hasActiveQuestion(msg)"
                :active-question-frame-id="activeQuestionFrameId"
                @open-prompt-snapshot="openPromptSnapshot"
                @cancel-tool="onCancelTool"
              />
            </div>
            <!-- Image previews + lightbox trigger (V1 index.html:690-726).
                 User messages only: the composer embeds uploaded images
                 as markdown links in the prompt text but the user bubble
                 renders as plain text, so the thumbnails are surfaced
                 here. Assistant bubbles already render their own inline
                 markdown images via `renderMarkdown`. -->
            <div
              v-if="msg.role === 'user' && extractImages(msg.content).length > 0"
              class="msg-image-preview"
              data-testid="msg-image-preview"
            >
              <template
                v-for="(img, idx) in extractImages(msg.content)"
                :key="img.url + ':' + idx"
              >
                <img
                  :src="img.url"
                  :alt="img.alt || t('chat.image', 'image')"
                  class="msg-image-thumb"
                  data-testid="msg-image-thumb"
                  @click="openLightbox(img.url)"
                  @error="onImageError"
                />
                <div
                  class="msg-image-missing"
                  style="display:none"
                  :title="img.url"
                >
                  🖼 image unavailable
                </div>
              </template>
            </div>
            <!-- C1 metrics row (V1 index.html:738-746). Each segment is
                 v-if-guarded; tps segments stay hidden until the transport
                 fills perf.input_tps / output_tps (manifest C1).
                 V1 真值 chat.css:1495-1502 — `.token-badge` 是无边框纯
                 文字（display:flex / gap / font-size / color），两层
                 span 结构（icon + 一组分段 span）；V2 早期加的
                 `.token-metrics { white-space: normal }` 包装层是死规
                 则，已连同 V2 自创 pill 样式一并移除。 -->
            <div
              v-if="hasMetrics(msg.usage, msg.perf)"
              class="token-badge"
              data-testid="token-badge"
            >
              <span class="token-icon">🔢</span>
              <span>
                <template
                  v-for="m in [buildMetrics(msg.usage, msg.perf)]"
                  :key="m === null ? 'no-metrics' : 'metrics'"
                >
                  <template v-if="m !== null">
                    <span v-if="m.inputTokens !== null">[ I ] {{ m.inputTokens }} tokens</span>
                    <span v-if="m.inputTokens !== null && m.inputTps !== null"> @ {{ m.inputTps }} tok/sec</span>
                    <span v-if="m.outputTokens !== null">  •  [ O ] {{ m.outputTokens }} tokens</span>
                    <span v-if="m.outputTokens !== null && m.outputTps !== null"> @ {{ m.outputTps }} tok/sec</span>
                    <span v-if="m.ttftMs !== null">  •  {{ m.ttftMs }}ms first token latency</span>
                    <span v-if="m.totalSeconds !== null">  •  total: {{ m.totalSeconds }}s</span>
                    <span v-if="m.toolRounds !== null">  •  🔧{{ m.toolRounds }} tool rounds</span>
                  </template>
                </template>
              </span>
            </div>
            <!-- Send-failure banner + retry (V1 index.html:685-689).
                 Shown on a user message whose turn failed — BUT only when the
                 failure is the last thing in the conversation (no later
                 assistant tool rounds). When the turn ran tool rounds and THEN
                 errored (e.g. a stall while generating a `write` tool's long
                 args), the error belongs at the bottom (error location), not
                 pinned to the far-up originating prompt — so we suppress this
                 top banner and let the bottom tab-level banner carry it. -->
            <div
              v-if="msg.role === 'user' && msg.sendError && !failedTurnHasLaterActivity"
              class="msg-error-banner"
              role="alert"
              data-testid="msg-error-banner"
            >
              <span>⚠ {{ localizedSendError(msg.sendError, msg.sendErrorCode) }}</span>
              <!-- Registry-driven action buttons (single rendering path). The
                   spec's primary/secondary actions come from the declarative
                   registry keyed by the error code; behaviour is dispatched to
                   the executor by action id. Retains the former apiKey /
                   unsupported-param buttons via the registry (no parallel
                   special-casing). -->
              <template
                v-for="action in [
                  resolveChatErrorSpec(msg.sendErrorCode).primaryAction,
                  resolveChatErrorSpec(msg.sendErrorCode).secondaryAction,
                ]"
                :key="action ? action.id : 'none'"
              >
                <button
                  v-if="action"
                  type="button"
                  class="msg-retry-btn"
                  :data-testid="`msg-error-action-${action.id}`"
                  @click="onChatErrorAction(action.id, msg.id, msg.content)"
                >
                  {{ chatErrorActionIcon(action) }} {{ chatErrorActionLabel(action) }}
                </button>
              </template>
              <button
                type="button"
                class="msg-retry-btn"
                data-testid="msg-retry-btn"
                @click="onRetry(msg.id, msg.content)"
              >
                ↻ {{ t('chat.retry') }}
              </button>
              <button
                type="button"
                class="msg-retry-btn"
                data-testid="msg-copy-diagnostics-btn"
                @click="onChatErrorAction('copy_diagnostics', msg.id, msg.content)"
              >
                ⧉ {{ t('chatErrors.actions.copyDiagnostics') }}
              </button>
            </div>
            <!-- A4: copy lives in the meta row as a pure ⧉ icon (V1
                 index.html:544). The previous bottom "⧉ Copy" text button
                 was removed to match V1 (icon-only, in the meta row). -->
        </div>
      </div>

      <!-- Context-compaction banner (V2 enhancement). Surfaced ONLY while the
           backend reports a slow (≈2s+) context compaction is delaying the
           model call (the `compaction_progress` frame flips `tab.compacting`).
           Passive inline status (no native dialog — AGENTS.md §3.9.2);
           `aria-live=polite` announces it without interrupting reading. -->
      <div
        v-if="activeTab.compacting === true"
        class="compaction-banner"
        role="status"
        aria-live="polite"
        data-testid="compaction-banner"
      >
        <span class="compaction-banner__spinner" aria-hidden="true"></span>
        {{ t("chat.compactingContext") }}
      </div>

      <!-- Network retry banner (V1 useChat.js:2270-2292 parity).
           Surfaced while the SSE transport is between attempts after a
           transient network drop. `aria-live=polite` so screen readers
           announce the recovery state without interrupting reading. -->
      <div
        v-if="activeTab.networkRetry !== null"
        class="network-retry-banner"
        role="status"
        aria-live="polite"
        data-testid="network-retry-banner"
      >
        <span class="network-retry-text">{{ networkRetryText }}</span>
        <button
          type="button"
          class="network-retry-now-btn"
          data-testid="network-retry-now"
          @click="onRetryNowClick"
        >
          {{ t("chat.retryNow") }}
        </button>
      </div>

      <!-- Streaming bubble (single-track model) — renders ONLY the trailing
           text the model is producing AFTER its last tool round (the live
           "summary" stream) plus a typing indicator. Per-round lead-in text +
           tool cards + sub-agent blocks are now real `messages` entries
           (pushed live by the frame handlers, marked meta.streaming), so they
           render through the committed-message loop ABOVE in true arrival
           order — each round's thinking text sits directly above ITS round's
           tool cards (V1 useChat.js:2455-2520). The old bottom-of-stream
           streamingToolCalls / streamingSubAgentBlocks blocks are gone. -->
      <div
        v-if="activeTab.streamingContent !== ''"
        class="message-row ai"
        data-testid="chat-streaming-bubble"
      >
        <div
          class="message-avatar"
          :class="{ 'message-avatar--speaker': activeTab.streamingSenderId !== null }"
          :style="activeTab.streamingSenderId !== null ? senderAvatarStyle(activeTab.streamingSenderColor ?? undefined) : undefined"
        >
          <span v-if="activeTab.streamingSenderId !== null">{{ senderInitial(activeTab.streamingSenderName ?? undefined) }}</span>
          <!-- eslint-disable-next-line vue/no-v-html -->
          <span v-else v-html="streamingAvatarSvg"></span>
        </div>
        <div class="message-content-wrap">
          <div class="message-meta">
            <span
              :style="activeTab.streamingSenderId !== null && activeTab.streamingSenderColor ? { color: activeTab.streamingSenderColor } : undefined"
            >{{ streamingLabel }}</span>
          </div>
          <!-- eslint-disable vue/no-v-html -->
          <div
            class="message-bubble markdown-body"
            data-mermaid-skip
            v-html="renderMarkdown(activeTab.streamingContent, { markedOptions: { breaks: true }, fastCodeBlocks: true })"
          />
          <!-- eslint-enable vue/no-v-html -->
        </div>
      </div>

      <!-- Typing indicator while streaming with no trailing text yet (V1
           index.html:831-836). Shown whenever the turn is streaming and the
           live trailing-text bubble is empty — the in-flight round's tool
           cards (in `messages`) carry their own spinners, but the parent
           agent's "thinking" still needs a typing cue until its text starts. -->
      <div
        v-if="
          activeTab.status === 'streaming' &&
            activeTab.streamingContent === ''
        "
        class="message-row ai"
      >
        <div
          class="message-avatar"
          :class="{ 'message-avatar--speaker': activeTab.streamingSenderId !== null }"
          :style="activeTab.streamingSenderId !== null ? senderAvatarStyle(activeTab.streamingSenderColor ?? undefined) : undefined"
        >
          <span v-if="activeTab.streamingSenderId !== null">{{ senderInitial(activeTab.streamingSenderName ?? undefined) }}</span>
          <!-- eslint-disable-next-line vue/no-v-html -->
          <span v-else v-html="streamingAvatarSvg"></span>
        </div>
        <div class="message-content-wrap">
          <div class="typing-indicator">
            <span class="typing-dot"></span>
            <span class="typing-dot"></span>
            <span class="typing-dot"></span>
          </div>
        </div>
      </div>

      <!-- Error banner -->
      <div
        v-if="activeTab.status === 'error' && activeTab.lastError !== null && !tabErrorShownOnMessage"
        class="message-row ai"
        role="alert"
        data-testid="chat-error-banner"
      >
        <div
          class="message-avatar"
          style="background: var(--error, #dc2626); color: #fff;"
        >
          !
        </div>
        <div class="message-content-wrap">
          <div
            class="message-bubble"
            style="border-color: var(--error, #dc2626); color: var(--error, #dc2626);"
          >
            <!-- Registry-driven terminal error (single rendering path). The
                 declarative spec (keyed by `lastError.code`) supplies the
                 localized title/message + up to two action buttons; behaviour
                 is dispatched to the executor by action id. The former
                 apiKey / unsupported-param special cases are folded into the
                 registry — there is no parallel mechanism here. -->
            <template v-if="failedErrorSpec !== null">
              <strong v-if="failedErrorSpec.titleKey">{{ t(failedErrorSpec.titleKey) }}</strong>
              <span>{{ localizedSendError(activeTab.lastError.message, activeTab.lastError.code) }}</span>
              <div
                v-if="failedErrorSpec.primaryAction || failedErrorSpec.secondaryAction || failedErrorSpec.showRetry || failedUserMessage !== null"
                class="tab-error-retry-row"
              >
                <template
                  v-for="action in [failedErrorSpec.primaryAction, failedErrorSpec.secondaryAction]"
                  :key="action ? action.id : 'none'"
                >
                  <button
                    v-if="action"
                    type="button"
                    class="msg-retry-btn"
                    :data-testid="`tab-error-action-${action.id}`"
                    @click="onChatErrorAction(action.id, failedUserMessage ? failedUserMessage.id : null, failedUserMessage ? failedUserMessage.content : '')"
                  >
                    {{ chatErrorActionIcon(action) }} {{ chatErrorActionLabel(action) }}
                  </button>
                </template>
                <!-- Retry: shown when the spec opts in OR when there is a
                     failed user message to re-run (a turn that errored after
                     tool rounds). Same `onRetry` path as the per-message
                     banner (identical behaviour regardless of banner). -->
                <button
                  v-if="(failedErrorSpec.showRetry || failedUserMessage !== null) && failedUserMessage !== null"
                  type="button"
                  class="msg-retry-btn"
                  data-testid="tab-error-retry-btn"
                  @click="onRetry(failedUserMessage.id, failedUserMessage.content)"
                >
                  ↻ {{ t('chat.retry') }}
                </button>
                <button
                  type="button"
                  class="msg-retry-btn"
                  data-testid="tab-error-copy-diagnostics-btn"
                  @click="onChatErrorAction('copy_diagnostics', failedUserMessage ? failedUserMessage.id : null, failedUserMessage ? failedUserMessage.content : '')"
                >
                  ⧉ {{ t('chatErrors.actions.copyDiagnostics') }}
                </button>
              </div>
            </template>
            <template v-else>
              <strong>{{ activeTab.lastError.code }}</strong>
              <span> — {{ activeTab.lastError.message }}</span>
              <div
                v-if="failedUserMessage !== null"
                class="tab-error-retry-row"
              >
                <button
                  type="button"
                  class="msg-retry-btn"
                  data-testid="tab-error-retry-btn"
                  @click="onRetry(failedUserMessage.id, failedUserMessage.content)"
                >
                  ↻ {{ t('chat.retry') }}
                </button>
              </div>
            </template>
          </div>
        </div>
      </div>
    </template>
    </div>
    <!-- /.messages-container (scroll container ends here) -->

    <!-- D1 quick-jump scroll nav (V1 index.html:894-916). Shown when the
         active tab has at least one message. ↓ highlights via
         scroll-nav-btn--active while the user is reading history.
         Sibling of `.messages-container` (NOT inside it) so it anchors to
         the non-scrolling `.chat-message-pane` and stays pinned regardless
         of content height (V1 parity). -->
    <transition name="scroll-btn-fade">
      <div
        v-if="hasMessages"
        class="scroll-nav-group"
        data-testid="scroll-nav-group"
      >
        <button
          type="button"
          class="scroll-nav-btn"
          :title="t('chat.scrollTop')"
          :aria-label="t('chat.scrollTop')"
          data-testid="scroll-top-btn"
          @click="scrollToTop"
        >
          ↑
        </button>
        <button
          type="button"
          class="scroll-nav-btn"
          :class="{ 'scroll-nav-btn--active': userScrolledUp }"
          :title="t('chat.scrollBottom')"
          :aria-label="t('chat.scrollBottom')"
          data-testid="scroll-bottom-btn"
          @click="scrollToBottom"
        >
          ↓
        </button>
      </div>
    </transition>

    <!-- Floating Stop button moved to ChatView so it anchors to the
         non-scrolling `.chat-view` (was anchored to the scrollable
         `.messages-container`, which made it scroll with the message list
         and visually disappear when the user scrolled up — the reported
         "时有时无 / 位置不固定" symptom). ChatView owns the stream
         lifecycle anyway (`onCancel` route), so co-locating the float
         with the cancel handler also tightens the wiring. -->

    <!-- Image lightbox overlay (V1 index.html:137-148 + app.js:168-213).
         Rendered outside the message loop so it covers the viewport. Wheel
         zooms, drag pans, dblclick resets, click-overlay / Esc closes. -->
    <div
      v-if="lightbox.isOpen.value"
      class="lightbox-overlay"
      data-testid="image-lightbox"
      role="dialog"
      aria-modal="true"
      @click="lightbox.close"
      @wheel.prevent="lightbox.onWheel"
    >
      <img
        :src="lightbox.src.value ?? ''"
        class="lightbox-image"
        :alt="t('chat.imagePreview', 'image preview')"
        :style="lightbox.imageStyle.value"
        @click.stop
        @mousedown.prevent="lightbox.onDragStart"
        @dblclick="lightbox.reset"
      />
      <button
        type="button"
        class="lightbox-close"
        data-testid="lightbox-close"
        :aria-label="t('common.close')"
        :title="t('common.close')"
        @click.stop="lightbox.close"
      >
        ✕
      </button>
      <div class="lightbox-hint">
        {{ t("chat.lightboxHint") }}
      </div>
    </div>
    <!-- Prompt Snapshot Modal (V1 useForgeConfig.js:20-127 + index.html:8403-12700).
         Shown when the user clicks the 📋 prompt-snapshot button on an
         assistant message. State + behaviour live in `usePromptSnapshot`;
         markup lives in `PromptSnapshotPanel.vue` (F4 cohesion split).
         §3.9: uses custom overlay, NOT window.confirm/alert. -->
    <PromptSnapshotPanel :api="promptSnapshot" />
    <!-- In-place "set cloud provider API key" dialog (internal-edition
         enhancement). Opened from CloudModelApiKeyOnboarding's CTA; saving
         PUTs the provider config with the real api_key. Teleports to body. -->
    <SetApiKeyDialog
      v-model:visible="apiKeyDialogVisible"
      :provider-id="cloudModelStatus.providerNeedingKey.value ?? ''"
      :loading="apiKeySaving"
      @save="onSaveApiKey"
      @cancel="apiKeyDialogVisible = false"
    />
  </div>
</template>

<style scoped>
/* All styling is handled by global chat.css classes.
   Only add scoped behavior here if needed. */

/* Retry row inside the bottom (tab-level) error banner. Reuses the global
   .msg-retry-btn button style; just adds spacing above it. */
.tab-error-retry-row {
  margin-top: 8px;
}

/* Network retry banner (V1 useChat.js:2270-2292 parity).
   Surfaced while the SSE transport recovers between attempts after a
   transient network drop. Styled like an inline system notice so it
   does not steal focus from the message stream. */
.network-retry-banner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 8px 16px;
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--bg-warning-soft, rgba(255, 193, 7, 0.08));
  border: 1px solid var(--border-warning, rgba(255, 193, 7, 0.4));
  color: var(--text-warning, #b07a00);
  font-size: 13px;
  line-height: 1.4;
  text-align: center;
}

.network-retry-now-btn {
  flex: none;
  padding: 3px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-warning, rgba(255, 193, 7, 0.6));
  background: transparent;
  color: var(--text-warning, #b07a00);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition:
    background 0.15s ease,
    color 0.15s ease;
}

.network-retry-now-btn:hover {
  background: var(--border-warning, rgba(255, 193, 7, 0.4));
  color: var(--text-warning-strong, #7a5400);
}

.network-retry-now-btn:active {
  transform: translateY(1px);
}

/* Context-compaction status banner (V2 enhancement). A passive, theme-aware
   inline notice shown while a slow context compaction delays the model call.
   Uses the shared accent token so it reads correctly in both light + dark. */
.compaction-banner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin: 8px 16px;
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--accent-soft, rgba(99, 102, 241, 0.08));
  border: 1px solid var(--accent, rgba(99, 102, 241, 0.4));
  color: var(--text-secondary, #555);
  font-size: 13px;
  line-height: 1.4;
}
.compaction-banner__spinner {
  width: 12px;
  height: 12px;
  border: 2px solid var(--accent, rgba(99, 102, 241, 0.6));
  border-top-color: transparent;
  border-radius: 50%;
  animation: compaction-spin 0.8s linear infinite;
}
@keyframes compaction-spin {
  to {
    transform: rotate(360deg);
  }
}

/* Sub-agent → main-agent summary separator (V1 chat.css:1797-1812).
   Lives between the last <SubAgentBlock> and the parent agent's text
   bubble, drawing a horizontal line on each side of the localized
   "main agent summary" label.  Kept here (instead of inside
   SubAgentBlock.vue) because the separator is a sibling of the
   blocks, not a child — it visually separates the *blocks region*
   from what comes after. */
.subagent-summary-separator {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin: var(--space-3) 0 6px;
  color: var(--text-secondary);
  font-size: 0.85em;
  font-weight: 600;
}
.subagent-summary-separator::before,
.subagent-summary-separator::after {
  content: "";
  flex: 1;
  height: 1px;
  background: var(--border);
}
</style>
