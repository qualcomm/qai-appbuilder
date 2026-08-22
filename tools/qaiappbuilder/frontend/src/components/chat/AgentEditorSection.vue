<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  AgentEditorSection — inline single-role "agent" editor section (M-agent-1).

  The 角色 tab's create/edit form, factored out of TemplateLibraryDialog so all
  three tabs share the same one-level shape (browse + inline editor) and the
  orchestrator stays under the §3.6 1000-line cap. Reuses the shared AgentRoleForm
  (display name / model / persona / tools / colour) so there is no field drift
  (§6.2 复用 > 重造). `editTemplate` null ⇒ create (POST); a template ⇒ edit (PATCH).

  All CRUD/HTTP lives in stores/agentTemplate.ts; this is presentation + draft
  state only. Theme tokens only; styling matches the host's .tl-editor visuals.
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useToast } from "@/composables/useToast";
import AgentRoleForm, {
  type RoleFormData,
} from "@/components/chat/AgentRoleForm.vue";
import {
  useAgentTemplateStore,
  type AgentTemplateView,
} from "@/stores/agentTemplate";

const props = defineProps<{
  /** Template to edit; null = create a new agent. */
  editTemplate?: AgentTemplateView | null;
}>();

const emit = defineEmits<{
  (e: "saved"): void;
  (e: "cancel"): void;
}>();

const { t } = useI18n();
const toast = useToast();
const agents = useAgentTemplateStore();

const editingId = ref<string | null>(null);
const saveName = ref("");
const saveDescription = ref("");
const roleForm = reactive<RoleFormData>({
  displayName: "",
  modelId: "",
  persona: "",
  allowedTools: [],
  enabledSkills: [],
  color: 0,
});

function assignRoleForm(next: RoleFormData): void {
  roleForm.displayName = next.displayName;
  roleForm.modelId = next.modelId;
  roleForm.persona = next.persona;
  roleForm.allowedTools = next.allowedTools;
  roleForm.enabledSkills = next.enabledSkills;
  roleForm.color = next.color;
}

function seed(tpl: AgentTemplateView | null | undefined): void {
  if (!tpl) {
    editingId.value = null;
    saveName.value = "";
    saveDescription.value = "";
    assignRoleForm({
      displayName: "",
      modelId: "",
      persona: "",
      allowedTools: [],
      enabledSkills: [],
      color: 0,
    });
    return;
  }
  editingId.value = tpl.id;
  saveName.value = tpl.name;
  saveDescription.value = tpl.description ?? "";
  assignRoleForm({
    displayName: tpl.displayName,
    modelId: tpl.modelId ?? "",
    persona: tpl.persona ?? "",
    allowedTools: [...tpl.allowedTools],
    enabledSkills: [...tpl.enabledSkills],
    color: typeof tpl.color === "number" ? tpl.color : 0,
  });
}

watch(() => props.editTemplate, (tpl) => seed(tpl), { immediate: true });

const canSave = computed(
  () =>
    saveName.value.trim() !== "" &&
    roleForm.displayName.trim() !== "" &&
    roleForm.modelId.trim() !== "",
);

async function save(): Promise<void> {
  if (!canSave.value) return;
  const input = {
    name: saveName.value.trim(),
    description: saveDescription.value.trim(),
    displayName: roleForm.displayName.trim(),
    modelId: roleForm.modelId.trim(),
    persona: roleForm.persona.trim() || undefined,
    allowedTools: [...roleForm.allowedTools],
    enabledSkills: [...roleForm.enabledSkills],
    color: roleForm.color,
  };
  try {
    if (editingId.value === null) {
      await agents.create(input);
    } else {
      await agents.update(editingId.value, input);
    }
    toast.success(t("chat.discussion.agentTemplates.saved"));
    emit("saved");
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}
</script>

<template>
  <section class="tl-editor" data-testid="library-agent-editor">
    <p class="tl-editor-title">
      {{
        editingId === null
          ? t("chat.discussion.agentTemplates.createTitle")
          : t("chat.discussion.agentTemplates.editTitle", { name: saveName })
      }}
    </p>
    <label class="tl-editor-field">
      <span>{{ t("chat.discussion.agentTemplates.saveAs") }}</span>
      <input
        v-model="saveName"
        class="tl-input"
        type="text"
        :placeholder="t('chat.discussion.agentTemplates.namePlaceholder')"
        data-testid="library-agent-save-name"
      />
    </label>
    <label class="tl-editor-field">
      <span>{{ t("chat.discussion.agentTemplates.descriptionLabel") }}</span>
      <textarea
        v-model="saveDescription"
        class="tl-input"
        rows="2"
        :placeholder="t('chat.discussion.agentTemplates.descriptionPlaceholder')"
        data-testid="library-agent-save-description"
      ></textarea>
    </label>
    <!-- Template-version chips are NEVER dimmed (§7.16). -->
    <AgentRoleForm
      :value="roleForm"
      :current-mode-policy="null"
      @update:value="assignRoleForm"
    />
    <div class="tl-editor-actions">
      <button type="button" class="tl-btn tl-btn--ghost" @click="emit('cancel')">
        {{ t("common.cancel") }}
      </button>
      <button
        type="button"
        class="tl-btn tl-btn--primary"
        :disabled="!canSave"
        data-testid="library-agent-save-confirm"
        @click="save"
      >
        {{ t("chat.discussion.agentTemplates.save") }}
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
  resize: vertical;
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
</style>
