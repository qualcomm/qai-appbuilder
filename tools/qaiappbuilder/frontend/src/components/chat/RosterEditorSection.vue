<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  RosterEditorSection — inline "team" (roster) editor section (M-roster-1).

  Extracted from the former second-tier RosterTemplateDialog so the TemplateLibrary
  团队 tab is ONE level (like 角色/Agents): browse + preview + import + create/edit
  all live in the library, no separate dialog. This section is the create/edit
  form, rendered inline below the list (mirrors the inline AgentRoleForm editor).

  One inline form reused for THREE flows (§6.2 复用 > 重造):
    * "save as team"  — seed members from the current discussion roster
    * "new team"      — blank, the user builds members from scratch
    * "edit team"     — prefill from a saved (non-builtin) template / cloned copy

  All CRUD/HTTP lives in stores/rosterTemplate.ts; this is presentation +
  draft-form state only. Members use the shared AgentRoleForm (no field drift).
  Theme tokens only; styling aligns with the host's .tl-* visual language.
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useToast } from "@/composables/useToast";
import { useTemplateI18n } from "@/composables/chat/useTemplateI18n";
import AgentRoleForm, {
  type RoleFormData,
} from "@/components/chat/AgentRoleForm.vue";
import { DISCUSSION_PALETTE_SIZE } from "@/stores/_chatTabsTypes";
import {
  useRosterTemplateStore,
  type RosterTemplateView,
  type RosterTemplateMemberView,
} from "@/stores/rosterTemplate";
import { useModeTemplateStore, type ModeTemplateView } from "@/stores/modeTemplate";

const props = defineProps<{
  /** "save as team" source = the current discussion roster (may be empty). */
  currentRoster?: RosterTemplateMemberView[];
  /** Preload the editor from this template (clone & edit / edit); null = new. */
  editTemplate?: RosterTemplateView | null;
  /** When true (and editTemplate is null), seed from currentRoster ("save as"). */
  fromCurrent?: boolean;
}>();

const emit = defineEmits<{
  (e: "saved"): void;
  (e: "cancel"): void;
}>();

const { t } = useI18n();
const { resolve: resolveI18n } = useTemplateI18n();
const toast = useToast();
const store = useRosterTemplateStore();
const modeStore = useModeTemplateStore();

/** Localised built-in mode name for the default-mode dropdown (custom modes
 *  fall back to own name). Display layer only — see useTemplateI18n. */
function modeName(m: ModeTemplateView): string {
  return resolveI18n(m.nameI18n, m.name);
}

// `editingId` null ⇒ create (POST); a string ⇒ edit that template (PATCH).
const editingId = ref<string | null>(null);
const editName = ref("");
const editDesc = ref("");
const editDefaultModeId = ref("");
const editMembers = reactive<RoleFormData[]>([]);

/** Collaboration modes offered for the "default mode" dropdown. */
const modeOptions = computed(() => modeStore.templates);

function memberToRoleForm(m: RosterTemplateMemberView): RoleFormData {
  return {
    displayName: m.displayName,
    modelId: m.modelId ?? "",
    persona: m.persona ?? "",
    allowedTools: [...m.allowedTools],
    enabledSkills: [...m.enabledSkills],
    color: typeof m.color === "number" ? m.color : 0,
  };
}

function roleFormToMember(r: RoleFormData): RosterTemplateMemberView {
  return {
    displayName: r.displayName.trim(),
    ...(r.modelId.trim() !== "" ? { modelId: r.modelId.trim() } : {}),
    ...(r.persona.trim() !== "" ? { persona: r.persona.trim() } : {}),
    allowedTools: [...r.allowedTools],
    enabledSkills: [...r.enabledSkills],
    color: r.color,
  };
}

function newBlankMember(): RosterTemplateMemberView {
  return {
    displayName: "",
    allowedTools: [],
    enabledSkills: [],
    color: editMembers.length % DISCUSSION_PALETTE_SIZE,
  };
}

function seed(
  members: RosterTemplateMemberView[],
  opts: { id: string | null; name: string; description: string; defaultModeId: string },
): void {
  editingId.value = opts.id;
  editName.value = opts.name;
  editDesc.value = opts.description;
  editDefaultModeId.value = opts.defaultModeId;
  editMembers.splice(0, editMembers.length, ...members.map(memberToRoleForm));
}

/** Reseed whenever the inputs change (the host toggles this section via v-if,
 *  remounting it, but watch keeps clone→edit hand-offs robust too). */
function reseed(): void {
  if (props.editTemplate) {
    seed(props.editTemplate.members, {
      id: props.editTemplate.id,
      name: props.editTemplate.name,
      description: props.editTemplate.description,
      defaultModeId: props.editTemplate.defaultModeId ?? "",
    });
    return;
  }
  if (props.fromCurrent) {
    const cur = props.currentRoster ?? [];
    seed(cur, { id: null, name: "", description: "", defaultModeId: "" });
    return;
  }
  seed([newBlankMember()], {
    id: null,
    name: "",
    description: "",
    defaultModeId: "",
  });
}

// Reseed when the editing target switches (clone-A → clone-B, or edit → save-as).
// NOTE: `currentRoster` is intentionally NOT a watch source. "Save as team" is a
// snapshot of the roster *at the moment the editor opens* — re-seeding from later
// roster edits would silently overwrite the user's in-progress draft. The host
// remounts this section via v-if, so reopening always re-snapshots the latest.
watch(
  () => [props.editTemplate, props.fromCurrent] as const,
  () => reseed(),
  { immediate: true },
);

void modeStore.fetchAll().catch(() => {
  /* non-fatal: the default-mode dropdown just shows none */
});

function addMember(): void {
  editMembers.push(memberToRoleForm(newBlankMember()));
}

function removeMember(index: number): void {
  editMembers.splice(index, 1);
}

function updateMember(index: number, next: RoleFormData): void {
  editMembers.splice(index, 1, next);
}

/** Valid when name + ≥1 member, and every member has a display name + model. */
const canSave = computed(
  () =>
    editName.value.trim() !== "" &&
    editMembers.length > 0 &&
    editMembers.every(
      (m) => m.displayName.trim() !== "" && m.modelId.trim() !== "",
    ),
);

async function save(): Promise<void> {
  if (!canSave.value) return;
  const input = {
    name: editName.value.trim(),
    description: editDesc.value.trim(),
    members: editMembers.map(roleFormToMember),
    ...(editDefaultModeId.value !== ""
      ? { defaultModeId: editDefaultModeId.value }
      : {}),
  };
  try {
    if (editingId.value === null) {
      await store.create(input);
    } else {
      await store.update(editingId.value, input);
    }
    toast.success(t("chat.discussion.templates.saved"));
    emit("saved");
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}
</script>

<template>
  <section class="tl-editor" data-testid="roster-template-editor">
    <p class="tl-editor-title">
      {{
        editingId === null
          ? t("chat.discussion.templates.createTitle")
          : t("chat.discussion.templates.editTitle", { name: editName })
      }}
    </p>
    <label class="tl-editor-field">
      <span>{{ t("chat.discussion.templates.nameLabel") }}</span>
      <input
        v-model="editName"
        class="tl-input"
        type="text"
        :placeholder="t('chat.discussion.templates.namePlaceholder')"
        data-testid="roster-template-save-name"
      />
    </label>
    <label class="tl-editor-field">
      <span>{{ t("chat.discussion.templates.descLabel") }}</span>
      <input
        v-model="editDesc"
        class="tl-input"
        type="text"
        :placeholder="t('chat.discussion.templates.descPlaceholder')"
      />
    </label>
    <label class="tl-editor-field">
      <span>{{ t("chat.discussion.templates.defaultModeLabel") }}</span>
      <select
        v-model="editDefaultModeId"
        class="tl-input"
        data-testid="roster-template-default-mode"
      >
        <option value="">
          {{ t("chat.discussion.templates.defaultModeNone") }}
        </option>
        <option v-for="m in modeOptions" :key="m.id" :value="m.id">
          {{ modeName(m) }}
        </option>
      </select>
    </label>

    <!-- Members editor — one shared AgentRoleForm per member. -->
    <div class="tl-members">
      <span class="tl-members-title">
        {{ t("chat.discussion.templates.membersLabel") }}
      </span>
      <p v-if="editMembers.length === 0" class="tl-empty">
        {{ t("chat.discussion.templates.noMembers") }}
      </p>
      <div
        v-for="(m, i) in editMembers"
        :key="i"
        class="tl-member"
        data-testid="roster-template-member"
      >
        <div class="tl-member-head">
          <span class="tl-member-index">
            {{ t("chat.discussion.templates.memberN", { n: i + 1 }) }}
          </span>
          <button
            type="button"
            class="tl-btn tl-btn--danger"
            :title="t('chat.discussion.templates.removeMember')"
            :data-testid="`roster-template-member-remove-${i}`"
            @click="removeMember(i)"
          >
            🗑️
          </button>
        </div>
        <!-- Template-version chips are NEVER dimmed (§7.16): future mode is
             unknown → currentModePolicy=null. -->
        <AgentRoleForm
          :value="m"
          :current-mode-policy="null"
          @update:value="(next) => updateMember(i, next)"
        />
      </div>
      <button
        type="button"
        class="tl-btn tl-btn--ghost"
        data-testid="roster-template-add-member"
        @click="addMember"
      >
        ➕ {{ t("chat.discussion.templates.addMember") }}
      </button>
    </div>

    <div class="tl-editor-actions">
      <button
        type="button"
        class="tl-btn tl-btn--ghost"
        @click="emit('cancel')"
      >
        {{ t("common.cancel") }}
      </button>
      <button
        type="button"
        class="tl-btn tl-btn--primary"
        :disabled="!canSave"
        data-testid="roster-template-save-confirm"
        @click="save"
      >
        {{ t("chat.discussion.templates.save") }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.tl-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-tertiary);
  margin-bottom: 16px;
}
.tl-editor-title {
  margin: 0;
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-primary);
}
.tl-editor-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 0.82rem;
  color: var(--text-secondary);
}
.tl-members {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tl-members-title {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text-primary);
}
.tl-member {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-secondary);
}
.tl-member-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.tl-member-index {
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--text-secondary);
}
.tl-empty {
  margin: 4px 0;
  font-size: 0.82rem;
  color: var(--text-secondary);
}
.tl-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.tl-input {
  width: 100%;
  box-sizing: border-box;
  padding: 6px 8px;
  font-size: 0.85rem;
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  font: inherit;
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
.tl-btn--primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.tl-btn--ghost {
  background: transparent;
  border-color: var(--border);
  color: var(--text-primary);
}
.tl-btn--danger {
  background: transparent;
  border-color: transparent;
  color: var(--error);
}
</style>
