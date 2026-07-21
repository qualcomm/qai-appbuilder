<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * AgentSettingsPanel — consolidates the agent-related settings that used to be
 * scattered across two other tabs:
 *
 *   1. "Agent Loop" — moved out of AppConfigPanel's 🤖 Agent Loop group
 *      (max iterations / auto-compress / experience extraction / auto title /
 *      context-compression ratios). Persisted via the sticky Save bar through
 *      POST /api/forge-config (same shallow-top-level-merge contract as before,
 *      so behaviour is unchanged — the full `chat` object is round-tripped so
 *      sibling chat sub-keys (hooks / hooks_enabled) survive the save).
 *
 *   2. "Sub-agent models" — moved out of ChatHooksSettings (the Hook tab). Each
 *      sub-agent profile (explore / general) can override which model it uses;
 *      empty = inherit the main chat model. Persists immediately on change via
 *      PUT /api/settings/subagent_profile_models.
 *
 * These two live under different backends but are both "how the agent behaves",
 * so they belong together on the 🤝 Agent tab rather than under App Config /
 * Hooks. i18n keys are reused verbatim (appConfig.* + chatHooks.subagents.*) so
 * no locale-parity churn is introduced by the move.
 */
import { onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { useConfig, type AppConfig } from "@/composables/useConfig";
import { useToast } from "@/composables/useToast";
import {
  useChatModelList,
  type ChatModelItem,
} from "@/composables/chat/useChatModelList";
import {
  compactionRatioPercent,
  resolveCompactionRatios,
} from "@/components/chat/compactionRatios";

const { t } = useI18n();
const toast = useToast();
const { config, loading, fetchConfig, saveConfig } = useConfig();
const { loadAll: loadAllModels } = useChatModelList();

// ─── Agent Loop form ──────────────────────────────────────────────────────
interface AgentLoopForm {
  max_iterations: number;
  auto_compress: boolean;
  experience_extraction: boolean;
  auto_title: boolean;
  // Stored as 0.0..1.0 floats under forge_config.chat.compaction_*_ratio;
  // surfaced as percent sliders.
  compaction_target_ratio: number;
  compaction_protect_ratio: number;
}

const form = reactive<AgentLoopForm>({
  max_iterations: 25,
  auto_compress: true,
  experience_extraction: true,
  auto_title: true,
  compaction_target_ratio: 0.35,
  compaction_protect_ratio: 0.35,
});

// Full persisted `chat` object, round-tripped so the backend's shallow
// top-level merge does not clobber sibling sub-keys (chat.hooks /
// chat.hooks_enabled) when we save chat.compaction_*_ratio.
const rawChat = ref<Record<string, unknown>>({});

function syncForm(): void {
  if (!config.value) return;
  const c = config.value as Record<string, unknown>;
  if (typeof c.max_iterations === "number") form.max_iterations = c.max_iterations;
  if (typeof c.auto_compress === "boolean") form.auto_compress = c.auto_compress;
  if (typeof c.experience_extraction === "boolean") {
    form.experience_extraction = c.experience_extraction;
  }
  if (typeof c.auto_title === "boolean") form.auto_title = c.auto_title;
  const chatSection = c.chat;
  if (chatSection != null && typeof chatSection === "object") {
    const ch = chatSection as Record<string, unknown>;
    rawChat.value = { ...ch };
    if (typeof ch.compaction_target_ratio === "number") {
      form.compaction_target_ratio = ch.compaction_target_ratio;
    }
    if (typeof ch.compaction_protect_ratio === "number") {
      form.compaction_protect_ratio = ch.compaction_protect_ratio;
    }
  } else {
    rawChat.value = {};
  }
}

function buildPayload(): Partial<AppConfig> {
  const compaction = resolveCompactionRatios(
    form.compaction_target_ratio,
    form.compaction_protect_ratio,
  );
  return {
    max_iterations: form.max_iterations,
    auto_compress: form.auto_compress,
    experience_extraction: form.experience_extraction,
    auto_title: form.auto_title,
    // Round-trip the full chat object so the backend's shallow top-level merge
    // keeps sibling chat sub-keys (hooks / hooks_enabled) intact.
    chat: {
      ...rawChat.value,
      compaction_target_ratio: compaction.target,
      compaction_protect_ratio: compaction.protect,
    },
  };
}

const saving = ref(false);

async function handleSave(): Promise<void> {
  saving.value = true;
  try {
    await saveConfig(buildPayload());
    syncForm();
  } finally {
    saving.value = false;
  }
}

async function handleReset(): Promise<void> {
  await fetchConfig();
  syncForm();
}

// ─── Sub-agent per-profile model overrides ──────────────────────────────────
const SUBAGENT_PROFILES = ["explore", "general"] as const;
type SubagentProfile = (typeof SUBAGENT_PROFILES)[number];
type SubagentProfileModels = Partial<Record<SubagentProfile, string>>;
interface SubagentProfileModelsResponse {
  models: SubagentProfileModels;
}

const profileModels = ref<SubagentProfileModels>({});
const availableModels = ref<ChatModelItem[]>([]);
const profileModelsSaving = ref(false);

function normalizeProfileModels(
  raw: SubagentProfileModels | undefined,
): SubagentProfileModels {
  const out: SubagentProfileModels = {};
  if (raw === null || typeof raw !== "object") return out;
  for (const profile of SUBAGENT_PROFILES) {
    const v = (raw as Record<string, unknown>)[profile];
    if (typeof v === "string" && v !== "") out[profile] = v;
  }
  return out;
}

async function loadProfileModels(): Promise<void> {
  try {
    const res = await apiJson<SubagentProfileModelsResponse>(
      "GET",
      "/api/settings/subagent_profile_models",
    );
    profileModels.value = normalizeProfileModels(res.models);
  } catch {
    profileModels.value = {};
    toast.error(t("chatHooks.subagents.loadFailed"));
  }
}

async function loadModelOptions(): Promise<void> {
  try {
    availableModels.value = await loadAllModels();
  } catch {
    availableModels.value = [];
  }
}

async function onProfileModelChange(
  profile: SubagentProfile,
  value: string,
): Promise<void> {
  if (profileModelsSaving.value) return;
  const previous = { ...profileModels.value };
  const next: SubagentProfileModels = { ...profileModels.value };
  if (value === "") {
    delete next[profile];
  } else {
    next[profile] = value;
  }
  profileModels.value = next;
  profileModelsSaving.value = true;
  try {
    const res = await apiJson<
      SubagentProfileModelsResponse,
      SubagentProfileModelsResponse
    >("PUT", "/api/settings/subagent_profile_models", { models: next });
    profileModels.value = normalizeProfileModels(res.models);
    toast.success(t("chatHooks.subagents.saved"));
  } catch (e) {
    profileModels.value = previous;
    toast.error(
      `${t("chatHooks.subagents.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    profileModelsSaving.value = false;
  }
}

// ─── Init ─────────────────────────────────────────────────────────────────
onMounted(async () => {
  await fetchConfig();
  syncForm();
  void loadProfileModels();
  void loadModelOptions();
});
</script>

<template>
  <div class="config-section">
    <div
      v-if="loading"
      style="padding: 24px; color: var(--text-muted);"
    >
      {{ t("common.loading") }}
    </div>

    <template v-else>
      <!-- ═══ Agent Loop ═══ -->
      <div class="config-group">
        <div class="config-group-header config-group-header--static">
          <span>🤖</span>
          <span>{{ t("appConfig.agentLoopTitle") }}</span>
        </div>
        <div class="config-group-body">
          <!-- Max Iterations -->
          <div class="config-field">
            <label class="config-label">{{ t("appConfig.maxIterationsLabel") }}</label>
            <div class="config-comment">
              {{ t("appConfig.maxIterationsDesc") }}
            </div>
            <input
              v-model.number="form.max_iterations"
              type="number"
              class="config-input config-number"
              min="1"
              max="100"
            />
          </div>
          <!-- Auto Compress -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.autoCompressLabel") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  v-model="form.auto_compress"
                  type="checkbox"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <div class="config-comment">
              {{ t("appConfig.autoCompressDesc") }}
            </div>
          </div>
          <!-- Experience Extraction -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.experienceExtractionLabel") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  v-model="form.experience_extraction"
                  type="checkbox"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <div class="config-comment">
              {{ t("appConfig.experienceExtractionDesc") }}
            </div>
          </div>
          <!-- Auto Title -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.autoTitleLabel") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  v-model="form.auto_title"
                  type="checkbox"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <div class="config-comment">
              {{ t("appConfig.autoTitleDesc") }}
            </div>
          </div>
          <!-- Context compression: post-compression keep size (target_window_ratio) -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.compactionTargetLabel") }}
              <span
                class="config-slider-value"
                style="margin-left: auto;"
                data-testid="compaction-target-value"
              >{{ compactionRatioPercent(form.compaction_target_ratio) }}</span>
            </label>
            <input
              v-model.number="form.compaction_target_ratio"
              type="range"
              min="0.2"
              max="0.6"
              step="0.05"
              class="config-slider"
              data-testid="compaction-target-slider"
            />
            <div class="config-comment">
              {{ t("appConfig.compactionTargetDesc") }}
            </div>
          </div>
          <!-- Context compression: recent-history protection (protect_ratio) -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.compactionProtectLabel") }}
              <span
                class="config-slider-value"
                style="margin-left: auto;"
                data-testid="compaction-protect-value"
              >{{ compactionRatioPercent(form.compaction_protect_ratio) }}</span>
            </label>
            <input
              v-model.number="form.compaction_protect_ratio"
              type="range"
              min="0.2"
              max="0.5"
              step="0.05"
              class="config-slider"
              data-testid="compaction-protect-slider"
            />
            <div class="config-comment">
              {{ t("appConfig.compactionProtectDesc") }}
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ Sub-agent models ═══ -->
      <div class="config-group">
        <div class="config-group-header config-group-header--static">
          <span>🤝</span>
          <span>{{ t("chatHooks.subagents.title") }}</span>
        </div>
        <div class="config-group-body">
          <div class="config-comment">
            {{ t("chatHooks.subagents.subtitle") }}
          </div>
          <div
            v-for="profile in SUBAGENT_PROFILES"
            :key="profile"
            class="config-field agent-subagent-row"
          >
            <label class="config-label">
              {{ t(`chatHooks.subagents.profile.${profile}.label`) }}
            </label>
            <select
              class="config-input"
              :value="profileModels[profile] ?? ''"
              :disabled="profileModelsSaving"
              @change="onProfileModelChange(profile, ($event.target as HTMLSelectElement).value)"
            >
              <option value="">
                {{ t("chatHooks.subagents.inherit") }}
              </option>
              <option
                v-for="m in availableModels"
                :key="m.model_id"
                :value="m.model_id"
              >
                {{ m.name }}
              </option>
            </select>
            <p class="config-comment">
              {{ t(`chatHooks.subagents.profile.${profile}.desc`) }}
            </p>
          </div>
        </div>
      </div>
    </template>

    <!-- ═══ Sticky Save Bar (Agent Loop only; sub-agent models save immediately) ═══ -->
    <div class="config-save-bar">
      <button
        class="btn btn-primary"
        :disabled="saving"
        @click="handleSave"
      >
        <span
          v-if="saving"
          class="spinner"
          aria-hidden="true"
        ></span>
        <span v-else>💾</span>
        {{ t("appConfig.saveBtn") }}
      </button>
      <button
        class="btn btn-ghost"
        :disabled="saving"
        @click="handleReset"
      >
        ↺ {{ t("appConfig.resetBtn") }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* The Agent tab groups are always-expanded (no accordion), so the header is
   non-interactive here — drop the pointer affordance the shared
   .config-group-header carries. */
.config-group-header--static {
  cursor: default;
}
.agent-subagent-row {
  max-width: 480px;
}
</style>
