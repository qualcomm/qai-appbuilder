<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModeFrameModelHub — chat-input sub-toolbar for `model-hub` mode.
 *
 * Model Hub is a first-class toolbar mode (upgraded from the legacy
 * `aihub-model-run` skill): download pre-compiled models from Qualcomm
 * AI Hub and export them to App Builder. It is a peer of the Model Builder
 * / App Builder sub-toolbars and reuses the SAME toolbar infrastructure
 * (the global `rit-*` classes + the `exit` emit contract consumed by
 * ChatComposer's mode-frame switch) rather than introducing a bespoke
 * layout.
 *
 * Scope (initial): the backend feature prompt drives the AI Hub
 * search/download conversation, so this frame does NOT need to emit
 * per-mode `tool_params` (see useChatTransport `deriveToolPayload`
 * model-hub branch → params=null). It surfaces:
 *   1. an "AI Hub model" entry the user can type a model id / name into,
 *      kept as a toolbar badge the user references in their chat message
 *      (no backend round-trip yet — the Agent takes it from there);
 *   2. an "Export to App Builder" hint button that opens the export
 *      submenu once a downloaded model workspace exists.
 *
 * The AI Hub selection is kept as local frame state (not persisted to
 * `toolParams`) because the transport intentionally sends `tool_params=null`
 * for this mode today; extend `ToolParams` + the transport branch when a
 * concrete machine-readable selection (model id / target device) needs to
 * reach the backend.
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useAppBuilderStore } from "@/stores/appBuilder";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { useToast } from "@/composables/useToast";
import { useModeFrameTriggers } from "@/composables/useModeFrameTriggers";
import { usePromoteReadyDetection } from "@/composables/usePromoteReadyDetection";
import { extractAllModelWorkdirsFromMessages } from "@/utils/modelWorkdir";
import PromoteToAppBuilderCard from "@/components/app-builder/model-builder/PromoteToAppBuilderCard.vue";

const { t } = useI18n();
const store = useChatTabsStore();
const appBuilderStore = useAppBuilderStore();
const { config: forgeConfig } = useForgeConfig();
const toast = useToast();

const emit = defineEmits<{
  exit: [];
  "fill-prompt": [text: string];
}>();

// ── AI Hub model entry ───────────────────────────────────────────────────────
// Local-only selection: the entered model id / name is retained as a badge on
// the toolbar so the user (and the Agent, via their typed message) can
// reference it. It is NOT persisted to toolParams because the transport
// intentionally sends tool_params=null for model-hub (see useChatTransport
// deriveToolPayload model-hub branch). Extend ToolParams + that branch when a
// machine-readable selection (model id / target device) must reach the backend.
const modelEntryOpen = ref(false);
const aiHubModel = ref("");

function toggleModelEntry(): void {
  modelEntryOpen.value = !modelEntryOpen.value;
}

function confirmModelEntry(): void {
  aiHubModel.value = aiHubModel.value.trim();
  modelEntryOpen.value = false;
  // Guide the user to the next step: entering a model name alone does nothing
  // actionable, so prefill the composer with a ready-to-send download+infer
  // instruction naming that model. The user can tweak and press Enter — the
  // backend feature prompt (SKILL) drives the actual AI Hub download flow.
  if (aiHubModel.value !== "") {
    emit(
      "fill-prompt",
      t("modelHubFrame.pickModelFilledPrompt", { model: aiHubModel.value }),
    );
  }
}

// ── Promote to App Builder ───────────────────────────────────────────────────
// Model Hub and Model Builder produce the SAME app_pack, so Model Hub REUSES
// Model Builder's PromoteToAppBuilderCard verbatim (scan output/ variants →
// pick default precision → generate + import the pack). No bespoke export UI:
// the card's dry-run/commit contract (`/api/app-builder/import/*`) is
// source-neutral, and `scanBins` now recognises both `.bin` and `.dlc`, so a
// downloaded AI Hub `.dlc` workspace lists its variants here identically to a
// Model-Builder-converted `.bin`.
const promotePanelOpen = ref(false);

function togglePromotePanel(): void {
  promotePanelOpen.value = !promotePanelOpen.value;
}

// Configured workspace root (workspace.model_root), mirrors ModeFrameModelBuilder.
// ── Promote-ready detection (single source of truth) ────────────────────────
// Reuse the SAME detection the ChatView promote-ready notice uses: it pulls
// ALL `<root>\<model>` candidates from the conversation and picks the FIRST
// one that ACTUALLY has precision variants (.bin/.dlc) on disk. This fixes two
// things at once:
//   * the promote CARD gets the RIGHT workdir (a stray path like
//     C:\WoS_AI\fix_skill_docs3 with no variants no longer masks the real
//     C:\WoS_AI\resnet50 that the conversation referenced repeatedly);
//   * the toolbar READY-DOT lights only when a model dir with real variants
//     was found (State-Truth-First — not merely "some path string exists").
const promoteReady = usePromoteReadyDetection();

// Configured workspace root (workspace.model_root) for scanning the chat.
const workspaceModelRoot = computed<string>(() => {
  const cfg = forgeConfig.value as Record<string, unknown> | null;
  if (cfg === null || typeof cfg !== "object") return "";
  const ws = cfg["workspace"];
  if (ws !== null && typeof ws === "object") {
    const root = (ws as Record<string, unknown>)["model_root"];
    if (typeof root === "string" && root.trim() !== "") return root;
  }
  return "";
});

// Workspace fed to the promote card. Priority:
//   1. promoteReady.detectedWorkdir — a candidate VERIFIED to have variants
//      on disk (the right one, e.g. resnet50, not a stray fix_skill_docs3).
//   2. FALLBACK: the most-recently-referenced candidate from the conversation,
//      even if it has no variants yet. This is essential so the card can still
//      scan it and surface the "un-normalized AI Hub model — run Step 6.5"
//      guidance (needs_normalize). Without this fallback, a downloaded-but-not-
//      normalized model would give an empty detectedWorkdir → the card shows
//      bare "no workspace" and the normalize guidance never appears.
// The card itself decides what to show: variants (export) / needs-normalize
// (guide) / nothing — all via its own scanBins call on this workdir.
const sessionModelWorkdir = computed<string>(() => {
  const verified = promoteReady.detectedWorkdir.value;
  if (verified !== "") return verified;
  const candidates = extractAllModelWorkdirsFromMessages(
    store.activeTab?.messages,
    workspaceModelRoot.value || undefined,
  );
  return candidates[0] ?? "";
});

// Ready-dot on the promote button: ON only when a model dir with real
// precision variants was detected for the active conversation.
const promoteDotReady = computed<boolean>(
  () => promoteReady.detectedVariants.value.length > 0,
);

function onPromoteImported(): void {
  promotePanelOpen.value = false;
  toast.success(t("modelBuilder.promote.importSuccess"));
  // Refresh the App Builder model registry (and re-select the current model if
  // any) so the freshly imported pack surfaces — mirrors ModeFrameModelBuilder.
  void appBuilderStore.fetchModels().then(() => {
    const sel = appBuilderStore.selectedModelId;
    if (typeof sel === "string" && sel !== "") {
      appBuilderStore.selectModel(sel);
    }
  });
}

// ── Cross-component trigger from ModeIntroCard / PromoteReadyNotice ──────────
// The intro card's "Promote to App Builder" chip (modeIntro.modelHub.chipPromote,
// id="open-promote") and the promote-ready notice both route through
// ChatView → useModeFrameTriggers `requestOpenPromote`. We open the promote
// panel only when Model Hub is the ACTIVE mode — otherwise the Model Builder /
// App Builder frames own that token (each guards on its own active mode).
const { openPromoteToken } = useModeFrameTriggers();
watch(openPromoteToken, () => {
  if (store.activeTab?.activeMode === "model-hub") {
    promotePanelOpen.value = true;
  }
});

function onExit(): void {
  // Clear transient frame UI; there is no per-mode toolParams to reset.
  modelEntryOpen.value = false;
  promotePanelOpen.value = false;
  aiHubModel.value = "";
  emit("exit");
}
</script>

<template>
  <div
    class="rit-left mh-frame"
    data-testid="mode-frame-model-hub"
  >
    <button
      type="button"
      class="rit-mode-badge"
      data-testid="mode-frame-exit"
      @click="onExit"
    >
      <!-- download cloud — matches the model_hub toolbar glyph -->
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <path d="M20 16.2A4.5 4.5 0 0 0 17.5 8h-1.8A7 7 0 1 0 4 14.9" />
        <polyline points="8 17 12 21 16 17" />
        <line
          x1="12"
          y1="12"
          x2="12"
          y2="21"
        />
      </svg>
      <span>{{ t("index.modelHub") }}</span>
      <span class="rit-close">✕</span>
    </button>

    <span class="rit-sep"></span>

    <!-- 1. AI Hub model entry -->
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        :class="{ 'rit-model-upload--active': aiHubModel !== '' }"
        :aria-expanded="modelEntryOpen"
        :title="t('modelHubFrame.pickModelHint')"
        data-testid="mh-toggle-model-entry"
        @click="toggleModelEntry"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><circle
          cx="11"
          cy="11"
          r="7"
        /><line
          x1="21"
          y1="21"
          x2="16.65"
          y2="16.65"
        /></svg>
        <span
          v-if="aiHubModel !== ''"
          class="rit-model-filename"
          :title="aiHubModel"
        >{{ aiHubModel }}</span>
        <span v-else>{{ t("modelHubFrame.pickModel") }}</span>
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline
            v-if="modelEntryOpen"
            points="18 15 12 9 6 15"
          />
          <polyline
            v-else
            points="6 9 12 15 18 9"
          />
        </svg>
      </button>
      <div
        v-if="modelEntryOpen"
        class="rit-submenu rit-submenu--wide"
        role="menu"
        data-testid="mh-model-entry-panel"
      >
        <div class="rit-submenu-header">
          {{ t("modelHubFrame.pickModelHeader") }}
        </div>
        <div class="mh-entry-body">
          <input
            v-model="aiHubModel"
            type="text"
            class="mh-entry-input"
            :placeholder="t('modelHubFrame.pickModelPlaceholder')"
            data-testid="mh-model-input"
            @keyup.enter="confirmModelEntry"
          />
          <button
            type="button"
            class="rit-btn mh-entry-confirm"
            data-testid="mh-model-confirm"
            @click="confirmModelEntry"
          >
            {{ t("modelHubFrame.pickModelConfirm") }}
          </button>
        </div>
        <div class="rit-submenu-item-desc mh-entry-hint">
          {{ t("modelHubFrame.pickModelDesc") }}
        </div>
      </div>
      <div
        v-if="modelEntryOpen"
        class="dropdown-overlay"
        @click="modelEntryOpen = false"
      ></div>
    </div>

    <!-- 2. Promote to App Builder — REUSES Model Builder's PromoteToAppBuilderCard
         (same app_pack contract). Button label shares modelBuilder.promote.title
         so Model Hub and Model Builder read identically. -->
    <span class="rit-sep"></span>
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        :class="{ 'rit-model-upload--active': promotePanelOpen }"
        :title="t('modelHubFrame.exportTitle')"
        data-testid="mh-toggle-export"
        @click="togglePromotePanel"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><path d="M4 14h6v6H4z" /><path d="M14 4h6v6h-6z" /><path d="M7 14V7h7" /><polyline points="14 10 7 10" /></svg>
        <span>{{ t("modelBuilder.promote.title") }}</span>
        <!-- Ready dot: a subtle 6px accent dot when a model workdir with real
             precision variants (.bin/.dlc) has been detected in the
             conversation. State-Truth-First (verified by scanBins), not just
             "a path string exists". -->
        <span
          v-if="promoteDotReady"
          class="mh-promote-ready-dot"
          role="status"
          :aria-label="t('modelBuilder.promote.readyBadgeAria')"
        ></span>
      </button>
      <div
        v-show="promotePanelOpen"
        class="rit-submenu rit-submenu--wide"
        style="min-width: 400px; max-height: 500px; overflow-y: auto"
        data-testid="mh-promote-panel"
      >
        <PromoteToAppBuilderCard
          :session-model-workdir="sessionModelWorkdir"
          @imported="onPromoteImported"
        />
      </div>
      <div
        v-if="promotePanelOpen"
        class="dropdown-overlay"
        @click="promotePanelOpen = false"
      ></div>
    </div>
  </div>
</template>

<style scoped>
.mh-frame {
  position: relative;
  flex-wrap: wrap;
}

.mh-entry-body {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
}

.mh-entry-input {
  flex: 1 1 auto;
  min-width: 0;
  padding: 4px 8px;
  font-size: var(--text-sm);
  color: var(--text-primary, inherit);
  background: var(--bg-primary, transparent);
  border: 1px solid var(--border, #3a3a42);
  border-radius: 6px;
}

.mh-entry-confirm {
  flex: 0 0 auto;
}

.mh-entry-hint {
  padding: 0 12px 8px;
}

/* "Ready" dot on the Promote button — 6px accent dot shown when a model
   workdir with real precision variants was detected. Mirrors ModeFrameModel-
   Builder's .mb-promote-ready-dot (same size / token / ring) so the two mode
   frames read identically. */
.mh-promote-ready-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #6d5efc);
  box-shadow: 0 0 0 2px var(--bg-secondary, #1c1c22);
  margin-left: 2px;
  flex: 0 0 auto;
}
</style>
