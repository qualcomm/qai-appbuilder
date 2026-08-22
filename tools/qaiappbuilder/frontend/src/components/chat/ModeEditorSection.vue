<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  ModeEditorSection — inline collaboration-mode editor section (M-mode-1).

  Extracted from the former second-tier ModeTemplateDialog so the TemplateLibrary
  模式 tab is ONE level (like 角色/Agents): browse + preview + select + create/edit
  all live in the library, no separate dialog. This section is the create/edit
  form, rendered inline below the list.

  A mode answers "怎么协作": name / description / framing (live char count) /
  tool_policy (3 preset tiers + custom per-tool editor) / flow_policy (template
  defaults) / hard_constraints (two independent soft caps) / live lint advisory.
  systemModel is MANDATORY (M2). All CRUD/HTTP lives in stores/modeTemplate.ts.
  Theme tokens only; styling aligns with the host's editor visual language.
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { useToast } from "@/composables/useToast";
import { useCloudModelOptions } from "@/composables/chat/useCloudModelOptions";
import {
  useModeTemplateStore,
  type ModeHardConstraintsWire,
  type ModeTemplateView,
} from "@/stores/modeTemplate";
import {
  detectPresetTier,
  lintMode,
  presetPolicy,
  type ModeToolPolicy,
  type PresetTier,
  type ToolPolicyValue,
} from "@/lib/modePolicy";

// UI soft-caps for the numeric mode knobs. These MUST stay within the backend's
// accepted ranges: ModeFlowPolicy.from_dict / hard-constraint parsing silently
// clamps or drops out-of-range values, so the form clamps client-side first to
// keep what the user sees == what gets stored.
const FRAMING_MAX = 8000;
const MIN_CHARS = 50;
const MAX_CHARS = 5000;
const MIN_SECONDS = 5;
const MAX_SECONDS = 600;
const MIN_ROUNDS = 1;
const MAX_ROUNDS = 50;

const props = defineProps<{
  /** Mode to edit; null = create a new mode. */
  editTemplate?: ModeTemplateView | null;
}>();

const emit = defineEmits<{
  (e: "saved"): void;
  (e: "cancel"): void;
}>();

const { t } = useI18n();
const toast = useToast();
const store = useModeTemplateStore();

const {
  cloudModelOptions,
  cloudModelLabel,
  loadCloudModels,
  modelMissing: isCloudModelMissing,
} = useCloudModelOptions();

// ── Form state ──────────────────────────────────────────────────────────────
const form = reactive<{
  name: string;
  description: string;
  framing: string;
  tier: PresetTier;
  customPolicy: ModeToolPolicy;
  speakerStrategy: "manager" | "round_robin";
  maxRounds: number;
  judgeEnabled: boolean;
  allowModeSwitch: boolean;
  systemModel: string;
  charsEnabled: boolean;
  maxChars: number;
  secondsEnabled: boolean;
  maxSeconds: number;
}>({
  name: "",
  description: "",
  framing: "",
  tier: "A",
  customPolicy: { default: "allow", tools: {} },
  speakerStrategy: "manager",
  maxRounds: 8,
  judgeEnabled: true,
  allowModeSwitch: true,
  systemModel: "",
  charsEnabled: false,
  maxChars: 500,
  secondsEnabled: false,
  maxSeconds: 60,
});

const showAdvanced = ref(false);

function seedFromMode(m: ModeTemplateView | null | undefined): void {
  if (!m) {
    form.name = "";
    form.description = "";
    form.framing = "";
    form.tier = "A";
    form.customPolicy = { default: "allow", tools: {} };
    form.speakerStrategy = "manager";
    form.maxRounds = 8;
    form.judgeEnabled = true;
    form.allowModeSwitch = true;
    form.systemModel = "";
    form.charsEnabled = false;
    form.maxChars = 500;
    form.secondsEnabled = false;
    form.maxSeconds = 60;
    showAdvanced.value = false;
    return;
  }
  form.name = m.name;
  form.description = m.description;
  form.framing = m.framing ?? "";
  const policy = m.toolPolicy as ModeToolPolicy;
  form.tier = detectPresetTier(policy);
  form.customPolicy = {
    default: policy?.default ?? "allow",
    tools: { ...(policy?.tools ?? {}) },
  };
  showAdvanced.value = form.tier === "custom";
  const flow = m.flowPolicy ?? {};
  form.speakerStrategy =
    flow.speaker_strategy === "round_robin" ? "round_robin" : "manager";
  form.maxRounds = typeof flow.max_rounds === "number" ? flow.max_rounds : 8;
  form.judgeEnabled = flow.judge_enabled !== false;
  form.allowModeSwitch = flow.allow_mode_switch !== false;
  form.systemModel = m.systemModel ?? "";
  const hc = m.hardConstraints;
  form.charsEnabled = typeof hc?.max_chars_per_turn === "number";
  form.maxChars = form.charsEnabled ? (hc?.max_chars_per_turn as number) : 500;
  form.secondsEnabled = typeof hc?.max_seconds_per_turn === "number";
  form.maxSeconds = form.secondsEnabled
    ? (hc?.max_seconds_per_turn as number)
    : 60;
}

watch(() => props.editTemplate, (m) => seedFromMode(m), { immediate: true });

void loadCloudModels();

// ── Tool policy resolution ──────────────────────────────────────────────────
const effectivePolicy = computed<ModeToolPolicy>(() =>
  form.tier === "custom" ? form.customPolicy : presetPolicy(form.tier),
);

function selectTier(tier: PresetTier): void {
  form.tier = tier;
  if (tier === "custom") {
    showAdvanced.value = true;
  } else {
    const p = presetPolicy(tier);
    form.customPolicy = {
      default: p.default ?? "allow",
      tools: { ...(p.tools ?? {}) },
    };
  }
}

function setCustomDefault(value: ToolPolicyValue): void {
  form.customPolicy = { ...form.customPolicy, default: value };
}

function setCustomTool(tool: string, value: ToolPolicyValue): void {
  form.customPolicy = {
    ...form.customPolicy,
    tools: { ...(form.customPolicy.tools ?? {}), [tool]: value },
  };
}

function removeCustomTool(tool: string): void {
  const tools = { ...(form.customPolicy.tools ?? {}) };
  delete tools[tool];
  form.customPolicy = { ...form.customPolicy, tools };
}

const newToolName = ref("");
function addCustomTool(): void {
  const name = newToolName.value.trim();
  if (!name) return;
  setCustomTool(name, "deny");
  newToolName.value = "";
}

const customToolEntries = computed(() =>
  Object.entries(form.customPolicy.tools ?? {}),
);

// ── Framing char count ───────────────────────────────────────────────────────
const framingLength = computed(() => form.framing.length);
const framingTooLong = computed(() => framingLength.value > FRAMING_MAX);

// ── Live lint (advisory only) ────────────────────────────────────────────────
const lintIssues = computed(() => lintMode(form.framing, effectivePolicy.value));

// ── Save ─────────────────────────────────────────────────────────────────────
const canSave = computed(
  () =>
    form.name.trim() !== "" &&
    form.systemModel.trim() !== "" &&
    !framingTooLong.value,
);

function clampInt(value: number, lo: number, hi: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(hi, Math.max(lo, Math.round(value)));
}

function buildHardConstraints(): ModeHardConstraintsWire {
  return {
    max_chars_per_turn: form.charsEnabled
      ? clampInt(form.maxChars, MIN_CHARS, MAX_CHARS, 500)
      : null,
    max_seconds_per_turn: form.secondsEnabled
      ? clampInt(form.maxSeconds, MIN_SECONDS, MAX_SECONDS, 60)
      : null,
  };
}

async function save(): Promise<void> {
  if (!canSave.value) return;
  const input = {
    name: form.name.trim(),
    description: form.description.trim(),
    framing: form.framing,
    toolPolicy: effectivePolicy.value,
    flowPolicy: {
      speaker_strategy: form.speakerStrategy,
      max_rounds: clampInt(form.maxRounds, MIN_ROUNDS, MAX_ROUNDS, 8),
      judge_enabled: form.judgeEnabled,
      allow_mode_switch: form.allowModeSwitch,
      system_model_id: form.systemModel.trim(),
    },
    hardConstraints: buildHardConstraints(),
  };
  try {
    if (props.editTemplate != null) {
      await store.update(props.editTemplate.id, input);
    } else {
      await store.create(input);
    }
    toast.success(t("chat.discussion.modes.saved"));
    emit("saved");
  } catch (e) {
    toast.error(e instanceof Error ? e.message : String(e));
  }
}

const title = computed(() =>
  props.editTemplate != null
    ? t("chat.discussion.modes.editTitle", { name: props.editTemplate.name })
    : t("chat.discussion.modes.createTitle"),
);
</script>

<template>
  <section class="mt-editor" data-testid="mode-template-editor">
    <p class="mt-editor-title">{{ title }}</p>

    <div class="mt-fields">
      <!-- name + description -->
      <label class="mt-field">
        <span>{{ t("chat.discussion.modes.name") }}</span>
        <input
          v-model="form.name"
          type="text"
          data-testid="mode-name"
          :placeholder="t('chat.discussion.modes.namePlaceholder')"
        />
      </label>
      <label class="mt-field">
        <span>{{ t("chat.discussion.modes.description") }}</span>
        <input
          v-model="form.description"
          type="text"
          data-testid="mode-description"
          :placeholder="t('chat.discussion.modes.descriptionPlaceholder')"
        />
      </label>

      <!-- framing + char count -->
      <label class="mt-field">
        <span>{{ t("chat.discussion.modes.framing") }}</span>
        <textarea
          v-model="form.framing"
          rows="4"
          data-testid="mode-framing"
          :placeholder="t('chat.discussion.modes.framingPlaceholder')"
        ></textarea>
        <span
          class="mt-count"
          :class="{ 'is-over': framingTooLong }"
          data-testid="mode-framing-count"
        >
          {{
            t("chat.discussion.modes.framingCount", {
              count: framingLength,
              max: FRAMING_MAX,
            })
          }}
        </span>
        <span v-if="framingTooLong" class="mt-error">
          {{ t("chat.discussion.modes.framingTooLong", { max: FRAMING_MAX }) }}
        </span>
      </label>

      <!-- tool policy: 3 tiers + custom -->
      <fieldset class="mt-field">
        <legend>{{ t("chat.discussion.modes.toolPolicy") }}</legend>
        <label
          v-for="tier in (['A', 'B', 'C', 'custom'] as PresetTier[])"
          :key="tier"
          class="mt-radio"
        >
          <input
            type="radio"
            name="mode-tier"
            :value="tier"
            :checked="form.tier === tier"
            :data-testid="`mode-tier-${tier}`"
            @change="selectTier(tier)"
          />
          <span class="mt-radio-main">
            <strong>{{
              tier === "custom"
                ? t("chat.discussion.modes.toolPolicyCustom")
                : t(`chat.discussion.modes.toolPolicyTier${tier}`)
            }}</strong>
            <small v-if="tier !== 'custom'">{{
              t(`chat.discussion.modes.toolPolicyTier${tier}Desc`)
            }}</small>
          </span>
        </label>

        <!-- advanced per-tool editor -->
        <div
          v-if="form.tier === 'custom' || showAdvanced"
          class="mt-advanced"
          data-testid="mode-advanced"
        >
          <details :open="form.tier === 'custom'">
            <summary>{{ t("chat.discussion.modes.toolPolicyAdvanced") }}</summary>
            <label class="mt-field">
              <span>{{ t("chat.discussion.modes.toolPolicyDefault") }}</span>
              <select
                :value="form.customPolicy.default ?? 'allow'"
                data-testid="mode-custom-default"
                @change="
                  setCustomDefault(
                    ($event.target as HTMLSelectElement).value as ToolPolicyValue,
                  )
                "
              >
                <option value="allow">
                  {{ t("chat.discussion.modes.policyAllow") }}
                </option>
                <option value="deny">
                  {{ t("chat.discussion.modes.policyDeny") }}
                </option>
              </select>
            </label>
            <ul class="mt-tool-list">
              <li v-for="[tool, val] in customToolEntries" :key="tool">
                <span class="mt-tool-name">{{ tool }}</span>
                <select
                  :value="val"
                  :data-testid="`mode-custom-tool-${tool}`"
                  @change="
                    setCustomTool(
                      tool,
                      ($event.target as HTMLSelectElement).value as ToolPolicyValue,
                    )
                  "
                >
                  <option value="allow">
                    {{ t("chat.discussion.modes.policyAllow") }}
                  </option>
                  <option value="deny">
                    {{ t("chat.discussion.modes.policyDeny") }}
                  </option>
                </select>
                <button
                  type="button"
                  class="mt-tool-remove"
                  @click="removeCustomTool(tool)"
                >
                  ✕
                </button>
              </li>
            </ul>
            <div class="mt-tool-add">
              <input
                v-model="newToolName"
                type="text"
                placeholder="tool"
                data-testid="mode-custom-tool-new"
                @keyup.enter="addCustomTool"
              />
              <button type="button" class="mt-btn" @click="addCustomTool">
                +
              </button>
            </div>
          </details>
        </div>
      </fieldset>

      <!-- flow policy (template defaults) -->
      <fieldset class="mt-field">
        <legend>{{ t("chat.discussion.modes.flowPolicy") }}</legend>
        <p class="mt-note">{{ t("chat.discussion.modes.flowPolicyNote") }}</p>
        <!-- Mandatory mode "system model" — cloud-only dropdown. -->
        <label class="mt-field">
          <span>{{ t("chat.discussion.modes.systemModel") }}</span>
          <select v-model="form.systemModel" data-testid="mode-system-model">
            <option value="" disabled>
              {{ t("chat.discussion.modes.systemModelPlaceholder") }}
            </option>
            <option
              v-for="m in cloudModelOptions"
              :key="m.model_id"
              :value="m.model_id"
            >
              {{ cloudModelLabel(m) }}
            </option>
            <option
              v-if="isCloudModelMissing(form.systemModel)"
              :value="form.systemModel"
            >
              {{ form.systemModel }}
            </option>
          </select>
          <small>{{ t("chat.discussion.modes.systemModelHint") }}</small>
        </label>
        <label class="mt-field">
          <span>{{ t("chat.discussion.modes.speakerStrategy") }}</span>
          <select v-model="form.speakerStrategy" data-testid="mode-speaker">
            <option value="manager">
              {{ t("chat.discussion.modes.speakerManager") }}
            </option>
            <option value="round_robin">
              {{ t("chat.discussion.modes.speakerRoundRobin") }}
            </option>
          </select>
        </label>
        <label class="mt-field">
          <span>{{ t("chat.discussion.modes.flowMaxRounds") }}</span>
          <input
            v-model.number="form.maxRounds"
            type="number"
            :min="MIN_ROUNDS"
            :max="MAX_ROUNDS"
            data-testid="mode-max-rounds"
          />
        </label>
        <label class="mt-check">
          <input
            v-model="form.judgeEnabled"
            type="checkbox"
            data-testid="mode-judge"
          />
          {{ t("chat.discussion.modes.judgeEnabled") }}
        </label>
        <label class="mt-check">
          <input
            v-model="form.allowModeSwitch"
            type="checkbox"
            data-testid="mode-allow-switch"
          />
          {{ t("chat.discussion.modes.allowModeSwitch") }}
        </label>
      </fieldset>

      <!-- hard constraints (two independent toggles) -->
      <fieldset class="mt-field">
        <legend>{{ t("chat.discussion.modes.hardConstraints") }}</legend>
        <p class="mt-note">
          {{ t("chat.discussion.modes.hardConstraintsNote") }}
        </p>
        <label class="mt-check">
          <input
            v-model="form.charsEnabled"
            type="checkbox"
            data-testid="mode-chars-enabled"
          />
          {{ t("chat.discussion.modes.maxCharsEnabled") }}
        </label>
        <label v-if="form.charsEnabled" class="mt-field">
          <span>{{ t("chat.discussion.modes.maxCharsLabel") }}</span>
          <input
            v-model.number="form.maxChars"
            type="number"
            :min="MIN_CHARS"
            :max="MAX_CHARS"
            data-testid="mode-max-chars"
          />
          <small>{{ t("chat.discussion.modes.maxCharsHint") }}</small>
        </label>
        <label class="mt-check">
          <input
            v-model="form.secondsEnabled"
            type="checkbox"
            data-testid="mode-seconds-enabled"
          />
          {{ t("chat.discussion.modes.maxSecondsEnabled") }}
        </label>
        <label v-if="form.secondsEnabled" class="mt-field">
          <span>{{ t("chat.discussion.modes.maxSecondsLabel") }}</span>
          <input
            v-model.number="form.maxSeconds"
            type="number"
            :min="MIN_SECONDS"
            :max="MAX_SECONDS"
            data-testid="mode-max-seconds"
          />
          <small>{{ t("chat.discussion.modes.maxSecondsHint") }}</small>
        </label>
      </fieldset>

      <!-- live lint (advisory) -->
      <section
        v-if="lintIssues.length > 0"
        class="mt-lint"
        data-testid="mode-lint"
      >
        <h4>{{ t("chat.discussion.modes.lintTitle") }}</h4>
        <ul>
          <li
            v-for="(issue, idx) in lintIssues"
            :key="idx"
            class="mt-lint-item"
          >
            <span class="mt-lint-sev">{{
              t("chat.discussion.modes.lintWarning")
            }}</span>
            <span class="mt-lint-msg">{{ issue.message }}</span>
          </li>
        </ul>
      </section>
    </div>

    <div class="mt-editor-actions">
      <button type="button" class="mt-btn" @click="emit('cancel')">
        {{ t("common.cancel") }}
      </button>
      <button
        type="button"
        class="mt-btn mt-btn--primary"
        :disabled="!canSave"
        data-testid="mode-save"
        @click="save"
      >
        {{ t("common.save") }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.mt-editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-tertiary);
  margin-bottom: 16px;
}
.mt-editor-title {
  margin: 0;
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-primary);
}
.mt-fields {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.mt-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 0.82rem;
  color: var(--text-secondary);
  border: none;
  padding: 0;
  margin: 0;
}
.mt-field legend {
  font-weight: 600;
  color: var(--text-primary);
  padding: 0;
  margin-bottom: 4px;
}
.mt-field input,
.mt-field textarea,
.mt-field select {
  padding: 6px 8px;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-primary);
  font: inherit;
  resize: vertical;
}
.mt-count {
  font-size: 0.72rem;
  color: var(--text-secondary);
  align-self: flex-end;
}
.mt-count.is-over {
  color: var(--error);
  font-weight: 600;
}
.mt-error {
  font-size: 0.72rem;
  color: var(--error);
}
.mt-note {
  font-size: 0.72rem;
  color: var(--text-secondary);
  margin: 0;
}
.mt-radio {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 4px 0;
  cursor: pointer;
}
.mt-radio-main {
  display: flex;
  flex-direction: column;
}
.mt-radio-main small {
  color: var(--text-secondary);
  font-size: 0.72rem;
}
.mt-advanced {
  margin-top: 8px;
  padding: 8px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 6px;
}
.mt-tool-list {
  list-style: none;
  padding: 0;
  margin: 8px 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.mt-tool-list li {
  display: flex;
  align-items: center;
  gap: 8px;
}
.mt-tool-name {
  flex: 1;
  font-family: var(--font-mono, monospace);
  color: var(--text-primary);
}
.mt-tool-remove {
  background: transparent;
  border: none;
  color: var(--error);
  cursor: pointer;
}
.mt-tool-add {
  display: flex;
  gap: 8px;
}
.mt-check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.82rem;
  color: var(--text-primary);
  cursor: pointer;
}
.mt-lint {
  background: var(--banner-warning-bg, var(--bg-secondary));
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
}
.mt-lint h4 {
  margin: 0 0 4px;
  font-size: 0.82rem;
}
.mt-lint ul {
  margin: 0;
  padding-left: 16px;
}
.mt-lint-item {
  font-size: 0.72rem;
}
.mt-lint-sev {
  color: var(--warning, var(--accent));
  font-weight: 600;
  margin-right: 4px;
}
.mt-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.mt-btn {
  padding: 5px 10px;
  font-size: 0.8rem;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-primary);
  cursor: pointer;
}
.mt-btn--primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.mt-btn--primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
