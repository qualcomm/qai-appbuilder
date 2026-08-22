// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useAppCommands — derives the three command-palette groups (Actions /
 * Skills / Models) consumed by the global `AppCommandPalette`.
 *
 * V1 parity source of truth: `frontend/js/components/CommandPalette.js`
 * (lines 70-154). V1 hand-rolled three `computed` lists over global refs
 * injected through `commandPaletteCtx`:
 *   - actions  → theme toggle / font size +-reset / navigation actions
 *                (each with an emoji icon + optional shortcut)
 *   - skills   → the skill registry (`cmdPaletteCtx.skills`)
 *   - models   → the chat-selectable cloud models (`cmdPaletteCtx.models`)
 * and merged them into `groupedResults` (actions → skills → models).
 *
 * Here the same *behaviour* is expressed with a single typed composable
 * that derives each group reactively from the real V2 stores / composables
 * (theme, font-size, skills registry, model catalog), instead of V1's
 * global-ref injection. Each command carries `category` (drives grouping
 * in the palette), `icon` and optional `shortcut` (V1 per-item icon/kbd).
 *
 * Returns a flat `commands` list (already ordered actions → skills →
 * models) so the palette only has to group by `category`; the grouping
 * + keyboard navigation lives in `AppCommandPalette.vue`.
 */
import { computed, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import { useTheme } from "@/composables/useTheme";
import { useFontSize } from "@/composables/useFontSize";
import { useSkillsStore } from "@/stores/skills";
import { useChatTabsStore } from "@/stores/chatTabs";
import { apiJson } from "@/api";
import type { CloudModelEntry, CloudModelsResponse } from "@/types/cloudModels";
import type { PaletteCommand } from "@/composables/useCommandPalette";

/**
 * Single composable instance assembling the actions / skills / models
 * commands. Mounted once from `AppCommandPalette.vue`.
 */
export function useAppCommands(): {
  commands: ComputedRef<readonly PaletteCommand[]>;
  /** Lazily load the async lists (skills + cloud models) that back the
   *  Skills / Models groups; safe to call repeatedly. */
  ensureLoaded: () => Promise<void>;
} {
  const { t } = useI18n();
  const router = useRouter();
  const { cycleTheme } = useTheme();
  const { increaseFontSize, decreaseFontSize, resetFontSize } = useFontSize();
  const skillsStore = useSkillsStore();
  const chatTabs = useChatTabsStore();

  // Cloud chat models (V1's `cmdPaletteCtx.models` = chat-selectable models).
  // Sourced from `/api/model-catalog/cloud-models` — the same list that feeds
  // the chat model selector — NOT the (empty) download catalog `/entries`.
  const cloudModels: Ref<CloudModelEntry[]> = ref([]);
  const cloudModelsLoaded = ref(false);

  async function ensureLoaded(): Promise<void> {
    await skillsStore.ensureLoaded();
    if (cloudModelsLoaded.value) return;
    try {
      const res = await apiJson<CloudModelsResponse>(
        "GET",
        "/api/model-catalog/cloud-models",
      );
      cloudModels.value = res.models ?? [];
      cloudModelsLoaded.value = true;
    } catch {
      cloudModels.value = [];
    }
  }

  function go(name: string): void {
    void router.push({ name });
  }

  // ── Actions (V1 CommandPalette.js:70-83) ──────────────────────────────────
  // Theme + font controls + navigation jumps. Navigation targets reuse the
  // router route names so they stay in sync with the route table.
  const actions = computed<PaletteCommand[]>(() => [
    {
      id: "act:toggle-theme",
      category: "actions",
      icon: "\u{1F313}", // 🌓
      label: t("commandPalette.action.toggleTheme"),
      run: () => cycleTheme(),
    },
    {
      id: "act:font-increase",
      category: "actions",
      icon: "\u{1F50D}", // 🔍
      label: t("commandPalette.action.fontIncrease"),
      run: () => increaseFontSize(),
    },
    {
      id: "act:font-decrease",
      category: "actions",
      icon: "\u{1F50E}", // 🔎
      label: t("commandPalette.action.fontDecrease"),
      run: () => decreaseFontSize(),
    },
    {
      id: "act:font-reset",
      category: "actions",
      icon: "\u{1F504}", // 🔄
      label: t("commandPalette.action.fontReset"),
      run: () => resetFontSize(),
    },
    {
      id: "act:new-conv",
      category: "actions",
      icon: "\u2795", // ➕
      label: t("commandPalette.action.newConversation"),
      shortcut: "Ctrl+N",
      run: () => {
        chatTabs.openTab();
        go("chat");
      },
    },
    {
      id: "act:clear-messages",
      category: "actions",
      icon: "\u{1F5D1}\uFE0F", // 🗑️
      label: t("commandPalette.action.clearMessages"),
      run: () => {
        const id = chatTabs.activeTabId;
        if (id !== null) chatTabs.clearMessages(id);
      },
    },
    {
      id: "act:view-chat",
      category: "actions",
      icon: "\u{1F4AC}", // 💬
      label: t("commandPalette.action.openChat"),
      run: () => go("chat"),
    },
    {
      id: "act:view-skills",
      category: "actions",
      icon: "\u26A1", // ⚡
      label: t("commandPalette.action.openSkills"),
      run: () => go("skills"),
    },
    {
      id: "act:view-channels",
      category: "actions",
      icon: "\u{1F4E1}", // 📡
      label: t("commandPalette.action.openChannels"),
      run: () => go("channels"),
    },
    {
      id: "act:view-service",
      category: "actions",
      icon: "\u25B6\uFE0F", // ▶️
      label: t("commandPalette.action.openService"),
      run: () => go("service"),
    },
    {
      id: "act:view-updates",
      category: "actions",
      icon: "\u{1F4E5}", // 📥
      label: t("commandPalette.action.openDownloads"),
      run: () => go("downloads"),
    },
    {
      id: "act:view-settings",
      category: "actions",
      icon: "\u2699\uFE0F", // ⚙️
      label: t("commandPalette.action.openSettings"),
      run: () => go("settings"),
    },
  ]);

  // ── Skills (V1 CommandPalette.js:86-101) ──────────────────────────────────
  // One command per registered skill; selecting jumps to the Skills view.
  // Data comes from the shared skills registry store (real /api/skills).
  const skills = computed<PaletteCommand[]>(() =>
    skillsStore.skills.map((s) => ({
      id: `skill:${s.id}`,
      category: "skills",
      icon: "\u26A1", // ⚡
      label: s.name || s.id,
      run: () => go("skills"),
    })),
  );

  // ── Models (V1 CommandPalette.js:104-115) ─────────────────────────────────
  // One command per chat-selectable cloud model; selecting jumps to the Chat
  // view (where the model selector lives). The same model id can appear under
  // multiple providers, so the command id includes the provider to stay unique.
  const models = computed<PaletteCommand[]>(() =>
    cloudModels.value.map((m) => ({
      id: `model:${m.provider}:${m.model_id}`,
      category: "models",
      icon: "\u{1F916}", // 🤖
      label: m.name || m.model_id,
      run: () => go("chat"),
    })),
  );

  // V1 groupedResults ordering: actions → skills → models.
  const commands = computed<readonly PaletteCommand[]>(() => [
    ...actions.value,
    ...skills.value,
    ...models.value,
  ]);

  return { commands, ensureLoaded };
}
