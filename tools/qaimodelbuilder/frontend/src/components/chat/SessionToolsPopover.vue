<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SessionToolsPopover — per-session ("this conversation only") temporary
 * tool / SKILL switches (V2 enhancement).
 *
 * Surfaced from a small button in the composer's `.rit-right` row (next to
 * prompt-enhance / voice / send). Lets the user quickly turn individual chat
 * TOOLS (read / edit / exec / webfetch / agent / …) and SKILLS on/off for the
 * ACTIVE conversation only — without touching the global tool-safety config or
 * the persistent per-skill mode in Settings.
 *
 * Semantics (OVERRIDE / DIFF — see `ChatTab.sessionToolOverride`):
 *   - Default = everything ON (follows global). Only items the user explicitly
 *     toggles OFF are recorded as `disabledTools` / `disabledSkills`.
 *   - Per-session + in-memory only: a reload returns to global defaults
 *     (`persistedLayout` does not persist the override).
 *   - The transport forwards the diff as the additive `disabled_tools` /
 *     `disabled_skills` payload fields; the backend applies them PER-TURN
 *     (it never mutates global forge.config / tool-safety config).
 *
 * Tool list is fetched live from `GET /api/chat/tools` (so newly registered
 * tools appear automatically); skill list comes from the shared `useSkillsStore`
 * (only skills NOT globally `off` are shown — turning a globally-off skill on
 * here is out of scope; this panel only narrows the current session).
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
import { useSkillsStore } from "@/stores/skills";
import { fetchChatTools, type ChatToolDescriptor } from "@/api/chatTools";
import ToolGroupToggle from "./ToolGroupToggle.vue";

interface Props {
  open: boolean;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  "update:open": [value: boolean];
}>();

const { t } = useI18n();
const store = useChatTabsStore();
const skillsStore = useSkillsStore();

const popoverRef = useTemplateRef<HTMLDivElement>("popover");

// Live chat-tool catalogue (canonical order from the backend). Fetched once on
// first open; failures degrade to an empty list (the panel then shows only the
// skills section).
const tools = ref<ChatToolDescriptor[]>([]);
const toolsLoaded = ref(false);

async function ensureToolsLoaded(): Promise<void> {
  if (toolsLoaded.value) return;
  try {
    const res = await fetchChatTools();
    tools.value = res.tools ?? [];
  } catch {
    tools.value = [];
  } finally {
    toolsLoaded.value = true;
  }
}

// Skills that are globally enabled (mode !== "off"). The per-session panel only
// NARROWS the active set — it cannot enable a skill the user turned off
// globally (that lives in Settings). Globally-off skills are hidden here.
const sessionSkills = computed(() =>
  skillsStore.skills.filter((s) => (s.mode ?? (s.enabled ? "cloud" : "off")) !== "off"),
);

const activeTab = computed(() => store.activeTab);

// Current per-session diff (sets of names switched OFF). Reading from the tab
// keeps the panel reactive to external resets / tab switches.
const disabledTools = computed<Set<string>>(
  () => new Set(activeTab.value?.sessionToolOverride?.disabledTools ?? []),
);
const disabledSkills = computed<Set<string>>(
  () => new Set(activeTab.value?.sessionToolOverride?.disabledSkills ?? []),
);

const hasOverride = computed(
  () => disabledTools.value.size > 0 || disabledSkills.value.size > 0,
);

function isToolOn(name: string): boolean {
  return !disabledTools.value.has(name);
}

// Bridge for ToolGroupToggle component.
const toolNames = computed(() => tools.value.map((t) => t.name));
const disabledToolsList = computed(() => [...disabledTools.value]);
function onToolsUpdate(newList: string[]): void {
  commit(new Set(newList), new Set(disabledSkills.value));
}

function isSkillOn(id: string): boolean {
  return !disabledSkills.value.has(id);
}

/** Persist the COMPLETE current diff back to the tab (store normalises an
 *  all-empty diff to "follow global defaults"). */
function commit(nextTools: Set<string>, nextSkills: Set<string>): void {
  const tab = activeTab.value;
  if (tab === null) return;
  store.setSessionToolOverride(tab.id, {
    disabledTools: [...nextTools],
    disabledSkills: [...nextSkills],
  });
}

function toggleTool(name: string): void {
  const next = new Set(disabledTools.value);
  if (next.has(name)) {
    next.delete(name); // turn back ON
  } else {
    next.add(name); // turn OFF for this session
  }
  commit(next, new Set(disabledSkills.value));
}

function toggleSkill(id: string): void {
  const next = new Set(disabledSkills.value);
  if (next.has(id)) {
    next.delete(id);
  } else {
    next.add(id);
  }
  commit(new Set(disabledTools.value), next);
}

function resetAll(): void {
  const tab = activeTab.value;
  if (tab === null) return;
  store.resetSessionToolOverride(tab.id);
}

// "Clear all" per group = switch every item in that group OFF for this session
// (the group's disabled set becomes the full list of its current items), while
// leaving the OTHER group's diff untouched. Same per-session semantics.
const allToolsOff = computed(
  () => tools.value.length > 0 && tools.value.every((tool) => !isToolOn(tool.name)),
);
const allSkillsOff = computed(
  () =>
    sessionSkills.value.length > 0 &&
    sessionSkills.value.every((skill) => !isSkillOn(skill.id)),
);

function clearAllTools(): void {
  commit(
    new Set(tools.value.map((tool) => tool.name)),
    new Set(disabledSkills.value),
  );
}

function clearAllSkills(): void {
  commit(
    new Set(disabledTools.value),
    new Set(sessionSkills.value.map((skill) => skill.id)),
  );
}

function close(): void {
  emit("update:open", false);
}

// Load tools + ensure skills are available whenever the popover opens.
watch(
  () => props.open,
  (next: boolean) => {
    if (next) {
      void ensureToolsLoaded();
      void skillsStore.ensureLoaded();
    }
  },
);

// Click-outside to close (capture-phase mousedown; let the trigger's own
// wrapper handle the toggle — same pattern as ModelParamsPopover).
function onDocMouseDown(ev: MouseEvent): void {
  if (!props.open) return;
  const el = popoverRef.value;
  if (el === null) return;
  const target = ev.target as Node | null;
  if (target === null) return;
  if (el.contains(target)) return;
  const wrap = el.parentElement;
  if (wrap !== null && wrap.contains(target)) return;
  close();
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
    class="session-tools-popover"
    role="dialog"
    aria-modal="false"
    :aria-label="t('chat.sessionTools.title', '本会话工具 / 技能')"
    data-testid="session-tools-popover"
    @mousedown.stop
    @keydown.escape="close"
  >
    <div class="session-tools-header">
      <span>{{ t("chat.sessionTools.title", "本会话工具 / 技能") }}</span>
      <button
        type="button"
        class="session-tools-reset"
        :disabled="!hasOverride"
        data-testid="session-tools-reset"
        @click="resetAll"
      >
        {{ t("chat.sessionTools.reset", "重置") }}
      </button>
    </div>

    <div class="session-tools-body">
      <!-- Tools -->
      <div class="session-tools-section-label">
        <span>{{ t("chat.sessionTools.toolsLabel", "工具 Tools") }}</span>
        <button
          type="button"
          class="session-tools-clear"
          :disabled="allToolsOff || tools.length === 0"
          data-testid="session-tools-clear-tools"
          :title="t('chat.sessionTools.clearAll', '全部清除')"
          @click="clearAllTools"
        >
          {{ t("chat.sessionTools.clearAll", "全部清除") }}
        </button>
      </div>
      <div
        v-if="toolsLoaded && tools.length === 0"
        class="session-tools-empty"
      >
        {{ t("chat.sessionTools.noTools", "没有可用工具") }}
      </div>
      <ToolGroupToggle
        v-else
        :model-value="disabledToolsList"
        :tools="toolNames"
        mode="disabled"
        @update:model-value="onToolsUpdate"
      />

      <!-- Skills -->
      <div class="session-tools-section-label session-tools-section-label--skills">
        <span>{{ t("chat.sessionTools.skillsLabel", "技能 Skills") }}</span>
        <button
          type="button"
          class="session-tools-clear"
          :disabled="allSkillsOff || sessionSkills.length === 0"
          data-testid="session-tools-clear-skills"
          :title="t('chat.sessionTools.clearAll', '全部清除')"
          @click="clearAllSkills"
        >
          {{ t("chat.sessionTools.clearAll", "全部清除") }}
        </button>
      </div>
      <div
        v-if="sessionSkills.length === 0"
        class="session-tools-empty"
      >
        {{ t("chat.sessionTools.noSkills", "没有已启用的技能") }}
      </div>
      <div
        v-else
        class="session-tools-grid"
      >
        <label
          v-for="skill in sessionSkills"
          :key="skill.id"
          class="session-tool-chip"
          :class="{ 'is-on': isSkillOn(skill.id) }"
          :title="skill.use_for || skill.description || skill.name"
        >
          <input
            type="checkbox"
            :checked="isSkillOn(skill.id)"
            :data-testid="`session-skill-${skill.id}`"
            @change="toggleSkill(skill.id)"
          />
          {{ skill.name || skill.id }}
        </label>
      </div>

      <!-- Token budget (max_budget_tokens) moved to a dedicated BudgetPopover
           (its own toolbar button); this panel is tools / skills only. -->

      <div class="session-tools-foot">
        💡 {{ t("chat.sessionTools.hint", "仅本会话临时生效，切换会话不影响其它会话，刷新后恢复默认。") }}
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Popover container — reuses global theme tokens (no magic numbers / colours),
   matching the visual language of ModelParamsPopover / WhisperEnginePopover. */
.session-tools-popover {
  position: absolute;
  bottom: calc(100% + 8px);
  right: 0;
  z-index: 50;
  min-width: 360px;
  max-width: 460px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  padding: 10px 12px 12px;
  animation: toast-in 0.12s ease-out;
}

.session-tools-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 8px;
}

.session-tools-reset {
  font-size: var(--text-xs);
  color: var(--accent);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 2px 4px;
}
.session-tools-reset:disabled {
  color: var(--text-muted);
  cursor: default;
}

.session-tools-section-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-bottom: 6px;
}
.session-tools-section-label--skills {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--border-light);
}

/* Per-group "clear all" (switch every item in the group OFF for this session).
   Same muted-accent affordance as the header reset; disabled when the group is
   already all-off or empty. */
.session-tools-clear {
  font-size: var(--text-xs);
  color: var(--accent);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0 2px;
}
.session-tools-clear:disabled {
  color: var(--text-muted);
  cursor: default;
  opacity: 0.6;
}

.session-tools-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.session-tool-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--text-xs);
  padding: 3px 8px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border-light);
  color: var(--text-muted);
  background: var(--bg-primary, transparent);
  cursor: pointer;
  user-select: none;
}
.session-tool-chip.is-on {
  color: var(--text-primary);
  border-color: var(--accent);
}
.session-tool-chip input {
  accent-color: var(--accent);
  cursor: pointer;
}

.session-tools-empty {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.session-tools-foot {
  margin-top: 10px;
  font-size: var(--text-xs);
  color: var(--text-muted);
  line-height: 1.4;
}
</style>
