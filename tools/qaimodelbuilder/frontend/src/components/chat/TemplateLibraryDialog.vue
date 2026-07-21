<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  TemplateLibraryDialog — the unified three-tier template panel (§27 decision 2).

  One overlay with three tabs — 角色 (single role) / 团队 (team) / 模式 (mode) —
  giving the user a SINGLE entry point instead of three separate dialogs
  ("避免三个独立入口造成心智过载", §27.2). Each tab browses built-in presets + the
  user's saved templates and imports/applies the selection (角色 → one named_agent;
  团队 → a group of participants; 模式 → the conversation's selected_mode_id).

  All THREE tabs are now ONE level: browse + Preview + Import/Select + inline
  create/edit/clone/reset/delete live here, with no second-tier dialogs. The
  repetitive browse lists are factored into <LibraryBrowseList>, the read-only
  previews into <TemplatePreviewPanels>, and the team/mode editors into
  <RosterEditorSection> / <ModeEditorSection> so this orchestrator stays under the
  §3.6 1000-line cap (the agent editor reuses <AgentRoleForm> inline). All HTTP
  lives in the three stores (agentTemplate/roster/mode); this component only
  orchestrates + wires the editors. Uses the custom ConfirmDialog (§3.9.2 — never
  window.confirm) and toast. PURE V2 enhancement.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useConfirm } from "@/composables/useConfirm";
import { useToast } from "@/composables/useToast";
import { useTemplateI18n } from "@/composables/chat/useTemplateI18n";
import AgentEditorSection from "@/components/chat/AgentEditorSection.vue";
import RosterEditorSection from "@/components/chat/RosterEditorSection.vue";
import ModeEditorSection from "@/components/chat/ModeEditorSection.vue";
import TemplatePreviewPanels from "@/components/chat/TemplatePreviewPanels.vue";
import LibraryBrowseList from "@/components/chat/LibraryBrowseList.vue";
import {
  useAgentTemplateStore,
  type AgentTemplateView,
} from "@/stores/agentTemplate";
import {
  useRosterTemplateStore,
  type RosterTemplateView,
  type RosterTemplateMemberView,
} from "@/stores/rosterTemplate";
import {
  useModeTemplateStore,
  type ModeTemplateView,
} from "@/stores/modeTemplate";

const emit = defineEmits<{
  (e: "close"): void;
  /** Import one agent (single role) into the current discussion. */
  (e: "import-agent", template: AgentTemplateView): void;
  /** Import a team (roster) into the current discussion. */
  (e: "import-roster", template: RosterTemplateView): void;
  /** Select a collaboration mode for the current discussion. */
  (e: "select-mode", template: ModeTemplateView): void;
}>();

const props = defineProps<{
  /** The tab to open initially. */
  initialTab?: TemplateTab;
  /** Currently-selected mode id (to flag the active mode in the 模式 tab). */
  selectedModeId?: string;
  /** The current discussion roster, offered for the team "save as team" flow. */
  currentRoster?: RosterTemplateMemberView[];
}>();

type TemplateTab = "agent" | "roster" | "mode";

const { t } = useI18n();
const { confirm } = useConfirm();
const toast = useToast();
const { resolve: resolveI18n } = useTemplateI18n();

// Localised display text for built-in presets (custom rows fall back to their
// own single-language fields). Display layer only — see useTemplateI18n.
function agentName(a: AgentTemplateView): string {
  return resolveI18n(a.nameI18n, a.name);
}
function agentDisplayName(a: AgentTemplateView): string {
  return resolveI18n(a.displayNameI18n, a.displayName);
}
function rosterName(r: RosterTemplateView): string {
  return resolveI18n(r.nameI18n, r.name);
}
function modeName(m: ModeTemplateView): string {
  return resolveI18n(m.nameI18n, m.name);
}
function modeDescription(m: ModeTemplateView): string {
  return resolveI18n(m.descriptionI18n, m.description ?? "");
}

const agents = useAgentTemplateStore();
const rosters = useRosterTemplateStore();
const modes = useModeTemplateStore();

const tab = ref<TemplateTab>(props.initialTab ?? "agent");
const loading = ref(false);

const agentBuiltins = computed(() =>
  agents.templates.filter((a) => a.isBuiltin),
);
const agentMine = computed(() => agents.templates.filter((a) => !a.isBuiltin));
const rosterBuiltins = computed(() =>
  rosters.templates.filter((r) => r.isBuiltin),
);
const rosterMine = computed(() => rosters.templates.filter((r) => !r.isBuiltin));
const modeBuiltins = computed(() => modes.templates.filter((m) => m.isBuiltin));
const modeMine = computed(() => modes.templates.filter((m) => !m.isBuiltin));

// ── Mode preview (read-only inline panel; mirrors Agents/Teams preview) ──────
const modePreviewId = ref<string | null>(null);
const modePreview = computed<ModeTemplateView | null>(
  () => modes.templates.find((m) => m.id === modePreviewId.value) ?? null,
);

function toggleModePreview(id: string): void {
  modePreviewId.value = modePreviewId.value === id ? null : id;
}

// ── Agent preview (read-only inline panel; M3-3 merge two-tier) ──────────────
const agentPreviewId = ref<string | null>(null);
const agentPreview = computed<AgentTemplateView | null>(
  () => agents.templates.find((a) => a.id === agentPreviewId.value) ?? null,
);

function toggleAgentPreview(id: string): void {
  agentPreviewId.value = agentPreviewId.value === id ? null : id;
}

// ── Roster preview (read-only inline panel; new — Teams had none) ────────────
const rosterPreviewId = ref<string | null>(null);
const rosterPreview = computed<RosterTemplateView | null>(
  () => rosters.templates.find((r) => r.id === rosterPreviewId.value) ?? null,
);
function toggleRosterPreview(id: string): void {
  rosterPreviewId.value = rosterPreviewId.value === id ? null : id;
}

// ── Roster editor (create / edit teams inline; M-roster-1) ───────────────────
// `editingRosterTemplate` null + `rosterFromCurrent` false ⇒ blank "new team";
// `rosterFromCurrent` true ⇒ "save as team" from currentRoster; a template ⇒ edit.
const showRosterEditor = ref(false);
const editingRosterTemplate = ref<RosterTemplateView | null>(null);
const rosterFromCurrent = ref(false);

function openRosterCreate(): void {
  editingRosterTemplate.value = null;
  rosterFromCurrent.value = false;
  showRosterEditor.value = true;
}
function openRosterSave(): void {
  if ((props.currentRoster ?? []).length === 0) {
    toast.error(t("chat.discussion.templates.noMembersToSave"));
    return;
  }
  editingRosterTemplate.value = null;
  rosterFromCurrent.value = true;
  showRosterEditor.value = true;
}
function openRosterEdit(tpl: RosterTemplateView): void {
  editingRosterTemplate.value = tpl;
  rosterFromCurrent.value = false;
  showRosterEditor.value = true;
}
async function onRosterSaved(): Promise<void> {
  showRosterEditor.value = false;
  editingRosterTemplate.value = null;
  rosterFromCurrent.value = false;
  try {
    await rosters.fetchAll();
  } catch {
    /* non-fatal: list already updated optimistically by the store */
  }
}

// ── Mode editor (create / edit collaboration modes inline; M-mode-1) ─────────
const showModeEditor = ref(false);
const editingModeTemplate = ref<ModeTemplateView | null>(null);

function openModeCreate(): void {
  editingModeTemplate.value = null;
  showModeEditor.value = true;
}
function openModeEdit(tpl: ModeTemplateView): void {
  editingModeTemplate.value = tpl;
  showModeEditor.value = true;
}
async function onModeSaved(): Promise<void> {
  showModeEditor.value = false;
  editingModeTemplate.value = null;
  try {
    await modes.fetchAll();
  } catch {
    /* non-fatal */
  }
}

// ── Agent editor (create / edit single-role templates inline; M-agent-1) ─────
// The 角色 tab hosts browse + Preview + Import + inline create/edit (one level,
// like Teams/Modes). The editor itself lives in <AgentEditorSection>; here we
// only toggle it open with the template to edit (null = create).
const showAgentEditor = ref(false);
const editingAgentTemplate = ref<AgentTemplateView | null>(null);

function openAgentCreate(): void {
  editingAgentTemplate.value = null;
  showAgentEditor.value = true;
}

function openAgentEdit(tpl: AgentTemplateView): void {
  editingAgentTemplate.value = tpl;
  showAgentEditor.value = true;
}

async function onAgentSaved(): Promise<void> {
  showAgentEditor.value = false;
  editingAgentTemplate.value = null;
  try {
    await agents.fetchAll();
  } catch {
    /* non-fatal: list already updated optimistically by the store */
  }
}

onMounted(async () => {
  loading.value = true;
  try {
    await Promise.all([
      agents.fetchAll(),
      rosters.fetchAll(),
      modes.fetchAll(),
    ]);
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  } finally {
    loading.value = false;
  }
});

async function deleteAgent(a: AgentTemplateView): Promise<void> {
  if (!(await _confirmDelete(agentName(a)))) return;
  try {
    await agents.remove(a.id);
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}
async function deleteRoster(r: RosterTemplateView): Promise<void> {
  if (!(await _confirmDelete(rosterName(r)))) return;
  try {
    await rosters.remove(r.id);
    if (rosterPreviewId.value === r.id) rosterPreviewId.value = null;
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}
async function deleteMode(m: ModeTemplateView): Promise<void> {
  // Decision 7 / §3.5: warn how many conversations will be reverted to the
  // sentinel ("跟随默认") before deleting a mode that is still in use.
  let count = 0;
  try {
    count = await modes.usage(m.id);
  } catch {
    // Non-fatal: fall back to the plain delete confirm if usage lookup fails.
    count = 0;
  }
  const ok =
    count > 0
      ? await confirm({
          icon: "🗑️",
          title: t("chat.discussion.modes.deleteTitle", { name: modeName(m) }),
          message: t("chat.discussion.modes.deleteInUseMessage", {
            n: count,
            name: modeName(m),
          }),
          confirmText: t("common.delete"),
          cancelText: t("common.cancel"),
          confirmStyle: "danger",
        })
      : await _confirmDelete(modeName(m));
  if (!ok) return;
  try {
    await modes.remove(m.id);
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

async function _confirmDelete(name: string): Promise<boolean> {
  return confirm({
    icon: "🗑️",
    title: t("chat.discussion.library.deleteTitle"),
    message: t("chat.discussion.library.deleteMessage", { name }),
    confirmText: t("common.delete"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
  });
}

// ── Clone (任意模板 → 新副本，克隆后自动打开编辑界面) ─────────────────────────
// Cloning any template (a factory preset OR one of mine) creates a brand-new
// non-builtin copy server-side; we then open the matching editor preloaded with
// the copy so the user can immediately customise it (方案 A: "编辑预设" = 克隆).
async function cloneAgent(a: AgentTemplateView): Promise<void> {
  try {
    const copy = await agents.clone(a.id);
    toast.success(t("chat.discussion.library.cloned"));
    openAgentEdit(copy);
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

async function cloneRoster(r: RosterTemplateView): Promise<void> {
  try {
    const copy = await rosters.clone(r.id);
    toast.success(t("chat.discussion.library.cloned"));
    // Clone & edit inline (方案 A): open the team editor preloaded with the copy.
    openRosterEdit(copy);
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

async function cloneMode(m: ModeTemplateView): Promise<void> {
  try {
    const copy = await modes.clone(m.id);
    toast.success(t("chat.discussion.library.cloned"));
    // Clone & edit inline (方案 A): open the mode editor preloaded with the copy.
    openModeEdit(copy);
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

// ── Reset (仅"克隆自出厂预设的副本"可用 → 恢复成来源出厂内容) ───────────────────
async function _confirmReset(name: string): Promise<boolean> {
  return confirm({
    icon: "↩️",
    title: t("chat.discussion.library.resetConfirmTitle", { name }),
    message: t("chat.discussion.library.resetConfirmMessage"),
    confirmText: t("chat.discussion.library.reset"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
  });
}

async function resetAgent(a: AgentTemplateView): Promise<void> {
  if (!(await _confirmReset(agentName(a)))) return;
  try {
    await agents.reset(a.id);
    toast.success(t("chat.discussion.library.resetDone"));
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

async function resetRoster(r: RosterTemplateView): Promise<void> {
  if (!(await _confirmReset(rosterName(r)))) return;
  try {
    await rosters.reset(r.id);
    toast.success(t("chat.discussion.library.resetDone"));
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

async function resetMode(m: ModeTemplateView): Promise<void> {
  if (!(await _confirmReset(modeName(m)))) return;
  try {
    await modes.reset(m.id);
    toast.success(t("chat.discussion.library.resetDone"));
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}
</script>

<template>
  <div class="tl-overlay" @click.self="emit('close')">
    <div
      class="tl-dialog"
      role="dialog"
      aria-modal="true"
      data-testid="template-library-dialog"
    >
      <header class="tl-header">
        <h2 class="tl-title">{{ t("chat.discussion.library.title") }}</h2>
        <button
          type="button"
          class="tl-close"
          :title="t('common.cancel')"
          @click="emit('close')"
        >
          ✕
        </button>
      </header>

      <!-- Tab bar: 角色 / 团队 / 模式 -->
      <div class="tl-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          class="tl-tab"
          :class="{ 'is-active': tab === 'agent' }"
          data-testid="template-tab-agent"
          @click="tab = 'agent'"
        >
          👤 {{ t("chat.discussion.library.tabAgent") }}
        </button>
        <button
          type="button"
          role="tab"
          class="tl-tab"
          :class="{ 'is-active': tab === 'roster' }"
          data-testid="template-tab-roster"
          @click="tab = 'roster'"
        >
          📋 {{ t("chat.discussion.library.tabRoster") }}
        </button>
        <button
          type="button"
          role="tab"
          class="tl-tab"
          :class="{ 'is-active': tab === 'mode' }"
          data-testid="template-tab-mode"
          @click="tab = 'mode'"
        >
          ⚙️ {{ t("chat.discussion.library.tabMode") }}
        </button>
      </div>

      <div class="tl-body">
        <p v-if="loading" class="tl-loading">…</p>

        <!-- ── 角色 tab ── -->
        <template v-else-if="tab === 'agent'">
          <p class="tl-hint">{{ t("chat.discussion.library.agentHint") }}</p>
          <LibraryBrowseList
            :builtins="agentBuiltins"
            :mine="agentMine"
            item-testid="library-agent-item"
          >
            <template #item-main="{ entry: a }">
              <span class="tl-item-name">{{ agentName(a) }}</span>
              <span class="tl-item-meta">{{ agentDisplayName(a) }}</span>
            </template>
            <template #actions="{ entry: a, isBuiltin }">
              <button
                type="button"
                class="tl-btn tl-btn--ghost"
                :data-testid="`library-agent-preview-${a.id}`"
                @click="toggleAgentPreview(a.id)"
              >
                {{ t("chat.discussion.library.preview") }}
              </button>
              <button
                type="button"
                class="tl-btn tl-btn--primary"
                :data-testid="`library-agent-import-${a.id}`"
                @click="emit('import-agent', a)"
              >
                {{ t("chat.discussion.library.import") }}
              </button>
              <button
                type="button"
                class="tl-btn tl-btn--ghost"
                :title="
                  t(
                    isBuiltin
                      ? 'chat.discussion.library.cloneAndEdit'
                      : 'chat.discussion.library.clone',
                  )
                "
                :data-testid="`library-agent-clone-${a.id}`"
                @click="cloneAgent(a)"
              >
                📋
              </button>
              <button
                v-if="!isBuiltin"
                type="button"
                class="tl-btn tl-btn--ghost"
                :title="t('chat.discussion.agentTemplates.edit')"
                :data-testid="`library-agent-edit-${a.id}`"
                @click="openAgentEdit(a)"
              >
                ✏️
              </button>
              <button
                v-if="!isBuiltin && a.clonedFromId"
                type="button"
                class="tl-btn tl-btn--ghost"
                :title="t('chat.discussion.library.reset')"
                :data-testid="`library-agent-reset-${a.id}`"
                @click="resetAgent(a)"
              >
                ↩️
              </button>
              <button
                v-if="!isBuiltin"
                type="button"
                class="tl-btn tl-btn--danger"
                :title="t('common.delete')"
                @click="deleteAgent(a)"
              >
                🗑️
              </button>
            </template>
          </LibraryBrowseList>

          <!-- Agent preview (read-only inline panel) -->
          <TemplatePreviewPanels :agent="agentPreview" />

          <!-- Inline create / edit editor (shared AgentRoleForm; no drift) -->
          <AgentEditorSection
            v-if="showAgentEditor"
            :edit-template="editingAgentTemplate"
            @saved="onAgentSaved"
            @cancel="showAgentEditor = false"
          />

          <div v-if="!showAgentEditor" class="tl-manage">
            <button
              type="button"
              class="tl-btn tl-btn--ghost"
              data-testid="library-agent-new"
              @click="openAgentCreate"
            >
              ➕ {{ t("chat.discussion.agentTemplates.saveAs") }}
            </button>
          </div>
        </template>

        <!-- ── 团队 tab ── -->
        <template v-else-if="tab === 'roster'">
          <p class="tl-hint">{{ t("chat.discussion.library.rosterHint") }}</p>
          <LibraryBrowseList
            :builtins="rosterBuiltins"
            :mine="rosterMine"
            item-testid="library-roster-item"
          >
            <template #item-main="{ entry: r }">
              <span class="tl-item-name">{{ rosterName(r) }}</span>
              <span class="tl-item-meta">{{
                t("chat.discussion.library.members", { n: r.members.length })
              }}</span>
            </template>
            <template #actions="{ entry: r, isBuiltin }">
              <button
                type="button"
                class="tl-btn tl-btn--ghost"
                :data-testid="`library-roster-preview-${r.id}`"
                @click="toggleRosterPreview(r.id)"
              >
                {{ t("chat.discussion.library.preview") }}
              </button>
              <button
                type="button"
                class="tl-btn tl-btn--primary"
                :data-testid="`library-roster-import-${r.id}`"
                @click="emit('import-roster', r)"
              >
                {{ t("chat.discussion.library.import") }}
              </button>
              <button
                type="button"
                class="tl-btn tl-btn--ghost"
                :title="
                  t(
                    isBuiltin
                      ? 'chat.discussion.library.cloneAndEdit'
                      : 'chat.discussion.library.clone',
                  )
                "
                :data-testid="`library-roster-clone-${r.id}`"
                @click="cloneRoster(r)"
              >
                📋
              </button>
              <button
                v-if="!isBuiltin"
                type="button"
                class="tl-btn tl-btn--ghost"
                :title="t('chat.discussion.templates.edit')"
                :data-testid="`library-roster-edit-${r.id}`"
                @click="openRosterEdit(r)"
              >
                ✏️
              </button>
              <button
                v-if="!isBuiltin && r.clonedFromId"
                type="button"
                class="tl-btn tl-btn--ghost"
                :title="t('chat.discussion.library.reset')"
                :data-testid="`library-roster-reset-${r.id}`"
                @click="resetRoster(r)"
              >
                ↩️
              </button>
              <button
                v-if="!isBuiltin"
                type="button"
                class="tl-btn tl-btn--danger"
                :title="t('common.delete')"
                @click="deleteRoster(r)"
              >
                🗑️
              </button>
            </template>
          </LibraryBrowseList>

          <!-- Team preview (read-only inline panel) -->
          <TemplatePreviewPanels :roster="rosterPreview" />

          <!-- Inline create / edit / save-as editor -->
          <RosterEditorSection
            v-if="showRosterEditor"
            :current-roster="props.currentRoster"
            :edit-template="editingRosterTemplate"
            :from-current="rosterFromCurrent"
            @saved="onRosterSaved"
            @cancel="showRosterEditor = false"
          />

          <div v-if="!showRosterEditor" class="tl-manage">
            <button
              type="button"
              class="tl-btn tl-btn--ghost"
              data-testid="library-roster-new"
              @click="openRosterCreate"
            >
              ➕ {{ t("chat.discussion.templates.newTeam") }}
            </button>
            <button
              type="button"
              class="tl-btn tl-btn--ghost"
              data-testid="library-roster-save-as"
              @click="openRosterSave"
            >
              💾 {{ t("chat.discussion.templates.saveAs") }}
            </button>
          </div>
        </template>

        <!-- ── 模式 tab ── -->
        <template v-else>
          <p class="tl-hint">{{ t("chat.discussion.library.modeHint") }}</p>
          <LibraryBrowseList
            :builtins="modeBuiltins"
            :mine="modeMine"
            item-testid="library-mode-item"
          >
            <template #item-main="{ entry: m }">
              <span class="tl-item-name">
                {{ modeName(m) }}
                <span
                  v-if="m.id === props.selectedModeId"
                  class="tl-active-badge"
                  >{{ t("chat.discussion.library.active") }}</span
                >
              </span>
              <span class="tl-item-meta">{{ modeDescription(m) }}</span>
            </template>
            <template #actions="{ entry: m, isBuiltin }">
              <button
                type="button"
                class="tl-btn tl-btn--ghost"
                :data-testid="`library-mode-preview-${m.id}`"
                @click="toggleModePreview(m.id)"
              >
                {{ t("chat.discussion.library.preview") }}
              </button>
              <button
                type="button"
                class="tl-btn tl-btn--primary"
                :data-testid="`library-mode-select-${m.id}`"
                @click="emit('select-mode', m)"
              >
                {{ t("chat.discussion.library.select") }}
              </button>
              <button
                type="button"
                class="tl-btn tl-btn--ghost"
                :title="
                  t(
                    isBuiltin
                      ? 'chat.discussion.library.cloneAndEdit'
                      : 'chat.discussion.library.clone',
                  )
                "
                :data-testid="`library-mode-clone-${m.id}`"
                @click="cloneMode(m)"
              >
                📋
              </button>
              <button
                v-if="!isBuiltin"
                type="button"
                class="tl-btn tl-btn--ghost"
                :title="t('chat.discussion.modes.edit')"
                :data-testid="`library-mode-edit-${m.id}`"
                @click="openModeEdit(m)"
              >
                ✏️
              </button>
              <button
                v-if="!isBuiltin && m.clonedFromId"
                type="button"
                class="tl-btn tl-btn--ghost"
                :title="t('chat.discussion.library.reset')"
                :data-testid="`library-mode-reset-${m.id}`"
                @click="resetMode(m)"
              >
                ↩️
              </button>
              <button
                v-if="!isBuiltin"
                type="button"
                class="tl-btn tl-btn--danger"
                :title="t('common.delete')"
                @click="deleteMode(m)"
              >
                🗑️
              </button>
            </template>
          </LibraryBrowseList>

          <!-- Mode preview (read-only inline panel) -->
          <TemplatePreviewPanels :mode="modePreview" />

          <!-- Inline create / edit editor -->
          <ModeEditorSection
            v-if="showModeEditor"
            :edit-template="editingModeTemplate"
            @saved="onModeSaved"
            @cancel="showModeEditor = false"
          />

          <div v-if="!showModeEditor" class="tl-manage">
            <button
              type="button"
              class="tl-btn tl-btn--ghost"
              data-testid="library-mode-new"
              @click="openModeCreate"
            >
              {{ t("chat.discussion.modes.newMode") }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tl-overlay {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--overlay-bg, rgba(0, 0, 0, 0.5));
}
.tl-dialog {
  display: flex;
  flex-direction: column;
  width: min(560px, 92vw);
  max-height: 82vh;
  background: var(--bg-secondary);
  color: var(--text-primary);
  border: 1px solid var(--border-light);
  border-radius: 12px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.tl-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px 8px;
}
.tl-title {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 600;
}
.tl-close {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  font-size: 1rem;
  cursor: pointer;
}
.tl-tabs {
  display: flex;
  gap: 4px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
}
.tl-tab {
  padding: 8px 12px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--text-secondary);
  font-size: 0.85rem;
  cursor: pointer;
}
.tl-tab.is-active {
  color: var(--text-primary);
  border-bottom-color: var(--accent);
}
.tl-body {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 12px 20px;
}
.tl-hint {
  margin: 0 0 12px;
  font-size: 0.82rem;
  color: var(--text-secondary);
}
.tl-item-name {
  font-weight: 500;
}
.tl-item-meta {
  font-size: 0.76rem;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tl-active-badge {
  margin-left: 6px;
  font-size: 0.7rem;
  color: var(--accent);
}
.tl-loading {
  margin: 8px 0;
  font-size: 0.82rem;
  color: var(--text-secondary);
}
.tl-btn {
  padding: 5px 10px;
  font-size: 0.8rem;
  border-radius: 6px;
  border: 1px solid transparent;
  cursor: pointer;
  flex: 0 0 auto;
}
.tl-btn--primary {
  background: var(--accent);
  color: #fff;
}
.tl-btn--danger {
  background: transparent;
  border-color: transparent;
  color: var(--error);
}
.tl-btn--ghost {
  background: transparent;
  border-color: var(--border);
  color: var(--text-primary);
}
.tl-manage {
  margin-top: 4px;
  display: flex;
  justify-content: flex-start;
  gap: 6px;
  flex-wrap: wrap;
}
</style>
