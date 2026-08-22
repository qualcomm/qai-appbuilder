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
 *      (max iterations / auto-compress / auto title /
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
 *   3. "Tool Output Limits" — moved out of Security → Tool Safety (2026-07).
 *      Caps on how much each built-in agent tool (read / glob / grep) returns
 *      to the model. Backed by the SAME `/api/security/runtime-config` route
 *      (ToolOutputSettings) via useRuntimeConfig — build-time, so a change
 *      shows the reboot-confirm dialog. i18n keys `toolSafety.*` reused verbatim.
 *
 * These live under different backends but are all "how the agent behaves", so
 * they belong together on the 🤝 Agent tab rather than under App Config /
 * Hooks / Security. Existing i18n keys are reused verbatim (appConfig.* +
 * chatHooks.subagents.* + toolSafety.*) so no locale-parity churn.
 */
import { onMounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { IS_INTERNAL } from "@/edition";
import { useConfig, type AppConfig } from "@/composables/useConfig";
import { useToast } from "@/composables/useToast";
import { useConfirm } from "@/composables/useConfirm";
import { useReboot } from "@/composables/useReboot";
import {
  useRuntimeConfig,
  type RuntimeConfig,
} from "@/composables/useRuntimeConfig";
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
const { confirm } = useConfirm();
const { requestRebootDirect } = useReboot();
const {
  config: toolOutputConfig,
  fetchConfig: fetchToolOutput,
  save: saveToolOutput,
} = useRuntimeConfig();

// ─── Sub-agent recursion ceiling bounds ───────────────────────────────────
// Mirrors the backend's `ChatSettings.max_subagent_recursion_depth`
// (`default=2, ge=1, le=5`). Declared before the form so the reactive default
// and the clamp share ONE source of truth.
const RECURSION_DEPTH_MIN = 1;
const RECURSION_DEPTH_MAX = 5;
const RECURSION_DEPTH_DEFAULT = 2;

// Bash auto-background bounds, mirroring
// ChatSettings.bash_auto_background_threshold_ms (ge=1000, le=600000) so the
// number input, the clamp and the backend field share ONE source of truth.
const AUTO_BG_THRESHOLD_MIN = 1000;
const AUTO_BG_THRESHOLD_MAX = 600000;
const AUTO_BG_THRESHOLD_DEFAULT = 15000;
// Mirrors ``ChatSettings.bash_auto_background_enabled`` (default False): the
// clock arm is off, so the form must not present it as on before the server
// value arrives.
const AUTO_BG_ENABLED_DEFAULT = false;

// QGenie quota warn threshold — percent of a model's DAILY allowance at which
// the user is offered a switch to the other traffic class. Daily only: RPM/TPM
// are rolling windows that recover within a minute, so warning on those would
// nag about a condition that fixes itself.
//
// Floor of 50 because a warning below half-spent is noise; 100 means "only
// warn once actually exhausted".
const QUOTA_WARN_MIN = 50;
const QUOTA_WARN_MAX = 100;
const QUOTA_WARN_DEFAULT = 95;

// ─── Agent Loop form ──────────────────────────────────────────────────────
interface AgentLoopForm {
  max_rounds: number;
  auto_compress: boolean;
  auto_title: boolean;
  // Turn-warning settings
  turn_warning_enabled: boolean;
  turn_warning_start: number;
  turn_warning_step: number;
  // Stored as 0.0..1.0 floats under forge_config.chat.compaction_*_ratio;
  // surfaced as percent sliders.
  compaction_target_ratio: number;
  compaction_protect_ratio: number;
  // Bash auto-background (N.28-N.30): whether a still-running exec is handed
  // off to the background-process manager once the threshold elapses on a
  // fresh user message, and how long that foreground wait is.  Both stored
  // under forge_config.chat.bash_auto_background_{enabled,threshold_ms}.
  // The threshold is surfaced in MILLISECONDS — the same unit the backend
  // field uses, so there is no conversion left to get wrong.  "Off" is the
  // boolean, never a magic threshold value.
  bash_auto_background_enabled: boolean;
  bash_auto_background_threshold_ms: number;
  // Sub-agent recursion ceiling (N.21/N.23). 1 = only the main agent may
  // spawn; 2 (default) = one nested level. Stored under
  // forge_config.chat.max_subagent_recursion_depth and re-read by the backend
  // on every spawn, so a change here binds without a restart. Range 1..5 —
  // each extra level multiplies concurrent model spend.
  max_subagent_recursion_depth: number;
  // Percent of the daily QGenie allowance at which to offer a traffic-class
  // switch. Stored under forge_config.chat.qgenie_quota_warn_percent and read
  // by the chat store at end-of-turn, so a change binds on the next turn.
  qgenie_quota_warn_percent: number;
}

const form = reactive<AgentLoopForm>({
  max_rounds: 0,
  auto_compress: true,
  auto_title: true,
  turn_warning_enabled: true,
  turn_warning_start: 30,
  turn_warning_step: 10,
  compaction_target_ratio: 0.35,
  compaction_protect_ratio: 0.35,
  bash_auto_background_enabled: AUTO_BG_ENABLED_DEFAULT,
  bash_auto_background_threshold_ms: AUTO_BG_THRESHOLD_DEFAULT,
  max_subagent_recursion_depth: RECURSION_DEPTH_DEFAULT,
  qgenie_quota_warn_percent: QUOTA_WARN_DEFAULT,
});

// Full persisted `chat` object, round-tripped so the backend's shallow
// top-level merge does not clobber sibling sub-keys (chat.hooks /
// chat.hooks_enabled) when we save chat.compaction_*_ratio.
const rawChat = ref<Record<string, unknown>>({});

function syncForm(): void {
  if (!config.value) return;
  const c = config.value as Record<string, unknown>;
  // Agent round cap: 0 = unlimited (no cap), positive = user cap.
  if (typeof c.max_rounds === "number") form.max_rounds = c.max_rounds;
  if (typeof c.auto_compress === "boolean") form.auto_compress = c.auto_compress;
  if (typeof c.auto_title === "boolean") form.auto_title = c.auto_title;
  // Turn-warning settings (nested under agent.turn_warning or flat)
  const tw = (c.turn_warning ?? (c.agent && (c.agent as Record<string, unknown>).turn_warning)) as Record<string, unknown> | undefined;
  if (tw != null && typeof tw === "object") {
    if (typeof tw.enabled === "boolean") form.turn_warning_enabled = tw.enabled;
    if (typeof tw.start === "number") form.turn_warning_start = tw.start;
    if (typeof tw.step === "number") form.turn_warning_step = tw.step;
  }
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
    // Bash auto-background: both fields are read as-is (no unit conversion —
    // the UI speaks ms like the backend).  Absent -> keep the form defaults.
    if (typeof ch.bash_auto_background_enabled === "boolean") {
      form.bash_auto_background_enabled = ch.bash_auto_background_enabled;
    }
    if (typeof ch.bash_auto_background_threshold_ms === "number") {
      form.bash_auto_background_threshold_ms = clampAutoBgThreshold(
        ch.bash_auto_background_threshold_ms,
      );
    }
    // Absent -> keep the form default (2), which mirrors
    // ChatSettings.max_subagent_recursion_depth.
    if (typeof ch.max_subagent_recursion_depth === "number") {
      form.max_subagent_recursion_depth = clampRecursionDepth(
        ch.max_subagent_recursion_depth,
      );
    }
    // Absent -> keep the form default (95).
    if (typeof ch.qgenie_quota_warn_percent === "number") {
      form.qgenie_quota_warn_percent = clampQuotaWarn(
        ch.qgenie_quota_warn_percent,
      );
    }
  } else {
    rawChat.value = {};
  }
}

/**
 * Coerce a recursion-depth value into the permitted band.
 *
 * A cleared number field yields `""` (or `null`) through `v-model.number`, and
 * `Number("")` is `0` — finite, so a plain clamp would silently save `1` as if
 * the user had asked for "no nesting". An absent value means "unset", which is
 * the field default, so blank restores the default rather than the floor.
 * Anything genuinely out of range (typed past the spinner) IS clamped: the
 * backend clamps too, and a UI showing a ceiling the agent never enforces is
 * worse than a corrected one.
 */
function clampRecursionDepth(value: unknown): number {
  if (value === "" || value === null || value === undefined) {
    return RECURSION_DEPTH_DEFAULT;
  }
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return RECURSION_DEPTH_DEFAULT;
  return Math.min(RECURSION_DEPTH_MAX, Math.max(RECURSION_DEPTH_MIN, n));
}

/**
 * Coerce an auto-background threshold (ms) into the permitted band.
 *
 * Mirrors `clampRecursionDepth`: a cleared `v-model.number` field yields `""`
 * / `null`, and `Number("")` is `0` — which is NOT a legal threshold any more
 * (the OFF state is `bash_auto_background_enabled`), so blank restores the
 * default rather than saving a value the backend would reject.
 */
function clampAutoBgThreshold(value: unknown): number {
  if (value === "" || value === null || value === undefined) {
    return AUTO_BG_THRESHOLD_DEFAULT;
  }
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return AUTO_BG_THRESHOLD_DEFAULT;
  return Math.min(AUTO_BG_THRESHOLD_MAX, Math.max(AUTO_BG_THRESHOLD_MIN, n));
}

/**
 * Coerce a QGenie quota warn percent into the permitted band.
 *
 * Mirrors the two clamps above: a cleared `v-model.number` field yields `""` /
 * `null` and `Number("")` is `0`, which would mean "warn immediately, always"
 * — so blank restores the default rather than turning the prompt into a nag.
 */
function clampQuotaWarn(value: unknown): number {
  if (value === "" || value === null || value === undefined) {
    return QUOTA_WARN_DEFAULT;
  }
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return QUOTA_WARN_DEFAULT;
  return Math.min(QUOTA_WARN_MAX, Math.max(QUOTA_WARN_MIN, n));
}

function buildPayload(): Partial<AppConfig> {
  const compaction = resolveCompactionRatios(
    form.compaction_target_ratio,
    form.compaction_protect_ratio,
  );
  return {
    // Agent round cap: 0 = unlimited, positive = user cap.
    max_rounds: form.max_rounds,
    auto_compress: form.auto_compress,
    auto_title: form.auto_title,
    // Turn-warning preferences
    turn_warning: {
      enabled: form.turn_warning_enabled,
      start: form.turn_warning_start,
      step: form.turn_warning_step,
    },
    // Round-trip the full chat object so the backend's shallow top-level merge
    // keeps sibling chat sub-keys (hooks / hooks_enabled) intact.
    chat: {
      ...rawChat.value,
      compaction_target_ratio: compaction.target,
      compaction_protect_ratio: compaction.protect,
      bash_auto_background_enabled: form.bash_auto_background_enabled,
      // Sent in ms, unconverted.  Always sent even when the switch is off so
      // the user's number survives a toggle round-trip.
      bash_auto_background_threshold_ms: clampAutoBgThreshold(
        form.bash_auto_background_threshold_ms,
      ),
      // The backend clamps this too (a forge_config document has no DTO
      // validation), but sending an in-range value keeps the saved document
      // honest about what the UI shows.
      max_subagent_recursion_depth: clampRecursionDepth(
        form.max_subagent_recursion_depth,
      ),
      qgenie_quota_warn_percent: clampQuotaWarn(
        form.qgenie_quota_warn_percent,
      ),
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

// ─── Desktop control (`computer` tool) master enable ────────────────────────
// Secure-by-default OFF switch for the native screenshot + mouse/keyboard
// tool. Persists immediately to forge_config.computer.enabled via
// PUT /api/settings/computer_enabled and takes effect on the next turn
// WITHOUT a restart (the tool is always registered but only advertised when
// this flag is on).
interface ComputerEnabledResponse {
  enabled: boolean;
}
const computerEnabled = ref(false);
const computerLoading = ref(false);
const computerSaving = ref(false);

async function loadComputerEnabled(): Promise<void> {
  computerLoading.value = true;
  try {
    const res = await apiJson<ComputerEnabledResponse>(
      "GET",
      "/api/settings/computer_enabled",
    );
    computerEnabled.value = res.enabled === true;
  } catch {
    computerEnabled.value = false;
  } finally {
    computerLoading.value = false;
  }
}

async function toggleComputerEnabled(next: boolean): Promise<void> {
  if (computerSaving.value) return;
  computerSaving.value = true;
  const prev = computerEnabled.value;
  computerEnabled.value = next;
  try {
    const res = await apiJson<ComputerEnabledResponse>(
      "PUT",
      "/api/settings/computer_enabled",
      { enabled: next },
    );
    computerEnabled.value = res.enabled === true;
    toast.success(
      computerEnabled.value
        ? t("chatHooks.computer.savedOn")
        : t("chatHooks.computer.savedOff"),
    );
  } catch (e) {
    computerEnabled.value = prev;
    toast.error(
      `${t("chatHooks.computer.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    computerSaving.value = false;
  }
}

// ─── Cloud image format/quality ─────────────────────────────────────────────
// Controls how image copies sent to the cloud model are re-compressed. Persists
// to forge_config.chat.image_cloud_format / image_cloud_quality via
// PUT /api/settings/image_cloud and takes effect on the next turn. The local
// originals are never modified.
type ImageCloudFormat = "webp" | "jpeg" | "png";
interface ImageCloudResponse {
  format: ImageCloudFormat;
  quality: number;
}
const IMAGE_CLOUD_FORMATS: readonly ImageCloudFormat[] = ["webp", "jpeg", "png"];
const imageCloudFormat = ref<ImageCloudFormat>("webp");
const imageCloudQuality = ref(50);
const imageCloudLoading = ref(false);
const imageCloudSaving = ref(false);

async function loadImageCloud(): Promise<void> {
  imageCloudLoading.value = true;
  try {
    const res = await apiJson<ImageCloudResponse>(
      "GET",
      "/api/settings/image_cloud",
    );
    imageCloudFormat.value = IMAGE_CLOUD_FORMATS.includes(res.format)
      ? res.format
      : "webp";
    imageCloudQuality.value = clampImageCloudQuality(res.quality);
  } catch {
    imageCloudFormat.value = "webp";
    imageCloudQuality.value = 50;
  } finally {
    imageCloudLoading.value = false;
  }
}

function clampImageCloudQuality(value: number): number {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return 50;
  return Math.min(100, Math.max(1, n));
}

async function saveImageCloud(): Promise<void> {
  if (imageCloudSaving.value) return;
  imageCloudSaving.value = true;
  imageCloudQuality.value = clampImageCloudQuality(imageCloudQuality.value);
  try {
    const res = await apiJson<ImageCloudResponse>(
      "PUT",
      "/api/settings/image_cloud",
      {
        format: imageCloudFormat.value,
        quality: imageCloudQuality.value,
      },
    );
    imageCloudFormat.value = IMAGE_CLOUD_FORMATS.includes(res.format)
      ? res.format
      : imageCloudFormat.value;
    imageCloudQuality.value = clampImageCloudQuality(res.quality);
    toast.success(t("chatHooks.imageCloud.saved"));
  } catch (e) {
    toast.error(
      `${t("chatHooks.imageCloud.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
    );
  } finally {
    imageCloudSaving.value = false;
  }
}


// ─── Tool output limits (moved from Security → Tool Safety, 2026-07) ─────────
// These cap how much each built-in agent tool (read / glob / grep) hands back
// to the model. They belong with "how the agent behaves", not "security", so
// they live on this tab. Backed by the SAME route the old panel used
// (`GET/PUT /api/security/runtime-config` via useRuntimeConfig → ToolOutputSettings),
// so persistence/reboot semantics are unchanged and no backend move is needed.
// Build-time → a change requires a restart (decision 3B reboot-confirm below).
// i18n keys `toolSafety.*` are reused verbatim (no locale-parity churn).
const toolOutput = reactive<RuntimeConfig>({ ...toolOutputConfig.value });
watch(
  toolOutputConfig,
  () => Object.assign(toolOutput, toolOutputConfig.value),
  { deep: true },
);

const toolOutputStatus = ref<{ type: "success" | "error"; message: string } | null>(
  null,
);
let toolOutputStatusTimer: ReturnType<typeof setTimeout> | null = null;
function showToolOutputStatus(type: "success" | "error", message: string): void {
  toolOutputStatus.value = { type, message };
  if (toolOutputStatusTimer) clearTimeout(toolOutputStatusTimer);
  toolOutputStatusTimer = setTimeout(() => {
    toolOutputStatus.value = null;
  }, 3500);
}

/**
 * Persist a tool-output limit change. These are build-time (installed into the
 * ai_coding tool-handler seam at tool-bridge build), so a save returns
 * `needs_reboot=true`; show the reboot-confirm dialog (decision 3B) and, on
 * accept, drive the shared reboot transition.
 */
async function persistToolOutput(patch: Partial<RuntimeConfig>): Promise<void> {
  const { needsReboot } = await saveToolOutput(patch);
  if (needsReboot) {
    const ok = await confirm({
      icon: "🔄",
      title: t("toolSafety.rebootTitle"),
      message: t("toolSafety.rebootMessage"),
      confirmText: t("toolSafety.rebootConfirm"),
      cancelText: t("toolSafety.rebootCancel"),
      confirmStyle: "primary",
    });
    if (ok) {
      await requestRebootDirect();
      return;
    }
    showToolOutputStatus("success", t("toolSafety.rebootDeferred"));
    return;
  }
  showToolOutputStatus("success", t("toolSafety.saved"));
}

function onReadMaxLinesChange(value: number): void {
  if (!Number.isFinite(value) || value < 1) return;
  toolOutput.read_max_lines = value;
  void persistToolOutput({ read_max_lines: value });
}
function onReadMaxBytesChange(value: number): void {
  if (!Number.isFinite(value) || value < 1024) return;
  toolOutput.read_max_bytes = value;
  void persistToolOutput({ read_max_bytes: value });
}
function onReadMaxLineLengthChange(value: number): void {
  if (!Number.isFinite(value) || value < 80) return;
  toolOutput.read_max_line_length = value;
  void persistToolOutput({ read_max_line_length: value });
}
function onGlobMaxResultsChange(value: number): void {
  if (!Number.isFinite(value) || value < 1) return;
  toolOutput.glob_max_results = value;
  void persistToolOutput({ glob_max_results: value });
}
function onGrepMaxMatchesChange(value: number): void {
  if (!Number.isFinite(value) || value < 1) return;
  toolOutput.grep_max_matches = value;
  void persistToolOutput({ grep_max_matches: value });
}
function onGrepMaxLineLengthChange(value: number): void {
  if (!Number.isFinite(value) || value < 80) return;
  toolOutput.grep_max_line_length = value;
  void persistToolOutput({ grep_max_line_length: value });
}
function onGrepMaxOutputBytesChange(value: number): void {
  if (!Number.isFinite(value) || value < 1024) return;
  toolOutput.grep_max_output_bytes = value;
  void persistToolOutput({ grep_max_output_bytes: value });
}

// ─── Init ─────────────────────────────────────────────────────────────────
onMounted(async () => {
  await fetchConfig();
  syncForm();
  void loadProfileModels();
  void loadModelOptions();
  void fetchToolOutput();
  void loadComputerEnabled();
  void loadImageCloud();
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
          <!-- Max Rounds (optional cap; 0 = unlimited) -->
          <div class="config-field">
            <label class="config-label">{{ t("appConfig.maxRoundsLabel") }}</label>
            <div class="config-comment">
              {{ t("appConfig.maxRoundsDesc") }}
            </div>
            <input
              v-model.number="form.max_rounds"
              type="number"
              class="config-input config-number"
              min="0"
              max="10000"
            />
          </div>
          <!-- Turn Warning Enable -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.turnWarningLabel") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  v-model="form.turn_warning_enabled"
                  type="checkbox"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
            <div class="config-comment">
              {{ t("appConfig.turnWarningDesc") }}
            </div>
          </div>
          <!-- Turn Warning Start Threshold -->
          <div
            v-if="form.turn_warning_enabled"
            class="config-field"
          >
            <label class="config-label">{{ t("appConfig.turnWarningStartLabel") }}</label>
            <div class="config-comment">
              {{ t("appConfig.turnWarningStartDesc") }}
            </div>
            <input
              v-model.number="form.turn_warning_start"
              type="number"
              class="config-input config-number"
              min="5"
              max="200"
            />
          </div>
          <!-- Turn Warning Step -->
          <div
            v-if="form.turn_warning_enabled"
            class="config-field"
          >
            <label class="config-label">{{ t("appConfig.turnWarningStepLabel") }}</label>
            <div class="config-comment">
              {{ t("appConfig.turnWarningStepDesc") }}
            </div>
            <input
              v-model.number="form.turn_warning_step"
              type="number"
              class="config-input config-number"
              min="1"
              max="50"
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

          <!-- ═══ Bash auto-background (N.28-N.30) ════════════════════════
               Whether a still-running exec is handed off to the background-
               process manager when a fresh user message arrives, and how long
               the foreground wait is.  OFF is the toggle, not a magic 0; the
               threshold is shown in ms, the same unit the backend stores. -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.bashAutoBackgroundEnabledLabel") }}
              <label
                class="toggle"
                style="margin-left: auto;"
              >
                <input
                  v-model="form.bash_auto_background_enabled"
                  type="checkbox"
                  data-testid="bash-auto-bg-enabled-toggle"
                />
                <span class="toggle-slider"></span>
              </label>
            </label>
          </div>
          <div
            v-if="form.bash_auto_background_enabled"
            class="config-field"
          >
            <label class="config-label">
              {{ t("appConfig.bashAutoBackgroundThresholdLabel") }}
            </label>
            <input
              v-model.number="form.bash_auto_background_threshold_ms"
              type="number"
              :min="AUTO_BG_THRESHOLD_MIN"
              :max="AUTO_BG_THRESHOLD_MAX"
              step="1000"
              class="config-input config-number"
              data-testid="bash-auto-bg-threshold-input"
            />
            <div class="config-comment">
              {{ t("appConfig.bashAutoBackgroundThresholdDesc") }}
            </div>
          </div>

          <!-- ═══ Max sub-agent recursion depth (N.22) ════════════════════
               How deep the sub-agent spawn tree may go. 1 = only the main
               agent may delegate; 2 (default) = one nested level. Re-read on
               every spawn, so a change binds without a restart. -->
          <div class="config-field">
            <label class="config-label">
              {{ t("appConfig.maxSubagentRecursionDepthLabel") }}
            </label>
            <input
              v-model.number="form.max_subagent_recursion_depth"
              type="number"
              :min="RECURSION_DEPTH_MIN"
              :max="RECURSION_DEPTH_MAX"
              step="1"
              class="config-input config-number"
              data-testid="max-subagent-recursion-depth-input"
            />
            <div class="config-comment">
              {{ t("appConfig.maxSubagentRecursionDepthDesc") }}
            </div>
          </div>

          <!-- ═══ QGenie quota warn threshold (internal edition only) ══════
               QGenie meters each model into two independent daily allowances
               (API/SDK and IDE/CLI). At this share of the ACTIVE one, the app
               offers to move subsequent requests to the other.

               Gated on IS_INTERNAL because the QGenie provider ships only with
               the internal edition — `internal_config.toml` is excluded from
               the release bundle entirely, so on an external build there is no
               QGenie model for this threshold to apply to and the field would
               be a setting that governs nothing. Being a build-time constant,
               the whole block is dead-code-eliminated there. -->
          <div v-if="IS_INTERNAL" class="config-field">
            <label class="config-label">
              {{ t("appConfig.qgenieQuotaWarnPercentLabel") }}
            </label>
            <input
              v-model.number="form.qgenie_quota_warn_percent"
              type="number"
              :min="QUOTA_WARN_MIN"
              :max="QUOTA_WARN_MAX"
              step="1"
              class="config-input config-number"
              data-testid="qgenie-quota-warn-percent-input"
            />
            <div class="config-comment">
              {{ t("appConfig.qgenieQuotaWarnPercentDesc") }}
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

      <!-- ═══ Desktop control (computer tool) ═══ -->
      <div class="config-group">
        <div class="config-group-header config-group-header--static">
          <span>🖱️</span>
          <span>{{ t("chatHooks.computer.title") }}</span>
        </div>
        <div class="config-group-body">
          <div class="config-comment">
            {{ t("chatHooks.computer.subtitle") }}
          </div>
          <label class="agent-toggle-row">
            <input
              type="checkbox"
              :checked="computerEnabled"
              :disabled="computerLoading || computerSaving"
              @change="
                toggleComputerEnabled(
                  ($event.target as HTMLInputElement).checked,
                )
              "
            />
            <span>{{ t("chatHooks.computer.label") }}</span>
          </label>
          <p class="config-comment">
            ⚠️ {{ t("chatHooks.computer.warning") }}
          </p>
        </div>
      </div>

      <!-- ═══ Cloud image format/quality ═══ -->
      <div class="config-group">
        <div class="config-group-header config-group-header--static">
          <span>🖼️</span>
          <span>{{ t("chatHooks.imageCloud.title") }}</span>
        </div>
        <div class="config-group-body">
          <div class="config-comment">
            {{ t("chatHooks.imageCloud.subtitle") }}
          </div>
          <div class="config-field">
            <label class="config-label">
              {{ t("chatHooks.imageCloud.formatLabel") }}
            </label>
            <select
              v-model="imageCloudFormat"
              class="config-input"
              :disabled="imageCloudLoading || imageCloudSaving"
              @change="saveImageCloud()"
            >
              <option
                v-for="fmt in IMAGE_CLOUD_FORMATS"
                :key="fmt"
                :value="fmt"
              >
                {{ fmt.toUpperCase() }}
              </option>
            </select>
          </div>
          <div class="config-field">
            <label class="config-label">
              {{ t("chatHooks.imageCloud.qualityLabel") }}
              <span
                class="config-slider-value"
                style="margin-left: auto;"
                data-testid="image-cloud-quality-value"
              >{{ imageCloudQuality }}</span>
            </label>
            <input
              v-model.number="imageCloudQuality"
              type="range"
              min="1"
              max="100"
              step="1"
              class="config-slider"
              data-testid="image-cloud-quality-slider"
              :disabled="imageCloudLoading || imageCloudSaving"
              @change="saveImageCloud()"
            />
          </div>
        </div>
      </div>


      <!-- ═══ Tool Output Limits (moved from Security → Tool Safety) ═══ -->
      <div class="config-group">
        <div class="config-group-header config-group-header--static">
          <span>📏</span>
          <span>{{ t("toolSafety.outputLimitsTitle") }}</span>
        </div>
        <div class="config-group-body">
          <div class="config-comment">
            {{ t("toolSafety.outputLimitsDesc") }}
          </div>

          <div class="config-field agent-subagent-row">
            <label class="config-label">{{ t("toolSafety.readMaxLines") }}</label>
            <input
              class="config-input"
              type="number"
              min="1"
              :value="toolOutput.read_max_lines"
              @change="onReadMaxLinesChange(Number(($event.target as HTMLInputElement).value))"
            />
            <p class="config-comment">{{ t("toolSafety.readMaxLinesDesc") }}</p>
          </div>

          <div class="config-field agent-subagent-row">
            <label class="config-label">{{ t("toolSafety.readMaxBytes") }}</label>
            <input
              class="config-input"
              type="number"
              min="1024"
              :value="toolOutput.read_max_bytes"
              @change="onReadMaxBytesChange(Number(($event.target as HTMLInputElement).value))"
            />
            <p class="config-comment">{{ t("toolSafety.readMaxBytesDesc") }}</p>
          </div>

          <div class="config-field agent-subagent-row">
            <label class="config-label">{{ t("toolSafety.readMaxLineLength") }}</label>
            <input
              class="config-input"
              type="number"
              min="80"
              :value="toolOutput.read_max_line_length"
              @change="onReadMaxLineLengthChange(Number(($event.target as HTMLInputElement).value))"
            />
            <p class="config-comment">{{ t("toolSafety.readMaxLineLengthDesc") }}</p>
          </div>

          <div class="config-field agent-subagent-row">
            <label class="config-label">{{ t("toolSafety.globMaxResults") }}</label>
            <input
              class="config-input"
              type="number"
              min="1"
              :value="toolOutput.glob_max_results"
              @change="onGlobMaxResultsChange(Number(($event.target as HTMLInputElement).value))"
            />
            <p class="config-comment">{{ t("toolSafety.globMaxResultsDesc") }}</p>
          </div>

          <div class="config-field agent-subagent-row">
            <label class="config-label">{{ t("toolSafety.grepMaxMatches") }}</label>
            <input
              class="config-input"
              type="number"
              min="1"
              :value="toolOutput.grep_max_matches"
              @change="onGrepMaxMatchesChange(Number(($event.target as HTMLInputElement).value))"
            />
            <p class="config-comment">{{ t("toolSafety.grepMaxMatchesDesc") }}</p>
          </div>

          <div class="config-field agent-subagent-row">
            <label class="config-label">{{ t("toolSafety.grepMaxLineLength") }}</label>
            <input
              class="config-input"
              type="number"
              min="80"
              :value="toolOutput.grep_max_line_length"
              @change="onGrepMaxLineLengthChange(Number(($event.target as HTMLInputElement).value))"
            />
            <p class="config-comment">{{ t("toolSafety.grepMaxLineLengthDesc") }}</p>
          </div>

          <div class="config-field agent-subagent-row">
            <label class="config-label">{{ t("toolSafety.grepMaxOutputBytes") }}</label>
            <input
              class="config-input"
              type="number"
              min="1024"
              :value="toolOutput.grep_max_output_bytes"
              @change="onGrepMaxOutputBytesChange(Number(($event.target as HTMLInputElement).value))"
            />
            <p class="config-comment">{{ t("toolSafety.grepMaxOutputBytesDesc") }}</p>
          </div>

          <div
            v-if="toolOutputStatus"
            class="config-comment"
            :style="{ color: toolOutputStatus.type === 'error' ? 'var(--err)' : 'var(--ok)' }"
            role="status"
          >
            {{ toolOutputStatus.message }}
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
