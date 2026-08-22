<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  CoordinatorEditorSection — inline editor for the built-in coordinator (主持人).

  The coordinator is a runtime-synthesised sentinel participant (id
  ``__coordinator__``), not a persisted roster row. It has a FIXED identity
  (name comes from ``chat.discussion.coordinator.displayName``, no persona),
  so the editor reuses <AgentRoleForm> with ``hiddenFields`` = displayName /
  persona / color and lets the user edit only the three dimensions that DO
  vary: model (``coordinatorModelId``), tools + skills (both flow through
  ``coordinatorConfig``).

  Save writes both dimensions in ONE PATCH: ``coordinatorModelId`` +
  ``coordinatorConfig = { allowedTools, enabledSkills }`` — the backend's
  ``SetDiscussionConfigUseCase`` accepts them together. "Reset to defaults"
  clears ``coordinatorConfig`` back to ``null`` so the backend applies its
  dynamic defaults (all registered tools minus ``agent`` + every globally
  enabled skill) — new tools / skills added later automatically extend the
  coordinator without a manual UI toggle.

  Seeding:
    * When ``coordinatorConfig`` is null the visual seed is the FULL
      catalog / skill list, so every chip is checked and the user narrows by
      unchecking. When the catalog / skill store resolves AFTER mount we
      re-seed the dimension — but only while the persisted override for that
      dimension is still absent, so we never clobber an in-flight edit.
    * An explicit override (including an intentional empty list) is honoured
      verbatim.
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useToast } from "@/composables/useToast";
import { useDiscussion } from "@/composables/chat/useDiscussion";
import AgentRoleForm, {
  type RoleFormData,
} from "@/components/chat/AgentRoleForm.vue";
import { fetchChatTools } from "@/api/chatTools";
import { useSkillsStore } from "@/stores/skills";
import type { CoordinatorConfig } from "@/stores/_chatTabsTypes";

const emit = defineEmits<{
  (e: "close"): void;
}>();

const { t } = useI18n();
const toast = useToast();
const discussion = useDiscussion();
const skillsStore = useSkillsStore();

/** The ``skill`` meta-tool is not chosen alongside ordinary tools — its
 *  activation is derived from a non-empty ``enabled_skills``. Kept out of the
 *  tool draft (mirrors :file:`AgentRoleForm.vue` behaviour). */
const SKILL_TOOL_NAME = "skill";
/** Baseline advertised in the tool grid before the live catalog resolves. */
const FALLBACK_TOOL_CATALOG: readonly string[] = [
  "read",
  "edit",
  "write",
  "exec",
  "glob",
  "grep",
  "web_fetch",
  "agent",
  "todowrite",
  "question",
];

// ── Load the live tool catalog so the "everything on" seed matches the exact
// set the coordinator would advertise on the wire ───────────────────────────
const toolCatalog = ref<readonly string[]>(FALLBACK_TOOL_CATALOG);
const toolCatalogLoaded = ref(false);

async function loadToolCatalog(): Promise<void> {
  try {
    const res = await fetchChatTools();
    const names = Array.isArray(res?.tools)
      ? res.tools
          .filter((td) => td.available_in_discussion)
          .map((td) => td.name)
          .filter((n) => n !== SKILL_TOOL_NAME)
      : [];
    if (names.length > 0) toolCatalog.value = names;
  } catch {
    // Non-fatal — keep the fallback list.
  } finally {
    toolCatalogLoaded.value = true;
  }
}

// ── Draft state — reused by <AgentRoleForm> via v-model:value ────────────────
// displayName / persona / color are hidden via ``hiddenFields``; their values
// here are inert placeholders (never displayed, never sent to the wire).
const draft = reactive<RoleFormData>({
  displayName: "",
  modelId: "",
  persona: "",
  allowedTools: [],
  enabledSkills: [],
  color: 0,
});

const cfg = computed(() => discussion.config.value);
const initialCoordinatorConfig = computed<CoordinatorConfig | null>(
  () => cfg.value?.coordinatorConfig ?? null,
);
/** True when the persisted override is fully absent — surfaces the note that
 *  explains the "everything on" default. */
const usingDefaults = computed(
  () =>
    initialCoordinatorConfig.value === null ||
    (initialCoordinatorConfig.value.allowedTools === undefined &&
      initialCoordinatorConfig.value.enabledSkills === undefined),
);

function seedAllowedTools(override: readonly string[] | undefined): string[] {
  return override !== undefined ? [...override] : [...toolCatalog.value];
}
function seedEnabledSkills(override: readonly string[] | undefined): string[] {
  if (override !== undefined) return [...override];
  return skillsStore.enabledSkills.map((s) => s.id);
}

function assignDraft(next: RoleFormData): void {
  draft.displayName = next.displayName;
  draft.modelId = next.modelId;
  draft.persona = next.persona;
  draft.allowedTools = next.allowedTools;
  draft.enabledSkills = next.enabledSkills;
  draft.color = next.color;
}

onMounted(() => {
  const c = cfg.value;
  const override = c?.coordinatorConfig ?? null;
  draft.modelId = c?.coordinatorModelId ?? "";
  draft.allowedTools = seedAllowedTools(override?.allowedTools);
  draft.enabledSkills = seedEnabledSkills(override?.enabledSkills);
  void loadToolCatalog();
  void skillsStore.ensureLoaded();
});

// Re-seed a dimension once its source resolves — but only while the persisted
// override for that dimension is still absent (i.e. the visible chips are
// the dynamic default). If the user has already unchecked something we'd be
// clobbering their edit, so we do nothing.
watch(toolCatalogLoaded, (loaded) => {
  if (!loaded) return;
  if (initialCoordinatorConfig.value?.allowedTools !== undefined) return;
  draft.allowedTools = seedAllowedTools(undefined);
});
watch(
  () => skillsStore.enabledSkills.map((s) => s.id).join("|"),
  () => {
    if (initialCoordinatorConfig.value?.enabledSkills !== undefined) return;
    draft.enabledSkills = seedEnabledSkills(undefined);
  },
);

// ── Save / reset ─────────────────────────────────────────────────────────────

async function save(): Promise<void> {
  const modelChanged = (cfg.value?.coordinatorModelId ?? "") !== draft.modelId;
  try {
    if (modelChanged) {
      await discussion.setCoordinatorModelId(
        draft.modelId.trim() === "" ? null : draft.modelId,
      );
    }
    // Persist the user's explicit checks verbatim — future tool / skill
    // additions do NOT automatically flow to the coordinator; the "Reset to
    // defaults" button is the way to opt back into the dynamic default.
    await discussion.setCoordinatorConfig({
      allowedTools: [...draft.allowedTools],
      enabledSkills: [...draft.enabledSkills],
    });
    toast.success(t("chat.discussion.coordinator.savedToast"));
    emit("close");
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

async function resetToDefaults(): Promise<void> {
  try {
    await discussion.setCoordinatorConfig(null);
    // Re-seed the visible chips from the freshly-cleared override.
    draft.allowedTools = seedAllowedTools(undefined);
    draft.enabledSkills = seedEnabledSkills(undefined);
    toast.success(t("chat.discussion.coordinator.resetToast"));
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}
</script>

<template>
  <section class="coord-editor" data-testid="coordinator-editor">
    <p class="coord-editor-title">
      {{ t("chat.discussion.coordinator.editTitle") }}
    </p>
    <p
      v-if="usingDefaults"
      class="coord-editor-note"
      data-testid="coordinator-editor-defaults-note"
    >
      {{ t("chat.discussion.coordinator.defaultsNote") }}
    </p>
    <!-- Reuse the shared role editor with identity dimensions hidden. -->
    <AgentRoleForm
      :value="draft"
      :hidden-fields="['displayName', 'persona', 'color']"
      :current-mode-policy="null"
      @update:value="assignDraft"
    />
    <div class="coord-editor-actions">
      <button
        type="button"
        class="coord-editor-btn coord-editor-btn--ghost"
        data-testid="coordinator-editor-reset"
        @click="resetToDefaults"
      >
        {{ t("chat.discussion.coordinator.resetToDefaults") }}
      </button>
      <span class="coord-editor-actions-spacer"></span>
      <button
        type="button"
        class="coord-editor-btn coord-editor-btn--ghost"
        data-testid="coordinator-editor-cancel"
        @click="emit('close')"
      >
        {{ t("common.cancel") }}
      </button>
      <button
        type="button"
        class="coord-editor-btn coord-editor-btn--primary"
        data-testid="coordinator-editor-save"
        @click="save"
      >
        {{ t("common.save") }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.coord-editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  margin-top: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-tertiary);
}
.coord-editor-title {
  margin: 0;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.coord-editor-note {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-secondary);
}
.coord-editor-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
}
.coord-editor-actions-spacer {
  flex: 1;
}
.coord-editor-btn {
  padding: 5px 10px;
  font-size: var(--text-xs);
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  cursor: pointer;
}
.coord-editor-btn--primary {
  background: var(--accent);
  color: #fff;
}
.coord-editor-btn--ghost {
  background: transparent;
  border-color: var(--border);
  color: var(--text-primary);
}
</style>
