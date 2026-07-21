<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  DiscussionPanel.vue — Multi-Agent discussion configuration panel (block-5).

  A self-contained panel (rendered from the chat composer toolbar) that lets the
  user run the active conversation as a multi-Agent discussion (design §2 / §7):

    • toggle discussion mode on/off (会话级开关),
    • manage the named-participant registry (增删改 — 显示名 / model_id / persona /
      每角色允许的工具集 / 颜色),
    • switch the speaker selector (manager ↔ round_robin),
    • set the round cap + toggle the final judge round,
    • "call on" (点名) a participant to speak on the next turn.

  All confirmation prompts use `useConfirm()` (定制对话框) — NEVER
  window.confirm/alert/prompt (AGENTS.md §3.9.2). Colours are theme-aware
  palette tokens (§5.3) resolved via `discussionColorToken`, never hardcoded.

  This is a PURE V2 enhancement (V1 has no multi-Agent discussion — design §1.3),
  protected by AGENTS.md 细则 4-bis. Non-discussion behaviour is untouched.

  Cohesion: the data/CRUD logic lives in `useDiscussion` + the discussion store;
  this component is presentation + local draft-form state only, well under the
  1000-line软上限.
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import { useConfirm } from "@/composables/useConfirm";
import { useToast } from "@/composables/useToast";
import { useDiscussion } from "@/composables/chat/useDiscussion";
import { useCloudModelStatus } from "@/composables/useCloudModelStatus";
import { useCloudModelOptions } from "@/composables/chat/useCloudModelOptions";
import { useTemplateI18n } from "@/composables/chat/useTemplateI18n";
import TemplateLibraryDialog from "@/components/chat/TemplateLibraryDialog.vue";
import AgentRoleForm, {
  type RoleFormData,
} from "@/components/chat/AgentRoleForm.vue";
import { fetchChatTools, type ChatToolDescriptor } from "@/api/chatTools";
import type { ParticipantInput } from "@/stores/discussion";
import { type ModeToolPolicy } from "@/lib/modePolicy";
import {
  useRosterTemplateStore,
  type RosterTemplateMemberView,
  type RosterTemplateView,
} from "@/stores/rosterTemplate";
import {
  useAgentTemplateStore,
  type AgentTemplateView,
} from "@/stores/agentTemplate";
import {
  useModeTemplateStore,
  type ModeTemplateView,
} from "@/stores/modeTemplate";
import {
  discussionColorToken,
  DISCUSSION_PALETTE_SIZE,
  type DiscussionConfig,
  type DiscussionParticipant,
  type SelectorMode,
} from "@/stores/_chatTabsTypes";

const { t } = useI18n();
const { confirm } = useConfirm();
const toast = useToast();
const { resolve: resolveI18n } = useTemplateI18n();
const router = useRouter();
const discussion = useDiscussion();
const rosterTemplates = useRosterTemplateStore();
const agentTemplates = useAgentTemplateStore();
const modeTemplates = useModeTemplateStore();

// ── Cloud-model availability gate (任务 3) ───────────────────────────────────
// Multi-Agent discussion runs exclusively on cloud models. When none are
// configured we disable the master switch + show a guidance banner (the
// useDiscussion guard is the belt-and-braces fallback for auto-enable paths).
const { hasCloudModels, ensureChecked: ensureCloudChecked } =
  useCloudModelStatus();

// ── Shared cloud-model dropdown source for the session-level model knobs ─────
const {
  cloudModelOptions,
  cloudModelLabel,
  loadCloudModels,
  modelMissing: isCloudModelMissing,
} = useCloudModelOptions();

/** Jump to Settings → Cloud Models (mirrors CloudModelOnboarding). */
function goToCloudModels(): void {
  void router.push({ path: "/settings", query: { tab: "cloud-models" } });
}

/**
 * Selectable built-in tools a participant may be granted (design §4.3 / D3).
 *
 * As of 2026-06-21 (user mandate): the full chat-tool registry is exposed —
 * including ``agent`` (sub-agent) and ``question`` (interactive ask). Per-role
 * tool selection is FULLY user-controlled; the back-end no longer hard-blocks
 * any tool for discussions. Sub-agent recursion safety is enforced INSIDE the
 * ``agent`` tool handler (a sub-agent cannot spawn another sub-agent), so
 * letting a role call ``agent`` is safe.
 *
 * The list comes from the LIVE back-end registry (``GET /api/chat/tools``) so
 * a newly-registered tool appears automatically — no front-end rebuild needed.
 * Until the fetch completes (or if it fails), the panel falls back to the
 * baseline set (cold-start render is still useful + non-blocking).
 */
const FALLBACK_TOOLS: readonly string[] = [
  "read",
  "edit",
  "write",
  "exec",
  "glob",
  "grep",
  "webfetch",
  "agent",
  "todowrite",
  "question",
] as const;

const toolCatalog = ref<readonly ChatToolDescriptor[]>([]);
const toolCatalogLoaded = ref(false);

/** The selectable tool names — live registry when loaded, else the fallback
 *  baseline. Used here only to seed a NEW role's default-on tool set
 *  (``defaultAllowedTools``); chip rendering / toggling now lives in
 *  ``AgentRoleForm.vue`` (which loads its own catalog). */
const SELECTABLE_TOOLS = computed<readonly string[]>(() => {
  if (!toolCatalogLoaded.value) return FALLBACK_TOOLS;
  return toolCatalog.value.map((t) => t.name);
});

/** Names of tools that are DEFAULT-ON when creating a brand-new role. The
 *  user mandated "default everything except ``agent`` and ``question`` —
 *  those two are powerful enough that the user should opt them in
 *  explicitly". Mirrors back-end safety defaults without hard-blocking. */
const DEFAULT_OFF_TOOLS = new Set<string>(["agent", "question"]);

function defaultAllowedTools(): string[] {
  return SELECTABLE_TOOLS.value.filter((tool) => !DEFAULT_OFF_TOOLS.has(tool));
}

async function loadToolCatalog(): Promise<void> {
  if (toolCatalogLoaded.value) return;
  try {
    const res = await fetchChatTools();
    // Filter out tools the back-end says are hard-blocked for discussions
    // (currently none, but future-proof). Order is back-end TOOL_ORDER.
    toolCatalog.value = (Array.isArray(res?.tools) ? res.tools : []).filter(
      (t) => t.available_in_discussion,
    );
  } catch {
    // Non-fatal: the fallback list keeps the panel usable when the catalog
    // is unreachable (e.g. transient back-end blip).
    toolCatalog.value = [];
  } finally {
    toolCatalogLoaded.value = true;
  }
}

const SELECTOR_MODES: readonly SelectorMode[] = ["manager", "round_robin"];

onMounted(() => {
  // Load the tool catalog so a brand-new role can seed its default-on set
  // (AgentRoleForm owns chip rendering + cloud-model dropdown internally now).
  void loadToolCatalog();
  // Cloud-model availability gate + the session-level model dropdown source.
  void ensureCloudChecked();
  void loadCloudModels();
});

// ── Participant draft editor state ──────────────────────────────────────────
// A single inline draft form reused for both "create" and "edit". `editingId`
// null ⇒ creating a new participant; a string ⇒ editing that participant.
const showEditor = ref(false);
const editingId = ref<string | null>(null);
const draft = reactive<{
  displayName: string;
  modelId: string;
  persona: string;
  allowedTools: string[];
  enabledSkills: string[];
  color: number;
}>({
  displayName: "",
  modelId: "",
  persona: "",
  allowedTools: [],
  enabledSkills: [],
  color: 0,
});

const config = discussion.config;
const participants = discussion.participants;
const isDiscussion = discussion.isDiscussion;

// ── Roster-template library (reusable "teams"; pure V2 enhancement) ─────────
// Teams + Modes are now created/edited INLINE inside TemplateLibraryDialog
// (one level), so this panel no longer hosts second-tier roster/mode dialogs.

/** The current discussion roster projected into the template member shape, so
 *  "save as team" captures exactly the roles on screen. */
const currentRosterAsMembers = computed<RosterTemplateMemberView[]>(() =>
  participants.value.map((p) => ({
    displayName: p.display_name,
    ...(p.model_id !== undefined && p.model_id !== ""
      ? { modelId: p.model_id }
      : {}),
    ...(p.persona !== undefined && p.persona !== "" ? { persona: p.persona } : {}),
    allowedTools: [...(p.config.allowed_tools ?? [])],
    enabledSkills: [...(p.config.enabled_skills ?? [])],
    ...(typeof p.config.color === "number" ? { color: p.config.color } : {}),
  })),
);

/** Import a template: instantiate its members as named agents on the current
 *  conversation (lazily creating one if needed), then reload the roster so the
 *  panel reflects the freshly-added roles. */
async function importTemplate(tpl: RosterTemplateView): Promise<void> {
  const convId = await discussion.ensureConversation();
  if (convId === null) {
    toast.error(t("chat.discussion.templates.builtinReadonly"));
    return;
  }
  try {
    const result = await rosterTemplates.applyToConversation(tpl.id, convId);
    await discussion.reload();
    // Importing a roster implies discussion mode is wanted — turn it on.
    if (!isDiscussion.value) await discussion.setDiscussionEnabled(true);
    toast.success(
      t("chat.discussion.templates.imported", { n: result.membersAdded }),
    );
    // If the team carried a bound default mode that resolved + was selected,
    // tell the user the collaboration mode was applied too.
    if (result.appliedModeName) {
      toast.success(
        t("chat.discussion.templates.modeApplied", {
          name: result.appliedModeName,
        }),
      );
    }
    showLibrary.value = false;
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

/** Import a single-role agent template: instantiate it as one named agent on
 *  the current conversation (lazily creating one if needed), then reload the
 *  roster so the panel reflects the freshly-added role. */
async function importAgentTemplate(tpl: AgentTemplateView): Promise<void> {
  const convId = await discussion.ensureConversation();
  if (convId === null) {
    toast.error(t("chat.discussion.agentTemplates.builtinReadonly"));
    return;
  }
  try {
    await agentTemplates.applyToConversation(tpl.id, convId);
    await discussion.reload();
    if (!isDiscussion.value) await discussion.setDiscussionEnabled(true);
    toast.success(t("chat.discussion.agentTemplates.imported"));
    showLibrary.value = false;
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

// ── Collaboration mode selector (§26/§27 V1) ───────────────────────────────
// The dropdown lets the user pick a mode for this conversation. Selecting a
// mode is the USER-EXPLICIT privilege action (§26.4) — the classifier never
// switches modes on its own. "" = no mode → existing discussion behaviour.
const selectedModeId = computed<string>(
  () => discussion.config.value?.selectedModeId ?? "",
);
const modeOptions = computed(() => modeTemplates.templates);

/** The currently-selected mode's full view (for chip dimming + name). */
const selectedMode = computed<ModeTemplateView | null>(
  () =>
    modeTemplates.templates.find((m) => m.id === selectedModeId.value) ?? null,
);
/** Tool policy of the selected mode for chip dimming (decision 2 / §3.6).
 *  null when no mode is selected (sentinel) → AgentRoleForm never dims. */
const selectedModePolicy = computed<ModeToolPolicy | null>(
  () => (selectedMode.value?.toolPolicy as ModeToolPolicy) ?? null,
);
const selectedModeName = computed<string | null>(() =>
  selectedMode.value ? modeName(selectedMode.value) : null,
);

/** Localised mode name for built-in presets (custom modes fall back to own
 *  name). Display layer only — see useTemplateI18n. */
function modeName(m: ModeTemplateView): string {
  return resolveI18n(m.nameI18n, m.name);
}

onMounted(async () => {
  try {
    await modeTemplates.fetchAll();
  } catch {
    // Non-fatal: the selector falls back to "no mode" (existing behaviour).
  }
});

async function onModeChange(event: Event): Promise<void> {
  const id = (event.target as HTMLSelectElement).value;
  if (!id) return;
  try {
    await modeTemplates.applyToConversation(
      id,
      (await discussion.ensureConversation()) ?? "",
    );
    await discussion.reload();
    if (!isDiscussion.value) await discussion.setDiscussionEnabled(true);
    const applied = modeTemplates.templates.find((m) => m.id === id);
    const name = applied ? modeName(applied) : id;
    toast.success(t("chat.discussion.modes.selected", { name }));
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

// ── Unified template library dialog (§27 decision 2: one panel, three tabs) ─
const showLibrary = ref(false);
// Which library tab to return to when a manage-* sub-dialog emits `back`
// (and which initial tab to open the library on after returning).
const libraryReturnTab = ref<"agent" | "roster" | "mode">("agent");

async function selectModeFromLibrary(m: ModeTemplateView): Promise<void> {
  const convId = await discussion.ensureConversation();
  if (convId === null) return;
  try {
    await modeTemplates.applyToConversation(m.id, convId);
    await discussion.reload();
    if (!isDiscussion.value) await discussion.setDiscussionEnabled(true);
    toast.success(t("chat.discussion.modes.selected", { name: modeName(m) }));
    showLibrary.value = false;
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

/** The colour token for a participant (palette index → CSS var). */
function participantColor(p: DiscussionParticipant, idx: number): string {
  const c = p.config.color;
  return discussionColorToken(typeof c === "number" ? c : idx);
}

/** Apply a RoleFormData update emitted by AgentRoleForm back into `draft`. */
function onDraftUpdate(next: RoleFormData): void {
  draft.displayName = next.displayName;
  draft.modelId = next.modelId;
  draft.persona = next.persona;
  draft.allowedTools = next.allowedTools;
  draft.enabledSkills = next.enabledSkills;
  draft.color = next.color;
}

function resetDraft(): void {
  draft.displayName = "";
  draft.modelId = "";
  draft.persona = "";
  // User mandate (2026-06-21): default-on every selectable tool EXCEPT
  // ``agent`` + ``question`` (powerful enough to warrant explicit opt-in).
  // Existing edit flow (``openEdit``) overrides this with the participant's
  // persisted list, so existing roles keep their saved tool set unchanged.
  // (AgentRoleForm itself imposes NO default — the caller seeds it, §7.9/§7.17.)
  draft.allowedTools = defaultAllowedTools();
  // A brand-new role has NO skill by default (design: role config defaults to
  // selecting no skill; the user opts in per role from the enabled pool).
  draft.enabledSkills = [];
  // Default the new participant's colour to the next palette slot.
  draft.color = participants.value.length % DISCUSSION_PALETTE_SIZE;
}

function openCreate(): void {
  void loadToolCatalog();
  editingId.value = null;
  resetDraft();
  showEditor.value = true;
}

function openEdit(p: DiscussionParticipant): void {
  void loadToolCatalog();
  editingId.value = p.id;
  draft.displayName = p.display_name;
  draft.modelId = p.model_id ?? "";
  draft.persona = p.persona ?? "";
  draft.allowedTools = [...(p.config.allowed_tools ?? [])];
  draft.enabledSkills = [...(p.config.enabled_skills ?? [])];
  draft.color =
    typeof p.config.color === "number"
      ? p.config.color
      : participants.value.findIndex((x) => x.id === p.id);
  showEditor.value = true;
}

function cancelEditor(): void {
  showEditor.value = false;
  editingId.value = null;
}

const canSaveDraft = computed(
  () => draft.displayName.trim() !== "" && draft.modelId.trim() !== "",
);

async function saveDraft(): Promise<void> {
  if (!canSaveDraft.value) return;
  const input: ParticipantInput = {
    display_name: draft.displayName.trim(),
    ...(draft.modelId.trim() !== "" ? { model_id: draft.modelId.trim() } : {}),
    ...(draft.persona.trim() !== "" ? { persona: draft.persona.trim() } : {}),
    config: {
      allowed_tools: [...draft.allowedTools],
      enabled_skills: [...draft.enabledSkills],
      color: draft.color,
    },
  };
  if (editingId.value === null) {
    await discussion.addParticipant(input);
  } else {
    await discussion.editParticipant(editingId.value, input);
  }
  if (discussion.error.value === null) {
    showEditor.value = false;
    editingId.value = null;
  }
}

async function confirmRemove(p: DiscussionParticipant): Promise<void> {
  // §3.9.2: custom confirm dialog — NEVER window.confirm.
  const ok = await confirm({
    icon: "🗑️",
    title: t("chat.discussion.removeTitle"),
    message: t("chat.discussion.removeMessage", { name: p.display_name }),
    confirmText: t("common.delete"),
    cancelText: t("common.cancel"),
    confirmStyle: "danger",
  });
  if (ok) await discussion.removeParticipant(p.id);
}

function onToggleDiscussion(e: Event): void {
  const enabled = (e.target as HTMLInputElement).checked;
  void discussion.setDiscussionEnabled(enabled);
}

function onSelectorChange(mode: SelectorMode): void {
  void discussion.setSelectorMode(mode);
}

function onMaxRoundsInput(e: Event): void {
  const v = Number((e.target as HTMLInputElement).value);
  if (Number.isFinite(v)) void discussion.setMaxRounds(v);
}

function onToggleJudge(e: Event): void {
  void discussion.setEnableJudge((e.target as HTMLInputElement).checked);
}

// ── Discussion convergence controls (DISC-2 §22A.8) ─────────────────────────
// Small, single-purpose change handlers (no inline logic in the template).
function onToggleConvergenceControl(e: Event): void {
  void discussion.setConvergenceControlEnabled(
    (e.target as HTMLInputElement).checked,
  );
}

function onToggleManagerEarlyEnd(e: Event): void {
  void discussion.setManagerEarlyEndEnabled(
    (e.target as HTMLInputElement).checked,
  );
}

function onToggleSoftStop(e: Event): void {
  void discussion.setSoftStopEnabled((e.target as HTMLInputElement).checked);
}

function onSoftStopModeChange(e: Event): void {
  void discussion.setSoftStopMode((e.target as HTMLSelectElement).value);
}

/** Soft-stop strategies offered in the dropdown. Only "conservative" ships in
 *  P1-step1; richer strategies (balanced / aggressive…) land in P1-step4 once
 *  the backend exposes them. */
const SOFT_STOP_MODES: readonly string[] = ["conservative"];

function onSocialResponsePolicyChange(e: Event): void {
  void discussion.setSocialResponsePolicy(
    (e.target as HTMLSelectElement).value,
  );
}

/** Social / lightweight-path response policies (DISC-2 §22A.7). Shape a
 *  greeting / thanks reply: silent (no reply), single_brief_reply (default =
 *  phase-1), single_closing_reply (closing tone), continue_last_topic. */
const SOCIAL_RESPONSE_POLICIES: readonly string[] = [
  "silent",
  "single_brief_reply",
  "single_closing_reply",
  "continue_last_topic",
  "random",
  "ai_decide",
];

function onDiscussionPromptInput(e: Event): void {
  void discussion.setDiscussionPrompt((e.target as HTMLTextAreaElement).value);
}

function onManagerPromptAppendInput(e: Event): void {
  void discussion.setManagerPromptAppend((e.target as HTMLTextAreaElement).value);
}

// ── Smart collaboration (DISC-1 §22.7 / DISC-2 §22A.5) ──────────────────────
function onToggleImplementationEnabled(e: Event): void {
  void discussion.setImplementationEnabled(
    (e.target as HTMLInputElement).checked,
  );
}

function onToggleIntentClassifierEnabled(e: Event): void {
  void discussion.setIntentClassifierEnabled(
    (e.target as HTMLInputElement).checked,
  );
}

// DISC-1 三期-step5 — OPTIONAL implementation item validator toggle.
function onToggleImplementationValidatorEnabled(e: Event): void {
  void discussion.setImplementationValidatorEnabled(
    (e.target as HTMLInputElement).checked,
  );
}

// ── Advanced parameters (DISC-1 TODO-2) ─────────────────────────────────────
type NumericKnobKey =
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
  | "implementationVerifyCommandTimeoutMs";
type ModelKnobKey = "intentClassifierModel" | "implementationPlannerModel";

interface NumberRow {
  key: NumericKnobKey;
  label: string;
  hint: string;
  testid: string;
  min: number;
  max: number;
  step: number;
  /**
   * Optional UI display unit divisor. The stored/wire value stays in its base
   * unit (seconds for runtime, milliseconds for the timeouts); the input box
   * shows `stored / displayDivisor` and writes back `entered * displayDivisor`.
   * `min`/`max`/`step` on rows that set this are expressed in DISPLAY units.
   * Omit (= 1) for rows whose stored value is already the displayed value.
   */
  displayDivisor?: number;
}
interface ModelRow {
  key: ModelKnobKey;
  label: string;
  hint: string;
  testid: string;
}

const budgetRows: readonly NumberRow[] = [
  { key: "implMaxTotalFileEdits", label: "chat.discussion.advanced.maxFileEdits", hint: "chat.discussion.advanced.maxFileEditsHint", testid: "discussion-budget-file-edits", min: 1, max: 100000, step: 1 },
  { key: "implMaxTotalExecCalls", label: "chat.discussion.advanced.maxExecCalls", hint: "chat.discussion.advanced.maxExecCallsHint", testid: "discussion-budget-exec-calls", min: 1, max: 100000, step: 1 },
  { key: "implMaxTotalRuntimeSeconds", label: "chat.discussion.advanced.maxRuntime", hint: "chat.discussion.advanced.maxRuntimeHint", testid: "discussion-budget-runtime", min: 1, max: 1440, step: 1, displayDivisor: 60 },
  { key: "implMaxTotalChangedFiles", label: "chat.discussion.advanced.maxChangedFiles", hint: "chat.discussion.advanced.maxChangedFilesHint", testid: "discussion-budget-changed-files", min: 1, max: 100000, step: 1 },
];
const softStopRows: readonly NumberRow[] = [
  { key: "softStopSimilarity", label: "chat.discussion.advanced.softStopSimilarity", hint: "chat.discussion.advanced.softStopSimilarityHint", testid: "discussion-soft-stop-similarity", min: 0.5, max: 0.99, step: 0.01 },
  { key: "softStopMinRounds", label: "chat.discussion.advanced.softStopMinRounds", hint: "chat.discussion.advanced.softStopMinRoundsHint", testid: "discussion-soft-stop-min-rounds", min: 1, max: 50, step: 1 },
  { key: "softStopConsecutiveTurns", label: "chat.discussion.advanced.softStopConsecutive", hint: "chat.discussion.advanced.softStopConsecutiveHint", testid: "discussion-soft-stop-consecutive", min: 1, max: 10, step: 1 },
];
const modelRows: readonly ModelRow[] = [
  { key: "intentClassifierModel", label: "chat.discussion.advanced.classifierModel", hint: "chat.discussion.advanced.classifierModelHint", testid: "discussion-classifier-model" },
  { key: "implementationPlannerModel", label: "chat.discussion.advanced.plannerModel", hint: "chat.discussion.advanced.plannerModelHint", testid: "discussion-planner-model" },
];
const timeoutRows: readonly NumberRow[] = [
  { key: "intentClassifierTimeoutMs", label: "chat.discussion.advanced.classifierTimeout", hint: "chat.discussion.advanced.classifierTimeoutHint", testid: "discussion-classifier-timeout", min: 0.2, max: 60, step: 0.1, displayDivisor: 1000 },
  { key: "implementationPlannerTimeoutMs", label: "chat.discussion.advanced.plannerTimeout", hint: "chat.discussion.advanced.plannerTimeoutHint", testid: "discussion-planner-timeout", min: 0.5, max: 120, step: 0.1, displayDivisor: 1000 },
  { key: "implementationValidatorTimeoutMs", label: "chat.discussion.advanced.validatorTimeout", hint: "chat.discussion.advanced.validatorTimeoutHint", testid: "discussion-validator-timeout", min: 0.5, max: 120, step: 0.1, displayDivisor: 1000 },
  { key: "implementationVerifyCommandTimeoutMs", label: "chat.discussion.advanced.verifyTimeout", hint: "chat.discussion.advanced.verifyTimeoutHint", testid: "discussion-verify-timeout", min: 1, max: 600, step: 1, displayDivisor: 1000 },
];

/**
 * The number to SHOW in the input box for `row`: the stored base-unit value
 * divided by the row's display divisor (e.g. seconds→minutes, ms→seconds).
 * Rows without a divisor display their stored value unchanged.
 */
function displayValue(row: NumberRow): number {
  const cfg = config.value;
  if (cfg == null) return row.min;
  const stored = Number((cfg as DiscussionConfig)[row.key]);
  const divisor = row.displayDivisor ?? 1;
  if (!Number.isFinite(stored)) return stored;
  if (divisor === 1) return stored;
  // Trim float noise from the division (e.g. 8000/1000 → 8, not 8.0000001).
  return Math.round((stored / divisor) * 1000) / 1000;
}

function onTunableInput(row: NumberRow, e: Event): void {
  const entered = Number((e.target as HTMLInputElement).value);
  if (!Number.isFinite(entered)) return;
  const divisor = row.displayDivisor ?? 1;
  // Convert the displayed unit back to the stored base unit (minutes→seconds,
  // seconds→ms). Round so the wire value stays an integer (DTO is `int`).
  const stored = divisor === 1 ? entered : Math.round(entered * divisor);
  void discussion.setTunable(row.key, stored);
}
function onModelTunableSelect(key: ModelKnobKey, e: Event): void {
  void discussion.setModelTunable(key, (e.target as HTMLSelectElement).value);
}

function isPinned(id: string): boolean {
  return discussion.pinnedSpeaker.value === id;
}

function onCallOn(id: string): void {
  // Toggle: click again to clear the call-on.
  discussion.callOn(isPinned(id) ? null : id);
}
</script>

<template>
  <div class="discussion-panel" data-testid="discussion-panel">
    <!-- ── No-cloud-model guidance banner (任务 3) ── -->
    <div
      v-if="!hasCloudModels"
      class="discussion-no-cloud"
      role="note"
      data-testid="discussion-no-cloud-banner"
    >
      <span class="discussion-no-cloud-text">{{
        t("chat.discussion.noCloudModels.banner")
      }}</span>
      <button
        type="button"
        class="discussion-no-cloud-cta"
        data-testid="discussion-no-cloud-cta"
        @click="goToCloudModels"
      >
        {{ t("chat.discussion.noCloudModels.cta") }}
      </button>
    </div>

    <!-- ── Discussion mode master switch ── -->
    <div class="discussion-row discussion-row--switch">
      <label class="discussion-switch-label">
        <span class="discussion-switch-title">{{ t("chat.discussion.title") }}</span>
        <span class="discussion-switch-hint">{{ t("chat.discussion.hint") }}</span>
      </label>
      <label class="toggle">
        <input
          type="checkbox"
          data-testid="discussion-toggle"
          :checked="isDiscussion"
          :disabled="!hasCloudModels"
          @change="onToggleDiscussion"
        />
        <span class="toggle-slider"></span>
      </label>
    </div>

    <!-- ── Settings (visible only when discussion is ON) ── -->
    <template v-if="isDiscussion && config">
      <!-- Collaboration mode (§26/§27 V1) -->
      <div class="discussion-row">
        <span class="discussion-label">{{ t("chat.discussion.modes.label") }}</span>
        <select
          class="discussion-mode-select"
          data-testid="discussion-mode-select"
          :value="selectedModeId"
          @change="onModeChange"
        >
          <option value="">{{ t("chat.discussion.modes.none") }}</option>
          <option v-for="m in modeOptions" :key="m.id" :value="m.id">
            {{ modeName(m) }}
          </option>
        </select>
      </div>

      <!-- Selector mode -->
      <div class="discussion-row">
        <span class="discussion-label">{{ t("chat.discussion.selectorMode") }}</span>
        <div class="discussion-selector-group">
          <button
            v-for="mode in SELECTOR_MODES"
            :key="mode"
            type="button"
            class="discussion-selector-btn"
            :class="{ 'is-active': config.selectorMode === mode }"
            :data-testid="`discussion-selector-${mode}`"
            @click="onSelectorChange(mode)"
          >
            {{ t(`chat.discussion.selector.${mode}`) }}
          </button>
        </div>
      </div>

      <!-- Round cap -->
      <div class="discussion-row">
        <span class="discussion-label">{{ t("chat.discussion.maxRounds") }}</span>
        <input
          type="number"
          min="1"
          max="50"
          class="discussion-rounds-input"
          data-testid="discussion-max-rounds"
          :value="config.maxRounds"
          @change="onMaxRoundsInput"
        />
      </div>

      <!-- Judge toggle -->
      <div class="discussion-row">
        <label class="discussion-label">{{ t("chat.discussion.enableJudge") }}</label>
        <label class="toggle">
          <input
            type="checkbox"
            data-testid="discussion-judge-toggle"
            :checked="config.enableJudge"
            @change="onToggleJudge"
          />
          <span class="toggle-slider"></span>
        </label>
      </div>

      <!-- ── Discussion convergence optimisation (DISC-2 §22A.8) ── -->
      <div class="discussion-convergence">
        <span class="discussion-convergence-title">{{
          t("chat.discussion.convergence.title")
        }}</span>
        <p class="discussion-advanced-note">
          {{ t("chat.discussion.convergence.note") }}
        </p>

        <!-- Master switch -->
        <div class="discussion-row">
          <label class="discussion-label">{{
            t("chat.discussion.convergence.controlEnabled")
          }}</label>
          <label class="toggle">
            <input
              type="checkbox"
              data-testid="discussion-convergence-toggle"
              :checked="config.convergenceControlEnabled"
              @change="onToggleConvergenceControl"
            />
            <span class="toggle-slider"></span>
          </label>
        </div>

        <!-- Manager may end early (disabled while the master switch is OFF) -->
        <div class="discussion-row">
          <label class="discussion-label">{{
            t("chat.discussion.convergence.managerEarlyEnd")
          }}</label>
          <label class="toggle">
            <input
              type="checkbox"
              data-testid="discussion-manager-early-end-toggle"
              :checked="config.managerEarlyEndEnabled"
              :disabled="!config.convergenceControlEnabled"
              @change="onToggleManagerEarlyEnd"
            />
            <span class="toggle-slider"></span>
          </label>
        </div>

        <!-- Soft-stop repeated turns -->
        <div class="discussion-row">
          <label class="discussion-label">{{
            t("chat.discussion.convergence.softStop")
          }}</label>
          <label class="toggle">
            <input
              type="checkbox"
              data-testid="discussion-soft-stop-toggle"
              :checked="config.softStopEnabled"
              :disabled="!config.convergenceControlEnabled"
              @change="onToggleSoftStop"
            />
            <span class="toggle-slider"></span>
          </label>
        </div>

        <!-- Soft-stop strategy -->
        <div class="discussion-row">
          <span class="discussion-label">{{
            t("chat.discussion.convergence.softStopMode")
          }}</span>
          <select
            class="discussion-mode-select"
            data-testid="discussion-soft-stop-mode-select"
            :value="config.softStopMode"
            :disabled="!config.convergenceControlEnabled || !config.softStopEnabled"
            @change="onSoftStopModeChange"
          >
            <option v-for="m in SOFT_STOP_MODES" :key="m" :value="m">
              {{ t(`chat.discussion.convergence.softStopModes.${m}`) }}
            </option>
          </select>
        </div>

        <!-- Social / lightweight-path response policy (DISC-2 §22A.7) -->
        <div class="discussion-row">
          <span class="discussion-label">{{
            t("chat.discussion.convergence.socialResponsePolicy")
          }}</span>
          <select
            class="discussion-mode-select"
            data-testid="discussion-social-response-policy-select"
            :value="config.socialResponsePolicy"
            @change="onSocialResponsePolicyChange"
          >
            <option v-for="p in SOCIAL_RESPONSE_POLICIES" :key="p" :value="p">
              {{ t(`chat.discussion.convergence.socialResponsePolicies.${p}`) }}
            </option>
          </select>
        </div>
      </div>

      <!-- ── Smart collaboration (DISC-1 §22.7 / DISC-2 §22A.5) ── -->
      <div class="discussion-convergence">
        <span class="discussion-convergence-title">{{
          t("chat.discussion.smart.title")
        }}</span>

        <!-- Discussion → implementation master switch (DISC-1 §22.7) -->
        <div class="discussion-row discussion-row--with-hint">
          <label class="discussion-switch-label">
            <span class="discussion-label">{{
              t("chat.discussion.smart.implementationEnabled")
            }}</span>
            <span class="discussion-switch-hint">{{
              t("chat.discussion.smart.implementationEnabledHint")
            }}</span>
          </label>
          <label class="toggle">
            <input
              type="checkbox"
              data-testid="discussion-implementation-toggle"
              :checked="config.implementationEnabled"
              @change="onToggleImplementationEnabled"
            />
            <span class="toggle-slider"></span>
          </label>
        </div>

        <!-- LLM grey-zone intent classifier (DISC-2 §22A.5) -->
        <div class="discussion-row discussion-row--with-hint">
          <label class="discussion-switch-label">
            <span class="discussion-label">{{
              t("chat.discussion.smart.intentClassifierEnabled")
            }}</span>
            <span class="discussion-switch-hint">{{
              t("chat.discussion.smart.intentClassifierEnabledHint")
            }}</span>
          </label>
          <label class="toggle">
            <input
              type="checkbox"
              data-testid="discussion-intent-classifier-toggle"
              :checked="config.intentClassifierEnabled"
              @change="onToggleIntentClassifierEnabled"
            />
            <span class="toggle-slider"></span>
          </label>
        </div>

        <!-- DISC-1 三期-step5: optional implementation item validator -->
        <div class="discussion-row discussion-row--with-hint">
          <label class="discussion-switch-label">
            <span class="discussion-label">{{
              t("chat.discussion.smart.validatorEnabled")
            }}</span>
            <span class="discussion-switch-hint">{{
              t("chat.discussion.smart.validatorEnabledHint")
            }}</span>
          </label>
          <label class="toggle">
            <input
              type="checkbox"
              data-testid="discussion-validator-toggle"
              :checked="config.implementationValidatorEnabled"
              @change="onToggleImplementationValidatorEnabled"
            />
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>

      <!-- ── Advanced parameters (DISC-1 TODO-2) — collapsed by default ── -->
      <details class="discussion-advanced">
        <summary class="discussion-advanced-summary">
          {{ t("chat.discussion.advanced.title") }}
        </summary>
        <p class="discussion-advanced-note">
          {{ t("chat.discussion.advanced.note") }}
        </p>

        <!-- Run-level implementation budget (§22.5) -->
        <div class="discussion-advanced-group">
          {{ t("chat.discussion.advanced.budgetTitle") }}
        </div>
        <div
          v-for="row in budgetRows"
          :key="row.key"
          class="discussion-row discussion-row--with-hint"
        >
          <label class="discussion-switch-label">
            <span class="discussion-label">{{ t(row.label) }}</span>
            <span class="discussion-switch-hint">{{ t(row.hint) }}</span>
          </label>
          <input
            type="number"
            class="discussion-rounds-input"
            :min="row.min"
            :max="row.max"
            :step="row.step"
            :data-testid="row.testid"
            :value="displayValue(row)"
            @change="onTunableInput(row, $event)"
          />
        </div>

        <!-- Soft-stop thresholds (§22A.4) -->
        <div class="discussion-advanced-group">
          {{ t("chat.discussion.advanced.softStopTitle") }}
        </div>
        <div
          v-for="row in softStopRows"
          :key="row.key"
          class="discussion-row discussion-row--with-hint"
        >
          <label class="discussion-switch-label">
            <span class="discussion-label">{{ t(row.label) }}</span>
            <span class="discussion-switch-hint">{{ t(row.hint) }}</span>
          </label>
          <input
            type="number"
            class="discussion-rounds-input"
            :min="row.min"
            :max="row.max"
            :step="row.step"
            :data-testid="row.testid"
            :value="displayValue(row)"
            @change="onTunableInput(row, $event)"
          />
        </div>

        <!-- Models + timeouts (§22A.5 / §22.4) -->
        <div class="discussion-advanced-group">
          {{ t("chat.discussion.advanced.modelsTitle") }}
        </div>
        <div
          v-for="row in modelRows"
          :key="row.key"
          class="discussion-row discussion-row--with-hint"
        >
          <label class="discussion-switch-label">
            <span class="discussion-label">{{ t(row.label) }}</span>
            <span class="discussion-switch-hint">{{ t(row.hint) }}</span>
          </label>
          <select
            class="discussion-model-input"
            :data-testid="row.testid"
            :value="(config as DiscussionConfig)[row.key]"
            @change="onModelTunableSelect(row.key, $event)"
          >
            <!-- Session-level model knobs are OPTIONAL: empty ⇒ backend ladder
                 picks (任务 4a). Only the role model + Mode system model are
                 mandatory. -->
            <option value="">
              {{ t("chat.discussion.advanced.modelAutoPlaceholder") }}
            </option>
            <option
              v-for="m in cloudModelOptions"
              :key="m.model_id"
              :value="m.model_id"
            >
              {{ cloudModelLabel(m) }}
            </option>
            <option
              v-if="isCloudModelMissing((config as DiscussionConfig)[row.key])"
              :value="(config as DiscussionConfig)[row.key]"
            >
              {{ (config as DiscussionConfig)[row.key] }}
            </option>
          </select>
        </div>
        <div
          v-for="row in timeoutRows"
          :key="row.key"
          class="discussion-row discussion-row--with-hint"
        >
          <label class="discussion-switch-label">
            <span class="discussion-label">{{ t(row.label) }}</span>
            <span class="discussion-switch-hint">{{ t(row.hint) }}</span>
          </label>
          <input
            type="number"
            class="discussion-rounds-input"
            :min="row.min"
            :max="row.max"
            :step="row.step"
            :data-testid="row.testid"
            :value="displayValue(row)"
            @change="onTunableInput(row, $event)"
          />
        </div>
      </details>
      <!-- Discussion framing prompt (§18.1) -->
      <div class="discussion-row discussion-row--column">
        <label class="discussion-label" for="discussion-prompt-input">{{
          t("chat.discussion.discussionPrompt")
        }}</label>
        <textarea
          id="discussion-prompt-input"
          class="discussion-prompt-input"
          rows="4"
          data-testid="discussion-prompt"
          :value="config.discussionPrompt ?? ''"
          :placeholder="t('chat.discussion.discussionPromptPlaceholder')"
          @change="onDiscussionPromptInput"
        ></textarea>
      </div>

      <!-- Manager scheduling-preference append (DISC-2 §22A.7 P4-step2).
           Manager-selector mode only: an advisory preference appended to the END
           of the moderator prompt (the protocol segment always precedes it). -->
      <div
        v-if="config.selectorMode === 'manager'"
        class="discussion-row discussion-row--column"
      >
        <label
          class="discussion-label"
          for="discussion-manager-prompt-append-input"
          >{{ t("chat.discussion.managerPromptAppend") }}</label
        >
        <textarea
          id="discussion-manager-prompt-append-input"
          class="discussion-prompt-input"
          rows="3"
          maxlength="2000"
          data-testid="discussion-manager-prompt-append"
          :value="config.managerPromptAppend ?? ''"
          :placeholder="t('chat.discussion.managerPromptAppendPlaceholder')"
          @change="onManagerPromptAppendInput"
        ></textarea>
      </div>

      <!-- ── Participant registry ── -->
      <div class="discussion-participants">
        <div class="discussion-participants-header">
          <span class="discussion-label">{{ t("chat.discussion.participants") }}</span>
          <div class="discussion-participants-header-actions">
            <button
              type="button"
              class="discussion-add-btn discussion-add-btn--ghost"
              data-testid="discussion-open-library"
              @click="showLibrary = true"
            >
              📚 {{ t("chat.discussion.library.open") }}
            </button>
            <button
              type="button"
              class="discussion-add-btn"
              data-testid="discussion-add-participant"
              @click="openCreate"
            >
              + {{ t("chat.discussion.addParticipant") }}
            </button>
          </div>
        </div>

        <p
          v-if="participants.length === 0"
          class="discussion-empty"
          data-testid="discussion-empty"
        >
          {{ t("chat.discussion.empty") }}
        </p>

        <ul v-else class="discussion-list" data-testid="discussion-list">
          <li
            v-for="(p, idx) in participants"
            :key="p.id"
            class="discussion-item"
            data-testid="discussion-participant"
          >
            <span
              class="discussion-item-avatar"
              :style="{ background: participantColor(p, idx) }"
              aria-hidden="true"
            >{{ p.display_name.trim().charAt(0).toUpperCase() || "#" }}</span>
            <div class="discussion-item-body">
              <span class="discussion-item-name">{{ p.display_name }}</span>
              <span class="discussion-item-meta">
                {{ p.model_id || t("chat.discussion.defaultModel") }}
                <template v-if="(p.config.allowed_tools?.length ?? 0) > 0">
                  · 🔧 {{ p.config.allowed_tools?.length }}
                </template>
              </span>
            </div>
            <div class="discussion-item-actions">
              <button
                type="button"
                class="discussion-icon-btn"
                :class="{ 'is-active': isPinned(p.id) }"
                :title="t('chat.discussion.callOn')"
                :data-testid="`discussion-callon-${p.id}`"
                @click="onCallOn(p.id)"
              >
                📣
              </button>
              <button
                type="button"
                class="discussion-icon-btn"
                :title="t('common.edit')"
                :data-testid="`discussion-edit-${p.id}`"
                @click="openEdit(p)"
              >
                ✏️
              </button>
              <button
                type="button"
                class="discussion-icon-btn discussion-icon-btn--danger"
                :title="t('common.delete')"
                :data-testid="`discussion-remove-${p.id}`"
                @click="confirmRemove(p)"
              >
                🗑️
              </button>
            </div>
          </li>
        </ul>

        <!-- ── Inline create / edit editor ── -->
        <div
          v-if="showEditor"
          class="discussion-editor"
          data-testid="discussion-editor"
        >
          <!-- Shared role form (display name / model / persona / tools / colour).
               Chip dimming reflects the currently-selected mode (decision 2);
               sentinel (no mode) → currentModePolicy=null → never dims. -->
          <AgentRoleForm
            :value="draft"
            :current-mode-policy="selectedModePolicy"
            :current-mode-name="selectedModeName"
            @update:value="onDraftUpdate"
          />

          <div class="discussion-editor-actions">
            <button
              type="button"
              class="btn"
              data-testid="discussion-draft-cancel"
              @click="cancelEditor"
            >
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              class="btn btn-primary"
              :disabled="!canSaveDraft"
              data-testid="discussion-draft-save"
              @click="saveDraft"
            >
              {{ editingId === null ? t("common.add") : t("common.save") }}
            </button>
          </div>
        </div>
      </div>

      <p
        v-if="discussion.error.value"
        class="discussion-error"
        data-testid="discussion-error"
      >
        {{ discussion.error.value }}
      </p>
    </template>

    <TemplateLibraryDialog
      v-if="showLibrary"
      :selected-mode-id="selectedModeId"
      :initial-tab="libraryReturnTab"
      :current-roster="currentRosterAsMembers"
      @close="showLibrary = false"
      @import-agent="
        (a) => {
          showLibrary = false;
          importAgentTemplate(a);
        }
      "
      @import-roster="
        (r) => {
          showLibrary = false;
          importTemplate(r);
        }
      "
      @select-mode="selectModeFromLibrary"
    />
  </div>
</template>

<style scoped>
.discussion-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-primary);
  width: 100%;
  box-sizing: border-box;
  max-height: 70vh;
  overflow-y: auto;
}
.discussion-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}
.discussion-no-cloud {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  background: var(--banner-warning-bg, var(--bg-tertiary));
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.discussion-no-cloud-text {
  flex: 1 1 auto;
  min-width: 0;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.discussion-no-cloud-cta {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-2);
  background: var(--accent-muted);
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: var(--text-sm);
  white-space: nowrap;
}
.discussion-no-cloud-cta:hover {
  text-decoration: underline;
}
.discussion-row--switch {
  align-items: flex-start;
}
.discussion-row--column {
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
}
.discussion-prompt-input {
  width: 100%;
  padding: var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font: inherit;
  resize: vertical;
}
.discussion-switch-label {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.discussion-switch-title {
  font-weight: var(--weight-semibold);
  white-space: nowrap;
}
.discussion-switch-hint,
.discussion-empty {
  font-size: var(--text-xs);
  color: var(--text-muted);
}
.discussion-label {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  white-space: nowrap;
}
.discussion-selector-group {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.discussion-selector-btn {
  padding: var(--space-1) var(--space-3);
  background: transparent;
  color: var(--text-secondary);
  border: none;
  cursor: pointer;
  font-size: var(--text-sm);
  white-space: nowrap;
}
.discussion-selector-btn.is-active {
  background: var(--accent);
  color: #fff;
}
.discussion-rounds-input {
  width: 64px;
  padding: var(--space-1) var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
}
.discussion-mode-select {
  flex: 1 1 auto;
  min-width: 0;
  padding: var(--space-1) var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
}
.discussion-model-input {
  flex: 1 1 auto;
  min-width: 0;
  max-width: 200px;
  padding: var(--space-1) var(--space-2);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
}
.discussion-row--with-hint {
  align-items: flex-start;
}
.discussion-advanced {
  border-top: 1px solid var(--border);
  padding-top: var(--space-2);
}
.discussion-advanced-summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--text-secondary);
  user-select: none;
}
.discussion-advanced-note {
  margin: var(--space-1) 0 var(--space-2);
  font-size: var(--text-xs);
  color: var(--text-muted);
}
.discussion-advanced-group {
  margin-top: var(--space-2);
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
}
.discussion-convergence {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.discussion-convergence-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--text-secondary);
}
.discussion-participants {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.discussion-participants-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.discussion-add-btn {
  padding: var(--space-1) var(--space-2);
  background: var(--accent-muted);
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: var(--text-sm);
}
.discussion-participants-header-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.discussion-add-btn--ghost {
  background: transparent;
  color: var(--text-secondary);
  border-color: var(--border);
}
.discussion-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.discussion-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  background: var(--bg-tertiary);
  border-radius: var(--radius-sm);
}
.discussion-item-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-weight: var(--weight-semibold);
  flex-shrink: 0;
}
.discussion-item-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.discussion-item-name {
  font-weight: var(--weight-medium);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.discussion-item-meta {
  font-size: var(--text-xs);
  color: var(--text-muted);
}
.discussion-item-actions {
  display: flex;
  gap: 2px;
}
.discussion-icon-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.discussion-icon-btn:hover {
  background: var(--bg-hover);
}
.discussion-icon-btn.is-active {
  background: var(--accent-muted);
}
.discussion-icon-btn--danger:hover {
  background: var(--banner-error-bg);
}
.discussion-editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.discussion-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
.discussion-error {
  font-size: var(--text-xs);
  color: var(--error);
}
</style>
